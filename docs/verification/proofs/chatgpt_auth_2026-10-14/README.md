# ChatGPT-account auth + provider — live proof (2026-10-14)

**Goal.** Let Machina run turns on a user's ChatGPT subscription through the
Codex CLI protocol, with an auth store it owns (`~/.machina/auth/chatgpt.json`,
0600, atomic) that imports credentials from `~/.codex/auth.json`.

**Verdict.** PASS — three real completions on the ChatGPT subscription through
the new provider: two scripted calls (plain text and tool-call streaming) plus
a full `machina chat` agent turn. All ended in `response.completed`, served by
the machine's ChatGPT account (`x-codex-plan-type: plus`, `x-codex-active-limit:
premium`).

- Repo: `/Users/shadow/machina`, git HEAD `9806bee` (uncommitted lane)
- Runner: `.venv/bin/python docs/verification/proofs/chatgpt_auth_2026-10-14/run_live_proof.py`
  (Python 3.14.5, repo venv; exit code 0)
- Raw output: [`live_completion.log`](live_completion.log)
- CLI turn: [`machina_chat_cli.log`](machina_chat_cli.log)
  (`machina chat --provider chatgpt -m gpt-6.1-sol --mode ask "…"`; exit 0,
  reply `cli-chatgpt-proof-ok`, audit events `llm_stream_start/complete`
  `provider: chatgpt`, `agent_run_complete`, session `c9a12d9c-…`)
- Account signed-in evidence: [`codex_login_status.txt`](codex_login_status.txt)
  (`codex login status` → “Logged in using ChatGPT”, codex-cli 0.162.1)

## Live results (from `live_completion.log`)

| call | input | outcome | usage |
| --- | --- | --- | --- |
| 1 — text | “Reply with exactly one short sentence: machina chatgpt proof ok” | streamed + terminal `response.completed`; content `machina chatgpt proof ok`; model `gpt-6.1-sol` | `{prompt 30, completion 11, total 41}` |
| 2 — tool | “What is the weather in Paris? Call get_weather.” with a `get_weather` function tool | streamed `function_call`; terminal `response.completed` | `{prompt 65, completion 18, total 83}` |

Tool call captured by the provider:

```json
{"id": "call_e7d00ce40ccd4015b42ad7b7bea44004", "type": "function",
 "function": {"name": "get_weather", "arguments": "{\"city\":\"Paris\"}"}}
```

All three live calls returned HTTP 200 from `chatgpt.com` with plan headers
`x-codex-plan-type: plus` and `x-codex-active-limit: premium` — i.e. the
requests were served by the ChatGPT subscription, not the metered API.

The CLI turn (`machina_chat_cli.log`) proves the same path end-to-end through
the harness: `Model: gpt-6.1-sol (chatgpt)` → streamed reply
`cli-chatgpt-proof-ok` → `Iterations: 1 | Tool calls: 0 | Tokens: 4432`,
with the post-turn auxiliary stream also completing on `provider: chatgpt`.

## Wire facts confirmed (and from where)

Endpoint: `POST https://chatgpt.com/backend-api/codex/responses`
(same host/path the Codex CLI and the Hermes reference use; confirmed by the
installed `codex-cli 0.162.1` binary strings and live probes on this machine).

Headers sent by `machina.core.provider.chatgpt.ChatGPTProvider`:

| header | value | source |
| --- | --- | --- |
| `Authorization` | `Bearer <access_token>` | Codex CLI / Hermes `agent/codex_headers.py` |
| `Accept` | `text/event-stream` | Responses SSE route |
| `Content-Type` | `application/json` | — |
| `originator` | `machina` | self-identifying harness header; Hermes uses `hermes-agent` on the same endpoint, both verified live here |
| `User-Agent` | `machina/0.2.0` | same |
| `chatgpt-account-id` | JWT claim `https://api.openai.com/auth` → `chatgpt_account_id` | Hermes `agent/codex_headers.py`, codex-rs `auth.rs` spelling |
| `session_id` | fresh UUID per request | Codex CLI (`session_id` header) |

`OpenAI-Beta` is **not** sent: it belongs to the Codex CLI's websocket
transport (`responses_websockets=2026-02-06` in the binary), and the HTTP SSE
route accepted requests without it.

Request body: Responses shape — `model`, `instructions` (from system
messages), `input` items (`input_text` / `output_text` / `function_call` /
`function_call_output`), `store: false`, `stream: true`, and `reasoning.effort`
only when explicitly set. Confirmed live: `temperature` is rejected by the
backend (`HTTP 400 Unsupported parameter: temperature`), so the transport
validates it for interface parity but never puts it on the wire;
`max_output_tokens` is accepted but omitted, matching the Codex CLI (whose
answer length is model-managed).

Stream: SSE events `response.output_text.delta` / `.done`,
`response.function_call_arguments.delta` / `.done`,
`response.output_item.added` / `.done`, and the terminal `response.completed`
(carries `model` + `usage {input_tokens, output_tokens, total_tokens}`).
Event names cross-checked against Hermes `agent/codex_runtime.py`
(`_CodexResponseAssembler`) and the codex-cli binary, then observed live.

## Credential path exercised

1. `chatgpt_auth.resolve_tokens()` found no `~/.machina/auth/chatgpt.json`.
2. `import_from_codex()` read `~/.codex/auth.json` (read-only) and wrote the
   Machina store: `drwx------ ~/.machina/auth`, `-rw------- chatgpt.json`
   (verified with `stat -f "%Sp"`).
3. The provider resolved the account id from the access token JWT and sent it
   as `chatgpt-account-id`.
4. `status()` reports `{"signed_in": true, "source": "machina", "expired":
   false, "account_id": "fa769bce-…"}` — expiry comes from the access token's
   `exp` claim (≈8.7 days out on this machine), so no refresh was needed. The
   401→refresh→retry-once path is covered by hermetic MockTransport tests
   (`tests/test_chatgpt_provider.py`).

## Redaction

No token material is present in any artifact. The runner's request hook prints
headers with `authorization` replaced by `Bearer <redacted>` (2 occurrences in
the log); the provider/auth modules never log or embed token values in errors.
A prefix scan of every artifact for the machine's access/refresh/id tokens
found **0** matches. `account_id` and request ids are included as evidence and
are not credentials.

## Reproduce

```sh
cd /Users/shadow/machina
codex login status                    # expect: Logged in using ChatGPT
.venv/bin/python docs/verification/proofs/chatgpt_auth_2026-10-14/run_live_proof.py
# exit 0, appends/overwrites live_completion.log with the evidence above
```

## Notes / next probes

- Default model is `gpt-6.1-sol` (Codex catalog priority 1 on this machine);
  any Codex catalog slug works (`gpt-6-astra`, `gpt-5.6-sol`, …) and
  `chatgpt/<slug>` prefixes are stripped. `models_cache.json` listing is the
  source of truth for the account's menu.
- Context-window budgeting for these slugs currently falls back to the
  curated/default window in `machina/core/context_windows.py` (32k); adding
  catalog windows there is a follow-up owned by that file's lane.
- `machina auth login` is deferred (CLI lane); the spec is reported in the
  lane summary.

## Sign-in surface follow-up (CLI + gateway + TUI, added 2026-10-14)

The user-facing sign-in surface for the same credentials is proven separately:
[`signin_surface.md`](signin_surface.md) covers `machina auth
login|status|logout`, the `auth.chatgpt.*` gateway methods with the
`auth.chatgpt.completed` / `auth.chatgpt.failed` events, and the TUI `/auth`
command (live CLI artifacts included; the browser exchange is the noted
manual step).
