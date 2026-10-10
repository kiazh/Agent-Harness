"""Evidence-retrieval and answer-accuracy evaluation for the official LoCoMo JSON layout.

Retrieval scoring measures whether the retriever surfaced the right memory
chunks. Answer-accuracy scoring measures whether a generated answer matches
the ground-truth answer using token F1 (the metric used by the LoCoMo paper).
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DialogueTurn:
    dia_id: str
    speaker: str
    text: str


@dataclass(frozen=True)
class Question:
    text: str
    evidence: tuple[str, ...]
    category: int | None = None
    answer: str = ""  # may arrive as a JSON number; coerced by _normalize_answer


@dataclass(frozen=True)
class Conversation:
    turns: tuple[DialogueTurn, ...]
    questions: tuple[Question, ...]


@dataclass(frozen=True)
class RetrievalMetrics:
    questions: int
    skipped_questions: int
    evidence_recall: float
    hit_rate: float
    mean_reciprocal_rank: float
    mean_retrieved_bytes: float
    k: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_locomo(path: str | Path) -> tuple[Conversation, ...]:
    """Read the ten-conversation official schema; reject malformed evidence IDs."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("LoCoMo data must be a non-empty JSON array")
    conversations = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("conversation"), dict):
            raise ValueError("each sample needs a conversation object")
        turns = []
        for key, rows in item["conversation"].items():
            if not re.fullmatch(r"session_\d+", key):
                continue
            if not isinstance(rows, list):
                raise ValueError(f"{key} must be a list")
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("dia_id"), str):
                    raise ValueError("dialogue turn needs dia_id")
                turns.append(
                    DialogueTurn(row["dia_id"], row.get("speaker", ""), row.get("text", ""))
                )
        if len({turn.dia_id for turn in turns}) != len(turns):
            raise ValueError("duplicate dialogue ID")
        questions = []
        if not isinstance(item.get("qa"), list):
            raise ValueError("each sample needs a qa list")
        for qa in item["qa"]:
            evidence = qa.get("evidence", [])
            if not isinstance(qa.get("question"), str) or not isinstance(evidence, list):
                raise ValueError("question and evidence must be present")
            normalized = []
            for entry in evidence:
                if not isinstance(entry, str):
                    raise ValueError("evidence ID must be a string")
                # A few official records join multiple IDs with spaces or semicolons.
                # Match evidence IDs like D1:1 first so they are not split
                # into partial matches ("1:1", "D1").
                identifiers = re.findall(r"\bD?\d+:\d+\b", entry)
                normalized.extend(identifiers or [entry])
            questions.append(
                Question(
                    qa["question"], tuple(normalized), qa.get("category"), qa.get("answer", "")
                )
            )
        conversations.append(Conversation(tuple(turns), tuple(questions)))
    return tuple(conversations)


def _words(text: str) -> Counter[str]:
    return Counter(re.findall(r"[a-z0-9]+", text.lower()))


def lexical_retrieve(question: str, turns: Sequence[DialogueTurn], k: int) -> list[str]:
    """Deterministic TF-IDF style baseline without model/API calls."""
    query = _words(question)
    if not query:
        return []
    documents = [_words(turn.text) for turn in turns]
    frequency = Counter(word for doc in documents for word in doc)
    scored = []
    for position, (turn, document) in enumerate(zip(turns, documents)):
        score = sum(
            min(document[word], 3) * math.log(1 + (len(turns) + 1) / (frequency[word] + 1))
            for word in query
        )
        if score > 0:
            scored.append((-score, position, turn.dia_id))
    scored.sort()
    return [dia_id for _, _, dia_id in scored[:k]]


def evaluate_retrieval(
    conversations: Sequence[Conversation],
    retrieve: Callable[[str, Sequence[DialogueTurn], int], Sequence[str]] = lexical_retrieve,
    *,
    k: int = 5,
) -> RetrievalMetrics:
    """Score a retriever against LoCoMo's annotated evidence IDs."""
    if k < 1:
        raise ValueError("k must be positive")
    rankings = [
        retrieve(question.text, conversation.turns, k)
        for conversation in conversations
        for question in conversation.questions
        if question.evidence
    ]
    return evaluate_rankings(conversations, rankings, k=k)


