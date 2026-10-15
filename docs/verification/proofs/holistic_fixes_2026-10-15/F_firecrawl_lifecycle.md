# F — machina-managed Firecrawl/SearXNG lifecycle (config-gated, OFF by default)

- Repo: `/Users/shadow/machina`, base revision `3ef6d5b` (working tree, uncommitted by instruction)
- Date: 2026-10-15
- Lane files: `machina/core/config.py` (additive keys only — the concurrent
  `max_tokens`/`MAX_TOKENS` policy edit was not touched), `machina/core/firecrawl_lifecycle.py`
  (new), `machina/cli/launcher.py` (interactive-entry wiring only), `tests/test_firecrawl_lifecycle.py`
  (new), `README.md` (config table + web-research paragraph, 3 payload rows).
  No other file was modified. `machina/services.py`, `machina/gateway/**`, `api/**`, `ui/**`,
  `memory/**`, `context.py`, `agent.py` untouched.
- Environment: `.venv` (Python 3.14.5, pytest 9.1.1),
  `MACHINA_TEST_DATABASE_URL='postgresql://postgres:postgres@localhost:5432/machina_test'`,
  `-p no:cacheprovider`. NO live E2E and no live stack/VM interaction in this lane (every run
  repoints `HOME` and uses stub executables; see the coordinator note about the VM's observed
  state).

---

## Requirement → implementation

Opening the interactive UI (`machina`, `machina repl`, `chat -i` — all three funnel through
`machina.cli.launcher.launch_ui`) can now bring the user's self-hosted stack up in the background,
and closing the last live session tears the containers and the Lima VM back down (~5 GiB).

### Config keys (`machina/core/config.py`, additive)

| key | default | env | behavior |
|---|---|---|---|
| `firecrawl_autostart` | `False` | `MACHINA_FIRECRAWL_AUTOSTART` | opt-in; false → `start_if_enabled()` returns immediately (no dirs, no log, no process) |
| `firecrawl_stop_on_exit` | `True` | `MACHINA_FIRECRAWL_STOP_ON_EXIT` | false → `stop_if_enabled()` returns immediately (stack and marker left alone) |
| `firecrawl_home` | `"~/firecrawl"` | `MACHINA_FIRECRAWL_HOME` | directory holding `up.sh`; `expanduser()` applied at call time |

Both new booleans ride the existing `validate_value` coercion (`true/1/yes/on`, `false/0/no/off`;
anything else raises the standard typed `ValueError`) and the standard `Config.get` precedence
(env → attribute → `DEFAULTS`). No new validator code was needed, so `config.py` gained only the
two definition sites plus comments.

### `machina/core/firecrawl_lifecycle.py` (new, 418 lines)

- **`start_if_enabled() -> bool`** — gate → `<home>/up.sh` existence check (missing → warn **once**
  per process in the log, return False) → under a bounded `flock`: prune dead-PID markers, write this
  session's marker (`~/.machina/run/firecrawl-session-<pid>.marker`, JSON `{pid, started_at}`) →
  spawn a **detached** chain (`/bin/sh -c "limactl start docker && <launcher> up"`,
  `start_new_session=True`, stdout+stderr appended to `~/.machina/logs/firecrawl-lifecycle.log`).
  The child environment is the caller's with `DOCKER_HOST` defaulted to
  `unix://$HOME/.lima/docker/sock/docker.sock` (an explicit `DOCKER_HOST` wins; `up.sh` also exports
  its own). `limactl` missing on `PATH` → warn once, chain runs the launcher only (an
  already-running VM still works). A spawn failure removes the just-written marker and returns False.
  **Launcher invocation (real-machine finding)**: the user's `~/firecrawl/up.sh` is mode `0644`, so a
  direct exec would `EACCES`. `_launcher_argv()` runs an executable script directly (shebang decides)
  and otherwise hands it to its declared interpreter (`#!` line, `env`-aware, `/bin/sh` fallback) —
  the demo transcript's log line reads `/bin/sh .../up.sh up` for a `#!/bin/sh` stub, and the real
  `#!/bin/bash` file resolves to `/bin/bash .../up.sh up`.
