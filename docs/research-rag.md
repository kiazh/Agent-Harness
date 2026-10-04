# Production RAG Pipeline Research for AgentHarness

**Date:** 2026-10-01  
**Context:** AgentHarness has a shipped RAG pipeline (`ah/rag/`) and memory system (`ah/memory/`). The architecture uses PostgreSQL + pgvector with a `context_chunks` table (embedding vector(1536), HNSW index, MessagePack payloads). This document researches what a production-grade RAG pipeline looks like and how it maps to AgentHarness.

---

## 1. Architecture Overview

A production RAG pipeline has three phases:

```
INGESTION:  parse → clean → chunk → embed → index
RETRIEVAL:  query → hybrid search → fusion → rerank → top-k
GENERATION: context + question → LLM → answer + citations
EVALUATION: continuous loop (golden set + metrics)
```

The current AgentHarness `ContextManager` already has:
- `search_by_embedding()` — single-vector cosine similarity search
- HNSW index on `embedding vector(1536)`
- `context_chunks` table with `payload_msgpack`, `token_count`, `chunk_type`

What's missing for production RAG:
- **Hybrid search** (BM25 + dense vector)
- **Reranking** (cross-encoder second stage)
- **Chunking strategies** (currently raw payloads, no intelligent splitting)
- **Evaluation framework** (no metrics, no golden set)
- **Query understanding** (no query rewriting/expansion)

---

## 2. Embedding Strategies

### 2.1 Model Selection

| Model | Dimensions | License | Best For | Context | MTEB Score |
|-------|-----------|---------|----------|---------|------------|
| **BGE-M3** (BAAI) | 1024 | MIT | Dense + sparse + multi-vector in one model | 8,192 | ~68 |
| **Qwen3-Embedding-8B** | 7168 (flex) | Commercial | Highest open-weight quality | 32,000 | ~75 |
| **text-embedding-3-large** (OpenAI) | 3072 (flex) | Proprietary | Managed API, no infra | 8,192 | ~64.6 |
| **text-embedding-3-small** (OpenAI) | 1536 (flex) | Proprietary | Low-latency, cost-effective | 8,192 | ~62 |
| **all-MiniLM-L6-v2** | 384 | Apache 2.0 | CPU-only, prototyping | 512 | ~56 |
| **Nomic Embed v2** | 768 (flex) | Apache 2.0 | Transparency/auditability | 8,192 | ~64 |

**Recommendation for AgentHarness:**
- **Default:** BGE-M3 (MIT license, self-hostable, dense+sparse+multi-vector in one model, 1024 dims)
- **API fallback:** OpenAI text-embedding-3-small (1536 dims — matches existing schema)
- **CPU-only:** all-MiniLM-L6-v2 (384 dims, fast, no GPU needed)

The current schema uses `vector(1536)` which matches OpenAI's text-embedding-3-small. If switching to BGE-M3, the column would need to be `vector(1024)`.

### 2.2 Embedding Best Practices

- **Normalize embeddings** (L2 normalization) for cosine similarity — most models do this by default
- **Use the same model** for indexing and querying — mixing models produces meaningless similarity scores
- **Matryoshka learning** (OpenAI, BGE-M3, Nomic): store fewer dimensions (e.g., 512 instead of 1536) for cost savings with minimal quality loss
- **Batch embedding** during ingestion — much faster than one-by-one
- **Cache embeddings** for repeated queries (e.g., LRU cache on query hash)

### 2.3 Embedding Storage in AgentHarness

Current schema:
```sql
embedding vector(1536)
-- HNSW index: m=16, ef_construction=64
```

Considerations:
- HNSW `ef_construction=64` is good for build quality; `ef_search` (query-time) defaults to 40 — increase to 64-128 for better recall at slight latency cost
- For BGE-M3 multi-vector: would need a separate table or JSONB column for sparse/multi-vector representations
- Consider adding `embedding_model` column to track which model produced each embedding (for migration/evaluation)

