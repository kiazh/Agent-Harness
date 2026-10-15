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
import statistics
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
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
    # Retrieval path actually observed (e.g. "tfidf", "keyword-only",
    # "hybrid x1595" / "hybrid x1590,keyword-only x5") for honest reporting.
    search_mode: str = ""
    # Per-path scores measured on the same questions/index (archive eval):
    # {"dense": {...}, "keyword-only": {...}, "hybrid": {...}}.
    components: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Reranker evidence (empty when reranking was not exercised): backend,
    # model, status counts (applied/fallback/passthrough/skipped) and latency.
    rerank_stats: dict[str, Any] = field(default_factory=dict)

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
                speaker = row.get("speaker", "")
                text = row.get("text", "")
                if not isinstance(speaker, str) or not isinstance(text, str):
                    raise ValueError("dialogue turn text and speaker must be strings")
                turns.append(DialogueTurn(row["dia_id"], speaker, text))
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
    return replace(evaluate_rankings(conversations, rankings, k=k), search_mode="tfidf")


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


#: Per-branch candidate depth before RRF fusion (production RAG uses the
#: same default: HybridSearch.DEFAULT_TOP_K = 50).
_ARCHIVE_RETRIEVAL_DEPTH = 50


def _component_metrics(metrics: RetrievalMetrics) -> dict[str, Any]:
    return {
        "evidence_recall": round(metrics.evidence_recall, 4),
        "hit_rate": round(metrics.hit_rate, 4),
        "mrr": round(metrics.mean_reciprocal_rank, 4),
        "questions": metrics.questions,
    }


def _archive_chunk_text(chunk: Any) -> str:
    """Best-effort candidate text for reranking across chunk payload shapes.

    Archived turns store ``content`` while RAG documents store ``text``.
    """
    try:
        payload = chunk.payload if isinstance(chunk.payload, dict) else {}
    except Exception:
        return ""
    for key in ("text", "content"):
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def _rrf_weights() -> tuple[float, float]:
    """Process-level RRF weights (keyword, dense); neutral on any failure."""
    try:
        from machina.core.config import config

        return (
            float(config.get("rrf_keyword_weight")),
            float(config.get("rrf_dense_weight")),
        )
    except Exception:
        return (1.0, 1.0)


def _candidate_chunks(candidates: Sequence[Any]) -> list[Any]:
    """Chunk objects from either SearchResult items or (chunk, score) tuples."""
    return [item.chunk if hasattr(item, "chunk") else item[0] for item in candidates]


