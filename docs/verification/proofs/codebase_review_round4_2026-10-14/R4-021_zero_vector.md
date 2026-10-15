# R4-021 — Embedder zero-vector fail-open

- Round: 4 · Date: 2026-10-14 · Finding: `CODEBASE_REVIEW_ROUND4_2026-10-14.md` #10 (V1-10 CONFIRMED).
- Fix: `machina/rag/embedder.py` (`_embed_uncached` payload validation + `embed()` fail-open
  fallback removal) and `machina/memory/embeddings.py` (`embed_query` all-zero guard).
- DB: not needed — proof is at the network boundary (`httpx.MockTransport`), per the
  R4-021 matrix row.

## Root cause (one sentence)

`_embed_uncached` treated `data.get("data", [])` as authoritative, so a 200 response with no
usable vectors produced an empty list; `embed()` then fabricated `[0.0] * dimensions`, cached
it, and `embed_query` returned it as a *successful* embedding, making `MemoryRetriever`
start a dense search with a directionless vector — silent poisoning until LRU eviction.

## Files changed

```text
 machina/rag/embedder.py          | +24 -3
 machina/memory/embeddings.py     | +12 -1
 tests/test_chaos_extended.py     | +6 -5   (bug-pinning test updated)
 tests/test_embedding_failopen_adversarial.py | new, 10 tests
 docs/verification/proofs/.../R4-021_*        | evidence + probe
```

### Source change 1 — `_embed_uncached` validates the payload before returning

```python
items = data.get("data") if isinstance(data, dict) else None
if not isinstance(items, list) or len(items) != len(texts):
    error = _core_symbol("machina.core.exceptions", "ProviderError")(
        f"Embedding API response has "
        f"{len(items) if isinstance(items, list) else 0} usable "
        f"vector(s) for {len(texts)} input(s)"
    )
    _core_symbol("machina.core.provider", "audit_log")(
        "rag_embed_error", model=self._model, error=str(error)
    )
    logger.error("Embedding API response unusable: %s", error)
    raise error
```

- **Typed error = `ProviderError`** (the module's existing error surface — already raised by
  this class for a missing credential; `MachinaError` + `RuntimeError` + `ValueError`). No
  new exception class → no public-API addition. The message is explicit and the sibling
  malformed-payload check (`missing 'index'`) keeps its established `ValueError` behavior.
- **Count validation** is part of the same malformed-response check: the unfixed batch path
  raised only *after* the strict-zip consumed the first pair — i.e. it cached row #1 of a
  short batch before failing (see RED batch probe). Validating `len(items) == len(texts)`
  before returning makes "nothing is cached on failure" true for both paths.
- The audit event + `logger.error` mirror the existing HTTP-failure branch.

### Source change 2 — `embed()` cannot fall back to zeros

```python
result = await self._embed_uncached([text])
embedding = result[0]   # was: result[0] if result else [0.0] * self.dimensions
```

`_embed_uncached` now guarantees exactly one vector for the one non-empty input, so the
zero fallback was dead code that re-introduced the fail-open if the invariant ever broke.
Genuine empty input (`embed("")`) still returns the documented zero vector (pinned by
`test_genuine_empty_input_keeps_zero_vector_contract_and_no_network` and
`test_chaos_extended.py::test_embed_with_empty_text_returns_zero_vector`).

### Source change 3 — memory boundary guard (`embed_query`)

```python
vector = await embedder.embed(text[:2000])
...
if not any(vector):
    error = ValueError("embedding provider returned an all-zero vector")
    logger.debug("embedding unusable, keyword fallback: %s", error)
    record_failure("embeddings.embed_query", error)
    return None
return vector
```

**Deliverable item 2 decision (justified):** the guard is kept because callers *can still
receive* an all-zero vector after the source fix — a well-formed 200 whose `embedding` is
literally all zeros passes shape validation (the API's error mode is unknown shapes; a
misbehaving local/compatible server can still emit a zero row). At the memory boundary that
is converted to the module's honest unavailable signal (`None` + `record_failure`), so
dense retrieval is simply not attempted. `embed_texts` deliberately did **not** get a blanket
zero check: `embed_batch` legitimately maps empty input strings to zero vectors (documented
contract, shared with the local embedders), and an input-aware filter would add
special-casing with no named finding behind it; the batch path's malformed/short failures
now raise at the source before anything is cached.

### Test change — `tests/test_chaos_extended.py`

`test_embed_with_malformed_response_missing_data_key` pinned the bug (`_embed_uncached` →
`[]`, "no crash"). It now pins the typed failure (`pytest.raises(ProviderError, match="usable vector")`).

## RED — unfixed HEAD (cdc5232, scratch git worktree)

Worktree: `/private/var/folders/.../T/opencode/machina-r4-021-red` (detached HEAD; verified
`machina.rag.embedder.__file__` resolved inside the worktree).

`R4-021_red_probe.txt` (real `OpenAIEmbedder.embed` + real `embed_query`, transport mocked):

