# CAT — model catalogues (Copilot default refresh), 2026-10-15

Lane: model catalogues. Sole writer scope: `machina/core/provider/copilot.py`
(DEFAULT_MODEL), `machina/core/context_windows.py` (Copilot/OpenAI family rows),
`ui/src/features/models.ts` (Copilot fallback row, if any), affected tests, one
new evidence test file. No commits.

## 1. Verdict

`CopilotProvider.DEFAULT_MODEL` was `"gpt-4o"` — an id that the account's live
menu only still lists as the **last** entry of a trailing legacy block. It is now
`"gpt-6.1-sol"`, the newest stable non-preview family in the live-fetched cache.

The user's claim is confirmed: the live Copilot catalogue still *contains*
`gpt-4o` (legacy tail), but it is no longer a sensible default.

## 2. Discovery — installed Hermes agent's caches (read-only)

`hermes status` lists the providers: **Nous Portal, GitHub Copilot, DeepSeek**
(`CAT_hermes_status.txt`). `~/.hermes/config.yaml` has `model.provider: nous`,
so no Copilot default is configured there.

Caches read (file + key):

| Cache file | Key read | Copilot content |
| --- | --- | --- |
| `~/.hermes/provider_models_cache.json` | `["copilot"]["models"]` | **53 ids** (see below) |
| `~/.hermes/cache/model_catalog.json` | `["providers"]` | `openrouter`, `nous` only — no Copilot |
| `~/.hermes/cache/nous_recommended_cache.json` | `["https://portal.nousresearch.com"]` | Nous Portal recommendations — no Copilot |
| `~/.hermes/context_length_cache.yaml` | `context_lengths` | one row (`stepfun/step-5-preview:free`) — no Copilot |

The Copilot row (`provider_models_cache.json#copilot`): `fp=5fe8335c327c656e`,
written `2026-10-15T15:58:12Z`, **no `fallback` flag** (so it is a real live
fetch, not Hermes' curated outage placeholder). Full snapshot captured in
`CAT_hermes_copilot_cache.json`.

Recorded id order (index → id):

```
 0 claude-fable-5.1          18 gpt-5.4-mini-free-auto   36 gpt-5-mini
 1 claude-fable-5            19 gpt-5.4-mini            37 gpt-4o-mini-2024-07-18
 2 claude-haiku-5.5          20 gpt-5.4-nano            38 gpt-4o-2024-11-20
 3 claude-opus-4.8-fast      21 gpt-5.4                 39 gpt-4o-2024-08-06
 4 claude-opus-4.8           22 gpt-5.5                 40 claude-haiku-4.5
 5 claude-opus-5.5           23 gpt-5.6-luna            41 gpt-4.1-2025-04-14
 6 claude-opus-5             24 gpt-5.6-sol             42 gpt-5.6-luna-free-auto
 7 claude-sonnet-5.5         25 gpt-5.6-terra           43 gpt-3.5-turbo-0613
 8 claude-sonnet-5           26 gpt-6.1-sol  <-- pick   44 gpt-4
 9 copilot-search-a          27 gpt-6-astra             45 gpt-4-0613
10 copilot-search-b          28 gpt-6-luna              46 gpt-4-0125-preview
11 copilot-search-c          29 gpt-6-sol               47 gpt-4o-2024-05-13
12 exec-agent-a              30 kimi-k3-base            48 gpt-4-o-preview
13 exec-agent-b              31 kimi-k3-copilot         49 gpt-4.1
14 exec-agent-c              32 kimi-k3                 50 gpt-3.5-turbo
15 gemini-3.7-flash          33 mai-code-1.1-flash      51 gpt-4o-mini
16 gemini-3.8-flash          34 mai-code-1-flash-4th    52 gpt-4o  <-- old default
17 gemini-4-argon            35 trajectory-compaction
```

Indices 0–35 are the current menu block; 36–52 are a trailing dated/legacy block
(`gpt-5-mini`…`gpt-4o`) — `gpt-4o` is dead last.

### Why `gpt-6.1-sol`

No cache carries a Copilot "default"/"recommended" marker (the only `default:
true` rows are OpenRouter/Nous `z-ai/glm-5.2`). Per the fallback rule, take the
**newest stable family the cache lists**: the GPT-6 generation, and within it the
`.1` point release → `gpt-6.1-sol` (single, non-preview member of the 6.1 line).

Corroboration (not a dependency):

* Machina's own ChatGPT-account snapshot (`provider/catalog.py`
  `VERIFIED_FALLBACK_MODELS`, live-verified 2026-10-14) lists `gpt-6.1-sol`
  **first**; `provider/chatgpt.py` already sets `DEFAULT_MODEL = "gpt-6.1-sol"`.
