# Final line review — gateway / api / mcp / cli / observability slice

Date: 2026-10-15
Scope (read every line, post-fix HEAD `1e1edbf`):

- `machina/gateway/*.py` — `server.py`, `__main__.py`, `errors.py`, `serializers.py`, `features/*.py`
- `machina/api/app.py`, `machina/api/routes/*.py` (esp. `streaming.py` turnId)
- `machina/mcp_server/__init__.py`
- `machina/cli/*.py` — `__init__.py`, `output.py`, `providers.py`, `growth.py`, `auth.py`, `interop.py`, `launcher.py`
- `machina/observability/*.py` — `audit.py`, `safe_logging.py`, `traces.py`, `diagnostics.py`, `metrics.py`, `tracing.py`
- `machina/protocol/*.json` + `machina/protocol/__init__.py`

Method: static read + callsite/grep verification only. No runtime execution.

Notes honored:
- The audit-singleton normalization is TEST-side (`tests/conftest.py`) — not flagged.
- The synthetic corpus contains secrets by design; no fixture reported as a leak.
- This revision **corrects** an earlier draft of this file which listed the MCP
  mode ceiling as "clean". The ceiling is bypassable by omitting `mode`
  (finding F2 below); the sibling `security_review.md` in this directory already
  filed it as **S2**. It is recorded here as confirmed (cross-referenced, not
  double-scoped).

---

## Confirmed findings (severity-ranked)

### F1 — P2 — Gateway `execution_mode` writes are silently shadowed by `MACHINA_EXECUTION_MODE`; the gateway never performs the env/`.env` sync the CLI performs

Citations:
- `machina/gateway/features/mode.py:66-70` — `mode_set(scope="global")` calls `config.set("execution_mode", mode, persist=…)` then returns `{"mode": mode, …}` (echoes the *requested* mode; never re-reads `config.get`).
- `machina/gateway/features/mode.py:148-155` — `mode_revoke(scope="global")` also `config.set("execution_mode", "ask")` with no env sync (see also F6).
- `machina/gateway/server.py:695-701` — the generic `_config_set` path for any `DEFAULTS` key (incl. `execution_mode`) calls `features.coerce_config_value` + `config.set(key, value, persist=…)`; the only env sync (`server.py:704-724`) is gated `if persist and key in ("model", "provider")`.
- `machina/gateway/server.py:498-502` — `_initialize` applies a `mode` param via `config.set("execution_mode", mode)` (non-persistent) — same shadowing hazard for the launcher/`--mode` path.
- `machina/core/config.py:518-543` — `Config.set` mutates the in-memory attribute / YAML only; it never touches `os.environ`.
- `machina/core/config.py:492-516` — `Config.get` returns `MACHINA_<KEY>` (then legacy env) **before** the in-memory attribute and the file, so any `MACHINA_EXECUTION_MODE` value wins over a gateway `config.set`.
- `machina/core/config.py:74-79` — `.env` is loaded with `load_dotenv(override=False)` at import, so a `.env` line reaches the environment on every start.
- Counterpart the CLI added deliberately: `machina/cli/__init__.py:93-112` `_sync_env_shadow` (writes `os.environ["MACHINA_<KEY>"]` + `set_env_values`), called at `:564`, `:600` and `:1637`. The comment at `:590-594` states the reason verbatim.

Failure scenario: operator runs `machina mode full` → `.env` gains `MACHINA_EXECUTION_MODE=full`. Later an authenticated gateway client (`POST /api/v1/rpc` `mode.set {scope:"global", mode:"ask"}` or `config.set {key:"execution_mode", value:"ask"}`) attempts the downgrade. `Config.set` updates the attribute/YAML, but `config.get("execution_mode")` still returns `"full"` (env precedence). The handler returns `{"mode":"ask"}`. `grant_full_default` (`mode.py:252-278`, reads `config.get("execution_mode")` at `:260`) keeps auto-granting full authority to every new session. A security-relevant downgrade is silently defeated.

Callsites verified: every gateway reader of `execution_mode` goes through `config.get` (`mode.py:27,30,108,260`; `_operator_mode` in `mcp_server/__init__.py:99`). No gateway path calls `set_env_values`/`os.environ[...]` for `execution_mode` (`grep set_env_values|os.environ\[ machina/gateway` hits only `server.py:714-718` model/provider and `secrets.py`).

