# Model catalog + user-added providers — live proofs (2026-10-14)

Two features, one lane:

- **A — Dynamic ChatGPT/Codex model catalog with per-model reasoning efforts.**
  No hardcoded model lists: the `chatgpt` provider discovers the real
  per-account catalog at runtime and validates slugs/efforts against each
  model's own entry.
- **B — User-addable providers.** `add provider (name + base URL + key)` →
  pull `GET {base}/models` → show/use the pulled models; keys go through the
  repo's existing `.env` secret idiom.

Everything below was produced on this machine on 2026-10-14.

## Verified catalog source (Phase A research)

Working references (read, not guessed):

1. `/Users/shadow/.hermes/hermes-agent` — `hermes_cli/codex_models.py`,
   `agent/model_metadata.py`, `agent/codex_headers.py`,
   `agent/reasoning_effort.py`. Its live path:
   `GET https://chatgpt.com/backend-api/codex/models?client_version=<v>`
   with `Authorization: Bearer <access_token>` plus `ChatGPT-Account-ID`
   (without the account header the endpoint answers `{"models": []}` with
   HTTP 200).
2. Installed Codex CLI 0.162.1 (`codex app-server generate-json-schema`):
   the app-server protocol treats `ReasoningEffort` as an opaque non-empty
   string "advertised by the model" — no enum to copy. The CLI's own cache
   (`~/.codex/models_cache.json`, written by the CLI today) and the embedded
   fallback strings in the binary confirm the same payload shape.
3. Direct probe with Machina's own ChatGPT auth
   (`machina/security/chatgpt_auth.py`, source `machina`, not expired):
   both `client_version=99.0.0` and `=0.0.0` answered **HTTP 200**; the raw
   responses are saved here:
   - `chatgpt_models_cv_99.0.0.json` — 9 entries (7 visible, 2 hidden)
   - `chatgpt_models_cv_0.0.0.json` — the frozen 6-entry legacy subset
   No token material is stored anywhere in this directory.

### Real per-account catalog observed (client_version=99.0.0)

| slug | visibility | default | supported efforts (wire-valid) | ctx | max ctx |
|---|---|---|---|---|---|
| gpt-6.1-sol | list | low | low/medium/high/xhigh/max | 500000 | 872000 |
| gpt-6-astra | list | low | low/medium/high/xhigh/max | 272000 | 872000 |
| gpt-6-sol | list | medium | low/medium/high/xhigh/max | 272000 | 872000 |
| gpt-6-luna | list | medium | low/medium/high/xhigh/max | 272000 | 872000 |
| gpt-5.6-sol | list | low | low/medium/high/xhigh/max | 272000 | 872000 |
| gpt-5.6-terra | list | medium | low/medium/high/xhigh/max | 272000 | 872000 |
| gpt-5.6-luna | list | medium | low/medium/high/xhigh/max | 272000 | 872000 |
| gpt-reserve | hide | medium | (hidden; parsed but never displayed) | 272000 | 872000 |
| codex-auto-review | hide | medium | (hidden) | 272000 | 872000 |

