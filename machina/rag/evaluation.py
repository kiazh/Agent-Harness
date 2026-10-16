"""Evaluate document retrieval through the same pipeline used by the agent.

No generation or oracle evidence injection: relevance is judged against versioned
source labels. Callers own indexing, session scope and provider configuration.
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from machina.rag.pipeline import RAGPipeline


@dataclass(frozen=True)
class RetrievalCase:
    """A query with relevant source labels; empty labels mean no known answer."""

    name: str
    query: str
    relevant_sources: tuple[str, ...]


async def evaluate_retrieval(
    pipeline: RAGPipeline,
    session_id: uuid.UUID,
    cases: list[RetrievalCase],
    top_k: int = 5,
    rerank: bool = True,
) -> dict[str, Any]:
    """Report source recall, first relevant rank, binary nDCG and latency.

    Duplicate source hits occupy rank slots but contribute relevance once. Empty
    gold sets are excluded from quality averages and report unexpected retrieval
    separately; scores across FTS, RRF and learned rerankers are not comparable.
    Latency includes the pipeline cache behavior of this run.
    """
    if type(top_k) is not int or top_k < 1:
        raise ValueError("top_k must be a positive integer")
    if len({case.name for case in cases}) != len(cases):
        raise ValueError("case names must be unique")
    rows = []
    for case in cases:
        started = time.perf_counter()
        results = await pipeline.search(
            query=case.query, session_id=session_id, top_k=top_k, rerank=rerank
        )
        elapsed = (time.perf_counter() - started) * 1000
        sources = [
            str(r.chunk.payload.get("metadata", {}).get("source", "unknown"))
            for r in results[:top_k]
        ]
        gold, seen, ranks = set(case.relevant_sources), set(), []
        for rank, source in enumerate(sources, 1):
            if source in gold and source not in seen:
                ranks.append(rank)
            seen.add(source)
        ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(len(gold), top_k) + 1))
        rows.append(
            {
                "name": case.name,
                "sources": sources,
                "retrieval_modes": sorted({r.retrieval_mode for r in results}),
                "rerank_statuses": sorted({r.rerank_status for r in results}),
                "degraded": any(
                    r.retrieval_degraded or r.rerank_status == "fallback" for r in results
                ),
                "recall_at_k": len(ranks) / len(gold) if gold else None,
                "hit_at_k": float(bool(ranks)) if gold else None,
                "mrr": 1 / ranks[0] if ranks else (0.0 if gold else None),
                "ndcg_at_k": sum(1 / math.log2(rank + 1) for rank in ranks) / ideal
                if gold
                else None,
                "unexpected_retrieval": bool(results) if not gold else None,
                "latency_ms": elapsed,
                "evidence_bytes": sum(
                    len(r.chunk.payload.get("text", "").encode()) for r in results[:top_k]
                ),
            }
        )
    answerable = [row for row in rows if row["recall_at_k"] is not None]
    absent = [row for row in rows if row["unexpected_retrieval"] is not None]
    return {
        "top_k": top_k,
        "configured_reranker": pipeline.reranker_identity(),
        "degraded_queries_with_results": sum(row["degraded"] for row in rows),
        "cases": rows,
        "means": {
            metric: sum(row[metric] for row in answerable) / len(answerable) if answerable else None
            for metric in ("recall_at_k", "hit_at_k", "mrr", "ndcg_at_k")
        },
        "no_answer_false_positive_rate": sum(row["unexpected_retrieval"] for row in absent)
        / len(absent)
        if absent
        else None,
    }


async def benchmark_documents(
    pipeline: RAGPipeline,
    dataset: dict[str, Any],
    root: Any,
    top_k: int = 5,
) -> dict[str, Any]:
    """Index a labelled corpus in a temporary session and always remove it.

    Intended for an isolated evaluation database, never a user's chat session.
    Documents are rooted at the checkout; report hashes make corpus drift visible.
    """
    import hashlib

    from machina.db.connection import db

    root = Path(root).resolve()
    sid = uuid.uuid4()
    paths = []
    for source in dataset["documents"]:
        path = (root / source).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Corpus document escapes repository root")
        paths.append((source, path))
    await db.execute("INSERT INTO sessions (id, title) VALUES ($1, $2)", sid, "RAG evaluation")
    try:
        hashes = {}
        for source, path in paths:
            hashes[source] = hashlib.sha256(path.read_bytes()).hexdigest()
            await pipeline.index_document(str(path), sid)
        cases = [
            RetrievalCase(
                row["name"],
                row["query"],
                tuple(str((root / source).resolve()) for source in row["relevant_sources"]),
            )
            for row in dataset["cases"]
        ]
        report = await evaluate_retrieval(pipeline, sid, cases, top_k)
        report.update(
            {
                "dataset_version": dataset["version"],
                "document_sha256": hashes,
                "embedding_mode": "configured"
                if pipeline.config.enable_embeddings
                else "keyword-only",
            }
        )
        return report
    finally:
        await db.execute("DELETE FROM sessions WHERE id = $1", sid)


def _devtools_dataset_path() -> Path | None:
    """Default dataset inside the Machina-DevTools checkout, or None.

    The labelled corpus lives with the DevTools harnesses (single source of
    truth); reuse the benchmark's resolver so ``MACHINA_DEVTOOLS`` and sibling
    checkouts behave identically here.
    """
    from machina.research.benchmark import _devtools_root_or_none

    devtools = _devtools_root_or_none()
    if devtools is None:
        return None
    return devtools / "tests" / "data" / "rag" / "machina_docs_v1.json"


def main() -> None:
    """Run a keyword-only benchmark by default; models require explicit opt-in."""
    import argparse
    import asyncio
    import json
    import os

    from machina.db.connection import db
    from machina.rag.pipeline import RAGConfig
    from machina.tools.rag import _build_shared_pipeline

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=None,
        help=(
            "labelled dataset JSON (default: Machina-DevTools tests/data/rag/machina_docs_v1.json)"
        ),
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--use-configured-models",
        action="store_true",
        help="Enable configured embedding/reranking APIs (may incur charges)",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.dataset is None:
        args.dataset = _devtools_dataset_path()
        if args.dataset is None:
            parser.error(
                "no --dataset given and the Machina-DevTools checkout was not found "
                "next to this repository; clone it or point MACHINA_DEVTOOLS at it"
            )
    if not args.dataset.is_file():
        parser.error(f"dataset not found: {args.dataset} (pass --dataset PATH)")
    dsn = os.environ.get("MACHINA_TEST_DATABASE_URL")
    if not dsn:
        parser.error("Set MACHINA_TEST_DATABASE_URL to an isolated initialized evaluation database")
    if args.top_k < 1:
        parser.error("--top-k must be positive")

    async def run():
        db.dsn = dsn
        await db.connect()
        pipeline = (
            _build_shared_pipeline()
            if args.use_configured_models
            else RAGPipeline(config=RAGConfig(enable_embeddings=False, enable_reranking=False))
        )
        try:
            return await benchmark_documents(
                pipeline, json.loads(args.dataset.read_text()), args.root, args.top_k
            )
        finally:
            for client in (pipeline._embedder, pipeline._reranker):
                if client is not None and hasattr(client, "close"):
                    await client.close()
            await db.close()

    output = json.dumps(asyncio.run(run()), indent=2)
    if args.output:
        args.output.write_text(output + "\n")
    else:
        print(output)


if __name__ == "__main__":
    main()
