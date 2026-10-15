# Fix evidence — F08 shared-RAG embedding identity + chunker leading-separator residual

- **Date:** 2026-10-15
- **Lane:** FIX (final review), sole writer of `machina/tools/rag.py`,
  `machina/rag/embedder.py`, `machina/rag/chunker.py`, affected tests, and
  one new evidence test file.
- **Baseline HEAD:** `4714e5e4aaba2650447d43fe8ccd0c8cc112a776`
  (`FR-F12: delegation cleanup scope covers _record_start; factory closes orphans`).
- **Findings addressed:**
  - **F08 (P2)** — `docs/verification/CODEBASE_REVIEW_FOLLOWUP_2026-10-15.md:79`
    (shared RAG pipelines retain old embedding credentials / failed backend selection).
  - **P3-1** — `docs/verification/proofs/final_review_2026-10-15/line_rag_tools_skills.md:53`
    (recursion drops a leading separator → single-character coverage hole).
- **Method:** RED-first. One new evidence pin per finding (plus two supporting
  pins), reverted-source RED run, then the minimal fix, then GREEN.

## Files changed

| File | Change |
| --- | --- |
| `machina/tools/rag.py` | F08: embedding identity now delegates to `rag.embedder._embedder_identity(resolve_embedding_settings())`; replaces the config-only 3-tuple; adds `_close_pipeline_quietly` and closes the superseded pipeline's owned clients. |
| `machina/rag/chunker.py` | P3-1: `_split_recursive` mirrors the forward bridge — the first sub-chunk is extended backward over the dropped leading separator so emissions stay source-contiguous. |
| `machina/rag/embedder.py` | Not modified. `_embedder_identity` is reused as-is (no duplicated digest). |
| `tests/test_final_review_fix_rag_identity_chunker.py` | NEW evidence file (6 pins). |

No existing test needed a modification (all remained green); no other file was
touched.

## F08 — shared RAG pipeline identity

### Root cause

`_current_embedding_identity()` read only `(embedding_backend, embedding_model,
embedding_base_url)` from config. `RAGPipeline.__init__` builds an
`OpenAIEmbedder` whose `httpx.AsyncClient` captures the `Authorization: Bearer
<key>` header at construction. With identity unchanged on a key rotation, the
cached pipeline kept serving the old key; a pipeline built keyword-only stayed
keyword-only after a key appeared (the resolved availability was never part of
the identity). The separate `get_shared_embedder` path already solved both with
`_embedder_identity`, but the RAG pipeline did not use it.

### RED → fix → GREEN

RED (source reverted, `logs/red_evidence.txt`):

```
tests/test_final_review_fix_rag_identity_chunker.py::test_shared_pipeline_identity_uses_shared_embedder_helper FAILED
tests/test_final_review_fix_rag_identity_chunker.py::test_shared_pipeline_rebuilds_on_embedding_key_rotation FAILED
tests/test_final_review_fix_rag_identity_chunker.py::test_shared_pipeline_rebuilds_when_embedding_key_appears FAILED
tests/test_final_review_fix_rag_identity_chunker.py::test_replaced_pipeline_owned_embedder_is_closed FAILED
6 failed in 0.45s
```

Fix:

- `_current_embedding_identity()` → `_embedder_identity(resolve_embedding_settings())`,
  a `(kind, model, base_url, key_digest)` 4-tuple. It **reuses** the shared
  helper (`rag/embedder.py:874`) so both the pipeline and the process-wide
  embedder use one 16-hex credential digest — no duplicated digest logic.
  The resolved `kind` carries effective availability, so `none → openai/onnx/local`
  (keyword-only → dense) changes the identity even when the configured text does
  not.
- `get_rag_pipeline()` now closes the superseded pipeline's owned `_embedder`
  and `_reranker` via `_close_pipeline_quietly` (each close isolated; failures
  only logged — never surfaced), matching the shared-embedder close seam.

GREEN: the four F08 pins pass; `logs/green_focused.txt` shows `208 passed`.

Pins:

- `test_shared_pipeline_identity_uses_shared_embedder_helper` — the identity
  equals the embedder's own helper output (reuse, not duplicate).
- `test_shared_pipeline_rebuilds_on_embedding_key_rotation` — same
  backend/model/base_url, rotated key → rebuild (one extra build).
- `test_shared_pipeline_rebuilds_when_embedding_key_appears` — keyword-only
  (`_embedder is None`) → dense (`_embedder is not None`) after a key appears.
