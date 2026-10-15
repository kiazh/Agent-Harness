# R5-S3 — Backfill starvation (follow-up 3): verdict DEAD CODE, method deleted

Lane: R5-S3 · Date: 2026-10-15 · Review ref: `CODEBASE_REVIEW_CURRENT_2026-10-14.md`
finding 3 ("Latent embedding-backfill starvation", `machina/memory/store.py:360`).

## Verdict: REFUTED as a live defect — no production caller exists; dead method deleted

Per the fix plan ("No production caller exists — confirm first; if truly dead,
DELETE the method + note it"), the caller census below shows the method is
unreachable in production. The starvation RED therefore cannot reproduce
against any live path, so per plan rules no cursor/failure-marker fix was
applied — the dead code was removed instead (smallest diff).

## Caller census (evidence)

1. `backfill_embeddings` (exact symbol), whole repo, Python files:
   - BEFORE: 2 hits — the definition in `machina/memory/store.py:346` and one
     test pin `tests/test_memory.py:880`
     (`test_backfill_embeddings_records_embed_failure`).
   - AFTER: 0 hits (`grep -rn "backfill_embeddings" --include="*.py" .` exits 1).
2. `backfill` (broad string), `machina/` tree: only the deleted method's
   docstring/body, a docstring in `machina/memory/embeddings.py:61`
   ("indexing/backfill paths" — generic wording, no call), and log/message
   strings in `machina/rag/pipeline.py` (no reference to the method).
3. Dynamic dispatch check: no `getattr`-based or registry-driven invocation of
   the method anywhere under `machina/memory/`; the method was not exported
   beyond the `MemoryStore` class (`__all__ = ["MemoryStore", "memory_store"]`
   unchanged). No scheduler, CLI, API-route, or consolidator wiring references it.
4. Collateral check: `record_failure`, `embedding_to_str`, and `audit_log`
   imports in `store.py` each retain other live users — no import cleanup needed.

## Files changed

- `machina/memory/store.py` (−41): deleted the dead `backfill_embeddings`
  method (40-line body + separator blank). No other method touched;
  `redaction.py` and all other memory files untouched.
- `tests/test_memory.py` (−20): removed its sole pin
  (`test_backfill_embeddings_records_embed_failure`), which called the deleted
  method. No other test touched.

## Verification

- `ast.parse` syntax check on both edited files: OK.
- Post-delete grep for `backfill_embeddings` in `*.py`: zero hits.
- `.venv/bin/python -m pytest tests/test_memory.py -q -p no:cacheprovider`:
  **59 passed**.

## Follow-up note

If a NULL-embedding backfill worker is ever reintroduced (e.g. wired to a
scheduler or migration), it must ship with a progress cursor / failure marker
from day one so permanently-skipped rows cannot monopolize the batch — the
original latent concern stands for any future implementation.
