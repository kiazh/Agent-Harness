# Document retrieval quality — 2026-10-13

Machina retains PostgreSQL full-text ranking (`ts_rank`), pgvector cosine search
and reciprocal rank fusion. Full-text ranking is not BM25; descriptions now say
PostgreSQL FTS. Cohere reranking remains optional. Installing a skills collection
does not install its example frameworks or replace the application's database.

All 83 skills from [Firecrawl AI research skills](https://github.com/firecrawl/AI-research-SKILLs)
were installed for Codex only, pinned to
`7cfa204ffb6db2ded22376eaf25f3072f3b02f96`. Installed SKILL.md files match that
checkout. Relevant guidance applied: LlamaIndex's source evidence boundaries,
sentence-transformers' ranking evaluation, Phoenix and LangSmith's versioned
experiments and measured retrieval, DSPy's representative evaluation before
optimization, and Qdrant's filtered retrieval and payload provenance. No remote
tracing or additional paid services were enabled.

## Corrections

Identity reranking and provider failure preserve retrieval order and scores.
`None` is an explicit passthrough score in the reranker contract; genuine learned
scores populate `rerank_score`. Both provider parsing and the pipeline validate
indices, uniqueness and finite scores before mutating candidates. Invalid,
empty or failed reranking falls back to the retrieved candidates. Per-result
retrieval mode, retrieval degradation and rerank status survive cache snapshots.

`search_documents` now returns bounded passages with chunk IDs, source labels,
line structure, score provenance and explicit truncation markers. The entire
response is budgeted below 8,500 characters, and the agent permits 9,000 for
this tool. The tool accepts 1–10 results and retains active-session ownership
checks. Whitespace-only searches make no embedding request.

## Reproduce the baseline

Use an initialized, isolated test database. The command creates a temporary
session, indexes the versioned corpus, runs the actual `RAGPipeline.search`, then
deletes that session and its chunks even when evaluation fails.

```sh
export MACHINA_TEST_DATABASE_URL='postgresql://postgres:postgres@localhost:5432/machina_test'
python -m machina.rag.evaluation --top-k 5 --output /tmp/rag-keyword.json
```

The default explicitly disables embeddings and learned reranking; it makes no
model API calls even if local credentials exist. `--use-configured-models`
opts into configured embeddings and optional Cohere reranking, including their
charges. Use the same corpus and k for a comparison. `--dataset` accepts the
JSON structure in `tests/data/rag/machina_docs_v1.json`; `--root` sets the checkout.
The report records corpus hashes, source rankings, source recall@k, hit@k, MRR,
binary nDCG@k, latency, evidence bytes and actual modes/statuses on returned hits.
Empty-result execution degradation is currently unmeasured. Configured reranker
identity is labelled separately from actual per-query results.

The initial PostgreSQL keyword run at k=5 achieved recall, hit rate, MRR and
nDCG of 1.0 on three answerable queries, and no hits on one nonsense query.
Per-query latency was about 4–10 ms on the local database. These are a small
source-level smoke baseline, not a passage-quality, semantic-retrieval,
production-load or answer-faithfulness benchmark. Duplicate source hits occupy
rank positions but earn relevance only once; no-answer cases are excluded from
quality averages and report unexpected retrieval separately.

The benchmark test runs in the existing DB-backed backend suite. Next evidence
needed: broader manually labelled questions, paraphrases and hard negatives,
passage-level answer spans, multilingual queries, and comparisons using actual
configured models. Query rewriting and cross-encoder tuning should be justified
by those comparisons. The shared recall tokenizer still has its existing ASCII
and term-budget limitations; this change does not claim to resolve them.

Final verification: Python 3.11 backend selection passed **2,562 tests**, with one
skip and nine UI/research exclusions. Ruff lint and format passed for the CI
backend scope and changed tests; Bandit reported no medium/high issues. A real
PostgreSQL corpus test and a provider-message regression cover retrieval cleanup
and evidence reaching the next model turn. No paid API calls were used.