Falsification attempt: (a) searched every `MACHINA_EXECUTION_MODE` / `_sync_env_shadow` writer in the repo — only the CLI writes it (`tests/test_r4_093_cli_cluster.py:99-107` pins that); (b) `Config.set` has no env hook anywhere; (c) `tests/test_audit_remediation.py:85-87` must `monkeypatch.delenv("MACHINA_EXECUTION_MODE")` — comment: "Drop the ask pin so the global set below is observable" — which is direct evidence the gateway global set does *not* win over the env. Not falsified.

Reachability: not via the TUI (`ui/src/features/mode.ts` only uses `scope:"session"`, `:60`), but reachable through the authenticated generic bridge `POST /api/v1/rpc` (methods `mode.set`/`config.set`/`initialize`) and direct gateway calls.

---

### F2 — P2 — MCP mode ceiling bypassed by omitting `mode` (already filed as `security_review.md` S2)

Citations:
- `machina/mcp_server/__init__.py:527-543` — the ceiling guard is inside `if requested:`; `requested = mode.strip().lower() if isinstance(mode, str) else ""`.
- `machina/mcp_server/__init__.py:424-437` — `_chat_command` adds `--mode` only when `mode` is truthy; with `mode=None` the child runs `machina chat -q -- <prompt>` with no `--mode`.
- `machina/mcp_server/__init__.py:105-122` — `_mcp_mode_ceiling()` = `min(operator mode, MCP_MAX_MODE)` (tighten-only, fail-closed on invalid). The operator mode is `config.get("execution_mode")` (`:94-102`).
- `machina/cli/__init__.py:235-240` — the child's `chat` applies `--mode` only if supplied; otherwise the session/global default (the operator's configured `execution_mode`) applies.

Attack sketch: operator sets `MCP_MAX_MODE=ask` below their own `execution_mode=full` (the whole point of the tightening knob). An MCP caller that supplies `mode="workspace"` is refused (`invalid_request`, `mode_ceiling_exceeded`, not spawned), but the same caller that simply **omits** `mode` is spawned with no `--mode`, so the child resolves `execution_mode=full` from config/`.env` — above the declared ceiling — with no typed refusal. Fail-open for the caller who leaves the parameter out.

Note (`_validate_chat_args`, `mcp_server/__init__.py:402-421`): `mode=""` is rejected ("mode must be one of …"), so the bypass is specifically `mode=None`/absent, not the empty string.

Falsification attempt: searched for any child-side/secondary enforcement of `MCP_MAX_MODE` — the child only sees `--mode` (never `MCP_MAX_MODE`; it is not in the child allowlist); none exists. `tests/test_mcp_server.py:275-335` only ever passes an explicit `mode`, so the omitted path is uncovered. Not falsified. Fix: clamp the *effective* mode when `mode is None` (always pass `--mode <min(operator, ceiling)>`).

---

### F3 — P3 — HTTP SSE v2 `tool.complete.isError` is always `false`

Citations:
- `machina/api/routes/streaming.py:228-240` — the SSE route builds `StreamEvent(type="tool_result", tool_name=…, tool_result=…, tool_outcome=payload.get("outcome",""), tool_call_id=…)` and **drops** `payload["isError"]` (the engine payload carries it: `core/turn_engine.py:361`).
- `machina/core/models.py:82-97` — `StreamEvent` has `tool_outcome` but no error field, so `_serialize_event` cannot forward one.
- `machina/api/routes/shared.py:81-87` — the `tool_result` branch sets `tool`/`result`/`outcome`/`id` only; never `isError`.
- `machina/protocol/__init__.py:132-142` — `canonical_http_event` defaults `isError=data.get("isError", False)`.
- Documented contract: `communications/ui-gateway-protocol.md:40` — "`tool.complete` … `isError: boolean` … isError reports failure".
- Contrast (correct): the stdio gateway forwards the real flag — `machina/gateway/server.py:1005-1015` `isError=payload["isError"]`.

Consequence: on the v2 HTTP/SSE wire a genuinely failed tool (engine `isError=True`, `outcome="failed"`) is serialized as `isError:false`; only `outcome` carries the truth. The TUI is unaffected (it maps `outcome` first — `ui/src/transcript.ts:27-39`), but a non-TUI HTTP client honoring the documented `isError` contract misreads failures as success.

Falsification attempt: grep shows no writer of `data["isError"]` anywhere on the HTTP path and no error member on `StreamEvent`, so canonical necessarily defaults `false`. `tests/test_r4_022_denied_outcome.py:127-150` only pins the *denied* case (`isError False` is intended there) and does not cover a failed tool over the HTTP wire. Not falsified.

---