async def evaluate_archive_retrieval(
    conversations: Sequence[Conversation],
    *,
    k: int = 5,
    reranker: Any | None = None,
    keyword_weight: float | None = None,
    dense_weight: float | None = None,
) -> RetrievalMetrics:
    """Replay annotated turns through the actual PostgreSQL archive search.

    Dense path: when an embedding backend is configured and available, every
    turn is embedded through the production memory embedder (the same shared
    instance queries use) and archived with its vector. Each question is then
    answered by read-only dense recall AND PostgreSQL FTS over the same rows,
    fused with the production weighted RRF (``machina.rag.search.rrf_fuse``).
    Dense and keyword rankings are scored separately in ``components`` so
    "dense was measured" is verifiable; the fused ranking is the headline
    metric, mirroring how production hybrid search answers queries when an
    embedder exists. Without a provider the keyword-only path runs.

    When ``reranker`` is provided, the fused (or keyword-only) candidate list
    is reranked exactly like ``RAGPipeline._finish_search`` (candidates →
    reranker → top k, fallback preserves retrieval order) and the pre/post
    rankings are both scored (``hybrid`` vs ``hybrid+rerank``), with status
    counts in ``rerank_stats`` proving the reranker actually ran.

    RRF weights default to the process config (``rrf_keyword_weight`` /
    ``rrf_dense_weight``) so the benchmark measures production semantics.

    A connected, initialized database is required. Temporary sessions and
    archive rows are deleted after each conversation, including on failure.
    """
    from machina.core.context import context_manager
    from machina.core.serialization import payload_to_msgpack
    from machina.core.session import session_manager
    from machina.memory.embeddings import embed_query, embed_texts
    from machina.rag.search import rrf_fuse

    if keyword_weight is None or dense_weight is None:
        config_kw, config_dn = _rrf_weights()
        keyword_weight = config_kw if keyword_weight is None else keyword_weight
        dense_weight = config_dn if dense_weight is None else dense_weight

    depth = max(_ARCHIVE_RETRIEVAL_DEPTH, k)
    rankings: list[list[str]] = []
    fused_rankings: list[list[str]] = []
    dense_rankings: list[list[str]] = []
    keyword_rankings: list[list[str]] = []
    modes: Counter[str] = Counter()
    query_seconds: list[float] = []
    rerank_counts: Counter[str] = Counter()
    rerank_seconds: list[float] = []
    for conversation in conversations:
        session = await session_manager.create(title="LoCoMo retrieval evaluation")
        try:
            turns = list(conversation.turns)
            # Production indexing path: embeddings come from the shared
            # memory embedder, so stored vectors and query vectors share one
            # enforced space. None (no provider/failure) keeps rows
            # keyword-searchable only.
            embeddings = await embed_texts([turn.text for turn in turns])
            identifiers = {}
            for position, turn in enumerate(turns):
                chunk_id = uuid.uuid4()
                identifiers[chunk_id] = turn.dia_id
                await context_manager.archive_chunk(
                    session.id,
                    chunk_id,
                    payload_to_msgpack({"content": turn.text, "speaker": turn.speaker}),
                    embedding=embeddings[position] if embeddings is not None else None,
                    agent_id="benchmark",
                    chunk_type="dialogue",
                )

            async def _rerank_ids(candidates: list, query: str) -> list[str]:
                """Rerank candidates like production; fallback keeps order."""
                chunks = _candidate_chunks(candidates)
                if len(chunks) <= k:
                    rerank_counts["skipped"] += 1
                    return [identifiers[chunk.id] for chunk in chunks[:k]]
                # Production parity: RAGPipeline._finish_search reranks only
                # the k*2 candidates it retrieved (candidate depth beyond that
                # is never reranked), so mirror that depth here.
                chunks = chunks[: 2 * k]
                documents = [_archive_chunk_text(chunk) for chunk in chunks]
                started = time.perf_counter()
                try:
                    results = await reranker.rerank(query, documents, top_k=k)
                except Exception:
                    rerank_counts["fallback"] += 1
                    return [identifiers[chunk.id] for chunk in chunks[:k]]
                finally:
                    rerank_seconds.append(time.perf_counter() - started)
                seen = set()
                valid = []
                for result in results:
                    if (
                        type(result.index) is not int
                        or not 0 <= result.index < len(chunks)
                        or result.index in seen
                        or (
                            result.score is not None
                            and (
                                type(result.score) not in (int, float)
                                or not math.isfinite(result.score)
                            )
                        )
                    ):
                        rerank_counts["fallback"] += 1
                        return [identifiers[chunk.id] for chunk in chunks[:k]]
                    seen.add(result.index)
                    valid.append(result)
                if not valid or any(result.score is None for result in valid):
                    status = (
                        "passthrough"
                        if getattr(reranker, "backend", "") == "passthrough"
                        else "fallback"
                    )
                    rerank_counts[status] += 1
                    return [identifiers[chunk.id] for chunk in chunks[:k]]
                rerank_counts["applied"] += 1
                return [identifiers[chunks[result.index].id] for result in valid]

            for question in conversation.questions:
                if not question.evidence:
                    continue
                query_embedding = (
                    await embed_query(question.text) if embeddings is not None else None
                )
                if query_embedding is not None:
                    started = time.perf_counter()
                    dense = await context_manager.search_archive_dense(
                        session.id, query_embedding, depth
                    )
                    sparse = await context_manager.search_archive_text(
                        session.id, question.text, depth
                    )
                    fused = sorted(
                        rrf_fuse(
                            dense,
                            sparse,
                            dense_weight=dense_weight,
                            keyword_weight=keyword_weight,
                        ),
                        key=lambda result: result.rrf_score,
                        reverse=True,
                    )
                    query_seconds.append(time.perf_counter() - started)
                    fused_ids = [identifiers[result.chunk.id] for result in fused]
                    fused_rankings.append(fused_ids)
                    dense_rankings.append([identifiers[chunk.id] for chunk, _ in dense])
                    keyword_rankings.append([identifiers[chunk.id] for chunk, _ in sparse])
                    if reranker is not None:
                        rankings.append(await _rerank_ids(fused, question.text))
                    else:
                        rankings.append(fused_ids)
                    modes["hybrid"] += 1
                else:
                    fetch_k = depth if reranker is not None else k
                    started = time.perf_counter()
                    matches = await context_manager.search_archive_text(
                        session.id, question.text, fetch_k
                    )
                    query_seconds.append(time.perf_counter() - started)
                    keyword_rankings.append([identifiers[chunk.id] for chunk, _ in matches])
                    if reranker is not None:
                        rankings.append(await _rerank_ids(matches, question.text))
                    else:
                        rankings.append([identifiers[chunk.id] for chunk, _ in matches])
                    modes["keyword-only"] += 1
        finally:
            await session_manager.delete(session.id)
    metrics = evaluate_rankings(conversations, rankings, k=k)
    components: dict[str, dict[str, Any]] = {}
    if modes.get("hybrid") and not modes.get("keyword-only"):
        # All questions ran the fused path: dense/keyword component scores
        # are directly comparable on the same question set.
        components["dense"] = _component_metrics(
            evaluate_rankings(conversations, dense_rankings, k=k)
        )
        components["keyword-only"] = _component_metrics(
            evaluate_rankings(conversations, keyword_rankings, k=k)
        )
        components["hybrid"] = _component_metrics(
            evaluate_rankings(conversations, fused_rankings, k=k)
        )
        if reranker is not None:
            components["hybrid+rerank"] = _component_metrics(metrics)
        components["hybrid"]["mean_query_latency_ms"] = (
            round(1000 * sum(query_seconds) / len(query_seconds), 2) if query_seconds else 0.0
        )
    elif modes.get("keyword-only"):
        components["keyword-only"] = _component_metrics(
            evaluate_rankings(conversations, keyword_rankings, k=k)
        )
        if reranker is not None:
            components["keyword-only+rerank"] = _component_metrics(metrics)
    mode = ",".join(f"{name} x{count}" for name, count in sorted(modes.items()))
    rerank_stats: dict[str, Any] = {}
    if reranker is not None:
        rerank_stats = {
            "backend": getattr(reranker, "backend", type(reranker).__name__),
            "model": getattr(reranker, "model_name", ""),
            "status_counts": dict(sorted(rerank_counts.items())),
            "mean_latency_ms": (
                round(1000 * sum(rerank_seconds) / len(rerank_seconds), 2)
                if rerank_seconds
                else 0.0
            ),
            "p50_latency_ms": (
                round(1000 * statistics.median(rerank_seconds), 2) if rerank_seconds else 0.0
            ),
        }
    return replace(
        metrics,
        search_mode=mode or "keyword-only",
        components=components,
        rerank_stats=rerank_stats,
    )


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

    from machina.core.context import context_manager
    from machina.core.serialization import payload_to_msgpack
    from machina.core.session import session_manager
    from machina.core.session_recall import SessionRecall

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
            # One failed delete must not skip the remaining sessions or mask
            # the error that triggered cleanup.
            in_flight = sys.exc_info()[1]
            cleanup_errors: list[str] = []
            for session_id in session_ids:
                try:
                    await session_manager.delete(session_id)
                except Exception as exc:
                    cleanup_errors.append(f"session {session_id}: {type(exc).__name__}: {exc}")
            if cleanup_errors:
                message = "LoCoMo session cleanup failed: " + "; ".join(cleanup_errors)
                if in_flight is None:
                    raise RuntimeError(message)
                in_flight.add_note(message)
    metrics = replace(
        evaluate_rankings(conversations, rankings, k=k),
        search_mode="keyword-only" if not title_only else "title-only",
    )
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
        from machina.core.models import LLMResponse

        self.call_count += 1
        return LLMResponse(content=self.answer, model="stub", usage={})


