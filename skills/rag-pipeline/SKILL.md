---
name: rag-pipeline
description: "RAG pipeline engineering — embedding models, hybrid retrieval, cross-encoder reranking, CRAG evaluation, query rewriting, context compression, web fallback, RRF"
triggers:
  - "rag"
  - "retrieval"
  - "embedding"
  - "rerank"
  - "crag"
  - "search"
  - "hybrid"
version: 1.0.0
---

# RAG Pipeline Engineering

## Core Skills

| Skill | Practice |
|---|---|
| Embedding models | Compare text-embedding-3-small vs nomic-embed-text |
| Hybrid retrieval | BM25 + pgvector, fuse with reciprocal rank |
| Cross-encoder reranking | ms-marco-MiniLM-L-6-v2 reranker |
| CRAG evaluation | Three-way classification (correct/incorrect/ambiguous) |
| Query rewriting | HyDE: generate hypothetical answer, embed it |
| Context compression | LLMLingua or extractive compression |
| Web fallback | SearXNG or Perplexity API |
| Reciprocal Rank Fusion | Combine multiple retrieval signals |

## Key Resources
- [CRAG paper](https://arxiv.org/abs/2401.15884)
- [Self-RAG paper](https://arxiv.org/abs/2310.11511)
- [SearXNG self-hosted](https://github.com/searxng/searxng)

## Practice Project
Build a RAG pipeline: query → embed → pgvector search → rerank → CRAG evaluate → inject top-3 into prompt. Measure Recall@5 and MRR.
