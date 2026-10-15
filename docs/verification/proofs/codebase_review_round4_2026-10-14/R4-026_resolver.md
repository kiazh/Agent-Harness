# R4-026 — `index_document` canonical workspace resolver (P1)

- **Task:** `FIX_PLAN_ROUND4_TASKS.md` → R4-026 · **Finding:** Round-4 #15
  (`CODEBASE_REVIEW_ROUND4_2026-10-14.md`).
- **Owned files:** `machina/tools/rag.py`, `machina/rag/loaders.py`
  (+ new test `tests/test_r4_026_rag_workspace_resolver.py`).
  `machina/rag/embedder.py` was NOT touched (belongs to R4-021; its
  worktree modification is another agent's).
- **Date:** 2026-10-14 · **No commit.**

## 1. Root cause (one sentence)

`index_document` handed the raw caller path to the pipeline whose default
`FileLoader` resolved relative paths against `machina_home or cwd`
(`rag/loaders.py:109-116,238-244`), while the permission broker normalized and
approved the target against the canonical workspace root
(`permissions/policy.py:662-680` → `core/execution_context.py:75-76`); with
`workspace_root` explicitly configured to differ, approved absolute workspace
paths were rejected and relative paths silently indexed a same-named file
under the old base.

## 2. Fix — one canonical resolution

- `machina/rag/loaders.py`
  - `FileLoader(base_dir=None)` no longer freezes `machina_home`/cwd at
    construction; `_resolve_path` resolves the default base per load through
    `_default_base_dir()` → `execution_context()` (the single resolver the
    broker also uses; broker snapshot during the guard+execute window,
    `configured_workspace()` otherwise). Explicit `base_dir=` callers are
    unchanged. Containment check and private-part filters are byte-for-byte
    intact (only the base variable changed).
- `machina/tools/rag.py`
  - `_execution_workspace()` reads `execution_context("index_document")`;
  - `index_document` resolves the caller path against that workspace (relative
    paths anchored; absolute paths normalized) and passes the resolved
    absolute path to `pipeline.index_document`, so the approved and accessed
    identities match. `execution_context` is bound through the existing
    `machina.tools.base._core_symbol` leaf-core seam (tools core-import
    ratchet stays 21, deferred census stays 692).

Not touched: document-HTTP route (pre-built `Document` bypasses the loader),
`Document`-based callers (`web_crawl`, `web_research`), embedder.

## 3. Proof (E2E, real DB — H-DB + H-FS, RED-first on unfixed code)

`tests/test_r4_026_rag_workspace_resolver.py` drives the real tool → real
shared pipeline → real `FileLoader` → real `search_documents` against
`MACHINA_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/machina_test`,
with `workspace_root` = tmp project dir, `machina_home` = planted lookalike
tree, both ≠ cwd. Session ownership check runs against the real DB
(`machina.tools.agents.db` → test DB).

| # | Test | Assertion |
|---|---|---|
| a | `test_absolute_workspace_path_indexes_that_file` | absolute workspace path indexes THAT file; search returns its content; stored `document_source` == real file |
| b | `test_relative_path_resolves_to_workspace_not_machina_home` | `docs/x.md` → workspace file, never the `machina_home` lookalike |
| c1 | `test_planted_old_base_lookalike_is_never_indexed` | adversarial: no chunk whose source starts with the planted base; planted path absent from search output |
| c2 | `test_absolute_path_outside_workspace_is_rejected` | real file outside workspace → `ToolError` "escapes the allowed base directory"; zero rows |
| c3 | `test_symlink_escape_is_rejected` | workspace symlink to an outside file → escape rejection; zero rows |
| c4 | `test_dotdot_traversal_is_rejected` | `docs/../../machina_home/docs/x.md` → escape rejection; zero rows |

### RED — scratch worktree at `cdc5232` (unfixed), final test text

Command (worktree at HEAD in `…/opencode/r4-026-red`, `PYTHONPATH=<worktree>`):

```
python -m pytest tests/test_r4_026_rag_workspace_resolver.py -q -p no:cacheprovider
→ 6 failed in 1.01s          [R4-026_red.log]
```

Captured failure modes (exactly the named ones):

```
E  ValidationError: Path '…/project/docs/x.md' escapes the allowed base
   directory '…/machina_home'                          # (a) approved path rejected
E  assert 'unique-workspace-marker' in "... machina_home/docs/x.md ...
   # Planted doc  unique-planted-marker sharedneedle"  # (b) wrong file indexed
E  assert '…/machina_home' not in "... machina_home/docs/x.md …"  # (c1) planted indexed
E  Failed: DID NOT RAISE ToolError                     # (c2) outside file indexed silently
E  Actual message: 'Error indexing document: File not found: escape-link.md'
                                                       # (c3) symlink lands on old base
E  Failed: DID NOT RAISE ToolError                     # (c4) `..` indexed planted file
```

### GREEN — fixed tree

```
python -m pytest tests/test_r4_026_rag_workspace_resolver.py -q -p no:cacheprovider
→ 6 passed in 0.90s          [R4-026_green.log]
```

Audit corroboration (GREEN): `rag_index_complete source=…/project/docs/x.md`
(the resolved workspace file), while RED logged `source=…/machina_home/docs/x.md`.

## 4. Checks (all on the fixed tree)

| Check | Result |
|---|---|
| new file + `tests/test_rag.py tests/test_deep_rag_scope.py tests/test_fm5_consistency.py tests/test_bug_fixes_rag_tools.py` | **108 passed** (102 + 6 new) — `R4-026_checks.log`, `R4-026_green.log` |
| wider RAG/loader/leaf/idempotence sweep (`test_leaf_independence`, `test_review_security`, `test_research_integration`, `test_rag_quality`, `test_fm5_redaction`, `test_audit_remediation`, `test_web_crawl*`, `test_session_web_research`, `test_execution_context_boundary`, `test_architecture_layers`) | **197 passed** |
| architecture ratchet (`tests/test_architecture_layers.py`) | **27 passed**; tools core-import census 21/21, rag 7/15, deferred census 692/692 (unchanged) |
| `ruff check` + `ruff format --check` on touched files | clean |
| diffstat | `loaders.py +37/−13`, `tools/rag.py +26/−2` → `R4-026_diffstat.txt` |

## 5. Residual risks

1. **Fail-closed inside session contexts:** with a session id set, DB
   connected, and no broker-stamped execution snapshot, the default loader
   base now raises (`execution_context` contract) instead of silently using
   `machina_home`/cwd. Agent tool calls always run inside the broker's
   guard+execute window (snapshot present); direct callers must pass an
   explicit `base_dir` or go through the broker.
2. **Behavior change for explicit `workspace_root`:** non-tool callers with
   relative paths (e.g. the evaluation CLI when run from a checkout with a
   *different* configured workspace) now resolve/are contained by the
   workspace root — the intended containment, but previously cwd could win.
3. **Lazy default resolved per load**, so runtime config changes apply
   immediately; cost is a ContextVar read + `Path.resolve()` (same pattern as
   `tools/file.py::_base_dir`). Broker-snapshot root still wins during the
   approved window, matching the policy doctrine.
4. **Audit spelling:** the tool-level success/error audit keeps the caller's
   path spelling; the pipeline-level audit records the resolved source (shown
   in the logs above). No contract change to the document-HTTP route.

## 6. Files

- `machina/rag/loaders.py` — canonical default base, per-load resolution,
  private-part filters and containment unchanged.
- `machina/tools/rag.py` — resolve via `execution_context`, pass the resolved
  absolute path; `_execution_workspace()` seam helper.
- `tests/test_r4_026_rag_workspace_resolver.py` — new E2E/adversarial proof.
- Evidence: `R4-026_red.log`, `R4-026_green.log`, `R4-026_checks.log`,
  `R4-026_diffstat.txt` (this directory).