### F4 — P3 — `render_diagnostics` omits the LLM-ledger overflow marker (trace-cap honesty)

Citations:
- `machina/observability/traces.py:824-868` — `render_diagnostics` renders `dropped_spans`, `dropped_tool_spans` and `dropped_turns` but **never** `ledger_dropped`.
- `machina/observability/traces.py:723-730` — the ledger ring drops the oldest entry and increments `turn.ledger_dropped` (marker written to the root span + report).
- `machina/observability/traces.py:818` — `turn_report` returns `"ledger_dropped"`; no renderer surfaces it.
- `machina/observability/traces.py:806-809` — `llm_calls` / token totals are computed from the **retained** `turn.ledger`, so a capped ledger understates them.

Consequence: for a turn exceeding `MAX_LEDGER_ENTRIES_PER_TURN` (500, `traces.py:70`), the rendered spend summary prints `llm: N calls` / token figures with no dropped-marker, unlike every other capped ring (tools/spans/turns all print `(+X dropped)`). The docstring at `traces.py:68-70` promises the marker "on the root span and the report", which holds for `turn_report` but not for the diagnostics string the gateway/UI shows.

Falsification attempt: grep shows no use of `ledger_dropped` in rendering; `tests/test_fm6_traces.py:54-68` asserts `report["ledger_dropped"] == 20` but never asserts it appears in `render_diagnostics`. Not falsified. (Low impact; a UI/diagnostic honesty gap.)

---

### F5 — P3 — API runtime shutdown stops the audit writer *before* draining background work (audit-event loss / ordering inconsistency)

Citations:
- `machina/core/runtime.py:227-238` — `audit_persistence.stop()` runs first; `machina/core/runtime.py:239-246` — `drain_background_tasks(...)` runs **after**.
- `machina/gateway/server.py:387-405` — the **opposite** (correct) order: `drain_background_tasks()` at `:390-393`, then `audit_persistence.stop()` at `:394-400`.
- Background work emits audit events: `machina/memory/consolidator.py` calls `audit_log("memory_consolidate", …)` and `machina/skills/learning.py:41` forwards to `audit_log` — both are exactly the task groups awaited by `drain_background_tasks` (`core/background_tasks.py:31-37,64-87`).
- `machina/observability/audit.py:49-57` — a `submit()` after the writer is stopped drops the event (metric + error log).

Consequence: on the HTTP API lifespan path (`machina/api/app.py:192-196` → `runtime_services.shutdown()`; the embedded gateway has `owns_db=False` so `close()` never stops the writer — `gateway/server.py:382-405`), consolidation/learning audit rows submitted after the writer stops are dropped, whereas the stdio gateway path preserves them. Bounded and metered, but a real ordering asymmetry between the two shutdown paths.

Falsification attempt: confirmed the two close paths order drain/stop oppositely; confirmed `drain_background_tasks` awaits the consolidation/learning tasks and those tasks call `audit_log`; `audit._drop` only counts/logs (no persistence). Not falsified.

---

### F6 — P3 — `mode_revoke(scope="global")` is not durable, contradicting its own docstring and the CLI

Citations:
- `machina/gateway/features/mode.py:131-137` — docstring: "Pass scope=global (CLI durable reset) to also reset the persistent default."
- `machina/gateway/features/mode.py:145-155` — both branches call `config.set("execution_mode", "ask")` with **no `persist=True`**.
- `machina/core/config.py:518-543` — `Config.set(..., persist=False)` (the default) changes only the in-memory attribute; `config.yaml` is untouched.
- CLI counterpart that *is* durable: `machina/cli/__init__.py:557-568` `config.set("execution_mode", "ask", persist=True)` + `_sync_env_shadow("execution_mode", "ask")`.

Consequence: a gateway `mode.revoke {scope:"global"}` (reachable via the authenticated `/rpc` bridge) reports `scope: "global"` and changes the running default, but a restart re-reads the old `execution_mode` from `config.yaml`. The reset is not durable.

Falsification attempt: verified `config.set`'s default is `persist=False` and no later write persists the value on this path; `activate_durable_mode` resets the *session* mode, not the global default. Ambiguity acknowledged: the docstring's "(CLI durable reset)" could be read as describing the CLI only, but the same sentence says "to also reset the persistent default", which the code does not do. Reported as P3 (correctness/honesty, not exploitable by itself).

---

## Checked and clean (confidence notes)

