# Code review — round 3 — 2026-10-14

Reviewed commit `d3adcf9`, including changes since the earlier review at `1f002b5`. Read-only application review; no application source was changed. Findings below are based on source inspection and targeted reproductions, using synthetic credentials, mocked HTTP transports, temporary files, and the dedicated `machina_test` database where needed. No real login or model inference was performed.

## Findings

### 1. [P1] Protect the actual ChatGPT token-store path from ordinary workspace reads

Locations: `machina/permissions/policy.py:111`, `machina/security/chatgpt_auth.py:66`.

The new store defaults to `<machina_home>/auth/chatgpt.json`, with an environment override. The shared sensitive-path predicate does not recognize that path. When the configured workspace contains this store, `read_file('auth/chatgpt.json')` is classified as a trusted project read in ask mode and returns its refresh token without credential approval. The stored token values also remain in the result after `redact_secrets`, so redaction does not contain the exposure.

Reproduction: `auth-security.py` sets both workspace and Machina home to a temporary directory, saves synthetic tokens through production `save_tokens`, and exercises the actual permission broker and file tool. Observed `READ_DECISION allowed`, `READ_RETURNS_REFRESH True`, and `REDACTION_STILL_CONTAINS_REFRESH True`.

Protect the resolved credential-store identity across file/search/terminal policy, including configured overrides, and register token values with secret redaction at credential boundaries. This finding requires workspace/store overlap; it does not assert that every installation permits this read.

### 2. [P2] Invalidate pending authentication work on logout

Locations: `machina/gateway/features/auth_chatgpt.py:306`, `machina/gateway/features/auth_chatgpt.py:238`, `machina/security/chatgpt_auth.py:428`.

Logout deletes the file but leaves `_PENDING` alive. A later callback still exchanges the code and recreates credentials, even after gateway closure. An already running refresh can likewise save its response after logout removes the store.

Reproduction: `auth-lifecycle.py` uses the actual loopback callback listener and a fake exchange. Logout returns `removed=true, signedIn=false`; pending work survives logout and gateway close; a subsequent callback recreates the fixture file and emits `auth.chatgpt.completed`. `auth-security.py` separately holds a mocked refresh response until after store deletion and observes `STORE_RESURRECTED True`.

Cancel pending login flows and fence all credential publication with an authentication generation or equivalent durable state. Cancelling an asyncio task alone does not stop a worker thread already performing an exchange.

### 3. [P2] Serialize rotating refresh-token exchanges

Location: `machina/security/chatgpt_auth.py:346`.

Peer adoption checks the stored token before the network call, but there is no lock covering read, exchange, and save. Two concurrent sessions can both submit the same rotating refresh token before either saves its replacement.

Reproduction: `auth-security.py` runs two real refresh calls against MockTransport with a barrier: two requests contain the same old refresh token; one succeeds and the other returns `invalid_grant`. Serialize by store across concurrent callers, reread after acquiring the lock, and coordinate publication with logout.

### 4. [P2] Scope catalog readers to the current account and endpoint

Location: `machina/core/provider/catalog.py:388` and `:428`.

Live entries are keyed by account and endpoint, but `cached_catalog()` and `live_models_or_none()` return the first nonempty cache entry without identifying the active account. After account A then account B are fetched, validation and context/effort lookup can continue using A. Logout does not clear that state.

Reproduction: `catalog-transport.py` populates two disjoint synthetic catalogs. Both account entries exist, but `cached_catalog()` returns only A and `validate_model_slug('only-B')` rejects B's available model. Thread the current account/endpoint through readers and invalidate stale identity state on account changes.

### 5. [P2] Translate image content blocks into Responses image inputs

Location: `machina/core/provider/chatgpt.py:342`.

The user-message converter applies `str(content)` and wraps it in `input_text`. A standard multimodal content list containing text and `image_url` therefore becomes a Python representation containing the image URL or base64 bytes; the request contains no `input_image` block. Image attachments lose their image semantics and may become a large text prompt.

Reproduction: `catalog-transport.py` supplies text plus a synthetic image data URL to the real payload builder. The sole outgoing content type is `input_text`, whose text contains the serialized `image_url` structure. Convert each supported content block to its Responses equivalent.

### 6. [P2] Include endpoint identity in response-cache keys for named providers

Locations: `machina/core/provider/direct.py:77`, `machina/security/providers.py:225`.

A named provider can be updated to another base URL while retaining its name and model. The response-cache namespace uses name/model but omits endpoint and credential identity. An identical request within the 60-second TTL can therefore return an answer from the previous endpoint without contacting the newly selected endpoint.

Reproduction: `catalog-transport.py` creates two real DirectProvider instances with the same name/model and different mocked hosts. Only the first host receives HTTP, and both calls return its response. Include endpoint and credential generation in cache identity or invalidate affected entries on provider changes.

### 7. [P2] Make automatic skill publication's daily cap atomic

Locations: `machina/skills/learning.py:402`, `machina/skills/learning.py:431`.

