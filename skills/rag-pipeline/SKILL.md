---
name: rag-pipeline
description: RAG pipeline engineering — embedding models, hybrid retrieval, cross-encoder reranking, CRAG
  evaluation, query rewriting, context compression, web fallback, RRF
triggers:
- rag
- retrieval
- embedding
- rerank
- crag
- search
- hybrid
version: 1.0.0
schema_version: 2
skill_id: rag-pipeline
task_families:
- backend-api
- data-analysis
capabilities:
- database
- performance
aliases:
- rag
- retrieval
- hybrid-search
- reranking
- retrieval-augmented-generation
stack:
  languages:
  - python
  frameworks:
  - pgvector
  - sentence-transformers
  - openai
  independent: true
preconditions:
- A document corpus is available for indexing
- An embedding model and vector store are chosen or choosable
- Queries or an evaluation set exist to measure retrieval quality
required_tools:
- python
- pgvector
exclusions:
- Corpus is already indexed and only the final prompt wording changes
- Training or adapting embedding weights (use embedding-finetuning)
- Unrelated API or frontend work with no retrieval component
sections:
- id: core-skills
  heading: Core Skills
  stage: implement
  requires_sections: []
- id: key-resources
  heading: Key Resources
  stage: inspect
  requires_sections: []
- id: practice-project
  heading: Practice Project
  stage: verify
  requires_sections:
  - core-skills
metadata_source: generated
metadata_generated_at: '2026-10-17T05:24:37.333192+00:00'
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
