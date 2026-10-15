# Final review — providers + config slice (line-by-line)

- Date: 2026-10-15
- Mode: READ-ONLY. No source edits. This file is the only artifact written.
- Scope: `machina/core/provider/*.py` (sse, direct, anthropic, chatgpt, copilot,
  openrouter, ollama, rate_limit, catalog, transport, factory, specs, base,
  caches, audit, reasoning) + `machina/core/config.py`,
  `machina/core/runtime.py`, `machina/core/metrics.py`.
- Method: full read of every listed file; callsite verification by grep; no
  runtime execution. Synthetic secrets in the test corpus ignored by design.
- Skills loaded: `code-reviewer`.

Verdict: **4 confirmed findings (1×P2, 3×P3)**; no P1. Cache-key, rate-limit
binding, and SSE `event:`/`[DONE]`/CRLF handling are otherwise correct. All
five streaming sites route through the shared parser.

---

## P2-1 — `runtime._detect_providers()` reports `provider=anthropic` as "unknown provider" → `chat_provider` unavailable

**Where.** `machina/core/runtime.py:298-387`.

**Evidence (line-by-line).** The readiness ladder is:

- `runtime.py:315` `if selected == "openrouter"` …
- `runtime.py:318` `elif selected == "custom"` …
- `runtime.py:327` `elif selected == "chatgpt"` …
- `runtime.py:342` `elif selected == "copilot"` …
- `runtime.py:356` `elif selected == "ollama"` …
- `runtime.py:359` `elif selected in PROVIDER_SPECS` ← API-key vendors
- `runtime.py:363` `else: readiness = _named_provider_readiness(selected)` →
  `runtime.py:366` `reason = f"unknown provider {selected!r}"`.

There is **no `anthropic` arm**, and `anthropic` is **not** a key of
`PROVIDER_SPECS` (`specs.py:45-130` lists openai, deepseek, xai, groq, together,
mistral, google, opencode, kimi — no anthropic). So for
`selected == "anthropic"` the flow reaches `runtime.py:363-368`, where
`_named_provider_readiness("anthropic")` (`runtime.py:414-441`) calls
`get_provider_entry("anthropic")` → `None` (the user store has no such entry) →
returns `None` → `key_ok=False`, reason `"unknown provider 'anthropic'"` →
`runtime.py:381-384` returns `chat_provider="unavailable"`.

**Callsite/contract verification.**
- `anthropic` is a first-class provider: `factory.py:112-115` builds
  `AnthropicProvider(model=model or AnthropicProvider.DEFAULT_MODEL, ...)`, and
  the transport is keyed by its own env var — `anthropic.py:65-67`
  (`config.get("anthropic_api_key")` → `ValidationError("ANTHROPIC_API_KEY not set")`).
- The selected-provider contract is explicit at `runtime.py:299-303`
  ("the SELECTED provider's key") and every other API-key vendor gets a branch;
  `google`/`kimi`/`opencode` are covered by the `in PROVIDER_SPECS` arm.
- `anthropic` is *not* a removed flow: `tests/test_subscription_removals.py:42`
  `REMOVED_PROVIDERS = ("claude", "gemini")`, and the module docstring
  (`tests/test_subscription_removals.py:13-14`) states "``provider=anthropic``
  (``ANTHROPIC_API_KEY``) are untouched by this wave."
- User-visible impact: `runtime.py:515-525` derives
  `Capability("chat_provider", "unavailable", "unknown provider 'anthropic' (selected: anthropic)", …)`;
  `services.py:675` surfaces `"chatProvider": runtime.get("chat_provider")`; and
  `tools/e2e_gate.py:2528-2539` asserts
  `payload.get("chat_provider") == "ready" and chat_provider_selected == selected`
  — this check fails for an anthropic-configured install even with a valid key.

