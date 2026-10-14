# Interop proof — OpenCode ↔ Machina MCP server shim (2026-10-14)

Phase-1 acceptance for the Machina MCP server shim
(`machina/mcp_server/`, package entry `python -m machina.mcp_server`).

OpenCode v2.0.26 was pointed at the shim over stdio, listed its tools, and ran
one real `machina_chat` turn whose reply token was verified in the Machina
PostgreSQL session rows.

## 1. Registration

```console
$ opencode mcp add machina -- /Users/shadow/machina/.venv/bin/python -m machina.mcp_server
MCP server "machina" added to /Users/shadow/machina/opencode.json

$ opencode mcp list
✓ machina              connected
✓ mcp-server-linkedin  connected
```

Configuration written (project scope, `opencode.json`):

```json
{
  "mcp": {
    "servers": {
      "machina": {
        "type": "local",
        "command": ["/Users/shadow/machina/.venv/bin/python", "-m", "machina.mcp_server"]
      }
    }
  }
}
```

OpenCode discovered the server's instructions and the three tools
(`machina_status`, `machina_chat`, `machina_session_create`) in the client's
tool catalog immediately after registration.

Note on CLI syntax: `opencode run` in v2.0.26 takes the message as a positional
argument; `--prompt` is not a flag (it prints usage). The equivalent, verified
form used below is `opencode run --standalone --auto --format json "<message>"`.

## 2. Round trip (final run, clean reply)

Command (run from `/Users/shadow/machina`):

```console
$ opencode run --standalone --auto --format json \
    "Use the machina_chat tool exactly once. Pass it this prompt: Reply with exactly INTEROP-9C4A7E and nothing else. Then return the machina_chat tool output."
```

Observed (raw JSONL excerpt, `opencode_run_raw.log`):

```json
{
  "type": "tool_use",
  "part": {
    "tool": "execute",
    "state": {
      "status": "completed",
      "input": {"code": "const res = await tools.machina.machina_chat({ prompt: \"Reply with exactly INTEROP-9C4A7E and nothing else.\" });\nreturn res;"},
      "output": "{\n  \"result\": \"{\\\"status\\\": \\\"completed\\\", \\\"session_id\\\": \\\"ac839bd9-8c72-41d2-976a-9849c2102d5f\\\", \\\"reply\\\": \\\"INTEROP-9C4A7E\\\", \\\"truncated\\\": false}\"\n}"
    }
  }
}
```

- OpenCode exit code: `0`
- Machina session id: `ac839bd9-8c72-41d2-976a-9849c2102d5f`
- Reply: `INTEROP-9C4A7E`

### Database verification (`repo_db_token_check.txt`)

Queried with the repo venv python against the Machina PostgreSQL instance,
decoding `context_chunks.payload_msgpack`:

```json
{
 "ok": true,
 "sessions": [
  {"token": "INTEROP-9C4A7E", "session_id": "ac839bd9-8c72-41d2-976a-9849c2102d5f",
   "chunks": 2, "token_in_chunks": ["user_message", "assistant_message"]}
 ]
}
```

## 3. First attempt (kept for the record)

The first run (`opencode_run_raw_first_attempt.log`, token `INTEROP-2E1D4B`,
session `4dea2599-bf4f-4e7b-bc1b-cd286f72ceb3`) succeeded end-to-end but
exposed a defect: the reply field contained `machina chat` stderr chatter
(audit lines, "Skipping skill" warnings) because the runner merged stderr into
stdout. Fix landed before the final run: the runner now reads stdout and
stderr separately (stdout = the reply; stderr capped for diagnostics only).
The same DB check confirms the token for that session too.

## Files

| file | content |
| --- | --- |
| `opencode_run_raw.log` | Raw `--format json` output of the final run |
| `opencode_run_stderr.log` | stderr of the final run (empty; exit 0) |
| `opencode_run_raw_first_attempt.log` | Raw output of the first (pre-fix) run |
| `repo_db_token_check.txt` | DB-level token verification for both sessions |
| `tools_sanity_output.txt` | Live `machina_status` + `machina_session_create` via the stdio server |
| `j9_deferred.patch` | Additive j9 journey patch (not applied; file was dirty) |
| `j9_from_patch_validation.txt` | j9 executed from the patched copy: PASS |

Unit + stdio handshake proof lives in `tests/test_mcp_server.py`
(19 tests, includes a real `python -m machina.mcp_server` client-SDK handshake).
The repeatable end-to-end journey is `j9 opencode-mcp` in `tools/e2e_gate.py`.

## j9 (e2e gate journey) — DEFERRED

`tools/e2e_gate.py` was **dirty** in the working tree during this build (another
lane removing inert `# noqa: BLE001` comments), so per the lane rule the file
was not touched. The complete additive patch is saved as
`j9_deferred.patch` and verified:

- `git apply --check docs/verification/proofs/interop_2026-10-14/j9_deferred.patch`
  → clean (147 insertions, 1 deletion, applies against the current working tree).
- The patched file is ruff-clean (`ruff check` + `ruff format --check` with the
  repo config).
- The journey itself was executed from the patched copy (without touching the
  shared file or its proof outputs) and **passed**, see
  `j9_from_patch_validation.txt`:

```console
STATUS: PASS
PASS opencode_run_exit_zero            {"exit_code": 0, "timed_out": false}
PASS machina_tool_call_present         {"matched": true}   # machina.machina_chat
PASS opencode_output_contains_token    {"token": "MCPJ9-942408", "matched": true}
PASS session_row_contains_token        {"session_id": "9ec23976-4f30-480d-8c12-b04e81701b62",
                                        "chunk_type": "assistant_message"}
```

j9 skips cleanly when `opencode` is absent or the project has no registered
`machina` MCP server (same skip convention as j4); budget 420s; session proof
is a real Machina `context_chunks` payload check, never OpenCode storage.

Apply once the noqa lane lands:

```sh
git apply docs/verification/proofs/interop_2026-10-14/j9_deferred.patch
```

