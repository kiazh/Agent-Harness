#!/usr/bin/env python3
"""Pilot: harness-evals judged LoCoMo QA over Machina production retrieval.

Measures the ONE gap harness-evals fills best for Machina: LLM-judged answer
quality on top of the existing retrieval benchmarks (``machina/research/locomo.py``
scores retrieval evidence and token F1 only; no judged QA protocol exists).

Pipeline per question (N=50 by default = 5 evenly spaced questions per LoCoMo
conversation, all with annotated evidence):

1. Retrieval  - a repo-venv subprocess mirrors ``evaluate_archive_retrieval``:
   embed and archive one conversation into a temporary session on the local
   PostgreSQL archive, hybrid RRF search (depth 50, production weights), top-5
   context; the session (and its archive rows) is deleted afterwards. The
   search itself is read-only.
2. Generation - DeepSeek ``deepseek-chat`` answers from the top-5 context with
   the same prompt shape as ``machina.research.locomo.generate_answer``.
3. Scoring    - harness-evals metrics: deterministic (ExactMatch, Contains,
   NumericDiff on numeric golds) plus LLM-judged (Faithfulness, ContextRecall).
   Token F1 comes from the repo's own ``token_f1`` (pure stdlib, loaded by path)
   as a comparability anchor - it is not a harness-evals metric.
4. Accounting - per-call token usage via ``collect_token_usage``; cost is
   computed from the DeepSeek price sheet at cache-miss rates (upper bound).

Run with the throwaway pilot venv (``pip install 'harness-evals[llm]'
python-dotenv``), NOT the repo venv:

  VENV=/private/var/folders/sk/y8w9dwmd4lnc2yvygqqxd4xw0000gn/T/opencode/he_pilot/venv
  $VENV/bin/python tools/harness_evals_pilot.py --self-check
  $VENV/bin/python tools/harness_evals_pilot.py --limit 4     # smoke run
  $VENV/bin/python tools/harness_evals_pilot.py               # full N=50

Results land in ``--out`` (default: temp dir) as JSON; a summary table is
printed. This script does not modify the repo and does not commit anything.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

DEFAULT_REPO = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = Path.home() / ".machina" / "locomo" / "locomo10.json"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_OUT_DIR = Path(tempfile.gettempdir()) / "opencode" / "he_pilot"

# DeepSeek price sheet snapshot, USD per 1M tokens (2026-10-14,
# https://api-docs.deepseek.com/quick_start/pricing). ``deepseek-chat`` is a
# legacy alias served by the current flash model, so flash rates apply.
# Cost estimates below use cache-miss input rates (upper bound; cache-hit
# input is 98% cheaper and LoCoMo judge prompts partially overlap prefixes).
DEEPSEEK_PRICING_USD_PER_MTOK = {
    "peak": {"cache_hit": 0.006, "cache_miss": 0.30, "output": 1.20},
    "offpeak": {"cache_hit": 0.003, "cache_miss": 0.15, "output": 0.60},
}

CATEGORY_NAMES = {
    1: "multi-hop",
    2: "temporal",
    3: "open-domain",
    4: "single-hop",
    5: "adversarial",
}

# ---------------------------------------------------------------------------
# Embedded retrieval helper - executed by the REPO venv via `python -`.
# Mirrors machina.research.locomo.evaluate_archive_retrieval's read-only
# archive search for a selected subset of questions.
# ---------------------------------------------------------------------------

_RETRIEVAL_HELPER_SOURCE = '''\
"""Retrieve production top-k context for selected LoCoMo questions (repo venv)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path