def evaluate_rankings(
    conversations: Sequence[Conversation],
    rankings: Sequence[Sequence[str]],
    *,
    k: int = 5,
) -> RetrievalMetrics:
    """Score ranked dialogue IDs with identical metrics for every backend."""
    if k < 1:
        raise ValueError("k must be positive")
    expected = sum(bool(question.evidence) for conv in conversations for question in conv.questions)
    if len(rankings) != expected:
        raise ValueError("one ranking is required per question with evidence")
    recall = hits = reciprocal = bytes_used = count = skipped = 0
    ranking_index = 0
    for conversation in conversations:
        texts = {turn.dia_id: turn.text for turn in conversation.turns}
        for question in conversation.questions:
            evidence = set(question.evidence)
            if not evidence:
                continue  # adversarial questions have no positive evidence
            ranked = list(rankings[ranking_index])[:k]
            ranking_index += 1
            if not evidence <= texts.keys():
                skipped += 1
                continue
            if len(ranked) != len(set(ranked)) or any(dia_id not in texts for dia_id in ranked):
                raise ValueError("retriever returned duplicate or unknown dialogue IDs")
            found = evidence.intersection(ranked)
            recall += len(found) / len(evidence)
            hits += bool(found)
            reciprocal += next(
                (1 / rank for rank, dia_id in enumerate(ranked, 1) if dia_id in evidence), 0
            )
            bytes_used += sum(len(texts[dia_id].encode("utf-8")) for dia_id in ranked)
            count += 1
    if count == 0:
        raise ValueError("dataset has no questions with annotated evidence")
    return RetrievalMetrics(
        questions=count,
        skipped_questions=skipped,
        evidence_recall=recall / count,
        hit_rate=hits / count,
        mean_reciprocal_rank=reciprocal / count,
        mean_retrieved_bytes=bytes_used / count,
        k=k,
    )


async def evaluate_archive_retrieval(
    conversations: Sequence[Conversation],
    *,
    k: int = 5,
) -> RetrievalMetrics:
    """Replay annotated turns through the actual PostgreSQL archive search.

    A connected, initialized database is required. Temporary sessions and
    archive rows are deleted after each conversation, including on failure.
    """
    from ah.core.context import context_manager
    from ah.core.serialization import payload_to_msgpack
    from ah.core.session import session_manager

    rankings: list[list[str]] = []
    for conversation in conversations:
        session = await session_manager.create(title="LoCoMo retrieval evaluation")
        try:
            identifiers = {}
            for turn in conversation.turns:
                chunk_id = uuid.uuid4()
                identifiers[chunk_id] = turn.dia_id
                await context_manager.archive_chunk(
                    session.id,
                    chunk_id,
                    payload_to_msgpack({"content": turn.text, "speaker": turn.speaker}),
                    agent_id="benchmark",
                    chunk_type="dialogue",
                )
            for question in conversation.questions:
                if not question.evidence:
                    continue
                matches = await context_manager.search_archive_text(session.id, question.text, k)
                rankings.append([identifiers[chunk.id] for chunk, _ in matches])
        finally:
            await session_manager.delete(session.id)
    return evaluate_rankings(conversations, rankings, k=k)


async def evaluate_session_recall(
    conversations: Sequence[Conversation],
    *,
    k: int = 5,
    turns_per_session: int = 50,
    title_only: bool = False,
) -> tuple[RetrievalMetrics, float]:
    """Score production cross-session recall over temporary archived transcripts.

    Each conversation gets a unique agent; its turns are partitioned into
    separate sessions. Cleanup removes both sessions and their archive rows.
    Returns evidence metrics and mean query latency in milliseconds.

    With ``title_only=True`` only session-title hits are scored — the
    title-only baseline used to show what transcript search adds.
    """
    if not 1 <= k <= 100:
        raise ValueError("k must be between 1 and 100")
    if turns_per_session < 1:
        raise ValueError("turns_per_session must be positive")

    from ah.core.context import context_manager
    from ah.core.serialization import payload_to_msgpack
    from ah.core.session import session_manager
    from ah.core.session_recall import SessionRecall

    rankings: list[list[str]] = []
    query_seconds: list[float] = []
    recall = SessionRecall()
    for conversation in conversations:
        agent_id = f"locomo-recall-{uuid.uuid4()}"
        session_ids: list[uuid.UUID] = []
        identifiers: dict[uuid.UUID, str] = {}
        try:
            for offset in range(0, len(conversation.turns), turns_per_session):
                session = await session_manager.create(title="LoCoMo transcript", agent_id=agent_id)
                session_ids.append(session.id)
                for turn in conversation.turns[offset : offset + turns_per_session]:
                    chunk_id = uuid.uuid4()
                    identifiers[chunk_id] = turn.dia_id
                    await context_manager.archive_chunk(
                        session.id,
                        chunk_id,
                        payload_to_msgpack({"content": turn.text, "speaker": turn.speaker}),
                        agent_id=agent_id,
                        chunk_type="dialogue",
                    )
            for question in conversation.questions:
                if not question.evidence:
                    continue
                started = time.perf_counter()
                hits = await recall.discover(agent_id, question.text, limit=k)
                query_seconds.append(time.perf_counter() - started)
                if title_only:
                    hits = [hit for hit in hits if hit.source == "title"]
                rankings.append(
                    [identifiers[hit.chunk_id] for hit in hits if hit.chunk_id in identifiers]
                )
        finally:
            for session_id in session_ids:
                await session_manager.delete(session_id)
    metrics = evaluate_rankings(conversations, rankings, k=k)
    mean_latency_ms = 1000 * sum(query_seconds) / len(query_seconds) if query_seconds else 0.0
    return metrics, mean_latency_ms


