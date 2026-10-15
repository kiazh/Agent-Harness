# H1 — harnesses feature (core): self-knowledge, frozen run interface, CLI rename

Date: 2026-10-15 · Lane: H1 (harnesses core). Sole-writer scope: `machina/core/
self_knowledge.py`, `machina/interop.py`, `machina/cli/interop.py`, `machina/
cli/__init__.py` (registration + doctor line), `machina/tools/harness.py`,
`README.md`, and the affected tests + one new evidence test file. `ui/` and
`machina/gateway/` are owned by another lane and were **not** touched.

## Goal

A TUI agent denied that `interop` existed because `self_knowledge.FEATURES` had
no entry for it. This change makes the harness visibly *know* the feature, gives
it the primary name **harnesses** with `interop` kept as an alias, and adds a
bounded, typed **delegate-a-task** capability (`harnesses run`).

## Files changed

| File | Change |
| --- | --- |
| `machina/core/self_knowledge.py` | new `FEATURES["harnesses"]` entry (terse house style). |
| `machina/interop.py` | internal `RunResult` → `_ProcessResult`; new frozen `RunResult`, `RUN_PLATFORMS`, `supported_runs()`, `run_task()`; new `state_summary()`; hints say `machina harnesses`. |
| `machina/cli/interop.py` | module docs + group help; new `run` command + `run_run()`; hint strings now `machina harnesses`. |
| `machina/cli/__init__.py` | group registered as `harnesses` **and** `interop` (same app); doctor line `machina harnesses enable <key> --scope global`, summary label `External agent harnesses`; setup message lines use `machina harnesses`. |
| `machina/tools/harness.py` | `harness_status` payload gains a compact, spawn-free `harnesses` block (`interop.state_summary()`). |
| `README.md` | `**Interop.**` section renamed to `**Harnesses.**`, covering detection, enable/disable/setup, the `run` delegation, the TUI `/harnesses` menu, the `interop` alias and the install/auth-touch boundary. |
| `tests/test_harnesses_core.py` | **new** H1 evidence suite (RED-first). |
| `tests/test_interop.py`, `tests/test_fm7_cli.py`, `tests/test_installer_env.py`, `tests/test_self_knowledge.py` | affected: internal type rename, hint/doctor strings, and the capability-brief token bound. |

`docs/MCP.md` and `OPERATIONS.md` contain **no** `machina interop` command
mentions (only the historical "[interop proof]" link label), so no edit was
required there.

## Frozen interface (the UI lane codes against these exact names)

```python
@dataclass
class RunResult:
    platform: str
    command: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    ok: bool
    unsupported_reason: str = ""

RUN_PLATFORMS: dict[str, list[str]] = {
    "opencode": ["opencode", "run"],
    "codex":    ["codex", "exec"],
    "hermes":   ["hermes", "-z"],
}

def supported_runs() -> dict[str, str]: ...        # platform -> human usage hint
def run_task(platform: str, prompt: str, *, timeout: float = 300.0) -> RunResult: ...
```

`run_task` runs `[*RUN_PLATFORMS[platform], prompt]` through the existing
`machina.core.child_process.run_bounded` (minimal env allowlist, own process
group, kill-tree deadline, size caps), ANSI-strips + secret-redacts stdout and
stderr, and is **total**: an unknown/unsupported platform, a missing binary, a
non-zero exit or a timeout is a value (`ok`/`exit_code`/`unsupported_reason`),
never an exception. The previous internal bounded-runner result type was renamed
to `_ProcessResult` (no behaviour change to enable/disable).

`state_summary()` returns `{"state_file": ..., "platforms": {key: {label,
installed, enabled, scope, registered}}}` from the state file + a `shutil.which`
lookup only — **no** version/list/running probe.

## Real run-syntax verification (installed binaries)

Pinned from the installed CLIs before writing the mapping (logs `10_`, `11_`,
`12_`):

| Platform | Binary | Documented headless entry point |
| --- | --- | --- |
| OpenCode | `opencode v2.0.26` | `opencode run [flags] [<message...>]` — the v1 `run` was interactive; the installed v2 `run` is the headless message entry. |
| Codex | `codex-cli 0.162.1` | `codex exec [OPTIONS] [PROMPT]` — "Run Codex non-interactively". |
| Hermes | `Hermes Agent v0.21.6+386.g5ba559c` | `-z/--oneshot PROMPT` — "One-shot mode: send a single prompt and print ONLY the final response text to stdout … Intended for scripts / pipes." |

Hermes **is** run-capable: its top-level `-z/--oneshot` flag is explicitly the
script/pipe headless path (there is no `hermes run`/`hermes exec` subcommand).
Because `opencode run` headless *is* available on the installed v2 CLI, OpenCode
is not marked unsupported for RUN.

## RED → GREEN

RED (before implementation), `pytest tests/test_harnesses_core.py -q`:
`12 failed` — the brief lacked `FEATURES["harnesses"]`; `interop.RunResult` /
`RUN_PLATFORMS` / `supported_runs` / `run_task` did not exist; the CLI had no
`harnesses` group; `harness_status` had no `harnesses` block.

GREEN (logs `20_`–`22_`):

```
tests/test_harnesses_core.py + tests/test_self_knowledge.py ... 27 passed
tests/test_interop.py + tests/test_fm7_cli.py +
  tests/test_final_review_cli_config_setup.py + tests/test_r4_093_cli_cluster.py ... 105 passed
tests/test_installer_env.py -k "interop or install_sh" ... 11 passed
```

`ruff check` and `ruff format --check` clean (log `23_`).

## Real end-to-end delegation

`machina harnesses run codex 'Reply with exactly the word PONG and nothing else.'
--timeout 120` → codex exec returned `PONG`, `codex exited 0` (log `05_`).
`machina harnesses status` detected all three installed harnesses with versions
and registrations (log `04_`); `machina harnesses run not-a-platform x` exited 2
with the unsupported reason (log `03_`). `machina --help` lists both groups
(`harnesses` primary and `interop` alias — Typer cannot hide the alias, which the
task allows) (log `06_`).

`state_summary()` marks `registered` only when the platform is `enabled` **and**
its last verify passed, so a verified *removal* (which also stores
`verify.ok = true`) never reads back as registered (pinned by the H1 suite).

## Shared guard note (not this lane)

`tests/test_architecture_layers.py::test_deferred_import_count_ratchet` measures
`717 > 713`. That growth is **+4 deferred imports in `machina/gateway/features/
interop.py`** (the H2 gateway lane, which this lane must not edit). Every file
this lane changed contributes **+0** deferred imports (log `24_`).

## Logs in this directory

- `01_harnesses_help.txt`, `02_interop_alias_help.txt` — group + alias help.
- `06_top_level_help.txt` — `machina --help` lists both `harnesses` and `interop`.
- `07_run_help.txt` — `machina harnesses run --help` (platform, prompt, --timeout).
- `03_run_unknown_platform.txt` — `run` unsupported → exit 2.
- `04_harnesses_status_real.txt` — real detection table.
- `05_run_real_codex.txt` — real bounded delegation (PONG).
- `10_/11_/12_` — installed-binary run-syntax help captures.
- `20_/21_/22_` — GREEN focused suites.
- `23_ruff.txt` — ruff check + format.
- `24_ratchet_attribution.txt` — deferred-import delta per changed file.
