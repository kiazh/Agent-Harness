# External audit — verification & fix proofs (2026-10-13)

Source review: `docs/verification/CODEBASE_REVIEW_2026-10-13.md` (untracked, left
as authored). Every security/correctness claim in that review was re-verified
against the worktree with a real reproduction before any fix; 18 claims
CONFIRMED (all fixed), 1 refuted at HEAD, 1 unprovable (an agent example, no
runnable artifact, no runtime impact).

`battery_after_fix.log` is the acceptance run of this whole directory against
the fixed tree. Run it yourself with:

```sh
bash docs/verification/proofs/audit_2026-10-13/run_battery.sh \
  | tee docs/verification/proofs/audit_2026-10-13/battery_after_fix.log
```

Environment conventions (same as the original verification): python repros use
the repo venv with a scratch `HOME` + empty `MACHINA_ENV_FILE`; DB repros use
only `machina_test` (`postgresql://postgres:postgres@localhost:5432/machina_test`)
and clean up their rows; Node repros import the real UI modules; ephemeral
fixtures use fake secrets (e.g. `review-fixture-*`).

Interpretation: `CLAIM NOT REPRODUCED` / exit 1 = the defect no longer exists.
A few harnesses cannot flip (they hardcode the pre-fix flow or a pre-fix
assertion); for those the green proof is listed explicitly.

## Claim → script → fix commit → after-fix evidence

| Claim (audit) | Script | Fix commit | After-fix evidence |
|---|---|---|---|
| Recursive terminal reads bypass credential approval | `audit_recursive_read.py` | `54a8151` | both reads now `pending (opaque_execution_review / credential_access)`; the repro's leak step is unreachable (guard assertion fires) |
| `find` output-writing flags accepted as routine | `audit_find_write_flags.py` | `0c9eeee` | NOT REPRODUCED; `-fprint` pends, `-delete` control still pends |
| Missing/ambiguous approval ids approve first request | `audit_approval_ids.mjs` | `660a063` | empty id → usage error; `aa` → lists 2 candidates, refuses; unique id → dispatches with sessionId (script prints cases, no CLAIM line) |
| Stored proposals/display persist raw credentials | `audit_approval_credentials.py` | `0c9eeee` | NOT REPRODUCED (proposal, display, REST, gateway listing all redacted) |
| Session grant stores operation, not capability set | `audit_grant_capability.py` **stale harness** | `0c9eeee` | harness hardcodes the pre-fix route payload — CANNOT FLIP. Green proof: `audit_grant_capability_green.py` drives the real route+mirror → `SAFE BEHAVIOR CONFIRMED`, scoped grant covers `credential_access` |
| Stale memory mirror re-claims durable approval | `audit_stale_memory_claim.py` | `0c9eeee` | NOT REPRODUCED (durable-origin refused when DB unavailable; memory-only control still claims) |
| Tool cache crosses workspaces | `audit_cache_workspace.py` | `af9d938` | NOT REPRODUCED (workspace B reads its own content; no cache keys) |
| `effects=('exec',)` ignored by cache | `audit_cache_effects.py` | `af9d938` | NOT REPRODUCED (exec-effect tool ran twice) |
| Cohere key rotation keeps old client | `audit_cohere_rotation.py` | `54a8151` | NOT REPRODUCED (pipeline rebuilt; reranker uses key-TWO) |
| Export deletes unrelated files | `audit_export_destructive.py` | `0c9eeee` | NOT REPRODUCED (`important.txt survived: True`; only manifest-owned files unlinked) |
| `/init` truncates existing AGENTS.md | `audit_init_overwrite_real_app.mjs` **cannot flip post-fix** | `660a063` | real app path now raises the refusal; `init_overwrite_green.mjs` → `CLAIM NOT REPRODUCED (old content survived)` |
| PEM streaming redaction leak | `audit_pem_stream_leak.py` | `a8de932` | NOT REPRODUCED (all three modes leak-free; oversized body → single marker) |
| `outcome='failed'` recorded as success | `audit_scheduler_failed_outcome.py` | `54a8151` | NOT REPRODUCED (error `RuntimeError: agent outcome: failed` recorded) |
| SSE partial frame + clean EOF yields `done` | `audit_provider_sse.py` | `a8de932` | repro now raises `ProviderError: ... ended without a terminal frame` before any `done` |
| CLI drops model/provider overrides | `audit_cli_model_override.py` | `a8de932` | NOT REPRODUCED (factory receives both overrides) |
| Concurrent document replacement leaves extra chunks | `audit_rag_race.py` | `b5e46ef` | NOT REPRODUCED (advisory xact lock; exactly 1 chunk per source) |
| `{}` accepted as empty extraction | `audit_consolidator_shape.py` | `54a8151` | NOT REPRODUCED (`{}` → failed, checkpoint None; `[]` stays empty) |
| Chunker drops preamble / language mismatch | `audit_chunker_preamble.py` | `b5e46ef` | NOT REPRODUCED (preamble kept; `py`/`python` identical) |
| `tools=[]` undercounts full schema (estimator) | `audit_services_estimator.py` **stale predicate** | `54a8151` | harness's exit predicate is true pre/post — the observable is the printed total: `7732` (was `4618`), undercount `3115 → 0`; exact-delta pin |
| `search_files` ReDoS | `audit_redos.py` | `af9d938` | NOT REPRODUCED (isolated matcher killed at 3s deadline; bounded ToolError) |
| Eviction protects documents, not conversations | `audit_context_eviction.py` | `54a8151` | NOT REPRODUCED ((0,10) evicted, 10 conversations protected) |
| `/agents save` swallows description into name | `audit_agents_save.mjs` | `660a063` | NOT REPRODUCED (name `helper`, description intact) |
| Relative MACHINA_ENV_FILE not exported | `audit_installer_env.sh` **cannot flip** | `660a063` | harness hardcodes the pre-fix export step; green proof `installer_env_green.sh` replays the real fixed flow → installer and child CLI agree |
| IPv6 OAuth authority mangled | `audit_oauth_ipv6.py` | `b5e46ef` | NOT REPRODUCED (bracketed round-trip incl. discovery URLs) |
| Duplicate rerank indices duplicate a memory | `audit_retriever_dupes.py` | `54a8151` | NOT REPRODUCED (deduped output) |
| Traces double-yield masks body errors | `audit_traces_double_yield.py` | `a8de932` | NOT REPRODUCED (original `ValueError` propagates) |
| C1 OSC/ST accepted by sanitizer | `audit_text_c1.mjs` | `660a063` | NOT REPRODUCED (C1 stripped/rejected; SGR preserved) |
| Release-gate lint fails | (no script) | `36ea193` | REFUTED at HEAD: `ruff check` + `format --check` green |
| Gateway cancellation emits completion after failed release | (audit itself unreproduced) | — | REFUTED at HEAD (`turn.cleanup_pending`, not completion) |
| Bundled ECC eval example fails | (no artifact) | — | UNPROVABLE — agent docs only, no runnable eval; no runtime impact |

## Notes

- `audit_redos_child.py` is the isolated-matcher child spawned by
  `audit_redos.py`; running it standalone exits 1 by design.
- `probe_ui_import.mjs` / `probe_app_import.mjs` are import probes used while
  building the UI repros.
- Regression pins for every fix live in the repo test suite (see each fix
  commit); the audit battery is the independent confirmation, not the only
  guard.
- Full suite on the consolidated tree (all fixes + W7 + Batch 1):
  `3531 passed, 0 failed, 1 skipped, 1 xfailed` (2026-10-13).