**Falsification attempt.** Searched for an `anthropic` arm or a `PROVIDER_SPECS`
entry: `grep -n "PROVIDER_NAME"` found only anthropic/chatgpt/copilot class
constants; `specs.py` PROVIDER_SPECS has no `"anthropic"` key; `runtime.py:315-368`
has no anthropic branch. A user could only make this pass by storing a
user-added provider literally named `anthropic` in the security store (which
would then shadow the built-in). Finding holds.

**Severity.** P2 — not a data-loss bug, but a first-class provider's readiness is
misreported in status and in the readiness gate.

---

## P3-1 — `_is_free_model` is provider-agnostic: direct transports inherit OpenRouter's "free" retry/quota policy

**Where.** `machina/core/provider/transport.py:34-36`, `:129-132`, `:140-146`,
`:173-175`; consumed by `direct.py:136` and `openrouter.py:179`.

**Evidence.**
- `transport.py:34-36` `_is_free_model` matches `openrouter/free`, `*:free`,
  `*/free` for **any** model string.
- `transport.py:129-132`: `free = _is_free_model(model); max_retries = 0 if free else _MAX_RETRIES`.
  The rationale (`transport.py:125-128`, `:131`) is "the caller walks the
  fallback chain instead" — but that fallback chain only exists in
  `openrouter.py:166-171` (`_FREE_FALLBACK_MODELS` rotation).
