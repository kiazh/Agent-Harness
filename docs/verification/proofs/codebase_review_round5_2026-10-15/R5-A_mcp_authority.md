# R5-A evidence — MCP authority + child lifecycle + grant path (P1s)

Lane: R5-A (findings 1, 15, 2-policy-side). Sole-writer files:
`machina/mcp_server/__init__.py`, `machina/permissions/policy.py` (grant path
only). No commit (coordinator commits). untouched by this lane: disclosure
copy (`ui/src/features/mode.ts`, `README.md`, `SECURITY.md` — R5-D owns).

## A1 — tool-supplied mode escalation (finding 1): REPRODUCED then FIXED

RED (pre-fix, all executed, logs in this directory):
- `R5-A_red_a1_forward.py` / `.log` — `_chat_command("hello", None, "full")`
  yields `[..., "-m", "machina", "chat", "-q", "--mode", "full", "--", "hello"]`:
  model-supplied `mode="full"` forwarded verbatim to the child CLI, and no
  ceiling seam existed (`_mcp_mode_ceiling` absent, no `MCP_MAX_MODE` read).
- Chain hop 2 (code reference, no execution needed): `machina/cli/__init__.py:240`
  `config.set("execution_mode", normalized)` (process-local, `persist=False`
  default — verified in `core/config.py:518`, so no operator-file mutation),
  then `:283-293` creates the session and calls `grant_full_default`, which
  mints the full-mode session grant whenever in-process config reads `full`
  (`gateway/features/mode.py:252-278`).
- `R5-A_red_a1_policy_bypass.py` / `.log` — live `decide()` with the exact
  minted grant identity (`mode=full, capability=session,
  digest=full-mode-session`) on a `file.read` of a descendant `credentials`
  file returns `allowed / "full-mode session grant"`; without the grant the
  same request pends. Escalation end-to-end: tool arg → child full default →
  minted grant → sensitive ops allowed with no Machina approval.

FIX (`machina/mcp_server/__init__.py`):
- `_MODE_RANK = {ask:0, workspace:1, sandbox:2, full:3}`.
- `_operator_mode()` — operator's configured `execution_mode` (lazy config
  import; shim stays light; falls back to `ask`).
- `_mcp_mode_ceiling()` — default = operator mode; `MCP_MAX_MODE` (server env,
  operator-set at launch) may only TIGHTEN, never exceed the operator mode;
  invalid value fails closed to `ask` with a `logging` warning
  (`machina.mcp_server` logger → server stderr, operator-observable).
- `machina_chat` refuses above-ceiling modes with a typed refusal
  (`status=invalid_request`, `code=mode_ceiling_exceeded`, `ceiling=<ceiling>`)
  BEFORE resolving the DSN or spawning — never a grant, never a child.
  At/under-ceiling and modeless calls are byte-identical to before.

GREEN: `R5-A_green_a1_ceiling.py` / `.log` — 8 cases pass: default-ask refuses
`full`+`workspace` unspawned; operator-full allows both with `--mode`
forwarded; `MCP_MAX_MODE=ask` tightens under operator-full; `MCP_MAX_MODE=full`
under operator-ask still refuses; invalid `MCP_MAX_MODE` fails closed;
modeless call spawns without `--mode`.

BEHAVIOR CHANGE (called out): with the default operator mode `ask`, MCP
callers that previously passed `mode=workspace/sandbox/full` now receive
`invalid_request/mode_ceiling_exceeded` instead of a turn. Operators who
intentionally entrust MCP callers set `execution_mode` (or tighter
`MCP_MAX_MODE`) in server configuration — authority stays in human/server
control, per the finding. `grant_full_default` is now unreachable from tool
args above the ceiling; at-ceiling `full` still derives from the operator's
own configured full default (disclosed consent, LP-07/13).

## A2 — cancelled chat orphans the child (finding 15): REPRODUCED then FIXED

RED: `R5-A_red_a2_cancel.py` / `.log` — real `machina_chat` path with
`_run_process` wrapped around the REAL `run_bounded` semantics (slow marker
child, no DB/provider needed — the seam under test is the await without a
stop handle at old `:474`). After task cancellation the handler was gone but
the child survived and wrote its marker. Bug reproduced.

FIX (`machina/mcp_server/__init__.py`):
- `_run_process(..., cancel_event=None)` threads the handle into the shared
  `child_process.run_bounded(cancel_event=...)`, which kills the process group
  promptly on the event (pre-existing leaf support, incl. its `BaseException`
  kill-tree path).