async def run(dataset: str, select_path: str, out_path: str, db_url: str, k: int) -> None:
    from machina.core.config import config
    from machina.core.context import context_manager
    from machina.core.serialization import payload_to_msgpack
    from machina.core.session import session_manager
    from machina.db.connection import db
    from machina.memory.embeddings import embed_query, embed_texts
    from machina.rag.search import rrf_fuse
    from machina.research.locomo import load_locomo

    selection = json.loads(Path(select_path).read_text(encoding="utf-8"))
    conversations = load_locomo(dataset)

    db.dsn = db_url
    await db.connect()
    try:
        results: list[dict] = []
        by_conv: dict[int, list[dict]] = {}
        for item in selection["items"]:
            by_conv.setdefault(item["conv"], []).append(item)
        for conv_index in sorted(by_conv):
            conversation = conversations[conv_index]
            session = await session_manager.create(title="harness-evals pilot retrieval")
            try:
                turns = list(conversation.turns)
                embeddings = await embed_texts([turn.text for turn in turns])
                identifiers: dict[uuid.UUID, str] = {}
                for position, turn in enumerate(turns):
                    chunk_id = uuid.uuid4()
                    identifiers[chunk_id] = turn.dia_id
                    await context_manager.archive_chunk(
                        session.id,
                        chunk_id,
                        payload_to_msgpack({"content": turn.text, "speaker": turn.speaker}),
                        embedding=embeddings[position] if embeddings is not None else None,
                        agent_id="harness-evals-pilot",
                        chunk_type="dialogue",
                    )
                for item in by_conv[conv_index]:
                    question = conversation.questions[item["q"]]
                    kw_weight = float(config.get("rrf_keyword_weight"))
                    dn_weight = float(config.get("rrf_dense_weight"))
                    query_embedding = (
                        await embed_query(question.text) if embeddings is not None else None
                    )
                    if query_embedding is not None:
                        dense = await context_manager.search_archive_dense(
                            session.id, query_embedding, 50
                        )
                        sparse = await context_manager.search_archive_text(
                            session.id, question.text, 50
                        )
                        fused = sorted(
                            rrf_fuse(
                                dense,
                                sparse,
                                dense_weight=dn_weight,
                                keyword_weight=kw_weight,
                            ),
                            key=lambda result: result.rrf_score,
                            reverse=True,
                        )
                        mode = "hybrid"
                    else:
                        fused = await context_manager.search_archive_text(
                            session.id, question.text, k
                        )
                        mode = "keyword-only"
                    retrieved = []
                    for entry in fused[:k]:
                        chunk = entry.chunk if hasattr(entry, "chunk") else entry[0]
                        payload = chunk.payload if isinstance(chunk.payload, dict) else {}
                        text = payload.get("text") or payload.get("content") or ""
                        retrieved.append({"dia_id": identifiers.get(chunk.id, ""), "text": str(text)})
                    results.append(
                        {
                            "conv": conv_index,
                            "q": item["q"],
                            "question": question.text,
                            "answer": question.answer,
                            "category": question.category,
                            "evidence": list(question.evidence),
                            "search_mode": mode,
                            "retrieved": retrieved,
                        }
                    )
            finally:
                await session_manager.delete(session.id)
    finally:
        await db.close()

    Path(out_path).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    modes: dict[str, int] = {}
    for row in results:
        modes[row["search_mode"]] = modes.get(row["search_mode"], 0) + 1
    print(json.dumps({"questions": len(results), "modes": modes}), file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--select", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--db-url", required=True)
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args()
    asyncio.run(run(args.dataset, args.select, args.out, args.db_url, args.k))


if __name__ == "__main__":
    main()