- `transport.py:140-146`: when free **and** `_quota_exhausted(resp)`, it raises
  `RateLimitError(_quota_message(...))`, whose text is hardcoded OpenRouter
  (`transport.py:79-83`: "OpenRouter free-tier daily quota exhausted … purchase
  credits"). `_stream_with_retry` mirrors this at `transport.py:181-187`.
- `_post_with_retry`/`_stream_with_retry` are shared: `direct.py:136`
  (`DirectProvider.complete`) and `direct.py:251` (`stream_complete`) call them.
  Any `ProviderSpec` model id ending `:free`/`/free` (e.g. a `custom` endpoint,
  a named provider, opencode/kimi id) therefore gets **one attempt, no retry**,
  and a vendor that happens to emit `X-RateLimit-Remaining: 0` yields the
  misleading OpenRouter daily-quota `RateLimitError`, which
  `machina.core.agent._is_rate_limit_error` treats as terminal (agent must not
  retry 429s — see `transport.py:22-27` comment).

**Falsification attempt.** Checked whether the free policy is gated per
provider: `grep _is_free_model` shows only `transport.py` (policy) and
`openrouter.py` (fallback list); `direct.py` imports the transport helpers but
never passes a provider flag. So a `DirectProvider(spec, model="x:free")` really
does set `max_retries=0`. A paid-id `DirectProvider` gets `_MAX_RETRIES=2`.
Finding holds. Practical impact is low (wrong retry budget / wrong error text on
an unusual model id), hence P3.

---

## P3-2 — `rate_limit_calls_per_minute = 0` ("disables throttling") is unreachable through config

**Where.** `machina/core/provider/rate_limit.py:19`, `:31-32`, `:46-50`, `:61`;
`machina/core/config.py:358`, `:368-372`.

**Evidence.**
- `rate_limit.py:19` and `:31-32` document/introduce the escape hatch:
  "Configurable via LLM_RATE_LIMIT_CALLS_PER_MINUTE … A zero configured limit
  disables throttling" with `if self.rate == 0: return`.
- `rate_limit.py:46-50` `_build_rate_limiter` rejects only `< 0`, explicitly
  allowing `0`.
- Every construction path reads the config key: `rate_limit.py:61`
  (`_get_rate_limiter`) and `rate_limit.py:85-88` (`reload_rate_limiters`), both
  through `config.get("rate_limit_calls_per_minute")`.
- But `config.py:358` bounds the key at `(1, None)`; `_check_int_bounds`
  (`config.py:368-372`) raises `ValueError` for `0`. `Config.get` routes env/
  override/file values through `validate_value` (`config.py:494-514`), and the
  only remaining source, the dataclass field, is also validated on load
  (`config.py:611-616`). So `rate == 0` cannot be produced from the supported
  config surface: the documented disable switch is dead code.

**Falsification attempt.** Looked for an unvalidated write path
(`_session_overrides`, direct attribute assignment, `Config.load`): all three
flow through `validate_value`. Looked for a direct `AsyncTokenBucket(...)`
construction in production: none (tests only). Finding holds. P3 (doc/dead
branch; behavior is only "you cannot disable throttling via 0", not a
correctness break).

---

## P3-3 — `audit._sanitize_value`'s `metadata()` pass raises `AttributeError` on non-string nested dict keys

**Where.** `machina/core/provider/audit.py:198-208` (vs `:158-172`, `:77`).

**Evidence.** `audit.metadata()` recurses into nested dicts and filters keys with
`key.lower()`:

```
audit.py:199-205
    if isinstance(value, dict):
        return {
            key: metadata(item)
            for key, item in value.items()
            if key.lower() not in protected
            and not key.lower().endswith(("_content", "_preview", "_payload"))
        }
```

`key` is any dict key, not necessarily `str`; `metadata` is the recursion seam
for arbitrarily structured audit values (`audit_log(event_type, **kwargs)` →
`_sanitize_value(metadata(kwargs))`, `audit.py:210`). A nested mapping with an
int/None/tuple key (e.g. `audit_log("x", data={1: "y"})`) raises
`AttributeError: 'int' object has no attribute 'lower'` out of `audit_log`,
which is called on error paths (`direct.py:146`, `ollama.py:149`, and many
others) and would mask the original outcome.

**Contrast (author intent).** The sibling helpers already defend against
non-string keys: `_is_sensitive_audit_key` (`audit.py:158-159`) and
`_is_credential_param` (`audit.py:122-123`) both use `str(key).lower()`, and
`_sanitize_value`'s dict branch (`audit.py:75-79`) calls
`_is_sensitive_audit_key(k)` (safe). Only `metadata()` uses the raw
`key.lower()`.

**Falsification attempt.** Searched for a *current* production callsite that can
pass non-string nested keys. `memory/approval.py:205-211` passes
`redactions` — a `list[str]` (`memory/redaction.py:46`,
`RedactionResult.redactions: list[str]`); `rag/pipeline.py:250-258` passes
strings; `audit_log(**kwargs)` keys are always `str`. So no confirmed live
trigger was found: the defect is latent, contingent on any future structured
payload with a non-str mapping key. Reported as P3, not P2.

---

## P3-4 — Shared SSE parser does not strip a leading UTF-8 BOM (spec-required)

**Where.** `machina/core/provider/sse.py:20-36`; consumers
`anthropic.py:419-426`, `direct.py:253-254`, `openrouter.py:422-423`,
`copilot.py:287-288`, `chatgpt.py:543-544`.

**Evidence.** `sse_field` recognizes only `"<field>:"` and `"<field>"`;
`startswith(field + ":")` fails for a line prefixed by U+FEFF. The SSE spec
(WHATWG HTML, "event stream interpretation") requires that a single leading BOM
be ignored. The parser's docstring enumerates its tolerances (optional single
space, bare field name, comments/other fields skipped) but omits BOM
(`sse.py:6-12`).

The wire layer does not strip it either: httpx decodes with
`TextDecoder(encoding=self.encoding or "utf-8")` and a `LineDecoder`
(`.venv/lib/python3.14/site-packages/httpx/_models.py:1015`, `:1028-1035`;
`_decoders.py` `class TextDecoder` / `class LineDecoder`), which handle CRLF and
chunk-split lines but leave U+FEFF intact. So a BOM survives onto the first
line.

Impact by site: on the OpenAI-compatible sites (`direct`/`openrouter`/`copilot`/
`chatgpt`) at worst the first frame is dropped; on Anthropic the **first
`event:` line** is dropped, and since `content_block_start`/`content_block_delta`/
`message_start` handling is keyed on `event_type` (`anthropic.py:443-508`) with no
`data.get("type")` fallback for those events, a BOM-prefixed
`event: message_start` yields an empty completion.

