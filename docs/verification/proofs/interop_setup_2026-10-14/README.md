# Interop self-setup proof — detect + register Machina's MCP server (2026-10-14)

Feature: `machina/interop.py` (+ `machina interop status|enable|disable`) —
detect external agent harnesses (OpenCode, Codex, Hermes), idempotently
register Machina's stdio MCP server (`python -m machina.mcp_server`) with the
ones the user chooses, verify by re-listing, and surface the enabled state.

- Repo: `/Users/shadow/machina` · git HEAD `b04203394c3233af3f0674d02dfc1e1e951e393f`
  (working tree + this wave's uncommitted changes; suite line in the completion report)
- Host: macOS (aarch64), OpenCode v2.0.26, Codex CLI 0.162.1, Hermes v0.21.6+386
- Interpreter registered with every harness: `/Users/shadow/machina/.venv/bin/python`

## 1. `machina interop status` (before) — `01_status_before.txt`

```
┏━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━┓
┃ Platform ┃ Installed ┃ Version                                      ┃ Running ┃ Registered ┃ Enabled ┃
┡━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━┩
│ OpenCode │ yes       │ opencode v2.0.26                             │ yes     │ project    │ manual  │
│ Codex    │ yes       │ codex-cli 0.162.1                            │ yes     │ no         │ no      │
│ Hermes   │ yes       │ Hermes Agent v0.21.6+386.g5ba559c (2026.9... │ yes     │ no         │ no      │
└──────────┴───────────┴──────────────────────────────────────────────┴─────────┴────────────┴─────────┘
```

OpenCode was already registered (project `opencode.json`, pre-existing
manual registration → `Enabled: manual`; see §4 for the record step). Codex
and Hermes were detected as installed + running but unregistered.

## 2. `machina interop enable codex` (real) — `02_enable_codex.txt`

```
$ .venv/bin/python -m machina interop enable codex
  Codex registers user-level only (no project scope); using global scope
  run: /Users/shadow/.local/bin/codex mcp add machina -- /Users/shadow/machina/.venv/bin/python -m machina.mcp_server
  registration command exited 0
  output: Added global MCP server 'machina'.
registered `machina` for Codex (global scope) and verified it.
  ✓ verify: present in `codex mcp list --json` (enabled)
```

`codex mcp add` has no project scope (the CLI supports user config only),
so the requested project scope was clamped to `global` and the clamp is
printed, never silent. Registration evidence
(`03_codex_mcp_registration.txt`; the table below drops the trailing
`Env`/`Cwd`/`Auth` columns for width):

```
$ codex mcp list
Name     Command                                 Args                   Status
machina  /Users/shadow/machina/.venv/bin/python  -m machina.mcp_server  enabled

$ grep -A3 mcp_servers ~/.codex/config.toml
[mcp_servers.machina]
command = "/Users/shadow/machina/.venv/bin/python"
args = ["-m", "machina.mcp_server"]
```

State written (`04_state_after_codex.json`, also §7 schema):

```json
{
  "codex": {
    "binary": "/Users/shadow/.local/bin/codex",
    "enabled": true,
    "registered_at": "2026-10-14T16:53:49+00:00",
    "scope": "global",
    "verify": {"detail": "present in `codex mcp list --json` (enabled)", "ok": true},
    "version": "codex-cli 0.162.1"
  }
}
```

## 3. Live round trip: Codex → machina MCP tool — `05_codex_exec_roundtrip.log`

```console
$ codex exec --json -C /Users/shadow/machina -s read-only \
    "Call the machina_status tool from the machina MCP server exactly once ...
     Then reply with exactly MCPX-CODEX-9F2A on the final line and nothing else."
```

Observed (JSONL, excerpt; exit code 0, stderr empty):

```json
{"type":"item.started","item":{"id":"item_1","type":"mcp_tool_call","server":"machina","tool":"machina_status","status":"in_progress"}}
{"type":"item.completed","item":{"id":"item_1","type":"mcp_tool_call","server":"machina","tool":"machina_status","result":{"content":[{"type":"text","text":"Machina status\npostgres: PostgreSQL 16.15 ..."}]},"status":"completed"}}
{"type":"item.completed","item":{"id":"item_2","type":"agent_message","text":"MCPX-CODEX-9F2A"}}
{"type":"turn.completed","usage":{"input_tokens":48761,"cached_input_tokens":36224,"output_tokens":115}}
```

Codex discovered the `machina` MCP server from the registration above,
called `machina_status` over stdio, received the real status payload, and
returned the proof token. No auth blocker; no interactive step needed.

## 4. OpenCode — record existing registration, mutate nothing — `06_enable_opencode_record.txt`

```
$ .venv/bin/python -m machina interop enable opencode
  already registered in /Users/shadow/machina/opencode.json; no changes made
`machina` is already registered for OpenCode (project scope); no changes made.
  ✓ verify: present in `opencode mcp list` (connected)
```

The idempotency short-circuit fired (config pre-check), the CLI was only
re-listed for verification, and `opencode.json` still holds the original
`mcp.servers.machina` entry verbatim (`git diff` empty — the file is also
gitignored by design).

Final state (`07_status_after.txt` + `07_state_after.json`):

```
│ OpenCode │ yes │ opencode v2.0.26  │ yes │ project │ yes │
│ Codex    │ yes │ codex-cli 0.162.1 │ yes │ global  │ yes │
│ Hermes   │ yes │ Hermes Agent v... │ yes │ no      │ no  │
```

## 5. Hermes — enable/disable proven against an isolated `HERMES_HOME`

Hermes' MCP client config is `mcp_servers:` in `$HERMES_HOME/config.yaml`
(user-level), written by `hermes mcp add|remove`. Its `add` is
discovery-first: it probes the server, then asks one enable-all question;
`remove` asks one confirm. The module drives the CLI headlessly (a newline
answers both prompts with their defaults) — **the user's live `~/.hermes`
was NOT modified**; the proof ran with `HERMES_HOME=/tmp/hermes-interop-verify`
and an isolated `MACHINA_INTEROP_STATE_FILE` (both cleaned up):

- `08_hermes_enable_temp_home.txt` — enable: probe found the 3 Machina
  tools, saved `mcp_servers.machina` (command + args + `enabled: true`),
  verified via `hermes mcp list` → `✓ enabled`.
- `09_hermes_disable_temp_home.txt` — status shows `Hermes … global … yes`,
  disable removes the entry, verify: "absent from `mcp list` and config file".

## 6. CLI surface & manual path — `10_cli_surface_and_manual_path.txt`

- `machina interop --help` lists `status | enable | disable`; `machina --help`
  lists the `interop` command.
- Bad platform → exit 2; failed action → exit 1; success → 0.
- `machina interop disable opencode` (OpenCode v2.0.26 has no
  `mcp remove` subcommand) → honest manual report naming both config files,
  exit 1, no mutation.

## 7. State file

Path: `~/.machina/interop.json` (override `MACHINA_INTEROP_STATE_FILE`).
Atomic write (temp + `os.replace`), mode `0600`. Schema:

```json
{
  "<platform>": {
    "enabled": true,
    "scope": "project|global",
    "registered_at": "ISO-8601 or null",
    "verify": {"ok": true, "detail": "how it was verified"},
    "binary": "/path/to/platform/binary or null",
    "version": "version string or null"
  }
}
```

## 8. Honesty notes

- The Codex token round trip is the only end-to-end AI turn in this proof;
  the token `MCPX-CODEX-9F2A` was returned by the model after the real
  `mcp_tool_call` above (single attempt, exit 0).
- Hermes was exercised against an isolated home; its live registration was
  intentionally left untouched. OpenCode's registration predates this wave
  and was left byte-identical.
- All subprocess output captured here is bounded/ANSI-free by the module;
  no credentials or auth files were read or written.