- `test_replaced_pipeline_owned_embedder_is_closed` — the replaced pipeline's
  embedder `close()` is awaited exactly once.

### API / behavior changes

- Private `machina.tools.rag._current_embedding_identity()` return shape:
  `(backend, model, base_url)` → `(kind, model, base_url, digest)`.
- `_rag_pipeline_identity` embedding slot is now a 4-tuple; the identity is
  rebuilt on credential rotation and keyword-only↔dense availability changes.
- `get_rag_pipeline()` may now `await` a close of the superseded pipeline while
  holding the module lock. A concurrent request still holding the old pipeline
  degrades safely: `RAGPipeline.search` catches the closed-client exception and
  falls back to keyword-only (`retrieval_degraded=True`) rather than crashing.

## P3-1 — chunker leading-separator coverage

### Root cause

`"aaa\n\n\n" + "W"*3000` splits on `"\n\n"` into `["aaa", "\nW…"]`. The
recursed part begins with the leftover `"\n"`, which the inner level treats as a
separator and drops (empty leading span filtered at `chunker.py:345`). The
forward bridge only stretches the *last* sub-chunk, so index 5 (the third `\n`)
was emitted by no chunk. No fabrication: the `_apply_overlap` offsets guard
(`prev.end != cur.start`) withheld overlap across the hole.

### RED → fix → GREEN

RED: `test_recursive_split_retains_dropped_leading_separator` →
`AssertionError: source characters covered by no chunk: [5]`; the adjacency pin
→ `forward coverage gap: 5 -> 6` (`logs/red_evidence.txt`).

Fix (minimal): before the existing forward bridge, extend `sub_chunks[0]`
backward over `text[part_start : first.start - base_offset]` (a genuine source
slice) and reset its `start` to the part boundary — the exact mirror of the
forward bridge. It is guarded by `if sub_chunks` and a `None`-offset check, and
updates `token_count`.

GREEN: both chunker pins pass; the hard `chunk_size*4` bound still holds (the
extra separator run can push a chunk past the cap, which the existing hard-bound
pass then re-splits at exact offsets).

Pins:

- `test_recursive_split_retains_dropped_leading_separator` — no interior source
  character is uncovered, and every chunk/span remains an exact source slice.
- `test_recursive_split_leading_separator_keeps_adjacency` — consecutive
  tracked chunks never leave a forward gap (`cur.start <= prev.end`), so
  overlap may only repeat real source characters.

### API / behavior changes

- Some emitted chunks can now be longer by the retained separator run; when that
  exceeds `chunk_size*4`, the pre-existing hard-bound pass splits it, so an
  occasional extra chunk is produced at the cap. Offsets stay exact and
  contiguous; no new fabrication is possible (all emissions remain literal
  slices of the source).

## Verification

Focused suites (all green, `logs/green_focused.txt`):

```
tests/test_rag.py tests/test_rag_quality.py tests/test_chunker_overlap.py
tests/test_r5f2_chunk_adjacency.py tests/test_fm5_chunker.py
tests/test_embedder_local.py tests/test_embedding_failopen_adversarial.py
tests/test_r4_026_rag_workspace_resolver.py tests/test_reranker_local.py
tests/test_bug_fixes_rag_tools.py tests/test_final_review_fix_rag_identity_chunker.py
→ 208 passed
```

Lint/format (`logs/ruff.txt`): `ruff check` all passed; `ruff format --check`
all 4 files already formatted.

Broader regression batch (34 RAG/chunker/embedder-touching test files) was also
run. The only failures observed were pre-existing DB-concurrency flakes from
other concurrent lanes on the shared test database (the same batch on the
reverted tree deadlocked, and each named failure passed in isolation with the
fix in place). Evidence that the fix does not cause them:
`test_turns_ownership / test_research_integration / test_fm5_consistency`
pass together (35 passed) and the 8 individually-named failures pass together
with the fix applied (8 passed).

## Residuals / limits

- The chunker change is coverage-only; the R5-F2 no-fabrication invariant is
  unchanged and re-verified by the hypothesis property pin in
  `test_r5f2_chunk_adjacency.py` (still green).
- Purely trailing/leading whitespace-only separator runs at document boundaries
  may still be dropped (intended; `test_fm5_chunker.py` documents it). P3-1 only
  removes the *interior* hole between two emitted chunks.
