# Unified static audit resolution

Input: `Machina_Unified_Static_Bug_Audit.md`, supplied on 2026-10-15.
Input SHA-256: `fd35f401aa213a37e60cf14bdff205b36c31ada7559c667149d0f18faf61a0e7`.
Implementation baseline: `8f966f9fefdd98c82e5b7089beb82ef7e3ac15d6`.

All 30 tracked entries have a source change or a documentation/contract correction below. This is an implementation ledger with static review evidence, not a claim of 30 executed exploit reproductions or certified runtime fixes. The audit itself distinguishes conditional gaps and contract inconsistencies from confirmed implementation defects; those distinctions remain applicable.

## Constraints and approach

- No tests were added or run, as requested. No test files changed.
- No application startup, runtime probes, database migrations, provider requests, container launches, dependency installation, or network calls were used.
- Findings were checked against the current source, grouped by independent subsystem, implemented, and reviewed across caller/storage/provider boundaries. Independent source review identified and corrected replacement-finalizer and abandoned-build races, quoted dotenv-key removal, and bounded-glob compatibility details before commit.
- Existing operator credential precedence and deliberate session-scoped full authority remain intact. Misleading effective-state feedback and MCP consent promises were corrected.

## Complete finding ledger

| ID | Resolution | Main source |
| --- | --- | --- |
| F-01 | Canonical context-window fields survive fetch/save normalization. | `security/providers.py` |
| F-02 | Ollama streaming retains supplied token counts and resolved model; absent counts remain unknown. | `core/provider/ollama.py` |
| F-03 | Anthropic completion requires the decoded stop payload, not its SSE header alone. | `core/provider/anthropic.py` |
| F-04 | FTS schema/query failures propagate through retrieval's unavailable contract rather than healthy emptiness. | `rag/search.py` |
| F-05 | Usage wrappers attribute shared direct transports to their configured provider identity. | `core/usage.py`, `core/agent.py` |
| F-06 | Maintenance and context views resolve provider-scoped capacity through one effective-session-route helper. | `services.py`, CLI/API/gateway callers |
| F-07 | Both bounded release waits inspect the actual boolean result; rejection cannot produce confirmed-release success. | `core/turn_engine.py` |
| F-08 | Persistence failures are not caught as transaction-interface compatibility and replayed. Signed memory/provenance publication requires a transaction. | `rag/pipeline.py`, `memory/store.py` |
| F-09 | Atomic file writes check `fchmod` capability and use the pathname fallback where required. | `tools/file.py` |
| F-10 | UI gateway stop waits after SIGTERM, escalates to SIGKILL, and rejects if exit remains unconfirmed. Repeated callers share the same outcome. | `ui/src/gateway.ts` |
| F-11 | Glob traversal and recursive-copy inspection bound enumeration before materialization. Listings disclose inspection/result limits. | `tools/file.py`, `permissions/policy.py` |
| F-12 | Explicit embedding-space identity is forwarded into the pending memory record. | `memory/store.py`, `memory/approval.py` |
| F-13 | Catalog provenance distinguishes cached from fetched data; a successful empty refresh replaces obsolete rows. | Gateway and CLI provider catalog handlers |
| F-14 | A missing family root no longer aliases and duplicates the current child. | `ui/src/family.ts` |
| F-15 | Canonical final-text suffixes create a retained assistant block when a tool transition cleared the active block. This hardens the stated conditional handler gap. | `ui/src/transcript.ts` |
| F-16 | Retrieval/result and embedding-verdict cache publication checks an invalidation generation. Old in-flight readers cannot refill invalidated caches. | `rag/pipeline.py` |
| F-17 | Dense and sparse memory SQL apply date eligibility before candidate limits. | `memory/retriever.py`, `memory/store.py` |
| F-18 | Newest-first legacy results pair with the nearest older unmatched same-name call, without crossing explicit IDs or session/agent boundaries. | `core/compression.py` |
| F-19 | A no-row review update raises into artifact compensation instead of returning success with an orphan skill artifact. | `skills/learning.py` |
| F-20 | Kimi uses the existing documented low/high/max integration contract across constructor, per-call/global normalization, gateway validation, and UI hints. Other providers keep their vocabularies. | `core/provider/reasoning.py`, `direct.py`, provider gateway, UI settings |
| F-21 | Runtime-compatible dotenv parsing handles exported, quoted-key, quoted-value, and multiline bindings during management updates/removal. Installer reads use runtime interpolation/environment precedence. | `security/env_file.py`, installer helpers |
| F-22 | Installer filter/parse/write errors stop before replacement; no swallowed read/filter error can replace unrelated configuration with only an update. Secret values travel through stdin rather than subprocess argv. | `tools/install-helpers.sh`, `install.sh` |
| F-23 | Compose forwards Kimi, OpenCode, and custom provider keys/endpoint settings. | `docker-compose.yml` |
| F-24 | POSIX secret-file opens use nonblocking no-follow descriptors before validating regular-file type. | `security/secrets.py` |
| F-25 | Request drain and post-cancel grace are bounded; resistant handlers cannot prevent resource teardown. The gateway entrypoint also avoids `asyncio.run`'s unbounded final cancellation gather. | `gateway/__main__.py` |
| F-26 | Secret-cache invalidation fences old external-backend publications with a generation under the existing lock. | `security/secrets.py` |
| F-27 | Credential management and UI distinguish saved state from effective state/source without exposing values or overriding stronger operator aliases. External resolution runs off the async event loop. | Gateway secrets handler, UI key feature/protocol |
| F-28 | Pending/approved lookup and atomic durable/in-memory execution claims bind the requesting agent as well as session/material. Missing actor identity fails closed. | `permissions/store.py`, `permissions/broker.py` |
| F-29 | Independent transport-driver, source-finalizer, and surviving agent background-task barriers defer provider close and ownership release. Late agent builds attach resources inside their owned task. Foreground cleanup remains bounded and reports pending ownership. | `core/turn_engine.py`, HTTP streaming adapter |
| F-30 | README, security/MCP guidance, and model-facing MCP descriptions consistently disclose the live full-grant exception to per-call exact approval. Deliberate policy is preserved. | `README.md`, `SECURITY.md`, `docs/MCP.md`, `tools/mcp.py` |

