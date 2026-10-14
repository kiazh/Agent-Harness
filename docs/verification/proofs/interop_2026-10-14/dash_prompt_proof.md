# Second interop run — dash-leading prompt through the N1-fixed shim (2026-10-14)

Stage-2 verification of fix **N1** (`machina/mcp_server/__init__.py`):
`_chat_command` now emits `… chat -q [flags] -- <prompt>`, so a dash-leading
prompt is delivered as the chat message instead of being parsed by click as a
CLI option. Before the fix, `-i` was consumed as the interactive flag
(`--interactive -i` in the CLI help) and `--help` returned usage text with
exit 0 — both recorded in the round-2 findings.

## Provenance

- Repo: `/Users/shadow/machina` · git HEAD `217f8c16e0877f65ac4fc24475106048157d0174`
  (working tree + Stage-2 shim/docs changes; suite and proof commands in the
  Stage-2 completion-bundle entry)
- Host: OpenCode v2.0.26, project-registered `machina` MCP server
  (`opencode mcp list` → `✓ machina connected`)

## Command (real `opencode run` → MCP shim → `machina chat`)

```console
$ opencode run --standalone --auto --format json \
    "Use the machina_chat tool exactly once. Pass it this prompt: -i Then return the machina_chat tool output."
```

## Observed

- OpenCode exit code: `0`; stderr empty (`opencode_run_dash_prompt_stderr.log`).
- Tool call: `machina.machina_chat` with `input: {"prompt": "-i"}` — raw JSON
  in `opencode_run_dash_prompt.log`.
- Result (raw, excerpt):

```json
{"status": "completed", "session_id": "63ead83a-92ba-4cfa-bb05-0ee5f2c876b1",
 "reply": "The query `-i` isn't something I can act on — it looks like a stray flag or a truncated command rather than a task.\n\nWhat would you like me to do? If you meant a specific command, paste it in full and I'll take it from there.",
 "truncated": false}
```

The reply is a real assistant answer *about* `-i`: the prompt reached
`machina chat` as the message. A pre-fix run would have hit the interactive
flag instead (no session id, no reply).

## Database verification (`dash_prompt_db_check.txt`)

Queried the repo venv python against the Machina PostgreSQL instance,
decoding `context_chunks.payload_msgpack` for the session above:

```json
{
 "ok": true,
 "session_id": "63ead83a-92ba-4cfa-bb05-0ee5f2c876b1",
 "chunk_count": 2,
 "user_message_contains_dash_i": true,
 "chunks": [
  {"chunk_type": "user_message", "payload": {"content": "-i"}},
  {"chunk_type": "assistant_message", "payload": {"content": "The query `-i` isn't something I can act on …"}}
 ]
}
```

## Pre-fix argv shape (root cause, `pre_fix_argv_shape_evidence.txt`)

`machina chat --help -q` (the old shim's dispatch for a `--help` prompt)
prints the CLI usage and exits `0` without running a turn — the recorded
round-2 symptom. The same capture shows `--interactive -i` in the option
table, the flag that used to swallow an `-i` prompt.

## Files

| file | content |
| --- | --- |
| `opencode_run_dash_prompt.log` | Raw `--format json` output of the dash-prompt run |
| `opencode_run_dash_prompt_stderr.log` | stderr of the run (empty; exit 0) |
| `dash_prompt_db_check.txt` | DB-level proof: `user_message` chunk `{"content": "-i"}` |
| `pre_fix_argv_shape_evidence.txt` | CLI parse evidence of the pre-fix dispatch shape |
| `new_argv_parse_check.txt` | Fixed shape `-q --session … -- hi` parses (flags before `--`; bad session rejected, exit 1) |

Unit pins for the fix live in `tests/test_mcp_server.py`
(`test_chat_dash_prompt_follows_double_dash_terminator`, plus the updated
success/resume argv-shape assertions; pre-fix RED verified against
`git show HEAD:machina/mcp_server/__init__.py`).