- **RPC authz/validation** — `handle_line` token gate for every method except bare `initialize` (`server.py:324-337`); `_initialize` restricts the token-free handshake to `{protocolVersion, token}` and checks the token before any state mutation (`server.py:478-502`); `_token_ok` fails closed on non-ASCII (`:423-435`). Generic `/rpc` blocks `prompt.submit`/`prompt.cancel`/`shutdown` and redacts results (`api/routes/rpc.py:35-46`). Feature handlers consistently validate via `_str/_int/_bool/_uuid` and `require_db`. OAuth handlers pin an explicit allowed-param set and owner binding (`features/connector_oauth.py:28-39,59-199`, incl. `expectedGeneration`/`revoke` type checks). Session/approval ownership is bound to the stored session (`mode.py:188-194`, `api/routes/approvals.py:38-39,88-89`).
- **SSE wire shape (excl. F3)** — v2 canonicalization is idempotent for the `needs_approval` done-branch (the second pass takes the generic branch since no `proposal` key exists) and emits schema-valid events (`protocol/__init__.py:119-174`); `turnId` is the canonical 12-hex id consistently across `turn.started`/`approval.resumed`/`turn.ownership_lost`/`turn.cleanup_pending` (`streaming.py:55,168,214,330,393`); the internal owner token never leaves the module. `message.complete.cancelled` is only emitted on genuine completion (cancel paths emit no terminal) so its hard-coded `False` is not observed.
- **turnId/ownership** — DB claim first, before SSE headers (`streaming.py:53-58`) and before starting the task (`server.py:640-651`); `_prompt_cancel` is session-keyed by protocol design (UI sends `{sessionId}`); `_turns`/`_turn_tokens`/`_turn_finished`/`_cancel_requested` cleanup is identity-guarded (`server.py:1074-1077`, `1151-1160`, `1206-1213`). `_ownership_lost` cancels the live step and surfaces `turn.ownership_lost` (`streaming.py:119-127,329-330`).
- **MCP cancel edges** — the child runs on a worker thread bound to a stop handle; handler cancellation sets the event, bounded-joins (`_join_worker`, `CANCEL_JOIN_SECONDS=10`, `mcp_server/__init__.py:497-501,571-586`), and `run_bounded` observes the event and kill-trees. DSN passthrough `_child_database_env` resolves from the live `db.dsn` and is merged onto the sanitized allowlist; it is never logged/returned (`mcp_server/__init__.py:161-186,244-286`).
- **CLI escaping** — `output.py` escapes every table cell/title/label incl. `config_table`/`memory_table` (`output.py:38-146`); `growth.py` uses `markup=False` on exception paths; `interop.py` escapes labels/messages and `_emit`/`doctor` use `markup=False`; `auth.py`/`providers.py` escape interpolated values; provider `api_key_env` derives from an `^[a-z][a-z0-9-]{0,31}$`-validated name (`security/providers.py:150,204`), so it cannot inject markup. No un-escaped external value reaches a markup console.
- **CLI error typing** — `fork`/`delete`/`growth`/`setup`/`config-set`/`providers` map `ValueError`/`OSError`/`ServiceError` to `output.error`+exit 1, never tracebacks (`cli/__init__.py:721-723,746-755,1608-1637`; `cli/growth.py:32-42`; `cli/providers.py:156-163`).
- **Audit writer lifecycle (excl. F5 ordering)** — owner-counted `start/stop` with a fresh-generation drain and a done-callback that cannot clobber a successor (`audit.py:24-80`); `_run` re-sanitizes at the persistence boundary and never dies per-entry (`audit.py:82-116`).

## Residual / not fully proven (not asserted as findings)

- `safe_logging.install` (`observability/safe_logging.py:28-38`) only redacts strings when `machina.memory.redaction` is already imported; log records emitted during early import (before that module loads) pass through unredacted. This is documented intent ("avoid importing the memory graph while modules initialize"); reachability requires a secret-bearing record in that startup window. Not exercised here.
- `AuditPersistence.start()` is not idempotent (each call adds an owner); in the HTTP path a degraded runtime (db unavailable, so `_RpcGateway`'s `_db_ready` is False) could let an `/rpc initialize` add a second owner that no shutdown decrements, leaving the writer undrained. Not confirmed reachable in the normal (db-connected) path; flagged as residual only.

## Verdict

Confirmed: **F1 P2**, **F2 P2** (already filed as sibling `security_review.md` S2), plus **F3/F4/F5/F6 P3**. No P1 found. F1/F2 are reproducible by static callsite trace and are un-covered by the existing tests; the four P3s are localized correctness/honesty gaps. No false-positive candidate was retained.