def _normalize_answer(text: Any) -> str:
    """Lowercase, remove punctuation, and collapse whitespace.

    LoCoMo ground-truth answers are not always strings — a count or a year is
    a JSON number — so coerce to text first and treat ``None`` as empty.
    Numeric values render with ``format(value, "g")``: ``str(2024.0)`` would
    otherwise produce "2024.0", whose punctuation stripping mangles the year
    into "20240". Booleans stay textual even though ``bool`` is an ``int``.

    A decimal point *between digits* is meaningful and survives the strip:
    gold ``3.5`` must not score against a wrong prediction of ``35``.
    """
    if text is None:
        return ""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        text = format(text, "g")
    else:
        text = str(text)
    text = text.lower()
    text = re.sub(
        r"(?P<decimal>(?<=\d)\.(?=\d))|[^\w\s]",
        lambda match: match.group("decimal") or "",
        text,
    )
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
    parser.add_argument("--test-db", action="store_true", help="Use MACHINA_TEST_DATABASE_URL")
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
                from machina.core.provider import get_provider

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

            import machina.core.config  # noqa: F401 - loads the local test environment

            database_url = os.environ.get("MACHINA_TEST_DATABASE_URL")
        if not database_url:
            parser.error("--db-url or --test-db is required for archive backend")

        async def run_database() -> tuple[RetrievalMetrics, float | None]:
            from machina.db.connection import db

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