# ---------------------------------------------------------------------------
# Answer-accuracy evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnswerMetrics:
    """Answer-accuracy metrics for LoCoMo question answering."""

    questions: int
    skipped_questions: int
    mean_f1: float
    exact_match_rate: float
    scores: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StubProvider:
    """Deterministic offline LLM provider for testing.

    Returns a pre-configured answer regardless of the prompt. Tracks the
    number of calls made. This keeps the test suite hermetic — no network
    calls, no API keys.
    """

    def __init__(self, answer: str = "I don't know") -> None:
        self.answer = answer
        self.call_count = 0

    async def complete(
        self,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> Any:
        from ah.core.models import LLMResponse

        self.call_count += 1
        return LLMResponse(content=self.answer, model="stub", usage={})


def _normalize_answer(text: Any) -> str:
    """Lowercase, remove punctuation, and collapse whitespace.

    LoCoMo ground-truth answers are not always strings — a count or a year is
    a JSON number — so coerce to text first and treat ``None`` as empty.
    """
    if text is None:
        return ""
    text = str(text).lower()
    text = re.sub(r"[^\w\s]", "", text)
    return " ".join(text.split())


def token_f1(prediction: str, ground_truth: str) -> float:
    """Compute token-level F1 score (SQuAD-style).

    This is the primary scoring metric used by the LoCoMo paper. It is
    deterministic, offline, and handles partial credit for overlapping
    tokens. Case-insensitive and punctuation-insensitive.
    """
    pred_tokens = _normalize_answer(prediction).split()
    gt_tokens = _normalize_answer(ground_truth).split()
    if not pred_tokens or not gt_tokens:
        return 0.0
    common = Counter(pred_tokens) & Counter(gt_tokens)
    num_common = sum(common.values())
    if num_common == 0:
        return 0.0
    precision = num_common / len(pred_tokens)
    recall = num_common / len(gt_tokens)
    return 2 * precision * recall / (precision + recall)


async def generate_answer(
    provider: Any,
    question: str,
    evidence_texts: Sequence[str],
    *,
    retries: int = 4,
    backoff: float = 1.0,
) -> str:
    """Generate an answer using the LLM provider.

    Builds a prompt with the question and retrieved evidence, then calls
    the provider's ``complete`` method. Returns the generated text.

    Retries transient provider failures (e.g. HTTP 429 on a free tier) with
    exponential backoff so a long corpus run is not aborted by one rate limit.
    After the final attempt the error propagates, so a run is never silently
    scored against empty answers.
    """
    evidence_block = "\n".join(
        f"[Evidence {i + 1}] {text}" for i, text in enumerate(evidence_texts)
    )
    prompt = (
        f"Answer the question based on the evidence below. "
        f"Give a concise answer.\n\n"
        f"Evidence:\n{evidence_block}\n\n"
        f"Question: {question}\n\n"
        f"Answer:"
    )
    attempt = 0
    while True:
        attempt += 1
        try:
            response = await provider.complete(
                [{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=256,
            )
        except Exception:
            if attempt >= max(1, retries):
                raise
            if backoff:
                await asyncio.sleep(backoff * (2 ** (attempt - 1)))
            continue
        # A provider may return None content (empty or filtered completion);
        # treat it as an empty answer so one bad response does not abort the run.
        return (response.content or "").strip()


async def evaluate_answers(
    conversations: Sequence[Conversation],
    provider: Any,
) -> AnswerMetrics:
    """Evaluate answer accuracy across all conversations.

    For each question with evidence, generates an answer using the provider
    and scores it against the ground-truth answer using token F1.

    Questions without evidence (adversarial) are skipped.
    """
    if not conversations:
        raise ValueError("no conversations to evaluate")

    scores: list[dict[str, Any]] = []
    total_f1 = 0.0
    total_exact = 0
    count = 0
    skipped = 0

    for conversation in conversations:
        texts = {turn.dia_id: turn.text for turn in conversation.turns}
        for question in conversation.questions:
            if not question.evidence:
                skipped += 1
                continue
            # Collect evidence text for the question's evidence IDs
            evidence_texts = []
            for dia_id in question.evidence:
                if dia_id in texts:
                    evidence_texts.append(texts[dia_id])

            prediction = await generate_answer(provider, question.text, evidence_texts)
            ground_truth = question.answer

            f1 = token_f1(prediction, ground_truth)
            exact = _normalize_answer(prediction) == _normalize_answer(ground_truth)

            scores.append(
                {
                    "question": question.text,
                    "prediction": prediction,
                    "ground_truth": ground_truth,
                    "f1": f1,
                    "exact_match": exact,
                }
            )
            total_f1 += f1
            total_exact += int(exact)
            count += 1

    if count == 0:
        raise ValueError("no questions with evidence to evaluate")

    return AnswerMetrics(
        questions=count,
        skipped_questions=skipped,
        mean_f1=total_f1 / count,
        exact_match_rate=total_exact / count,
        scores=scores,
    )


def main() -> None:
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="Evaluate LoCoMo evidence retrieval")
    parser.add_argument("dataset", help="Path to official locomo10.json")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument(
        "--backend",
        choices=("lexical", "archive", "session-recall", "title-only"),
        default="lexical",
    )
    parser.add_argument("--db-url", help="Initialized PostgreSQL DSN for archive backend")
    parser.add_argument(
        "--test-db", action="store_true", help="Use AGENT_HARNESS_TEST_DATABASE_URL"
    )
    parser.add_argument(
        "--answers",
        action="store_true",
        help="Score answer accuracy (token F1) using an LLM instead of retrieval",
    )
    parser.add_argument(
        "--stub-answer",
        help="Use a deterministic offline stub provider returning this answer "
        "(no network; for smoke runs)",
    )
    parser.add_argument("--max-conversations", type=int, help="Limit samples for a smoke run")
    args = parser.parse_args()
    conversations = load_locomo(args.dataset)
    if args.max_conversations is not None:
        if args.max_conversations < 1:
            parser.error("--max-conversations must be positive")
        conversations = conversations[: args.max_conversations]

    if args.answers:
        # Answer-accuracy mode: generate an answer per question and score it
        # against the ground truth with token F1.
        async def run_answers() -> AnswerMetrics:
            if args.stub_answer is not None:
                provider: Any = StubProvider(args.stub_answer)
            else:
                from ah.core.provider import get_provider

                provider = get_provider()
            return await evaluate_answers(conversations, provider)

        answer_metrics = asyncio.run(run_answers())
        result = answer_metrics.to_dict()
        # Keep the per-question scores only in verbose/smoke runs; the summary
        # is what a corpus run reports.
        result.pop("scores", None)
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    if args.backend in ("archive", "session-recall", "title-only"):
        if args.db_url and args.test_db:
            parser.error("use either --db-url or --test-db")
        database_url = args.db_url
        if args.test_db:
            import os

            import ah.core.config  # noqa: F401 - loads the local test environment

            database_url = os.environ.get("AGENT_HARNESS_TEST_DATABASE_URL")
        if not database_url:
            parser.error("--db-url or --test-db is required for archive backend")

        async def run_database() -> tuple[RetrievalMetrics, float | None]:
            from ah.db.connection import db

            db.dsn = database_url
            await db.connect()
            try:
                if args.backend == "session-recall":
                    return await evaluate_session_recall(conversations, k=args.k)
                if args.backend == "title-only":
                    return await evaluate_session_recall(conversations, k=args.k, title_only=True)
                return await evaluate_archive_retrieval(conversations, k=args.k), None
            finally:
                await db.close()

        metrics, latency_ms = asyncio.run(run_database())
    else:
        metrics = evaluate_retrieval(conversations, k=args.k)
        latency_ms = None
    result = metrics.to_dict()
    if latency_ms is not None:
        result["mean_query_latency_ms"] = latency_ms
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
