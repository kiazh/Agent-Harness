# Round-4 — Stage 1 completion ledger (2026-10-14)

Status: **COMPLETE** except one decision-held task. Both final gates green, run
sequentially on a quiesced tree.

## Gates
| Gate | Result | Log |
|---|---|---|
| Baseline (pre-fix) | 4179 passed, 1 skipped, 1 xfailed, 0 failed | `baseline_backend.log` |
| Final backend | **4326 passed, 1 skipped, 38 xfailed, 0 failed** (202 s) | `stage1_final_backend.log` |
| Final UI (alone; sequential rule) | **426 passed, 6 skipped, 0 failed**, tsc+eslint clean | `stage1_final_ui_rerun.log` |
| Earlier interim backend (after wave 2) | 4314 passed, 38 xfailed | `stage1_closeout_backend.log` |

## Task status
| Task | Status | Proof |
|---|---|---|
| R4-001 ratchet (reasoning.py 61/5501) | DONE | guard green; comment removed, invariant moved to a test |
| R4-002 baseline triage | DONE | `baseline_triage.md` (incl. 2 flake instances classified) |
| R4-003 gate hardening | DONE (config) | CI already runs the full `pytest tests/`; SSE + architecture assertions are in it |
| R4-010 SSE error frame | DONE | `R4-010_sse.md` (+ tolerant teardown; latent resume-reset bug fixed) |
| R4-011 compaction result text | DONE | `R4-011_compaction.md` (full E2E, no narrowing) |
| R4-012 Anthropic tool coalescing | DONE | `R4-012_anthropic.md` (net-zero diff) |
| R4-013/14/15 auth fencing | DONE | `R4-013_auth_fencing.md`; new `security/store_fence.py`; 3 xfails → hard passes |
| R4-017 Ctrl+C | DONE | `R4-017_018_cli.md` (130 in 0.07 s, claim released) |
| R4-018 headless full | DONE | same doc (gateway-parity grant; no pauses) |
| R4-019 terminal timeout | DONE | `R4-019_timeouts.md` (cap=300 per schema; typed message) |
| R4-020 grader integrity | DONE | `R4-020_grader.md` (4 attacks → false; genuine fix → true) |
| R4-021 zero-vector | DONE | `R4-021_zero_vector.md` (typed error, no cache writes) |
| R4-022 denied→denied | DONE | `R4-022_023_ui_honesty.md` (⊘ denied; paused also un-false-successed) |
| R4-023 Menu sanitization | DONE | same doc (control-byte corpus through real TuiAltScreen) |
| R4-024 provider pin | DONE | `R4-024_provider_pin.md` |
| R4-026 index resolver | DONE | `R4-026_resolver.md` (canonical execution_context root) |
| R4-027 shim matching | DONE | `R4-027_shim.md` (segment-aware) |
| R4-025 max_iterations | **HELD** | owner decision: enforce vs remove (both change public/DB surface) |

Harnesses built as standing gates: `test_sse_stream_adversarial.py`,
`test_auth_store_adversarial.py`, `test_redaction_adversarial.py` (37 strict-xfails awaiting
Stage 2's R4-016), plus per-task adversarial suites.

## Net change (all stages so far)
- 30 files modified **+1261/−296**; 11 new files (1 production module `store_fence.py`,
  10 test/proof files); docs/proofs under `docs/verification/proofs/codebase_review_round4_2026-10-14/`.
- Code removed: wrapper scope lifecycle, 7-line comment-to-test relocation, embedder
  zero-vector fallback + dead `import os`, duplicated clipboard fixture, duplicate
  subprocess fixture; no dead code added.

## Abstractions
- Added: `security/store_fence.py` (5 narrow functions; one authoritative lock/generation/
  fingerprint/logout implementation used by 3 stores); 3 adversarial harnesses.
- Relocated: turn-scope ownership wrapper→drivers (documented `TurnEngine.stream` contract);
  knowledge comment→executable test. No speculative interfaces; no new dependencies.

## Behavior changes explicitly called out
1. `end_turn_scope` tolerates foreign-context reset (teardown-only).
2. Resolver fail-closed: session context + DB + no broker snapshot now raises instead of
   silently using machina_home/cwd (direct callers pass `base_dir`).
3. Terminal timeout honors the schema (cap 300 s); typed timeout message.
4. UI: `paused` no longer renders as success (now `○ awaiting approval`).
5. `fenced_logout` bumps generation lock-free (documented deviation; rationale falsifiable
   in `store_fence.py` docstring + auth proof).
6. Grading runs hermetic (`--noconftest`, pinned ini) — fixture conftest reliance removed.
7. Embedder rejects mismatched/absent `data` strictly (OpenAI-compatible contract).

## Deferred / held
- **R4-025** owner decision.
- R4-042 (register claude/gemini/copilot tokens) → Stage-2 wave B.
- OTel stream-span detach (logged+swallowed; no frame) → tracked under R4-064.
- `store_fence` nits: racing logouts can lose one durable bump; `fenced_logout` uses the
  caller's path spelling (verify in R4-049 pass).
- Cross-process contention tests → R4-105; lock timeouts → R4-049 (this wave).