* Task brief: "verified: gpt-6.1-sol … gpt-5.6-luna".
* `opencode models` (captured in `CAT_opencode_models.txt`) prints a flat
  `provider/model` list and carries **no** Copilot rows — corroborates the
  catalogue shape only.

## 3. Context length — no new entry (deliberate)

`machina/core/context_windows.py` gets **no** new Copilot row, because none of
the named Hermes caches carries a Copilot context length: Copilot's per-model
`max_prompt_tokens` is fetched from the live `/models` response and kept only in
Hermes' in-process `get_copilot_model_context` cache (1 h TTL) — it is never
written to disk. The one length-bearing Hermes cache, `~/.hermes/models_dev_cache.json`,
gives `gpt-6.1-sol` `limit.context = 1_050_000`, which **contradicts** Machina's
own live-verified ChatGPT snapshot (`catalog.py`: `context_window = 500_000` for
the same id). Pinning either value would be a guess, so the conservative default
(`DEFAULT_CONTEXT_WINDOW = 32_000`, unchanged for Copilot) is retained.

The existing `"gpt-4o-mini": 128_000` row is **kept**: `gpt-4o-mini` is still in
the live Copilot set (index 51) and remains the OpenAI-direct default in
`specs.py`, so the entry is still valid.

## 4. `ui/src/features/models.ts` — no change

`CURATED_MODELS` has **no Copilot row** (providers: openrouter, openai,
anthropic, chatgpt, google, mistral, groq, together, deepseek, xai, opencode,
kimi, ollama). Nothing pins a Copilot id, so no fallback row was added or edited.
`settings.ts` mentions `copilot` only as a provider name, not a model id.

## 5. `specs.py` OpenAI API provider — left as-is (noted)

`machina/core/provider/specs.py:50` keeps `default_model="gpt-4o-mini"` for the
OpenAI **API** provider. No OpenAI API key exists on this machine, and the task
explicitly says not to invent an API id; left untouched and recorded here.

## 6. Change set

* `machina/core/provider/copilot.py:134` — `DEFAULT_MODEL`: `"gpt-4o"` →
  `"gpt-6.1-sol"` (+ inline comment; file stays 431 lines, within the
  `test_architecture_layers` ceiling).
* `tests/test_copilot_provider.py` — helper default and wire-body assertion
  `gpt-4o` → `gpt-6.1-sol`; factory prefix-strip test → `copilot/gpt-6.1-sol`.
* `tests/test_r5_011_013_provider_transport.py` — Copilot SSE fixture id →
  `gpt-6.1-sol`.
* `tests/test_model_catalog_copilot_2026_10_15.py` — **new** RED-style evidence
  pin (discovery date 2026-10-15), embeds the recorded current-menu block and
  cites the cache file+key.

## 7. Verification

* `CAT_focused_tests.log` — 234 passed (copilot provider/auth, oauth CLI auth,
  context windows, new evidence test, model catalog, gateway providers, provider
  store, direct providers, architecture layers, chatgpt provider, context audit,
  r5 transport).
* `CAT_ruff.log` — `ruff check`: All checks passed; `ruff format --check`:
  4 files already formatted.
* `CAT_red_before_fix.log` — pre-fix `HEAD` pins `"gpt-4o"`; asserting
  `DEFAULT_MODEL == "gpt-4o"` against the current module fails
  (`current=gpt-6.1-sol`) → the new pin was RED before the change.
* `CAT_opencode_models.txt`, `CAT_hermes_status.txt`, `CAT_hermes_copilot_cache.json`
  — raw corroboration.

## 8. How Hermes and OpenCode expose catalogues

* Hermes resolves a per-provider catalogue through `provider_model_ids()`:
  per-provider live fetchers probe the vendor's `/v1/models` (Copilot uses
  `_copilot_catalog` → `_fetch_github_models` against `api.githubcopilot.com/models`),
  falling back to a static curated list only on outage.
* Results are persisted to `$HERMES_HOME/provider_models_cache.json`, keyed by
  the provider slug and a credential fingerprint, with a 1 h TTL, a 7 d
  stale-while-revalidate window, and a 60 s TTL for curated fallback rows.
* `hermes model --refresh` / `/model --refresh` call
  `clear_provider_models_cache()`, deleting the provider's cache row so the next
  lookup re-probes live; only non-empty results are cached.
* OpenCode exposes its own catalogue via the `opencode models` CLI, which prints
  a flat `provider/model` list (`deepseek/*`, `opencode/*`; 13 rows here). It
  carries no Copilot rows and no context windows — used here for shape
  corroboration only.