- `machina_chat` runs the child on a daemon worker thread with a stop event
  instead of bare `asyncio.to_thread`. Normal path polls worker liveness and
  returns the typed result unchanged. On ANY `BaseException` (cancellation,
  loop shutdown) it sets the event, joins with a bounded shielded wait
  (`CANCEL_JOIN_SECONDS = 10.0`; a second cancel cannot strand the tree),
  then re-raises so the request still reports cancelled — tree retired BEFORE
  the handler reports stopped.

GREEN: `R5-A_green_a2_cancel.py` / `.log` — `cancelled=True`,
`join_wait=0.05s`, marker absent after a 3s survivor window.

BEHAVIOR CHANGE (called out): cancellation now kills the child tree (files
stop changing, provider budget stops) where before it ran to the 300s
deadline. No change to completed/timeout/failed/paused shapes.

## A3 — full-grant fast path vs sensitive checks (finding 2, policy side)

RED: same live proof as A1-hop3 (`R5-A_red_a1_policy_bypass.py`): the fast
path at `policy.py:858` precedes `_sensitive_capabilities` — a full grant
allows credential/elevation/destructive ops without approval. Reproduced.

DISPOSITION: fast path STANDS (no grant-path behavior change). Reason:
headless transports deliberately depend on full meaning no-approval — CLI
chat auto-grants new sessions under a persistent full default precisely so
governed turns do not pause with unresolvable exit-3 approvals (R4-018,
`cli/__init__.py:285-297`). Gating sensitive caps inside `decide()` would
break that design and strand MCP turns in `paused_for_approval`. The defect
is therefore a DISCLOSURE defect, owned by R5-D. Added a comment-only grant-path
note in `policy.py` recording this disposition (no verdict change).

GREEN: `R5-A_green_a3_fastpath.py` / `.log` pins the standing semantics:
credential read, `sudo`, and destructive exec all `allowed / "full-mode
session grant"` with the grant, `pending` without it.

## Exact grant-path semantics for R5-D (finding 2, disclosure side)

R5-D must disclose THIS (verified against `decide()` + `grant_full_default`
above; do not soften):
> Activating full mode for a session mints a live full-mode session grant
> (`mode=full, capability=session, digest=full-mode-session`) that authorizes
> EVERYTHING in that session without further approval — including reading
> credential files, privilege escalation, and destructive operations. No
> sensitive capability (credentials, elevation, destructive system, security
> settings, opaque execution) re-asks while the grant is live. Authority is
> session-scoped (session A never elevates session B) and expires/revokes
> with the grant; without a live grant, `full` mode pends with "requires
> explicit session activation".
R5-A's A1 ceiling does NOT change these semantics — it only bounds which
modes an MCP tool argument may request (default: operator mode).

## Files changed

- `machina/mcp_server/__init__.py` — imports (`logging`, `threading`, `time`,
  logger); `_MODE_RANK`, `CANCEL_JOIN_SECONDS`, `_operator_mode()`,
  `_mcp_mode_ceiling()`; `_run_process(..., cancel_event)`; `machina_chat`
  ceiling refusal + cancellable worker thread (`_join_worker`).
- `machina/permissions/policy.py` — comment-only R5-A disposition note on the
  full-grant fast path (zero verdict change).
- `tests/test_mcp_server.py` — `_fake_runner`/`forbidden` accept
  `cancel_event`; `test_chat_resume_and_mode_flags` raises the operator
  ceiling via `MACHINA_EXECUTION_MODE=full`; 5 new tests (ceiling refusal,
  at-ceiling spawn, tighten/never-escalate, invalid-ceiling fail-closed,
  cancellation retires child).

## Verification

- RED logs: `R5-A_red_a1_forward.log`, `R5-A_red_a1_policy_bypass.log`,
  `R5-A_red_a2_cancel.log` (all reproduced pre-fix; scripts kept).
- GREEN logs: `R5-A_green_a1_ceiling.log` (8/8), `R5-A_green_a2_cancel.log`,
  `R5-A_green_a3_fastpath.log`.
- `R5-A_pytest_mcp.log`: `tests/test_mcp_server.py` 42 passed (incl. R4-081
  DSN E2E `test_chat_child_observes_the_parent_database[env,env_file]` —
  watch-out stays green; modeless/DSN path untouched).
- `R5-A_pytest_policy.log`: 217 passed (`test_permission_risk_policy`,
  `test_fm4_session_mode`, `test_session_mode_persistence`,
  `test_mcp_manage_tool`).
- `set -o pipefail` used on all shell proofs; no commit made.
