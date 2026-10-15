"""Unified offline benchmark — one command, all reproducible numbers.

Aggregates the harnesses that already exist as single sources of truth:

- incident replay (tests/test_replay_harness.py): fixed-loop metrics vs the
  2026-10-13 incident constants;
- retrieval quality (tests/test_retrieval_eval.py): memory/RAG/recall
  relevancy + faithfulness on the catalog-polluted fixture;
- LoCoMo memory evaluation (machina/research/locomo.py): evidence
  recall / hit rate / MRR of the deterministic TF-IDF baseline, plus —
  with --locomo-db — the SAME questions through the real PostgreSQL archive
  search and production cross-session recall (with latency). This is the
  baseline-vs-production comparison showing whether memory actually works.
- prefix composition (tests/test_s5_prefix_probe.py): stable-prefix size and
  identical-prefix resend share per fixture turn;
- architecture headroom: deferred imports, god-file sizes, LOC/files;
- optional: full backend suite (--with-suite) and UI check (--with-ui).

The LoCoMo dataset (locomo10.json) is external and never auto-downloaded:
pass --locomo-data PATH or set MACHINA_LOCOMO_DATA; without it the section
reports SKIPPED honestly (never fabricated numbers).

Everything runs offline (mock providers, no DB, no network) except the
explicitly gated sections. Usage:

    python -m machina.research.benchmark
    python -m machina.research.benchmark --json bench.json
    python -m machina.research.benchmark --locomo-data ~/locomo10.json --locomo-db
    python -m machina.research.benchmark --with-suite --with-ui
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from machina import __version__
from machina.db.connection import db
from machina.research.locomo import (
    evaluate_archive_retrieval,
    evaluate_retrieval,
    evaluate_session_recall,
    load_locomo,
)

# The benchmark reuses test-harness functions (single source of truth). Those
# live in the repo's tests/ package, which is deliberately NOT shipped in the
# wheel — so this tool requires a source checkout and fails with a clear
# message otherwise.
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Subprocess sections must be bounded: an unbounded child (suite/UI) would
# hang the whole benchmark with no way to notice. A timed-out section is
# reported as an error so main() exits non-zero.
_SUITE_TIMEOUT_SECONDS = 3600
_UI_TIMEOUT_SECONDS = 900

# 2026-10-13 incident constants (user-confirmed DeepSeek usage).
_INCIDENT = {
    "tokens": 2_119_552,
    "tool_calls": 228,
    "post_ask_recalls": 25,
    "artifact_written": False,
    "display": "2,119,552 tokens, 228 calls, 25 post-ask recalls, no report",
}


def _ensure_tests_importable() -> None:
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    if not (_REPO_ROOT / "tests" / "__init__.py").is_file():
        raise SystemExit(
            "benchmark requires a source checkout (tests/ not found). "
            "Run it from the repository root."
        )


def _git_head() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _environment() -> dict:
    return {
        "version": __version__,
        "git_head": _git_head(),
        "python": platform.python_version(),
        "platform": platform.platform(),
    }


def _incident_replay() -> dict:
    _ensure_tests_importable()
    from tests.test_replay_harness import replay_incident

    with tempfile.TemporaryDirectory() as tmp:
        metrics = replay_incident(Path(tmp))
    metrics["incident"] = _INCIDENT
    metrics["deltas"] = {
        "artifact_written": metrics["artifact_written"] is True,
        "recall_calls": f"{metrics['recall_calls']} vs {_INCIDENT['post_ask_recalls']} (incident)",
    }
    return metrics


def _retrieval() -> dict:
    _ensure_tests_importable()
    import pytest

    from tests import test_retrieval_eval as ev

    def _run(callable_):
        mp = pytest.MonkeyPatch()
        try:
            return asyncio.run(callable_(mp))
        finally:
            mp.undo()

    def _row(scores):
        return {
            "relevancy": round(ev._mean(scores, 0), 2),
            "faithfulness": round(ev._mean(scores, 1), 2),
        }

    return {
        "memory": _row(_run(ev._eval_memory)),
        "rag": _row(asyncio.run(ev._eval_rag())),
        "recall": _row(_run(ev._eval_recall)),
        "baseline_before_program": {"memory_faithfulness": 0.0, "rag_faithfulness": 0.0},
    }


# ─── LoCoMo memory evaluation ───────────────────────────────────────────────


def _locomo_path(explicit: str | None) -> Path | None:
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env = os.environ.get("MACHINA_LOCOMO_DATA")
    if env:
        candidates.append(Path(env).expanduser())
    candidates += [
        _REPO_ROOT / "tests" / "data" / "locomo10.json",
        Path.home() / ".machina" / "locomo" / "locomo10.json",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _locomo(args: argparse.Namespace) -> dict:
    path = _locomo_path(args.locomo_data)
    if path is None:
        return {
            "status": "SKIPPED",
            "reason": "no LoCoMo dataset found",
            "how": (
                "download locomo10.json (snap-research/locomo) and pass "
                "--locomo-data PATH or set MACHINA_LOCOMO_DATA"
            ),
        }

    conversations = load_locomo(path)
    questions = sum(len(c.questions) for c in conversations)
    result: dict = {
        "dataset": str(path),
        "conversations": len(conversations),
        "questions": questions,
        "baseline_tfidf": {},
    }
    # Retrieval quality at two k values: recall@k growth is the standard view.
    for k in (5, 10):
        metrics = evaluate_retrieval(conversations, k=k)
        result["baseline_tfidf"][f"k={k}"] = {
            "evidence_recall": round(metrics.evidence_recall, 3),
            "hit_rate": round(metrics.hit_rate, 3),
            "mrr": round(metrics.mean_reciprocal_rank, 3),
            "mean_retrieved_bytes": round(metrics.mean_retrieved_bytes, 1),
        }

    if args.locomo_db:
        # Production comparison: the SAME questions through Machina's real
        # engines. Requires a connected, initialized database.
        try:

            async def _with_db():
                if not db.connected:
                    await db.connect()
                archive = await evaluate_archive_retrieval(conversations, k=5)
                archive_k10 = await evaluate_archive_retrieval(conversations, k=10)
                recall, latency_ms = await evaluate_session_recall(conversations, k=5)
                rerank_result = None
                rerank_error = ""
                if getattr(args, "locomo_rerank", False):
                    try:
                        from machina.rag.reranker import (
                            build_reranker,
                            resolve_reranker_selection,
                        )

                        selection = resolve_reranker_selection()
                        if selection.kind == "none":
                            rerank_error = selection.reason or "no reranker backend available"
                        else:
                            reranker = build_reranker(selection)
                            try:
                                rerank_result = await evaluate_archive_retrieval(
                                    conversations, k=5, reranker=reranker
                                )
                            finally:
                                close = getattr(reranker, "close", None)
                                if callable(close):
                                    await close()
                    except Exception as exc:
                        rerank_error = f"{type(exc).__name__}: {exc}"
                return archive, archive_k10, recall, latency_ms, rerank_result, rerank_error

            archive, archive_k10, recall, latency_ms, rerank_result, rerank_error = asyncio.run(
                _with_db()
            )
            result["production_archive_search"] = {
                "evidence_recall": round(archive.evidence_recall, 3),
                "hit_rate": round(archive.hit_rate, 3),
                "mrr": round(archive.mean_reciprocal_rank, 3),
                "k": archive.k,
                "search_mode": archive.search_mode,
                "components": archive.components,
            }
            # Recall@k growth for the production engine (same questions),
            # mirroring the TF-IDF baseline view (separate run at k=10).
            result["production_archive_search_k10"] = {
                "evidence_recall": round(archive_k10.evidence_recall, 3),
                "hit_rate": round(archive_k10.hit_rate, 3),
                "mrr": round(archive_k10.mean_reciprocal_rank, 3),
                "k": archive_k10.k,
                "search_mode": archive_k10.search_mode,
                "components": archive_k10.components,
            }
            result["production_session_recall"] = {
                "evidence_recall": round(recall.evidence_recall, 3),
                "hit_rate": round(recall.hit_rate, 3),
                "mrr": round(recall.mean_reciprocal_rank, 3),
                "mean_latency_ms": round(latency_ms, 1),
                "k": recall.k,
            }
            if getattr(args, "locomo_rerank", False):
                if rerank_result is not None:
                    result["production_archive_search_rerank"] = {
                        "evidence_recall": round(rerank_result.evidence_recall, 3),
                        "hit_rate": round(rerank_result.hit_rate, 3),
                        "mrr": round(rerank_result.mean_reciprocal_rank, 3),
                        "k": rerank_result.k,
                        "search_mode": rerank_result.search_mode,
                        "components": rerank_result.components,
                        "rerank": rerank_result.rerank_stats,
                    }
                    result["rerank_comparison"] = {
                        "note": "same questions, k=5: archive search rerank off vs on",
                        "recall_delta": round(
                            rerank_result.evidence_recall - archive.evidence_recall, 4
                        ),
                        "hit_delta": round(rerank_result.hit_rate - archive.hit_rate, 4),
                        "mrr_delta": round(
                            rerank_result.mean_reciprocal_rank - archive.mean_reciprocal_rank, 4
                        ),
                    }
                else:
                    result["production_archive_search_rerank"] = {
                        "status": "SKIPPED",
                        "reason": rerank_error or "reranker unavailable",
                    }
            result["comparison"] = {
                "note": "same questions, same k=5: TF-IDF baseline vs production engines",
                "baseline": result["baseline_tfidf"]["k=5"],
                "archive": result["production_archive_search"],
                "session_recall": result["production_session_recall"],
            }
        except Exception as exc:
            result["production_archive_search"] = {
                "status": "SKIPPED",
                "reason": f"{type(exc).__name__}: {exc}",
            }
    result["status"] = "OK"
    return result


# ─── Prefix composition ─────────────────────────────────────────────────────


def _prefix() -> dict:
    _ensure_tests_importable()
    from tests.test_s5_prefix_probe import _measure

    m = _measure()
    return {
        "n_tools": m["n_tools"],
        "prefix_tokens": m["prefix_tokens"],
        "prefix_bytes": m["prefix_bytes"],
        "rounds": m["rounds"],
        "turn_bytes": m["turn_bytes"],
        "identical_prefix_resent_bytes": m["resent_prefix_bytes"],
        "identical_prefix_resent_pct": round(100 * m["resent_share"], 1),
    }


def _architecture() -> dict:
    _ensure_tests_importable()
    from tests.test_architecture_layers import (
        DEFERRED_IMPORT_CAP,
        GOD_FILE_CEILINGS,
        _deferred_import_count,
    )

    root = _REPO_ROOT / "machina"
    total_deferred = sum(_deferred_import_count(p) for p in root.rglob("*.py"))
    files = list(root.rglob("*.py"))
    total_loc = sum(len(f.read_text(encoding="utf-8").splitlines()) for f in files)
    god = {}
    for rel, ceiling in GOD_FILE_CEILINGS:
        god[rel] = {"lines": sum(1 for _ in open(root / rel)), "ceiling": ceiling}
    return {
        "files": len(files),
        "loc": total_loc,
        "deferred_imports": total_deferred,
        # Single-sourced from tests/test_architecture_layers.py (delta table
        # lives there); no mirrored literal to drift.
        "ratchet_cap": DEFERRED_IMPORT_CAP,
        "god_files": god,
    }


# ─── Comparisons (incl. the honest cross-tool protocol) ─────────────────────


def _comparisons(results: dict) -> dict:
    replay = results.get("replay", {})
    comparisons: dict = {
        "incident_vs_now": {
            "incident": _INCIDENT["display"],
            "now": (
                f"provider_calls={replay.get('provider_calls')}, "
                f"recall_calls={replay.get('recall_calls')}, "
                f"artifact_written={replay.get('artifact_written')}"
            ),
        },
        "memory_baseline_vs_production": (
            results.get("locomo", {}).get("comparison", {})
            or "run with --locomo-data and --locomo-db for the baseline-vs-production table"
        ),
        "cross_tool_vs_opencode": {
            "status": "PROTOCOL — not directly comparable by construction",
            "why": (
                "OpenCode has no LoCoMo-style persistent-memory implementation to "
                "run the same questions through, and this benchmark cannot drive "
                "another agent harness headlessly. Comparing token counts across "
                "different systems without a shared task is not a measurement."
            ),
            "fair_axes": [
                "same-task delivery: tokens/calls/wall-time to produce the same "
                "artifact (this benchmark's replay harness is the task definition)",
                "memory quality: LoCoMo recall@k/MRR of each system's memory engine",
            ],
            "how_to_fill": (
                "run the identical replay task and LoCoMo dataset through the "
                "other system with its own harness, then compare rows here"
            ),
        },
    }
    return comparisons


# ─── Optional: full suite / UI ──────────────────────────────────────────────


def _suite() -> dict:
    started = time.monotonic()
    try:
        out = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/",
                "-q",
                "-p",
                "no:cacheprovider",
            ],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=_SUITE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {
            "summary": "",
            "error": f"backend suite timed out after {_SUITE_TIMEOUT_SECONDS}s",
            "wall_seconds": round(time.monotonic() - started, 1),
            "exit_code": 1,
        }
    elapsed = time.monotonic() - started
    summary = ""
    for line in reversed(out.stdout.splitlines()):
        if "passed" in line or "failed" in line:
            summary = line.strip("= ")
            break
    return {"summary": summary, "wall_seconds": round(elapsed, 1), "exit_code": out.returncode}


def _ui() -> dict:
    ui_dir = _REPO_ROOT / "ui"
    started = time.monotonic()
    try:
        out = subprocess.run(
            ["npm", "run", "check"],
            cwd=ui_dir,
            capture_output=True,
            text=True,
            timeout=_UI_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {
            "counts": {},
            "error": f"UI check timed out after {_UI_TIMEOUT_SECONDS}s",
            "wall_seconds": round(time.monotonic() - started, 1),
            "exit_code": 1,
        }
    elapsed = time.monotonic() - started
    counts = {}
    for line in out.stdout.splitlines():
        stripped = line.strip()
        for key in ("pass", "fail", "skipped"):
            if stripped.startswith(f"ℹ {key}") or stripped.startswith(f"# {key}"):
                counts[key] = int(stripped.split()[-1])
    return {"counts": counts, "wall_seconds": round(elapsed, 1), "exit_code": out.returncode}


# ─── Output ─────────────────────────────────────────────────────────────────


def _print_section(title: str, payload: dict) -> None:
    print(f"\n## {title}")
    if isinstance(payload, str):
        print(f"  {payload}")
        return
    for key, value in payload.items():
        if isinstance(value, dict):
            print(f"  {key}:")
            for k2, v2 in value.items():
                if isinstance(v2, dict):
                    print(f"    {k2}: {json.dumps(v2)}")
                else:
                    print(f"    {k2}: {v2}")
        elif isinstance(value, list):
            print(f"  {key}:")
            for item in value:
                print(f"    - {item}")
        else:
            print(f"  {key}: {value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", metavar="PATH", help="also write results as JSON")
    parser.add_argument("--with-suite", action="store_true", help="add the full backend suite")
    parser.add_argument("--with-ui", action="store_true", help="add the UI check")
    parser.add_argument("--locomo-data", metavar="PATH", help="path to locomo10.json")
    parser.add_argument(
        "--locomo-db",
        action="store_true",
        help="also run LoCoMo through production archive search + session recall (needs DB)",
    )
    parser.add_argument(
        "--locomo-rerank",
        action="store_true",
        help=(
            "also replay archive search with the configured reranker and report the "
            "rerank off/on delta (status counts prove the reranker ran)"
        ),
    )
    parser.add_argument(
        "--section",
        default="environment,replay,retrieval,locomo,prefix,architecture",
        help="comma-separated sections to run",
    )
    args = parser.parse_args(argv)

    wanted = {s.strip() for s in args.section.split(",") if s.strip()}
    results: dict = {}
    failures: list[str] = []

    def _record(name: str, payload: dict) -> None:
        """Track failed sections so the process exits non-zero (honest CI)."""
        error = payload.get("error")
        if error:
            failures.append(f"{name}: {error}")
        elif payload.get("exit_code") not in (None, 0):
            failures.append(f"{name}: exit code {payload['exit_code']}")

    runners = {
        "environment": _environment,
        "replay": _incident_replay,
        "retrieval": _retrieval,
        "locomo": lambda: _locomo(args),
        "prefix": _prefix,
        "architecture": _architecture,
    }

    print("=" * 60)
    print("Machina offline benchmark")
    print("=" * 60)
    for name, fn in runners.items():
        if name not in wanted:
            continue
        started = time.monotonic()
        try:
            payload = fn()
        except Exception as exc:  # a single broken section must not kill the run
            payload = {"error": f"{type(exc).__name__}: {exc}"}
        payload["_section_seconds"] = round(time.monotonic() - started, 2)
        results[name] = payload
        _print_section(name, payload)
        _record(name, payload)

    if args.with_suite:
        _print_section("suite", {"running": "full backend suite — this takes minutes"})
        payload = _suite()
        results["suite"] = payload
        _print_section("suite", payload)
        _record("suite", payload)
    if args.with_ui:
        payload = _ui()
        results["ui"] = payload
        _print_section("ui", payload)
        _record("ui", payload)

    comparisons = _comparisons(results)
    results["comparisons"] = comparisons
    _print_section("comparisons", comparisons)

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nJSON written: {args.json}")
    if failures:
        print("\nfailed sections:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\ndone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