'''

# ---------------------------------------------------------------------------
# Repo module loading (pure stdlib) and question selection
# ---------------------------------------------------------------------------


def load_repo_locomo(repo: Path) -> Any:
    """Load machina/research/locomo.py by path without importing the package.

    The module is pure stdlib, so the pilot venv can reuse the repo's exact
    parsing (``load_locomo``) and scoring (``token_f1``) without installing
    Machina's dependencies.
    """
    spec = importlib.util.spec_from_file_location(
        "harness_evals_pilot_locomo", repo / "machina" / "research" / "locomo.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up by name
    spec.loader.exec_module(module)
    return module


def select_questions(conversations: Any, per_conv: int) -> list[dict[str, int]]:
    """Deterministic even spacing: ``per_conv`` questions with evidence per conversation."""
    if per_conv < 1:
        raise ValueError("--per-conv must be positive")
    items: list[dict[str, int]] = []
    for conv_index, conversation in enumerate(conversations):
        indices = [i for i, q in enumerate(conversation.questions) if q.evidence]
        if not indices:
            continue
        if per_conv == 1:
            picks = [indices[0]]
        else:
            picks = sorted(
                {indices[round(i * (len(indices) - 1) / (per_conv - 1))] for i in range(per_conv)}
            )
        items.extend({"conv": conv_index, "q": i} for i in picks)
    return items


def _parse_float(value: Any) -> float | None:
    """Strict numeric parse - only bare numbers count as numeric golds.

    LoCoMo answers like "7 May 2023" contain digits but are dates, not numbers;
    they must not reach the pure-numeric comparison metric.
    """
    text = str(value).strip().replace(",", "").replace("$", "").replace("%", "")
    try:
        return float(text)
    except ValueError:
        return None


def score_token_f1(locomo_module: Any, answer: str, gold: Any) -> float:
    """Token F1 against the ORIGINAL gold value — never a pre-stringified one.

    LoCoMo golds can be JSON numbers (e.g. ``2024.0``). The repo's canonical
    scorer renders numeric golds with ``format(value, "g")`` ("2024");
    stringifying first would produce "2024.0", whose punctuation stripping
    mangles the year into "20240" and scores a correct "2024" answer as 0.0.
    String golds pass through unchanged.
    """
    return float(locomo_module.token_f1(answer, gold))


# ---------------------------------------------------------------------------
# Retrieval (repo venv subprocess)
# ---------------------------------------------------------------------------


def run_retrieval(
    *,
    repo: Path,
    repo_python: Path,
    dataset: Path,
    items: list[dict[str, int]],
    db_url: str,
    k: int,
    work_dir: Path,
    timeout: int,
) -> list[dict[str, Any]]:
    select_path = work_dir / "selection.json"
    contexts_path = work_dir / "contexts.json"
    select_path.write_text(json.dumps({"items": items}), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(repo), "PYTHONUNBUFFERED": "1"}
    command = [
        str(repo_python),
        "-",
        "--dataset",
        str(dataset),
        "--select",
        str(select_path),
        "--out",
        str(contexts_path),
        "--db-url",
        db_url,
        "--k",
        str(k),
    ]
    print(f"[retrieval] {repo_python} (repo venv) -> {len(items)} questions, k={k}", flush=True)
    started = time.perf_counter()
    proc = subprocess.run(
        command,
        input=_RETRIEVAL_HELPER_SOURCE,
        text=True,
        capture_output=True,
        cwd=str(repo),
        env=env,
        timeout=timeout,
    )
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").splitlines()[-15:])
        raise SystemExit(f"retrieval helper failed (rc={proc.returncode}):\n{tail}")
    print(f"[retrieval] done in {time.perf_counter() - started:.1f}s", flush=True)
    return json.loads(contexts_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# DeepSeek judge shim
# ---------------------------------------------------------------------------


def make_deepseek_json_llm(model: str, api_key: str, base_url: str, max_tokens: int = 1024) -> Any:
    """OpenAILLM subclass with DeepSeek-compatible JSON mode.

    harness-evals' stock ``OpenAILLM.generate_json`` sends
    ``response_format={"type": "json_schema", "strict": true}``, which the
    DeepSeek API rejects ("This response_format type is unavailable now").
    DeepSeek supports ``json_object`` mode, so this shim injects the schema
    into the prompt and parses the JSON object from the reply.
    """
    from harness_evals.llm.openai import OpenAILLM, _record_openai_usage

    class DeepSeekJSONLLM(OpenAILLM):
        async def generate_json(self, prompt: str, schema: dict, **kwargs: object) -> dict:
            full_prompt = (
                f"{prompt}\n\nRespond with a single JSON object matching this JSON schema:\n"
                f"{json.dumps(schema)}\nReturn only the JSON object."
            )
            response = await self._client.chat.completions.create(
                model=self.model,
                messages=self._messages(full_prompt, kwargs.get("system_prompt")),
                max_completion_tokens=self.max_tokens,
                response_format={"type": "json_object"},
                **self._optional_params(),
            )
            _record_openai_usage(response, model=self.model)
            text = (response.choices[0].message.content or "{}").strip()
            if text.startswith("```"):
                text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                match = re.search(r"\{.*\}", text, re.DOTALL)
                if match is None:
                    raise
                parsed = json.loads(match.group(0))
            if not isinstance(parsed, dict):
                raise ValueError(f"judge returned {type(parsed).__name__}, expected object")
            return parsed

    return DeepSeekJSONLLM(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=0.0,
        max_tokens=max_tokens,
    )


def make_answer_llm(model: str, api_key: str, base_url: str) -> Any:
    from harness_evals.llm.openai import OpenAILLM

    return OpenAILLM(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=0.0,
        max_tokens=256,
    )


# ---------------------------------------------------------------------------
# Per-question pipeline
# ---------------------------------------------------------------------------


def build_prompt(question: str, evidence_texts: list[str]) -> str:
    """Same prompt shape as machina.research.locomo.generate_answer."""
    evidence_block = "\n".join(
        f"[Evidence {i + 1}] {text}" for i, text in enumerate(evidence_texts)
    )
    return (
        f"Answer the question based on the evidence below. "
        f"Give a concise answer.\n\n"
        f"Evidence:\n{evidence_block}\n\n"
        f"Question: {question}\n\n"
        f"Answer:"
    )


async def _retry(coro_factory: Any, attempts: int = 3, base_delay: float = 2.0) -> Any:
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await coro_factory()
        except Exception as exc:  # noqa: BLE001 - pilot must survive provider blips
            last = exc
            if attempt < attempts:
                await asyncio.sleep(base_delay * attempt)
    raise last if last is not None else RuntimeError("retry with no attempts")


async def score_question(
    row: dict[str, Any],
    *,
    answer_llm: Any,
    judge_llm: Any | None,
    locomo_module: Any,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    from harness_evals.core.eval_case import EvalCase
    from harness_evals.core.runner import a_evaluate
    from harness_evals.llm.usage import collect_token_usage
    from harness_evals.metrics.deterministic import (
        ContainsMetric,
        ExactMatchMetric,
        NumericDiffMetric,
    )
    from harness_evals.metrics.rag import ContextRecallMetric, FaithfulnessMetric

    async with semaphore:
        result: dict[str, Any] = {
            "conv": row["conv"],
            "q": row["q"],
            "question": row["question"],
            "gold": row["answer"],
            "category": row["category"],
            "evidence": row["evidence"],
            "search_mode": row["search_mode"],
            "retrieved": [
                {"dia_id": item["dia_id"], "text": item["text"]} for item in row["retrieved"]
            ],
        }
        context_texts = [item["text"] for item in row["retrieved"]]
        result["retrieval_evidence_recall"] = (
            len(set(row["evidence"]) & {item["dia_id"] for item in row["retrieved"]})
            / len(row["evidence"])
            if row["evidence"]
            else None
        )

        prompt = build_prompt(row["question"], context_texts)
        try:
            with collect_token_usage() as gen_usage:
                answer = await _retry(lambda: answer_llm.generate(prompt))
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"generation failed: {type(exc).__name__}: {exc}"
            return result

        result["answer"] = answer
        result["generation"] = {
            "input_tokens": gen_usage.input_tokens or 0,
            "output_tokens": gen_usage.output_tokens or 0,
        }

        case = EvalCase(
            input=row["question"],
            output=answer,
            expected=row["answer"],
            context=context_texts,
        )
        metrics: list[Any] = [
            ExactMatchMetric(case_sensitive=False),
            ContainsMetric(case_sensitive=False),
        ]
        numeric_gold = _parse_float(row["answer"])
        result["numeric_gold"] = numeric_gold
        if numeric_gold is not None:
            metrics.append(NumericDiffMetric())

        if judge_llm is not None:
            metrics.append(FaithfulnessMetric(llm=judge_llm))
            metrics.append(ContextRecallMetric(llm=judge_llm))

        # The runner wraps every metric in its own ``collect_token_usage``
        # block, so judge usage lands on ``Score.metadata`` (an outer collector
        # around a_evaluate would stay empty).
        scores = await a_evaluate(case, metrics)

        result["scores"] = {
            score.name: {
                "value": round(score.value, 4),
                "threshold": score.threshold,
                "passed": score.passed,
                "reason": score.reason,
                "input_tokens": (score.metadata or {}).get("input_tokens"),
                "output_tokens": (score.metadata or {}).get("output_tokens"),
            }
            for score in scores
        }
        result["judging"] = {
            "input_tokens": sum(
                (score.metadata or {}).get("input_tokens") or 0 for score in scores
            ),
            "output_tokens": sum(
                (score.metadata or {}).get("output_tokens") or 0 for score in scores
            ),
        }
        result["token_f1"] = round(score_token_f1(locomo_module, answer, row["answer"]), 4)
        result["judge_raised"] = [
            name
            for name, score in result["scores"].items()
            if (score.get("reason") or "").startswith("Metric raised")
        ]
        return result


# ---------------------------------------------------------------------------
# Aggregation, cost, reporting
# ---------------------------------------------------------------------------


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _cost_usd(input_tokens: int, output_tokens: int, rates: dict[str, float]) -> float:
    return input_tokens / 1e6 * rates["cache_miss"] + output_tokens / 1e6 * rates["output"]


def summarize(rows: list[dict[str, Any]], total_dataset_questions: int) -> dict[str, Any]:
    scored = [row for row in rows if "scores" in row]
    metric_names = ["exact_match", "contains", "numeric_diff", "faithfulness", "context_recall"]
    metric_means: dict[str, float | None] = {}
    metric_pass: dict[str, tuple[int, int]] = {}
    for name in metric_names:
        values = [row["scores"][name]["value"] for row in scored if name in row["scores"]]
        passes = [row["scores"][name]["passed"] for row in scored if name in row["scores"]]
        metric_means[name] = round(_mean(values), 4) if values else None
        metric_pass[name] = (sum(passes), len(passes))

    gen_in = sum(row.get("generation", {}).get("input_tokens", 0) for row in scored)
    gen_out = sum(row.get("generation", {}).get("output_tokens", 0) for row in scored)
    judge_in = sum(row.get("judging", {}).get("input_tokens", 0) for row in scored)
    judge_out = sum(row.get("judging", {}).get("output_tokens", 0) for row in scored)
    n = len(scored) or 1

    costs = {
        window: {
            "generation_usd": _cost_usd(gen_in, gen_out, rates),
            "judging_usd": _cost_usd(judge_in, judge_out, rates),
            "total_usd": _cost_usd(gen_in + judge_in, gen_out + judge_out, rates),
        }
        for window, rates in DEEPSEEK_PRICING_USD_PER_MTOK.items()
    }

    by_category: dict[str, dict[str, Any]] = {}
    for category in sorted({row["category"] for row in scored}):
        subset = [row for row in scored if row["category"] == category]
        by_category[f"{category}:{CATEGORY_NAMES.get(category, '?')}"] = {
            "n": len(subset),
            "token_f1_mean": round(_mean([row.get("token_f1", 0.0) for row in subset]), 4),
            "faithfulness_mean": round(
                _mean(
                    [
                        row["scores"]["faithfulness"]["value"]
                        for row in subset
                        if "faithfulness" in row["scores"]
                    ]
                ),
                4,
            ),
            "context_recall_mean": round(
                _mean(
                    [
                        row["scores"]["context_recall"]["value"]
                        for row in subset
                        if "context_recall" in row["scores"]
                    ]
                ),
                4,
            ),
        }

    vacuous = {
        name: sum(
            1
            for row in scored
            if name in row["scores"]
            and re.search(
                r"no (factual )?(claims|statements)", row["scores"][name]["reason"] or "", re.I
            )
        )
        for name in ("faithfulness", "context_recall")
    }

    return {
        "questions_scored": len(scored),
        "questions_failed": len(rows) - len(scored),
        "judge_raised_count": sum(len(row.get("judge_raised", [])) for row in scored),
        "vacuous_judge_scores": vacuous,
        "metric_means": metric_means,
        "metric_pass_rates": {
            name: round(passed / count, 4) if count else None
            for name, (passed, count) in metric_pass.items()
        },
        "metric_counts": {name: count for name, (_, count) in metric_pass.items()},
        "token_f1_mean": round(_mean([row.get("token_f1", 0.0) for row in scored]), 4),
        "retrieval_evidence_recall_mean": round(
            _mean(
                [
                    row["retrieval_evidence_recall"]
                    for row in scored
                    if row.get("retrieval_evidence_recall") is not None
                ]
            ),
            4,
        ),
        "token_f1_pass_ge_0_7": round(
            sum(1 for row in scored if row.get("token_f1", 0.0) >= 0.7) / n, 4
        ),
        "tokens": {
            "generation_input": gen_in,
            "generation_output": gen_out,
            "judging_input": judge_in,
            "judging_output": judge_out,
            "per_question_generation_input": round(gen_in / n, 1),
            "per_question_generation_output": round(gen_out / n, 1),
            "per_question_judging_input": round(judge_in / n, 1),
            "per_question_judging_output": round(judge_out / n, 1),
        },
        "cost_usd": costs,
        "cost_extrapolated_full_dataset": {
            window: {
                key: round(value / n * total_dataset_questions, 4)
                for key, value in window_cost.items()
            }
            for window, window_cost in costs.items()
        },
        "dataset_questions_total": total_dataset_questions,
        "extrapolation_basis_questions": n,
        "by_category": by_category,
    }


def print_summary(summary: dict[str, Any]) -> None:
    means = summary["metric_means"]
    pass_rates = summary["metric_pass_rates"]
    counts = summary["metric_counts"]
    print(
        f"\n== metric summary (n={summary['questions_scored']} scored, "
        f"{summary['questions_failed']} failed) =="
    )
    print(f"{'metric':<16} {'mean':>8} {'pass rate':>10} {'n':>4}")
    for name in ("exact_match", "contains", "numeric_diff", "faithfulness", "context_recall"):
        mean = means.get(name)
        rate = pass_rates.get(name)
        count = counts.get(name, 0)
        mean_text = f"{mean:.4f}" if mean is not None else "-"
        rate_text = f"{rate:.3f}" if rate is not None else "-"
        print(f"{name:<16} {mean_text:>8} {rate_text:>10} {count:>4}")
    print(
        f"{'token_f1(repo)':<16} {summary['token_f1_mean']:>8.4f} "
        f"{summary['token_f1_pass_ge_0_7']:>10.3f} {summary['questions_scored']:>4}"
    )
    print(f"retrieval evidence recall@5 (mean): {summary['retrieval_evidence_recall_mean']}")
    vacuous = summary["vacuous_judge_scores"]
    print(
        'vacuous judge scores ("no claims" short-circuit, counted as 1.0): '
        f"faithfulness={vacuous['faithfulness']}, context_recall={vacuous['context_recall']}"
    )
    tokens = summary["tokens"]
    print(
        "tokens/question: generation in/out "
        f"{tokens['per_question_generation_input']}/{tokens['per_question_generation_output']}, "
        f"judging in/out {tokens['per_question_judging_input']}/{tokens['per_question_judging_output']}"
    )
    peaks = summary["cost_usd"]["peak"]
    offpeak = summary["cost_usd"]["offpeak"]
    print(
        f"cost/run (N={summary['questions_scored']}): "
        f"peak ${peaks['total_usd']:.4f} / off-peak ${offpeak['total_usd']:.4f}"
    )
    extrap_peak = summary["cost_extrapolated_full_dataset"]["peak"]
    extrap_off = summary["cost_extrapolated_full_dataset"]["offpeak"]
    print(
        f"extrapolated full dataset ({summary['dataset_questions_total']} q): "
        f"peak ${extrap_peak['total_usd']:.2f} / off-peak ${extrap_off['total_usd']:.2f}"
    )
    print("\n== by category ==")
    for name, stats in summary["by_category"].items():
        print(
            f"{name:<18} n={stats['n']:<3} token_f1={stats['token_f1_mean']:.3f} "
            f"faithfulness={stats['faithfulness_mean']:.3f} "
            f"context_recall={stats['context_recall_mean']:.3f}"
        )


# ---------------------------------------------------------------------------
# Self-check (assert_test demonstration)
# ---------------------------------------------------------------------------


def run_self_check(*, live: bool, judge_model: str, api_key: str, base_url: str) -> None:
    from harness_evals import assert_test
    from harness_evals.core.eval_case import EvalCase
    from harness_evals.metrics.deterministic import ExactMatchMetric

    print("[self-check] harness-evals version:", importlib.metadata.version("harness-evals"))
    scores = assert_test(
        EvalCase(input="Capital of France?", output="Paris", expected="paris"),
        [ExactMatchMetric(case_sensitive=False)],
    )
    print("[self-check] assert_test pass path OK:", scores[0].reason)
    try:
        assert_test(
            EvalCase(input="Capital of France?", output="London", expected="Paris"),
            [ExactMatchMetric(case_sensitive=False)],
        )
    except AssertionError as exc:
        print("[self-check] assert_test fail path raises AssertionError:", exc)
    else:
        raise SystemExit("[self-check] expected AssertionError did not fire")

    # Regression pin: numeric LoCoMo golds must reach the repo scorer as
    # numbers. The pilot previously stringified them, so answer "2024"
    # against gold 2024.0 scored 0.0 instead of the canonical 1.0.
    locomo_module = load_repo_locomo(DEFAULT_REPO)
    numeric_f1 = score_token_f1(locomo_module, "2024", 2024.0)
    string_f1 = score_token_f1(locomo_module, "Paris!", "paris")
    assert numeric_f1 == 1.0, f"numeric gold 2024.0 vs answer '2024' scored {numeric_f1}"
    assert string_f1 == 1.0, f"string gold 'paris' vs answer 'Paris!' scored {string_f1}"
    print("[self-check] numeric gold 2024.0 + answer '2024' = 1.0; string gold unchanged")

    if live:
        from harness_evals.metrics.rag import FaithfulnessMetric

        judge = make_deepseek_json_llm(judge_model, api_key, base_url)
        case = EvalCase(
            input="What is Caroline's job?",
            output="Caroline works as a counselor.",
            expected="counselor",
            context=["Caroline: I started my new job as a counselor last week."],
        )
        judged = assert_test(case, [FaithfulnessMetric(llm=judge, threshold=0.5)])
        print(
            "[self-check] live judged assert_test OK:",
            judged[0].name,
            judged[0].value,
            "-",
            judged[0].reason,
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _load_env(env_file: Path) -> dict[str, str]:
    from dotenv import dotenv_values

    values = {key: value for key, value in dotenv_values(env_file).items() if value is not None}
    return values


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument(
        "--repo-python",
        type=Path,
        default=None,
        help="repo venv python (default: <repo>/.venv/bin/python)",
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--env-file", type=Path, default=None, help="default: <repo>/.env")
    parser.add_argument(
        "--db-url", default=None, help="default: MACHINA_TEST_DATABASE_URL from .env"
    )
    parser.add_argument(
        "--prod-db",
        action="store_true",
        help="use DATABASE_URL instead of MACHINA_TEST_DATABASE_URL",
    )
    parser.add_argument(
        "--per-conv", type=int, default=5, help="questions per conversation (N=10x)"
    )
    parser.add_argument("--k", type=int, default=5, help="retrieved context depth")
    parser.add_argument("--limit", type=int, default=None, help="truncate selection (smoke runs)")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--out", type=Path, default=None, help="results JSON path")
    parser.add_argument(
        "--reuse-contexts", type=Path, default=None, help="skip retrieval, reuse contexts JSON"
    )
    parser.add_argument("--skip-judge", action="store_true", help="deterministic metrics only")
    parser.add_argument("--retrieval-timeout", type=int, default=1800)
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument(
        "--live", action="store_true", help="with --self-check: also run a judged case"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo = args.repo.resolve()
    repo_python = args.repo_python or repo / ".venv" / "bin" / "python"
    env_file = args.env_file or repo / ".env"
    env = _load_env(env_file)
    db_url = args.db_url or env.get("DATABASE_URL" if args.prod_db else "MACHINA_TEST_DATABASE_URL")
    api_key = env.get("DEEPSEEK_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", "")

    if args.self_check:
        if args.live and not api_key:
            raise SystemExit("--self-check --live needs DEEPSEEK_API_KEY")
        run_self_check(
            live=args.live,
            judge_model=args.model,
            api_key=api_key or "missing",
            base_url=args.base_url,
        )
        return 0
    if args.live:
        raise SystemExit("--live requires --self-check")

    if not repo_python.exists():
        raise SystemExit(f"repo python not found: {repo_python}")
    if not args.dataset.exists():
        raise SystemExit(f"dataset not found: {args.dataset}")
    if not api_key:
        raise SystemExit("DEEPSEEK_API_KEY missing from .env and environment")
    if not db_url:
        raise SystemExit("no database URL (set --db-url or MACHINA_TEST_DATABASE_URL in .env)")

    locomo_module = load_repo_locomo(repo)
    conversations = locomo_module.load_locomo(args.dataset)
    dataset_total = sum(len(conversation.questions) for conversation in conversations)
    items = select_questions(conversations, args.per_conv)
    if args.limit is not None:
        items = items[: args.limit]
    print(
        f"[setup] {len(conversations)} conversations, {dataset_total} questions in dataset, "
        f"{len(items)} selected (per-conv={args.per_conv}, k={args.k})"
    )

    work_dir = Path(tempfile.mkdtemp(prefix="harness_evals_pilot_"))
    if args.reuse_contexts:
        rows = json.loads(args.reuse_contexts.read_text(encoding="utf-8"))
        by_key = {(row["conv"], row["q"]): row for row in rows}
        missing = [item for item in items if (item["conv"], item["q"]) not in by_key]
        if missing:
            raise SystemExit(f"--reuse-contexts missing {len(missing)} selected questions")
        rows = [by_key[(item["conv"], item["q"])] for item in items]
        print(f"[retrieval] reusing {args.reuse_contexts}")
    else:
        rows = run_retrieval(
            repo=repo,
            repo_python=repo_python,
            dataset=args.dataset,
            items=items,
            db_url=db_url,
            k=args.k,
            work_dir=work_dir,
            timeout=args.retrieval_timeout,
        )

    answer_llm = make_answer_llm(args.model, api_key, args.base_url)
    judge_llm = (
        None if args.skip_judge else make_deepseek_json_llm(args.model, api_key, args.base_url)
    )

    async def run_pipeline() -> list[dict[str, Any]]:
        semaphore = asyncio.Semaphore(max(1, args.concurrency))
        started = time.perf_counter()
        tasks = [
            score_question(
                row,
                answer_llm=answer_llm,
                judge_llm=judge_llm,
                locomo_module=locomo_module,
                semaphore=semaphore,
            )
            for row in rows
        ]
        results = await asyncio.gather(*tasks)
        print(
            f"[scoring] {len(results)} questions in {time.perf_counter() - started:.1f}s "
            f"(concurrency={args.concurrency}, judge={'on' if judge_llm else 'off'})",
            flush=True,
        )
        return results

    scored = asyncio.run(run_pipeline())
    for row in scored:
        if "error" in row:
            print(f"  ! conv{row['conv']} q{row['q']} {row['error']}")

    summary = summarize(scored, dataset_total)
    out_path = args.out or DEFAULT_OUT_DIR / f"results_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "harness_evals_version": importlib.metadata.version("harness-evals"),
            "model": args.model,
            "base_url": args.base_url,
            "dataset": str(args.dataset),
            "k": args.k,
            "per_conv": args.per_conv,
            "n": len(scored),
            "database": "machina" if args.prod_db else "machina_test",
            "judge_enabled": judge_llm is not None,
            "git_commit": subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(repo),
                capture_output=True,
                text=True,
            ).stdout.strip(),
        },
        "summary": summary,
        "questions": scored,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print_summary(summary)
    print(f"\nresults written: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
