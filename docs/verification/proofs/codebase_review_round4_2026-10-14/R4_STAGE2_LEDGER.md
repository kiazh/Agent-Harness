# Round-4 — Stage 2 completion ledger (2026-10-14)

Status: **COMPLETE** (gate green, sequential). Vendor-compliance wave follows (wave C).

## Gates
| Gate | Result | Log |
|---|---|---|
| Stage-2 backend | **4452 passed, 1 skipped, 37 xfailed, 0 failed** (224 s) | `stage2_gate_backend.log` |
| Stage-2 UI (alone) | **430 passed, 6 skipped, 0 failed**, tsc+eslint clean | `stage2_gate_ui.log` |

Cumulative tree: 43 files modified, **+2397/−537**, plus 26 new files (incl.
`security/store_fence.py` and 13 new harness/proof test files).

## Task status (Stage-2)
| Task | Status | Proof |
|---|---|---|
| R4-040/041 chatgpt fence windows | DONE | `R4-040_041_049_chatgpt.md` (RED 4F/1P → 5P ×3) |
| R4-042 register claude/gemini/copilot tokens | **SCOPE CHANGED** → copilot-only (claude/gemini flows being removed in wave C) | wave C2 |
| R4-043/044/046/048 env-file hardening | DONE | `R4-043_048_env_file.md` (RED 14F → 50P) |
| R4-045/047/049 providers | DONE | `R4-045_047_049_providers.md` (RED 7F → 21P; lock 0.412s/5.006s) |
| R4-060/061/062 policy matrix | DONE | `R4-060_062_policy.md` (RED 38F → 237P; 54 new cases) |
| R4-063/064 observability + OTel span | DONE | `R4-063_064_observability.md` (8F/1P → 9P; SSE-span deliberately stopped, documented) |
| R4-065/066/067/068 approvals | DONE | `R4-065_068_approvals.md` (RED 11F/6P + 3F/1P → 17P + 4P; sweep 521P) |
| R4-049b chmod-fd helper | pending (wave C2; gemini+claude sites disappear with removal) | — |
| R4-025 max_iterations | **HELD** (owner decision) | — |

## Behavior changes explicitly called out (Stage 2)
1. Bounded store locks: `store_fence.store_lock` 45 s (typed `StoreLockTimeout`); providers store
   lock 5 s (`ProviderStoreLockTimeout`). claude/gemini/copilot callers surface the shared timeout
   error until their lane translates (becoming moot for claude/gemini in wave C).
2. Policy calibration: exactly six benign flag spellings are now card-free; everything hostile
   stays blocked; git-grep traversal/revspec/backup families now require review; unreadable tree
   scans fail closed.
3. `.env` writer: mkstemp + O_EXCL semantics, fsync file+dir, unpredictable names; escape
   round-trip fixed; key matching case-insensitive; no site-packages fallback.
4. Approvals emit a new `approval.resolved{status:"expired"}` frame (additive); durable-approved
   is honored at the deadline; audit rows carry bounded/redacted targets.
5. `safe_logging` masks the whole record if the redactor fails; traces expose dropped counters.

## Deferred / next
- **Erratum:** R4-016 (redaction under-match/tail leak, a P1) was omitted from the Stage-2
  scheduling; launched as its own agent on 2026-10-14 in parallel with wave C (disjoint files);
  the H-CORPUS corpus (37 strict-xfails in `tests/test_redaction_adversarial.py`) is its
  acceptance gate.
- **Wave C**: Gemini+Claude subscription-flow removal (vendor ToS, owner-directed) + Kimi provider
  add; then C2: R4-042 (copilot redaction registration) + R4-049b (chmod-fd helper).
- **History strip** of the Gemini client id/secret from the 5 unpushed commits → then push.
- R4-025 owner decision still open.
