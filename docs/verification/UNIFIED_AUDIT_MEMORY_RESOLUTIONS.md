# Unified audit memory and retrieval resolutions

Source: the Machina unified static bug audit (`Machina_Unified_Static_Bug_Audit.md`), findings F-04, F-08, F-12, F-16, F-17, F-18, F-19. Changes were reviewed statically; no application modules were imported, tests added/run, runtime probes performed, database/migration calls made, or network accessed.

## F-04 — FTS outage visibility

`machina/rag/search.py`: `_bm25_search` propagates schema failures. Hybrid search already collects branch exceptions: a surviving dense branch is marked degraded; two failures raise `SearchUnavailableError`. Keyword-only search propagates failure instead of returning an ordinary empty match set. An empty normalized query still legitimately returns no matches.

## F-08 — No persistence replay

`machina/rag/pipeline.py`: transaction support is selected before `_replace_and_insert` starts. Transaction-body/exit exceptions propagate without invoking the replacement again. The acquisition-only compatibility path remains before writes.

`machina/memory/store.py`: signed memory plus provenance uses a single acquired transaction (or the caller-owned connection). The old AttributeError/TypeError handlers that retried writes and suppressed provenance errors are removed. Missing provenance execution capability fails before the memory INSERT. Signed writes now require a transaction-capable database rather than silently accepting incomplete persistence. Unsigned writes retain direct INSERT semantics.

## F-12 — Preserve explicit embedding identity through review

`machina/memory/store.py`, `machina/memory/approval.py`: `add(embedding_space=...)` forwards the value to `submit`, which persists that explicit identity instead of deriving a potentially different configured identity. Approval already forwards the pending row's identity to the final memory write. Existing legacy migration fallback behavior is retained; no schema changes were necessary.

## F-16 — Fence both cache publication sites

`machina/rag/pipeline.py`: invalidation increments a pipeline generation before clearing the session's caches. Search captures its generation before awaits; both keyword and dense/hybrid completion pass it to `_finish_search`, which publishes only if unchanged. Embedding-space verification independently captures a generation before its paginated read and checks it before publishing its verdict. All cache assignment sites are fenced. A global generation conservatively prevents unrelated in-flight sessions from publishing when another session invalidates; existing unaffected cache entries remain usable. An already running search may return its result to its original caller, but cannot repopulate invalidated cache state. Cross-process invalidation remains outside this in-process cache design.

## F-17 — Date eligibility before candidate budgets

`machina/memory/retriever.py`, `machina/memory/store.py`: `retrieve` passes `date_range` into both dense and sparse branches. Each branch binds inclusive `created_at BETWEEN start AND end` predicates in SQL before ordering and LIMIT, including the sparse over-fetch cap. The existing Python filter remains defense in depth. No date restriction is added when absent; agent/category/quarantine/embedding-space filters retain their original scope.

## F-18 — Legacy invocation pairing

`machina/core/compression.py`: for newest-first chunks, each legacy result selects the first unmatched older call (a greater index), rather than the oldest call in the entire list. Legacy fallback only uses chunks without explicit call IDs, and requires matching session and agent. The reported `[result_B, call_B, result_A, call_A]` witness now statically maps `0↔1` and `2↔3`. Combined call/result records remain self-contained. Truly concurrent ID-less invocations cannot be uniquely identified from names alone; explicit call IDs remain the reliable representation.

## F-19 — Compensate no-row publication

`machina/skills/learning.py`: an auto-publication UPDATE returning no row now raises inside the transaction, entering the existing source-guarded artifact compensation path and suppressing the success audit. Cancellation and transaction/commit failures retain their existing compensation behavior. Artifact deletion remains best effort as implemented by `_discard_artifact`; filesystem failure can require operational cleanup.

## Validation and limits

Read the modified callpaths and audit causal witnesses; inspected diffs and parsed all seven modified Python files using `ast.parse` without importing them. Runtime SQL behavior, concurrency scheduling, integration compatibility, and external persistence failures have not been exercised. No claim of passing runtime tests is made.