- **`stop_if_enabled() -> bool`** — gate → no-op unless *this process* started the stack
  (`_started_pid`, set only after a successful spawn). Under the `flock`: prune dead markers, remove
  own marker, and only when **no marker remains** run `up.sh down` then `limactl stop docker`
  synchronously. Both steps share one `STOP_TIMEOUT_SECONDS = 60.0` deadline (a nearly-spent budget
  still gives the next step a 0.1 s floor; a fully-spent budget logs and skips it); nonzero exits are
  tolerated and logged; a missing `limactl` logs and leaves the VM. Lock not acquired → **keep the
  stack** (never tear down without the refcount view); start degrades to a logged best-effort
  marker+spawn.
- **Never raises into machina**: every public path is wrapped (`except Exception` → log + `False`);
  I/O, lock, spawn and teardown failures are caught individually; the only exception deliberately
  *not* swallowed is `KeyboardInterrupt` mid-teardown — `_started_pid` is cleared only after a
  completed stop pass, so the `atexit` handler retries and the VM still goes down.

### Cross-session refcount semantics

One marker per session PID; prune + decide + teardown under `~/.machina/run/firecrawl-lifecycle.lock`
(POSIX `flock`, bounded acquisition = stop bound + 10 s, so a session opening during a peer's
teardown waits it out and then re-ups). A session that never started the stack never tears one down
(protects a manually started stack when autostart is off). A session that completed its stop pass is
done: if a peer was still live, the *peer's* exit is the one that tears down. Stale markers from
crashed sessions are pruned on the next start or stop.

### Interactive-entry wiring (`machina/cli/launcher.py`, +11 lines)

`launch_ui()` — the single funnel for the bare `machina` callback, `repl`, and `chat -i` — now does
`atexit.register(stop_if_enabled); start_if_enabled()` before `subprocess.call`, and
`stop_if_enabled()` in a `finally` (so the KeyboardInterrupt → exit-130 path also stops). One-shot
subcommands (`doctor`, `harnesses`, `skills`, …) never reach `launch_ui` and are untouched
(`grep -rn "launch_ui" machina/` shows only the three interactive call sites).

### README

The web-research paragraph gained 5 wrapped lines and the configuration table 3 rows for the three
keys, right after the existing `SEARXNG_URL` row.

## Tests — `tests/test_firecrawl_lifecycle.py` (20 tests, all pass)

Hermetic by construction: fake `HOME` (temp dir), stub `limactl` on `PATH`, stub
`<home>/firecrawl/up.sh`; the detached chain actually executes against the stubs, so order, child
env, marker lifecycle and log routing are asserted end to end. Coverage:

- disabled no-op (nothing created); `stop_on_exit=False` skips teardown and keeps the marker;
  stop-without-start never touches a user stack;
- start: `limactl start docker` then `up.sh up` in order, `DOCKER_HOST` default (fake HOME-derived
  socket) and explicit-override honored, marker contents = own PID, log lines written; a 0644
  (non-executable, real-machine shape) launcher runs via its shebang interpreter for both start and
  stop; idempotent repeat start (single marker per PID); dead-PID marker pruned on start; missing
  `up.sh` warns exactly once; spawn failure / invalid config value → logged, not raised, marker
  cleaned;
- refcount: second live session's marker keeps the stack on stop (no teardown traced); last session
  prunes a stale marker, removes its own, tears down in order (`up.sh down` → `limactl stop docker`)
  and a repeat call no-ops; lock-unavailable stop keeps the stack;
- bounded teardown: hung `up.sh down` is cut at a patched 0.7 s bound (< 5 s wall clock), logged
  `timed out`; nonzero `up.sh down` and missing `limactl` tolerated and logged;
- wiring: `launch_ui` calls start then stop and registers the atexit net; KeyboardInterrupt →
  130 with stop still run;
- config: defaults in `DEFAULTS` and the dataclass, env override, boolean coercion,
  invalid value raises.

## Evidence (logs/)

- `logs/F_tests_green.txt` — 20 passed (verbose).
- `logs/F_tests_green_rerun_plus_launcher.txt` — repeat run + the existing
  `test_launcher_propagates_mode_to_gateway` (21 passed; the pre-existing launcher test still passes
  with the lifecycle hook in place).