```text
embed() returned list(len=1536, all-zero=True)
cache_size after embed(): 1
embed_query() -> list(len=1536, all-zero=True) (a 'successful' zero-vector embedding)
embed() second call returned all-zero=True
cache_size final: 1
```

`R4-021_red_batch_probe.txt` (2 inputs, 1 vector returned):

```text
embed_batch raised ValueError: zip() argument 2 is shorter than argument 1
cache_size after failed batch: 1 (row 'alpha' cached before the raise)
cached keys: ['6e331572548f24027e40e13491fff4c79122a3017649cef8d4003e7c86d72a79']
```

`R4-021_red_pytest.txt` — the adversarial E2E file (copy) against unfixed code:

```text
FAILED .../test_malformed_200_raises_typed_error_and_never_caches[no-data-key|empty-data-list|
       null-data|dict-data|string-data|list-body]
FAILED .../test_empty_data_list_keeps_zero_vector_out_of_dense_retrieval
FAILED .../test_valid_response_caches_and_recovers_after_malformed
FAILED .../test_batch_short_result_raises_without_partial_cache_writes
PASSED .../test_genuine_empty_input_keeps_zero_vector_contract_and_no_network
9 failed, 1 passed
```

Representative failure: `Failed: DID NOT RAISE ProviderError` (the malformed 200 returned
the fabricated zero vector instead). The retrieval test additionally fails because
`embed_query` returns `[0.0] * 1536` (truthy) and the real `MemoryRetriever` starts dense
search with it; the `list-body` case raised a bare `AttributeError: 'list' object has no
attribute 'get'` — also not the typed surface. The single RED pass is the genuine-empty-input
pin (expected: it must pass before and after).

## GREEN — fixed tree

`R4-021_green_probe.txt`:

```text
Embedding API response unusable: ProviderError
embed() raised ProviderError: Embedding API response has 0 usable vector(s) for 1 input(s)
cache_size after embed(): 0
embed_query() -> None (honest keyword-only fallback)
embed() second call raised ProviderError: Embedding API response has 0 usable vector(s) for 1 input(s)
cache_size final: 0
```

`R4-021_green_batch_probe.txt`:

```text
embed_batch raised ProviderError: Embedding API response has 1 usable vector(s) for 2 input(s)
cache_size after failed batch: 0
```

`R4-021_green_pytest.txt`: `10 passed` — malformed 200s (a) raise the typed error and never
touch the cache, and the retry actually reaches the network again; (b) `embed_query` returns
`None` and the real `MemoryRetriever.retrieve` runs the keyword branch only
(`store.dense_queries == []`); (c) a valid response caches with a real direction, is served
from cache on the next call, and recovers after the failure; (d) the short batch raises
with no partial cache write and recovers; empty inputs keep the zero-vector contract with
zero network calls.

## Checks run (all `.venv/bin/python`, no DB needed)

| Command | Result |
|---|---|
| `pytest tests/test_embedding_failopen_adversarial.py -q -p no:cacheprovider -rA` | 10 passed |
| `pytest tests/test_embedder_local.py tests/test_embedding_storage.py tests/test_deep_rag_scope.py tests/test_rag_quality.py -q -p no:cacheprovider` | 91 passed |
| `pytest tests/test_architecture_layers.py -q -p no:cacheprovider` (ratchet) | 27 passed |
| `pytest tests/test_chaos_extended.py tests/test_rag.py -q -p no:cacheprovider` | 115 passed |
| `pytest tests/test_memory.py -q -p no:cacheprovider` | 60 passed |
| `pytest tests/test_deep_rag_backfill.py tests/test_bug_fixes_rag_tools.py tests/test_r4_026_rag_workspace_resolver.py -q -p no:cacheprovider` | 20 passed |
| `ruff check` + `ruff format --check` on touched files | clean |

The architecture ratchet is unaffected: no import statements were added (only the existing
deferred `_core_symbol` seam was reused), and `rag/embedder.py` is not in `GOD_FILE_CEILINGS`.
One combined run showed a transient `test_deferred_import_count_ratchet` failure while
parallel sessions were writing files (the census walks the tree on disk); the isolated run
and the repeated combined run both pass (27/27, 303/303).

## Residual risks

1. **Well-formed all-zero API rows** (pathological compatible server): rejected only at the
   `embed_query` memory boundary, not inside `embed()`/`embed_batch()` themselves. Direct
   embedder consumers (RAG indexing) would still store a zero row; deliberately not
   hard-rejected at the source because that is beyond the confirmed finding and would make
   the source enforce a stronger contract than the OpenAI API guarantees.
2. **Exact-count contract**: a provider returning more vectors than inputs now raises where
   the single path previously ignored the extras (and the batch path raised only on fewer).
   This is the OpenAI-compatible API contract; no in-repo producer violates it.
3. The boundary guard converts the residual zero case into `None` + a diagnostics failure
   counter (throttled log) — dense retrieval is skipped, not repaired; this matches the
   module's documented "degradation, not crash" contract.
4. `ProviderError` messages are not echoed into the audit payload (the `error` key is in the
   audit writer's protected set, pre-existing behavior); the message remains available to
   callers, tests, and diagnostics.