The catalog ALSO advertises `ultra` ("Maximum reasoning with automatic task
delegation") for 6.1-sol / 6-astra / 6-sol / 5.6-sol / 5.6-terra. That is a
Codex-client tier, not a Responses wire value: probed live, the wire answers
**HTTP 400** `Invalid value: 'ultra'. Supported values are: 'none',
'minimal', 'low', 'medium', 'high', 'xhigh', and 'max'` (the same 400 appears
even on models whose catalog advertises it). Machina therefore exposes
`catalog entry ∩ wire vocabulary` and rejects `ultra` with a typed error that
names the supported set and says why. A control probe with `high` on the same
model returned a normal 200 stream (`OK`).

### Fallback decision

Live discovery > Codex CLI's local `models_cache.json` (read-only) > a
bundled snapshot of exactly the 7 visible entries verified live on
2026-10-14 (with their then-current efforts/windows). The bundled snapshot is
used only when no live/cache data exists (offline first run); it is
date-stamped in the code and live data always wins. Strict slug rejection
only happens against **fresh live** data, so a dated snapshot can never
block a model the account gains later — the backend stays the final
authority.

## Provider-store design + key handling (Phase B)

- Metadata store: `~/.machina/providers.json` (override
  `MACHINA_PROVIDERS_FILE`), **0600**, atomic (mkstemp + fsync +
  `os.replace` + chmod), parent dir 0700. It contains `{name, base_url,
  api_key_env, models[], models_fetched_at, default_model, created_at}` —
  **never key material**. Names are `^[a-z][a-z0-9-]{0,31}$`; built-in
  provider names are reserved.
- Keys: the same mechanism `/keys` uses — `machina/security/env_file.py`
  writes `MACHINA_PROVIDER_<NAME>_API_KEY` into the git-ignored `.env`
  (0600, atomic) and the live process env; resolution at call time goes
  through `machina/security/secrets.py` (environment → `*_FILE` mount →
  Vault/AWS), so mounted/backend credentials work too. Keyless local
  endpoints (127.0.0.1/localhost/::1) are a supported mode.
- Registry/factory: named providers are selectable everywhere a built-in
  provider is (gateway `config.set provider`, `session.setModel`, API
  routes) because `PROVIDERS` is now a registry whose membership consults
  the store dynamically. `get_provider(name)` builds the same
  OpenAI-compatible transport as `custom`, defaulting to the first pulled
  model.
- Readiness is honest: `ready` (reachable, key set or keyless local),
  `no key` (remote without credential), `unreachable` (bounded TCP probe).

## Surfaces added

- Gateway RPC: `models.list` (live catalog + `source`/`fetchError`),
  `models.setEffort` (per-model validation), `providers.list/add/remove`.
- TUI: `/models` (uses the real catalog when available, curated fallback
  otherwise), `/models catalog` (table with efforts + context),
  `/provider list|add|remove` (interactive add with masked key prompt and
  confirm), `/effort` now validates per model (low/medium/high for built-ins;
  the model's own set for chatgpt).
- CLI: `machina providers list|add|remove|models [provider]` —
  `machina providers models chatgpt` prints the live catalog with efforts.
  No separate top-level `machina models` was added (the `providers` sub-app
  keeps it uncluttered).

## Live proof results

1. `proof_model_catalog_live.py` → `proof_model_catalog_live.log` +
   `catalog_normalized_live.json`:
   - 9 entries / 7 visible, all slugs/efforts/windows above
   - `xhigh` accepted; `ultra` → `ValidationError: ... supported: low,
     medium, high, xhigh, max`
   - gateway `models.list` → `source=live, error=None, models=7`
   - gateway `models.setEffort xhigh/ultra` → applied / typed rejection.
2. `named_provider_live.log` + `named_provider_completion.json`
   (Hermes loopback proxy at `127.0.0.1:8645/v1`, started with
   `hermes proxy start --provider nous`, stopped afterwards):
   - `machina providers add hermes-live http://127.0.0.1:8645/v1 --no-key`
     → 423 models pulled (`hermes_models_live.json` is the raw 200 response)
   - `machina providers list` shows `hermes-live · user · keyless local ·
     423`
   - runtime readiness: `ready`, reason `no key (local endpoint)`, selected
     `hermes-live`
   - real completion through the factory-resolved provider: content exactly
     `MACHINA-NAMED-PROOF-OK`, usage 29/55/84, `keyless: true`
   - `machina providers remove hermes-live --yes` → 0 user providers remain.

## Tests

- New: `tests/test_model_catalog.py` (parse of the REAL fixture, TTL +
  negative TTL, fetch fallback order, per-model effort/slug validation,
  codex-cache + bundled fallback, windows),
  `tests/test_provider_store.py` (validation, 0600 atomic writes, `.env` key
  idiom, model cache, dynamic registry, factory resolution, runtime
  readiness), `tests/test_gateway_providers.py` (all five RPC handlers,
  including honest fetch-failure fallbacks), UI tests in
  `ui/test/features.test.ts` (catalog table rendering, catalog picker,
  provider list/add/remove flows, /effort routing).
- Extended: `tests/test_chatgpt_provider.py` (catalog-backed slug/effort
  enforcement, xhigh/max on the wire, config-key validation),
  `tests/test_context_windows.py` (chatgpt catalog windows,
  provider-scoped), `tests/conftest.py` (catalog cache isolation).
- Fixture provenance: `tests/fixtures/model_catalog_chatgpt.json` is a
  trimmed copy of the REAL `cv_99.0.0` response (4 of 9 rows kept, hidden
  ones included); no synthetic shapes.

Ratchet lines (delta-table procedure followed in
`tests/test_architecture_layers.py`):

- provider package total 3578 → 4200 (+622: catalog.py +508, chatgpt.py
  +26, factory.py +47, specs.py +41)
- per-file ceilings: chatgpt.py 590→616, factory.py 87→134, specs.py
  125→166, new catalog.py=508
- deferred-import census 685 → **689** (+4, itemized: catalog/factory/specs/
  context_windows lazy bindings; all other new imports were hoisted to
  module level)

## Undeterminable on this account / next probes

- Plan-gated slugs beyond this account's entitlement are invisible here;
  `available_in_plans` is captured in the raw proof JSON but not surfaced.
  Next probe for another plan: rerun `proof_model_catalog_live.py` under
  that account.
- The `universal`/`minimal` wire levels are accepted by the endpoint but no
  catalog entry advertises them; Machina maps `none/off/default/auto` to
  "unset" (Machina idiom) and never sends them.
- Hermes `/v1/models` returns no `context_length` for its 423 models
  (`windowed: 0`), so those rows keep the 32K default; a server that
  publishes `context_length`/`max_model_len`/`context_window` values is
  honored automatically.