**Falsification attempt.** Reasoned on the parser: `sse_data("\ufeffdata: {}")`
→ `startswith("data:")` is False, `line == "data"` is False → returns `None`;
no caller (grep `startswith(`, `removeprefix(` in `provider/`) strips BOM. Real
vendors (OpenAI/Anthropic/OpenRouter/Copilot) do not emit BOMs, so the trigger is
a proxy/gateway, not the canonical endpoints — hence P3, not higher.

---

## Q — Notes / residual (no action required, recorded for completeness)

- **Q1 (cache-key symmetry).** `direct.py:79-88` scopes the response cache by
  `spec.name : base_url : sha256(api_key)[:16] : model`, and `ollama.py:57-65`
  scopes by endpoint — the round-3/round-5 fixes. `anthropic.py:272-278`
  (`f"anthropic:{model}"`) and `openrouter.py:138-144` (`f"openrouter:{model}"`)
  do **not** add endpoint/credential scope. No runtime hole today: both
  transports hardcode a single `BASE_URL` (`anthropic.py:52`, `openrouter.py:65`)
  and neither exposes a base-url override. Flagged only as a hardening
  asymmetry (the "Claude subscription subclass" comment at `anthropic.py:55-57`
  is stale — no subclass exists; `tests/test_subscription_removals.py:42,105-109`
  confirms `ClaudeProvider` was removed).
- **Q2 (documented identity residual, N4).** `caches.py:20-25` documents that
  callers without `session_id`/`agent_id` share entries; `_caller_identity`
  (`caches.py:31-39`) returns `None` for both-unset. `usage.complete_call`
  (`usage.py:106-110`) forwards only `messages`/`tools`/`max_tokens`/kwargs, so
  the main agent path is identity-free by design. Consistent with the docstring;
  not a defect.
- **Q3 (effort vocabulary split).** `reasoning.normalize_reasoning_effort`
  (`reasoning.py:29-40`) accepts only `REASONING_EFFORTS` (low/medium/high),
  while config accepts `+ REASONING_CATALOG_EFFORTS` (minimal/xhigh/max,
  `config.py:37-39`, `:295-307`). The `reasoning.py:26-29` comment claims the
  vocabulary is "single-sourced in machina.core.config", which is imprecise (the
  catalog tier set is not imported). Safe in practice: the config path degrades
  (caught at `reasoning.py:49-61`), the gateway rejects catalog tiers for
  non-chatgpt (`gateway/features/providers_feature.py:198-208`), and the
  factory `reasoning_effort` parameter has **no production caller**
  (`grep get_provider(` → `gateway/server.py:160`, `services.py:169`,
  `agent.py:284`, `agent_factory.py:137` all pass provider/model only). One
  coupling footgun: a chatgpt-tier effort stored globally is validated against
  whatever model the next call resolves (`chatgpt.py:401-413`,
  `catalog.validate_effort`), so switching to a model lacking that tier raises
  rather than degrading — intentional per `chatgpt.py` docstring.
- **Q4 (catalog account fallback).** `chatgpt._account_hint()` returning `""`
  (`chatgpt.py:439-449`) sends `validate_model_slug`/`supported_efforts_for`
  through the unknown-account path → `_most_recent_live` (`catalog.py:382-397`,
  `:454-465`), i.e. another account's freshest list. Documented R6-1.
- **Q5 (SSE multi-line `data:`).** Per SSE, multiple `data:` lines in one event
  must be joined with `\n` before dispatch. All five sites parse each `data:`
  line independently and `continue` on `JSONDecodeError`
  (`direct.py:260-263`, `openrouter.py:429-432`, `copilot.py:297-300`,
  `chatgpt.py:550-553`, `anthropic.py:429-432`), so a payload split across lines
  would lose content while a later single-line terminal frame still sets
  `saw_terminal` → possibly a truncated answer reported as done. Not triggerable
  by the canonical vendors (they emit single-line JSON); latent P3-grade only if
  a gateway splits frames. Recorded as Q, not counted as a finding.
