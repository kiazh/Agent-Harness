# H2 — `/harnesses` UI lane: proof

Date: 2026-10-15 · Lane: H2 (harnesses UI) · Sole writer of the files below.

## What shipped

A TUI `/harnesses` menu that lists external agent harnesses like `/provider`
lists providers, with per-harness actions **Status**, **Register** (enable),
**Unregister** (disable) and **Run a task** (delegate a prompt into the
harness). Backed by new authenticated gateway methods `interop.list` and
`interop.run`, plus the existing `interop.enable` / `interop.disable`.

## Files changed

Gateway (Python):
- `machina/gateway/features/interop.py` — added `interop_list` + `interop_run`
  handlers, bounded/redacted output helpers, and lazy module-level bindings
  (`supported_runs`, `run_task`, `state_path`) to the frozen H1 run surface.
- `machina/gateway/features/__init__.py` — registered `interop.list` /
  `interop.run` in the method table.
- `tests/test_features_split.py` — extended the exact method-set expectation
  (required for the suite to stay green; the only non-owned test touched).

Gateway tests (new):
- `tests/test_harnesses_feature.py` — list/enable/disable/run, auth, bounded
  output, redaction, input validation. Interop functions are stubbed at the
  `machina.interop` external-process boundary (no harness CLI is spawned).

UI (TypeScript):
- `ui/src/features/harnesses.ts` (new) — the `/harnesses` command.
- `ui/src/features/index.ts` — registered `harnessesCommand`.
- `ui/src/commands.ts` — added `harnesses` to the `/help` Setup group.
- `ui/src/protocol.ts` — added `HarnessRow`, `HarnessListResult`,
  `HarnessRunResult` wire types.
- `ui/test/harnesses.test.ts` (new) — 11 node tests over a fake host.

Display renames (labels only; provider id `chatgpt` untouched):
- `ui/src/features/settings.ts` (`/provider` label, `/provider` hint, `/effort`
  description + tier labels), `ui/src/features/keys.ts` hint,
  `ui/src/features/models.ts` fallback description + effort hint,
  `ui/src/features/auth.ts` all user-facing strings.

Docs:
- `communications/ui-gateway-protocol.md` — added the harness method list.

Evidence: `docs/verification/proofs/harnesses_feature_2026-10-15/`
(`gateway-tests.log`, `ui-check.log`, `ui-harnesses-test.log`, `ruff.log`).

## Frozen interface used (lane H1, `machina/interop.py`)

Code is written against these exact names and they are now present in the tree:

- `interop.detect() -> dict[str, DetectResult]`
- `interop.enable(platform, scope="project", dry_run=False)`
- `interop.disable(platform)`
- `interop.state_path()` (public state-file reader)
- `interop.supported_runs() -> dict[str, str]`
- `interop.run_task(platform, prompt, *, timeout=300.0) -> RunResult(platform, command, exit_code, stdout, stderr, ok, unsupported_reason)`

`interop.list` reads `DetectResult` defensively (`scope`, falling back to
`state_scope` / `scopes`) so it tolerates either detect shape.

## Per-deliverable status

1. **Gateway methods — done.** `interop.list` returns
   `{harnesses: [{key, label, installed, version, running, registered, scope,
   runSupported, runHint}], stateFile}`. `interop.run` returns bounded
   `{ok, exitCode, command, stdout, stderr, unsupportedReason}`. Detection,
   enable/disable and delegation all run on a worker thread
   (`asyncio.to_thread`); detection stays on-demand. All methods are
   authenticated (the gateway rejects them without the token).
2. **UI `/harnesses` — done.** Picker shows `name — ✓ installed (version) ·
   ● running · registered (scope) · runnable`. Actions picker offers Status /
   Register / Unregister and, only when `runSupported`, Run a task. Run prompts
   for text via `askText`, shows a confirm line warning the prompt executes in
   the external harness with host access, calls `interop.run`, and renders the
   bounded output via `sanitizeDisplay` in plain, scroll-friendly transcript
   blocks. Unsupported harnesses show `runHint` and never offer the run action;
   failures/timeouts surface honestly (`failed`, exit code, "no exit code …").
   Status shows version/registered/scope/run support/state file path.
3. **Display renames — done.** Every user-visible "ChatGPT account" /
   "ChatGPT" provider label now reads "OpenAI account"/"OpenAI". The provider
   id `chatgpt` is unchanged everywhere (config keys, method names
   `auth.chatgpt.*`, wire type names, `/provider chatgpt`, `--scope` ids).
4. **Tests — done.** Gateway feature test stubs every spawning helper at the
   `machina.interop` boundary (documented in the module docstring). UI node
   tests drive `/harnesses` against a `FeatureHost` fake. Protocol parity is
   unaffected (the parity test covers events/`SessionInfo`/`AgentInfo`/
   `HistoryEntry`/`ProviderRemoveResult`; the new interfaces add no events).

## Verification

```
uv run pytest tests/test_harnesses_feature.py tests/test_features_split.py \
  tests/test_gateway_features.py tests/test_gateway_providers.py \
  tests/test_gateway_connectors.py tests/test_interop.py \
  tests/test_harnesses_core.py tests/test_fm7_cli.py tests/test_self_knowledge.py -q
# 187 passed

cd ui && npm run check
# typecheck + lint clean; node tests 491 tests / 485 pass / 0 fail / 6 skipped
# (incl. the 11 new /harnesses tests)

uv run ruff check machina/gateway/features/interop.py \
  machina/gateway/features/__init__.py \
  tests/test_harnesses_feature.py tests/test_features_split.py
# All checks passed
```

`interop_run` redaction is proven in
`tests/test_harnesses_feature.py::test_interop_run_returns_bounded_redacted_output`
(a `sk-…` argv token comes back as `[REDACTED_OPENAI_KEY]`) and oversized
streams are capped in `test_interop_run_caps_oversized_streams`.

## Notes / caveats

- **Provider id preserved.** The `chatgpt` provider key, `auth.chatgpt.*`
  method/event names, `ChatGPT*` TypeScript type names and `/provider chatgpt`
  are intentionally unchanged; only display labels changed. This keeps
  persisted configs and the wire protocol stable.
- **Parallel lane H1.** H1 landed its `machina/interop.py` run surface in the
  same working tree during this session (uncommitted). An early full-suite run
  showed two transient `tests/test_interop.py` failures while H1 was mid-edit
  of those assertions; a re-run is green (53 passed). No H1-owned file was
  touched by this lane.
- **Run is not streamed.** One bounded `interop.run` result per invocation
  (timeout 1–900s, default 300; stdout ≤32k, stderr ≤8k).
