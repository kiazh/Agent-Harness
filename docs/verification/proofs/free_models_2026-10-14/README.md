# Free-model routing proof — 2026-10-14

Lane: generic OpenAI-compatible provider (`--provider custom`) + OpenCode Zen
preset (`--provider opencode`). All commands below were run on 2026-10-14 from
`/Users/shadow/machina`; raw outputs are saved next to this file. No API keys,
tokens, or other secrets are recorded here.

## Part A — OpenCode Zen base-URL probe (live)

Two candidate bases were probed per the lane note (V2 docs name
`https://opencode.ai/inference/v1`; Hermes uses
`https://opencode.ai/zen/v1`).

```console
$ curl -sS -m 15 -o models_inference_v1.json -w "%{http_code}" https://opencode.ai/inference/v1/models
200
$ curl -sS -m 15 -o models_zen_v1.json -w "%{http_code}" https://opencode.ai/zen/v1/models
200
$ diff -q models_inference_v1.json models_zen_v1.json
# identical
```

- **Both bases are live and byte-identical** (`probe_models.txt`,
  `models_inference_v1.json`, `models_zen_v1.json`): HTTP 200, `object:list`,
  **85 models** including the free set (`big-pickle`, `cat-jam-free`,
  `mimo-v2.6-flash-free`, `nemotron-3.5-lightning-free`, `jev-1.13-free`,
  `step-5-preview-free`, `longcat-2.5-preview-free`, `space-bunny-free`).
- **Chosen default**: `https://opencode.ai/inference/v1` (the V2-docs base).
  The preset is overridable via `opencode_base_url` / `OPENCODE_BASE_URL`
  (e.g. to `https://opencode.ai/zen/v1`), which is covered by tests.
- **Unauthenticated/placeholder-key chat completions are gated**:

```console
$ curl -sS -o unauth_chat_completion.json -w "%{http_code}" -X POST https://opencode.ai/inference/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"big-pickle","messages":[{"role":"user","content":"hi"}],"max_tokens":5}'
403
{"type":"error","error":{"type":"FreeTierError","message":"OpenCode's free tier can only be used from within OpenCode. Download it at https://opencode.ai and keep building."}}
```

The same 403 comes back with a placeholder bearer
(`invalid_key_chat_completion.json`) — the gateway only honors genuine console
keys. **Blocker**: no `OPENCODE_API_KEY` exists on this machine (env checked;
auth files deliberately not scraped). **One-step fix**: create a key at the
OpenCode console, then:

```console
$ export OPENCODE_API_KEY=...            # or add to .env
$ machina chat --provider opencode -m big-pickle "..."
```

Machina-side resolution is covered by tests
(`tests/test_provider_custom.py::test_opencode_*`): spec base URL, default
model `big-pickle`, key requirement, and the status honesty path
(`_detect_providers` -> ready only with a key, otherwise
`OPENCODE_API_KEY not set`).

## Part B — Hermes local endpoint: LIVE completion through the generic provider

Hermes (installed under `/Users/shadow/.hermes`) ships an **OpenAI-compatible
loopback proxy** (`hermes proxy`, `hermes_cli/proxy/`): allowed paths
`/chat/completions`, `/completions`, `/embeddings`, `/models`; it attaches the
operator's credential to any request that reaches the loopback bind. Its
`nous` adapter reported ready (`hermes proxy status`).

```console
$ /Users/shadow/.hermes/hermes-agent/.hermes/bin/hermes proxy start --provider nous --port 8645
Starting Hermes proxy for Nous Portal
  Listening on:  http://127.0.0.1:8645/v1
$ curl -sS http://127.0.0.1:8645/v1/models   # HTTP 200, 423 models (hermes_models.json)
```

Machina's **generic `custom` provider** was pointed at it keyless
(`CUSTOM_BASE_URL=http://127.0.0.1:8645/v1`, no key) and ran a real
non-streaming completion plus a real streamed completion:

```console
$ .venv/bin/python docs/verification/proofs/free_models_2026-10-14/proof_custom_live.py
```

Result (`hermes_completion.json`): `ok: true`, model
`stepfun/step-5-preview:free` (the id Hermes itself is configured with;
the catalog's credit-gated `stepfun/step-5-preview` variant 404s — see
`hermes_catalog_model_rejection.txt`), both paths returned
**`HERMES-PROOF-OK`** with real usage `{prompt 23, completion 50, total 73}`
and a `done` event on the stream. The script asserts the keyless path: no
resolved key, no `Authorization` header on the wire client.

The proxy process was stopped after the proof; port 8645 is free again.

## Verification summary

- `pytest tests/test_provider_custom.py tests/test_providers_direct.py tests/test_fm2_provider.py -q` — green.
- `pytest tests/test_fm2_provider.py tests/test_providers_direct.py tests/test_architecture_layers.py tests/test_benchmark_tool.py -q` — green (ratchet delta table updated for this lane: provider package 3511 -> 3578, +67).
- `ruff check` + `ruff format --check` on every touched file — clean.
- Artifacts: `probe_models.txt`, `models_inference_v1.json`, `models_zen_v1.json`,
  `unauth_chat_completion.json`, `invalid_key_chat_completion.json`,
  `hermes_models.json`, `hermes_completion.json`,
  `hermes_catalog_model_rejection.txt`, `proof_custom_live.py`.