The per-agent daily cap is checked before publication's transaction without a shared lock or reservation. Concurrent reviews from separate sessions can both observe available capacity and publish beyond the configured limit.

Reproduction: `skill-cap.py` uses real test-database transactions and a temporary registry, with fake completion and a barrier after the genuine cap checks. With cap 1, both reviews are approved, the durable auto-applied count is 2, and two learned skills are live. Its own rows are cleaned up. Serialize or reserve capacity by agent and recheck inside the publication transaction.

### 8. [P2] Preserve qualified embedding model IDs when targeting OpenRouter

Locations: `machina/rag/embedder.py:597`, `machina/rag/embedder.py:757`.

The API-prefix fix now routes `openai/text-embedding-3-small` to an API backend, but `_strip_api_provider()` removes `openai/` regardless of destination. With only an OpenRouter key configured, the request goes to OpenRouter with `model: text-embedding-3-small`, losing the qualified identifier used by that API.

Reproduction: `embedding.py` captures the actual request through MockTransport. OpenRouter's [embedding request documentation](https://openrouter.ai/docs/api_reference/embeddings) uses `openai/text-embedding-3-small`. Normalize according to the selected endpoint rather than stripping every recognized provider prefix.

### 9. [P2] Terminate overflowing child processes while waiting for pipe readers

Location: `machina/core/child_process.py:310`.

The stdout reader sets `exceeded`, but the supervisor only checks cancellation while joining pipe-reader threads. It checks overflow after those waits. A child can exceed the output cap and continue running until stderr closes or the deadline fires.

Reproduction: `runner.py` invokes the production MCP runner with a child that flushes 65,536 characters against the 64,000-character cap, waits 350 ms, and writes a temporary marker. The marker exists; the call returns success after roughly 0.73 seconds with `truncated=True`, `timed_out=False`. Observe overflow during the wait or signal termination immediately from the reader.

### 10. [P2] Resolve model commands against the resumed session

Locations: `machina/gateway/features/providers_feature.py:115`, `:141`, `ui/src/features/models.ts:86`, `ui/src/features/settings.ts:419`.

`models.list` defaults to `gw.provider`, and `models.setEffort` validates against `gw.provider/gw.model`. Resuming a session pinned to a different route does not change those gateway defaults, while UI requests omit session identity. Commands can display another provider's catalog and reject valid effort levels for the active session.

Reproduction: `session-config.py` exercises production session resume with a mocked session record pinned to ChatGPT while gateway defaults remain OpenRouter. Resume returns the pinned ChatGPT route; model listing returns OpenRouter's empty catalog; `max` effort is rejected using OpenRouter's allowed levels. Pass session identity and resolve its pinned route.

### 11. [P2] Normalize the advertised `off` reasoning-effort option

Location: `machina/gateway/features/providers_feature.py:150`.

Outside ChatGPT, validation accepts only empty/low/medium/high. The UI's advertised off option submits literal `off`, which is rejected instead of clearing the setting. `session-config.py` confirms this for OpenRouter. Normalize unset aliases before provider-specific validation.

### 12. [P2] Preserve active turn and tool identities when returning to a session

Locations: `ui/src/app.ts:385`, `ui/src/tail.ts:65`.

Switching back replays buffered live events as historical entries. That conversion loses assistant turn identity and converts a running tool into an ordinary system notice. Subsequent completion events cannot reconcile those entries with live state.

Reproduction: `ui-tail.ts` runs the real App with a fake terminal/client: begin a turn, switch away, receive text and tool start, return, then receive successful tool and message completion. The final transcript contains the assistant text twice, retains `terminal running…`, and omits the completed tool result. Restore active blocks with their IDs and reconciliation state.

## Confirmed earlier fixes

- A failed skill-publication COMMIT no longer leaves a discoverable skill.
- CLI ownership loss now cancels execution and completes generator cleanup while ownership remains held, before database closure.
- Qualified API embedding IDs avoid the previous ONNX misrouting; endpoint-specific ID preservation remains finding 8.
- Numeric pilot gold values now score correctly against equivalent string predictions.

## Verification and limits

- Ruff lint passed; Ruff formatting check passed for 233 files; offline lockfile validation passed for 130 packages.
- UI typecheck/lint passed; 404 UI tests passed, with 6 integration tests skipped.
- The first broad backend run encountered two database deadlocks while concurrent review work was using the test database. Both cases passed when rerun alone.
- A subsequent serial backend run finished with **4032 passed, 2 failed, 1 skipped, 9 deselected, 1 xfailed**. Failures were `test_sdk_remote_call_runs_through_agent_approval_pipeline[boom-arguments2-boom-failed]` and `test_mutation_auth_failure_never_replays[401]`. These failures were not investigated further after the user requested faster source-focused review; they are not represented as confirmed additional defects above.
- Live model behavior, real OAuth accounts, and production-database behavior were not exercised. This is a source and targeted-reproduction review, not a claim of exhaustive correctness.

Reproduction scripts are in [the companion evidence directory](proofs/codebase_review_round3_2026-10-14/README.md).