- `logs/F_invocation_contract.txt` — real-module transcript against a temp HOME
  (`F_invocation_contract.py`, 0644 stub launcher mirroring the real machine): (1) default config
  no-op; (2) missing `up.sh` warn-once; (3) enabled with stubs, **cross-process** refcount — a
  second real Python session lives while the first stop keeps the stack, then the peer's stop (last
  marker) tears down in order. RESULT: PASS.
- `logs/F_ruff.txt` — `ruff format --check` + `ruff check` clean on all four files.
- `logs/F_ratchet_scan.txt` / `F_ratchet_scan.py` — per-file deferred/line deltas.
- `logs/F_census.txt` — tree deferred-import census with per-file deltas.
- `logs/F_arch_layers.txt` — architecture suite run (failures are other lanes', see below).

Related suites re-run green: `test_configuration_precedence`, `test_config_import_cycle`,
`test_config_reentrancy`, `test_uninstall`, `test_e1_prompt_cache`, `test_production_readiness_p1`,
`test_fm4_lifecycle`, `test_provider_custom`, `test_integration_permissions::test_launcher_*`.

## Ratchet / deferred-import deltas (coordinator reconciles; `tests/test_architecture_layers.py` NOT edited)

| Lane F file | deferred imports | lines |
|---|---|---|
| `machina/core/firecrawl_lifecycle.py` (new) | **0** | 418 (not pinned) |
| `machina/cli/launcher.py` | **+0** (0 → 0) | 115 (+11, not pinned) |
| `machina/core/config.py` | **+0** (3 → 3) | +22 working-tree (14 F + 8 of the concurrent `MAX_TOKENS` lane) |
| `tests/test_firecrawl_lifecycle.py` (new) | n/a (tests are outside the machina/ census) | 403 |

- **DEFERRED_IMPORT_CAP**: tree census is **720** (cap 717) on the shared working tree — every point
  of overflow belongs to other lanes (`core/scheduler.py` +3, `core/turn_engine.py` −1,
  `machina/services.py` +1; see `logs/F_census.txt`). **LANE F delta: +0.**
- **God-file ceilings / provider package total**: no LANE F file is pinned; the four architecture
  failures in `logs/F_arch_layers.txt` reference `core/agent.py`, `core/provider/copilot.py` and the
  provider package total — all other lanes.
- Module-level `machina.core` import ceilings govern leaf packages; `firecrawl_lifecycle.py` is
  itself under `machina/core/` and the launcher's import of it is module-level (zero deferred
  growth by design — the import is deliberately hoisted so the ratchet does not move).

## Coordinator notes for the live E2E (not run by this lane; config not flipped)

Environment observation: `limactl list` showed the `docker` VM **Stopped** at the end of this lane,
with its instance logs ending `13:26:15` — before any LANE F code executed (the first run was at
~13:31 and used fake HOME + stub `limactl`/`up.sh` only; the real `~/.machina/run/` has no markers
and no `~/.machina/logs/firecrawl-lifecycle.log` exists). This lane did not touch the real stack/VM.
Because the autostart chain begins with `limactl start docker`, the live E2E can exercise a cold
start directly.

1. Default (as-shipped) → launching the TUI is unchanged; no log file, no markers.
2. `MACHINA_FIRECRAWL_AUTOSTART=true machina` → `~/.machina/logs/firecrawl-lifecycle.log` shows
   `start: scheduled`, then the stack comes up; `~/.machina/run/firecrawl-session-<pid>.marker`
   exists. A second machina window adds its marker; closing one keeps the stack; closing the last
   logs `stop: ... ok` for `up.sh down` and `limactl stop docker`.
3. `MACHINA_FIRECRAWL_STOP_ON_EXIT=false` → closing keeps the stack (marker pruned next session).
4. Known honest limitation: closing a window in the seconds *while* a cold `limactl start` is still
   in flight can race that boot (the detached chain may finish after teardown; compose then fails
   harmlessly in the log). The relaunch start chain is idempotent and self-heals.
5. The user's real `~/firecrawl/up.sh` is mode **0644** (verified on this machine); the module runs
   non-executable launchers under their shebang interpreter, so the live log line will read
   `/bin/bash .../up.sh up` for this file's `#!/bin/bash`. `limactl` must be on `PATH`
   for the VM step; without it start still runs the launcher (warn-once) and stop logs that the VM
   step was skipped.