- **Q6 (tool-arg guard).** `direct.py:294-298` / `openrouter.py:468-474` do
  `tc["arguments"] += frag` after stringifying only `dict` fragments; a numeric
  `arguments` value would `TypeError`. Vendors send strings; latent.
- **Q7 (metric label).** `usage.complete_call` labels every `DirectProvider`
  family `"direct"` (`usage.py:85-86`), while the providers' own reserve path
  uses `self.spec.name` (`direct.py:131`). Outside this slice; no budget impact
  (reservation is scope-based, not label-based).

## Verified-correct (called out because the task asked)

- **SSE `event:`/`data:` parsing (anthropic).** Event detection order is event
  first, then data (`anthropic.py:420-426`); `sse_event` returns `None` on a
  `data:` line, so the branches cannot cross-talk. `message_stop` is caught both
  as `event:` (`:423-424`) and as a `data.type` (`:508-509`). Correct.
- **`[DONE]` variants.** All sites compare `value.strip() == "[DONE]"`
  (`direct.py:257`, `openrouter.py:426,534`, `copilot.py:291-294`,
  `chatgpt.py:547-548`), so `data:[DONE]`, `data: [DONE]`, and trailing CR/space
  all match; the shared parser strips exactly one leading space (`sse.py:31-32`).
- **CRLF / chunk boundaries.** Handled upstream by httpx `LineDecoder`
  (buffers partial lines, defers a trailing `\r`) — checked in
  `.venv/lib/python3.14/site-packages/httpx/_decoders.py`.
- **NDJSON (ollama).** Correctly bypasses SSE: `json.loads(line)` per line,
  benign keep-alive lines skipped, terminal requires `done: true`
  (`ollama.py:301-317`, `:341-350`). No `[DONE]` assumption. Correct.
- **Rate-limit binding under reload / multi-provider.** `RateLimitBinding`
  resolves the registry on each access (`rate_limit.py:107-116`), so
  `reload_rate_limiters` reaches live instances; keys are per provider
  (`direct.py:65`, `anthropic.py:70`, `openrouter.py:83`, `ollama.py:47`,
  `chatgpt.py:264`, `copilot.py:146`); replacement (not in-place mutation)
  avoids mid-acquire parameter races (`rate_limit.py:66-88`). Correct.
- **Error classification / retries.** Paid retries 2× with `Retry-After`
  honored (`transport.py:86-115`); free adds an explicit account-wide
  quota fail-fast (`transport.py:140-146`, `:181-187`); truncation raises a
  typed error at every site (`transport.py:211-219` and each provider's
  post-loop guard). Correct.
- **Audit URL redaction.** `_sanitize_value` runs token/DB patterns before
  `redact_url_credentials` (`audit.py:70-74`), preserves already-redacted
  markers (`:127-133`, `:136-142`), and the userinfo regex correctly excludes
  ports/IPv6 by requiring `@` (`:99`, comment `:95-98`) while allowing embedded
  `@` in passwords (greedy `[^/\s?#]*`). Correct.
- **Config coercion.** `validate_value` rejects NaN/inf, out-of-range ints/
  floats, and unknown enums before any mutation (`config.py:270-349`,
  `:352-383`); `Config.set` rolls back the attribute if a persisted save fails
  (`config.py:536-543`); `Config.load` isolates per-key failures
  (`config.py:611-624`). Correct.
- **Runtime ordering.** `startup` DB → schema → audit → skills → probes
  (`runtime.py:99-141`); `shutdown` bounded join → connectors → audit stop →
  background drain → embedders → DB close, with cancellation preserved and
  flags reset in `finally` (`runtime.py:189-286`). Correct.

## Method / limits

- Every line of the listed files was read post-fix (sse.py is new).
- No runtime execution was performed; the BOM/multi-line/regex claims are
  derived by direct reading of the parser and httpx decoder source, and each
  finding includes an explicit falsification attempt.
- The test corpus was not treated as evidence (synthetic secrets by design).