---

## 3. Chunking Strategies

### 3.1 Strategy Comparison

| Strategy | Quality | Speed | Best For |
|----------|---------|-------|----------|
| **Fixed-size** | ★★★ | ★★★★★ | Prototyping, uniform content |
| **Recursive character** | ★★★★ | ★★★★ | General-purpose, production default |
| **Semantic** | ★★★★★ | ★★ | High-accuracy, research papers |
| **Structure-aware** | ★★★★★ | ★★★ | Markdown, code, HTML, PDFs |
| **Parent-child (small-to-big)** | ★★★★★ | ★★★ | Precision + context both important |
| **Contextual retrieval** | ★★★★★ | ★★ | Large diverse corpora |

### 3.2 Recommended Approach for AgentHarness

**Default: Recursive character splitting**
- Split on paragraph → sentence → word boundaries
- Chunk size: 400-512 tokens
- Overlap: 10-20% (40-100 tokens)
- Preserves semantic coherence, no embedding API calls needed during ingestion

**For structured content: Structure-aware chunking**
- Markdown: split on `#`, `##`, `###` headers, keep heading path in metadata
- Code: split by function/class using AST parser (tree-sitter)
- HTML: split on `<section>`, `<article>`, `<h1-h6>`

**For production quality: Parent-child chunking**
1. Split into small chunks (~150 tokens) → embed these for precise retrieval
2. Store mapping to parent chunks (~1000 tokens) for LLM context
3. At query time: retrieve using small embeddings, return parents to LLM