Detailed subsystem notes:

- [Provider resolutions](UNIFIED_AUDIT_PROVIDER_RESOLUTIONS.md)
- [Memory and retrieval resolutions](UNIFIED_AUDIT_MEMORY_RESOLUTIONS.md)
- [Security and installer resolutions](UNIFIED_AUDIT_SECURITY_RESOLUTIONS.md)

## Lifecycle details

An HTTP step completing does not establish that the source finalizer it started has completed. The engine retains those barriers separately and rechecks them after every deferred join. A driver's own cleanup may replace its source barrier with a finalizer; the joining caller clears only the barrier it installed, then reevaluates the replacement. Agent background tasks that resist the bounded optional-work join are included in the same retirement decision.

While effects remain live, cleanup reports pending, keeps the provider open, and leaves claim renewal running. Resource close and exact-token release happen after actual retirement. Repeated cancellation is not treated as proof that an effect stopped. On process termination or unavailable lease renewal, external lease expiry/fencing still governs recovery; this source change does not promise uninterrupted ownership during arbitrary infrastructure failure.

Gateway process shutdown uses bounded event-loop-task cancellation after service cleanup. Arbitrarily blocking synchronous code, executor threads that never return, and native process behavior remain distinct concerns; the UI's post-signal confirmation reports failure rather than claiming an unconfirmed process exit.

## Static validation

- Python `ast.parse` on all 33 changed Python modules.
- Ruff on all changed Python modules.
- TypeScript `tsc --noEmit` via `npm run typecheck`.
- ESLint on the changed UI modules.
- `bash -n install.sh tools/install-helpers.sh`.
- `git diff --check` and confirmation that test paths are unchanged.

These checks do not establish live provider compatibility, database transaction behavior, OS-specific semantics, timing-sensitive cancellation behavior, or container startup. None of the test suites was run.