**Contextual retrieval (Anthropic's approach):**
- Before embedding, prepend a short LLM-generated description of what the chunk is about in the context of the whole document
- Example: `"[Excerpt from Acme Q3 2024 earnings, Financial Performance section] Revenue grew 12%..."`
- Reduces retrieval-failure rate significantly (Anthropic reported 5.7% → 1.9%)

### 3.3 Chunking in AgentHarness Context

`context_chunks` stores both raw conversation/tool payloads and RAG document chunks produced by `RecursiveCharacterTextSplitter`. For very long content:

- **Tool results** and **assistant messages** should be chunked if they exceed ~512 tokens
- **Metadata to preserve:** source, section heading, document date, chunk position
- **Chunk ID strategy:** deterministic IDs (hash of content + position) for deduplication

---

## 4. Hybrid Search

### 4.1 Why Hybrid?

Dense vector search excels at semantic meaning but misses:
- Exact terminology, product codes, identifiers
- Proper nouns, version numbers
- Short factual matches

Sparse search (BM25) excels at:
- Exact word match, rare keywords, numeric tokens
- Technical identifiers, jargon

**Hybrid = best of both worlds.** Typically +10-20% recall over dense-only.

### 4.2 Architecture

```
Query
  ├──▶ Dense retrieval (top 50)  ──┐
  │                                  ├──▶ RRF Fusion ──▶ Reranker ──▶ Top 5-10
  └──▶ BM25 retrieval (top 50)  ──┘
```

### 4.3 Reciprocal Rank Fusion (RRF)

The production-default fusion method. Operates on ranks, not scores — no normalization needed.

```
RRF(d) = Σ 1 / (k + rank_i(d))
```

Where `k=60` is the standard constant (prevents any single result from dominating).

**Tuning k:**
- Large corpora (10K+ docs): k=60 (default)
- Small corpora (100-300 docs): k=10-20
- k=60 flattens rank differences in small corpora

### 4.4 Implementation in AgentHarness

Shipped: `HybridSearch` runs dense pgvector cosine search concurrently with BM25 (Postgres FTS) and fuses via RRF; `search_by_embedding()` remains for dense-only callers.

For hybrid search, add:
1. **BM25 index** — either:
   - PostgreSQL full-text search (`tsvector` + GIN index) on a text column
   - Separate BM25 library (e.g., `rank_bm25`, `bm25s`) for in-process search
2. **RRF fusion** — in-process, <1ms
3. **Concurrent retrieval** — run BM25 and dense in parallel (`asyncio.gather()`)

Schema addition:
```sql
-- Add text search column
ALTER TABLE context_chunks ADD COLUMN search_text TEXT;
CREATE INDEX idx_context_chunks_fts ON context_chunks 
    USING GIN (to_tsvector('english', search_text));
```

### 4.5 SPLADE Alternative

SPLADE (SParse Lexical AnD Expansion Model) is a BERT-based sparse retrieval model that learns to expand queries with relevant terms. More accurate than BM25 but adds 100-300ms inference latency. Good for query-time only (not document indexing).

---

## 5. Reranking

### 5.1 Why Rerank?

Bi-encoder (embedding model) encodes query and document independently — fast but coarse. Cross-encoder sees query and document together — slow but precise.

**Reranking is the single highest-leverage quality improvement in a RAG pipeline** (+5-15 NDCG points).

### 5.2 Two-Stage Pattern

```
Stage 1: Bi-encoder retrieve top 50-100 candidates (fast, ~10ms)
Stage 2: Cross-encoder rerank top 50 → keep top 5-10 (slower, ~50-300ms)
```

### 5.3 Reranker Options (2026)

| Model | Type | Latency (100 docs) | Cost/1K queries | License | Best For |
|-------|------|-------------------|-----------------|---------|----------|
| **Cohere Rerank 3.5** | Managed API | ~85ms | $2.00 | Proprietary | Fast prototyping, English + multilingual |
| **BGE Reranker v2-M3** | Self-hosted | ~140ms (L4 GPU) | ~$0.10 | Apache 20 | Best open-weight, self-hosted |
| **Jina Reranker v3** | API + self-host | ~188ms | $0.30 | Open weights | Sub-200ms latency, multilingual |
| **Voyage Rerank 2.5** | Managed API | ~120ms | $0.05/1M tokens | Proprietary | Code, finance, legal domains |
| **FlashRank** | Self-hosted (CPU) | ~15-30ms | Free | Apache 2.0 | Edge, sub-30ms budgets |
| **ColBERT v2** | Late interaction | ~25ms (indexed) | ~$0.05 self-host | MIT | High QPS, English-heavy |

### 5.4 Reranker Comparison on BEIR

| Pipeline | NDCG@10 |
|----------|---------|
| BM25 only | 41.7 |
| Bi-encoder (BGE-base) | 51.0 |
| Bi-encoder + Cross-encoder rerank | 56.5 |
| Bi-encoder + GPT-4 rerank | 58.2 |

### 5.5 Production Best Practices

- **Retrieve wide, rerank tight:** Retrieve 50-100, rerank to 5-10
- **Batch reranking calls** — 100 pairs in one batch, not 100 sequential calls
- **Set timeout** — reranker timeout ≤ 60% of total latency budget; fallback to un-reranked results
- **Chunk length assertions** — cross-encoders silently truncate; assert chunks fit in model's window
- **Warmup on deploy** — first request to a loaded model can take 2-5x longer
- **Cache scores** — for stable corpora, cache (query, doc) score pairs
- **Monitor quality drift** — run RAGAS/DeepEval weekly, alert on NDCG@10 regressions >2 points

### 5.6 When NOT to Rerank

- Very small corpora (< 1000 chunks) — marginal gain doesn't justify latency
- Hard-real-time UX (< 200ms total budget) — use FlashRank or skip
- Pure exact-match workloads — BM25 alone suffices

---

## 6. Evaluation

### 6.1 Core Metrics

Every serious RAG evaluation framework converges on four metrics:

| Metric | What It Measures | Target | Inputs |
|--------|-----------------|--------|--------|
| **Faithfulness** | Is the answer grounded in retrieved context? | ≥ 0.85 | answer, contexts |
| **Answer Relevancy** | Does the answer address the question? | ≥ 0.80 | question, answer |
| **Context Precision** | Are relevant chunks ranked highly? | ≥ 0.75 | question, contexts, ground truth |
| **Context Recall** | Did retrieval find all needed info? | ≥ 0.80 | question, contexts, ground truth |

### 6.2 Metric Definitions

**Faithfulness** = (# claims in answer supported by context) / (# claims in answer)
- LLM extracts atomic claims from answer, then checks each against context
- Primary hallucination defense

**Answer Relevancy** = mean(cosine_sim(original_question, generated_question_i))
- LLM generates N synthetic questions from the answer
- Measures cosine similarity between original and generated questions
- Penalizes evasive/non-committal answers

**Context Precision@K** = sum(Precision@k * v_k for k in 1..K) / total_relevant
- Rank-weighted: relevant chunks at top score higher
- Signal-to-noise ratio of retriever

**Context Recall** = (# ground-truth claims attributable to context) / (# ground-truth claims)
- Did the retriever find everything needed?

### 6.3 Diagnostic Patterns

| Pattern | Diagnosis | Fix |
|---------|-----------|-----|
| Low faithfulness, high context precision | Generator hallucinating | Lower temperature, stronger system prompt |
| Low context recall, high faithfulness | Retriever missing chunks | Hybrid search, larger top-k, better chunking |
| Low context precision, acceptable recall | Poor ranking | Add reranker, reduce top-k |
| High recall, low precision | Too much noise | Reranker, smaller chunks |
| Low answer relevancy, high everything else | Prompt issue | Improve prompt template |

### 6.4 Evaluation Frameworks

| Framework | Best For | Key Feature |
|-----------|----------|-------------|
| **RAGAS** | Offline experimentation | Reference-free metrics, synthetic data generation |
| **DeepEval** | CI/CD gating | Pytest-native, fails build on regression |
| **TruLens** | Production observability | OpenTelemetry tracing, dashboard |
| **Patronus AI** | Production monitoring | Hallucination detection (Lynx model) |

**Recommendation:** Use RAGAS for offline evaluation, DeepEval for CI gating.

### 6.5 Building a Golden Set

- Start with 50-150 representative queries with hand-labeled ground truth
- Include out-of-scope questions (should return "I don't know")
- Record chunk IDs that a perfect retriever should surface
- Grow the set over time from production failures
- Run evaluation on every change to chunking, embedding, or reranking

### 6.6 RAGAS Example

```python
from ragas import evaluate
from ragas.metrics import faithfulness, answer_relevancy, context_precision, context_recall
from datasets import Dataset

data = {
    "question": ["What is the default chunk size?"],
    "answer": ["The default chunk size is 1024 tokens."],
    "contexts": [["The SentenceSplitter default chunk_size is 1024 tokens."]],
    "ground_truth": ["1024 tokens"],
}
dataset = Dataset.from_dict(data)
result = evaluate(dataset, metrics=[faithfulness, answer_relevancy, context_precision, context_recall])
```

### 6.7 Production Thresholds (2026 Consensus)

| Metric | Minimum | Good | Excellent |
|--------|---------|------|-----------|
| Faithfulness | 0.75 | 0.85 | 0.93+ |
| Answer Relevancy | 0.80 | 0.85 | 0.92+ |
| Context Precision | 0.70 | 0.75 | 0.85+ |
| Context Recall | 0.80 | 0.85 | 0.90+ |

---

## 7. Implementation Roadmap for AgentHarness

### Phase 1: Foundation
1. **Chunking module** (`ah/rag/chunker.py`)
   - Recursive character splitter (default)
   - Structure-aware splitter for Markdown/code
   - Configurable chunk size and overlap

2. **Embedding module** (`ah/rag/embedder.py`)
   - Abstract embedder interface
   - OpenAI text-embedding-3-small (matches existing 1536-dim schema)
   - BGE-M3 option (would need schema migration to 1024 dims)
   - Batch embedding support

3. **Hybrid search** (`ah/rag/search.py`)
   - BM25 via PostgreSQL FTS or `bm25s` library
   - RRF fusion (k=60 default)
   - Concurrent dense + sparse retrieval

### Phase 2: Quality
4. **Reranking** (`ah/rag/reranker.py`)
   - Abstract reranker interface
   - Cohere Rerank 3.5 (API, easy start)
   - BGE Reranker v2-M3 (self-hosted, production)
   - Configurable top-k and timeout

5. **Contextual retrieval**
   - Prepend chunk context before embedding
   - LLM-generated chunk descriptions

### Phase 3: Evaluation
6. **Evaluation module** (`ah/rag/evaluation.py`)
   - RAGAS integration
   - Golden set management
   - CI/CD gating with DeepEval
   - Metrics dashboard

7. **Query understanding**
   - Query rewriting/expansion
   - Multi-query retrieval
   - Query decomposition for complex questions

### Phase 4: Production
8. **Caching layer**
   - Embedding cache (query hash → embedding)
   - Rerank score cache
   - Result cache for repeated queries

9. **Observability**
   - Per-stage latency tracking
   - Retrieval quality metrics
   - Drift detection

---

## 8. Key Design Decisions

| Decision | Recommendation | Rationale |
|----------|---------------|-----------|
| Embedding model | BGE-M3 (self-hosted) or OpenAI text-embedding-3-small (API) | BGE-M3: MIT, dense+sparse+multi-vector; OpenAI: matches existing schema |
| Chunk size | 400-512 tokens | Balances precision and context |
| Chunk overlap | 10-20% | Prevents information loss at boundaries |
| Chunking strategy | Recursive character (default), structure-aware (for structured content) | Good quality/complexity tradeoff |
| Hybrid search | BM25 + dense + RRF | +10-20% recall over dense-only |
| Reranker | BGE Reranker v2-M3 (self-hosted) or Cohere Rerank 3.5 (API) | Highest-leverage quality improvement |
| Evaluation | RAGAS (offline) + DeepEval (CI) | Standard, well-supported |
| Vector DB | PostgreSQL + pgvector (current) | Already in use, no new infra needed |

---

## 9. References

- [RAGAS: Automated Evaluation of Retrieval Augmented Generation](https://arxiv.org/abs/2309.15217)
- [Anthropic: Contextual Retrieval](https://www.anthropic.com/news/contextual-retrieval)
- [BGE-M3: Multi-Lingual, Multi-Functionality, Multi-Granularity Text Embeddings](https://arxiv.org/abs/2402.03216)
- [Reciprocal Rank Fusion outperforms Condorcet and individual Rank Learning Methods](https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf)
- [RAG Chunking Strategies](https://github.com/p-n-rao/RAG-chunking)
- [Production RAG Architecture Guide](https://ragnight.com/blog/architecture-rag-production-guide-complet)
- [Hybrid RAG System](https://github.com/mirofadlalla/Hybrid-RAG-System)
- [Reranking for RAG in Python: 4 Compared (2026)](https://pythondatabench.com/article/reranking-rag-python-cohere-bge-jina-colbert-2026)
- [Best Embedding Models for RAG 2026](https://www.premai.io/blog/best-embedding-models-for-rag-2026-ranked-by-mteb-score-cost-and-self-hosting/)
- [RAG Evaluation: RAGAS vs DeepEval 2026](https://pythondatabench.com/article/rag-evaluation-python-ragas-deepeval-trulens-2026)
- [Hybrid Search in RAG: Dense + Sparse + RRF](https://blog.gopenai.com/hybrid-search-in-rag-dense-sparse-bm25-splade-reciprocal-rank-fusion-and-when-to-use-which-fafe4fd6156e)
- [RAG Evaluation Metrics: Best Practices](https://www.patronus.ai/llm-testing/rag-evaluation-metrics)

---

*Status: the Phase 1 pipeline (chunker, embedder, hybrid search) and Phase 2 reranker shipped in `ah/rag/`. Phase 3 (evaluation module, query understanding) remains open.*

