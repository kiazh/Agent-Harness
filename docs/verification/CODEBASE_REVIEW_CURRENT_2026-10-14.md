# Current-source review — 2026-10-14

Reviewed working tree based on `b1794be8622e44d59636fa1654b59052f7c6ed2b` (`main`), including existing uncommitted source changes. Existing edits and previous review artifacts were preserved. No implementation changes were made.

Tests are excluded at the user's request. No tests or application behavior probes were run. Findings below are supported by source inspection; concurrency interleavings have not been reproduced here. “Confirmed” means that the described code path exists, not that an exploit was demonstrated.

## Coverage and limits

Application inventory: 234 Python/SQL files under `machina` (71,389 lines), 56 source files under `ui/src` (11,128 lines), plus installation and deployment configuration. Counts include comments and source-local utilities. Test files, dependencies, generated files, old proofs, and bundled third-party reference text are outside this review's scope.

This is a broad review, **not a completed line-by-line audit of every application file**. Full or substantial inspection covered API routes and lifecycle; DB connection/migrations; session/turn ownership and scheduler; agent execution, history and consolidation paths; provider transport/cache/rate limiting; memory retrieval, growth and RSI; file/terminal and selected network/repository tools; connector execution/policy/OAuth; credential stores; permission broker/store and policy decisions; MCP server and protocol; skills registry/runtime/workflows/learning/repository installation; SoulSpec; controller; observability; repository map; RAG ingestion/search/chunking; CLI and UI feature flows; installation/deployment; and research evaluation paths.

Remaining incomplete areas include portions of the large agent/orchestrator/runtime modules, provider implementations, connector management, auxiliary tools, UI App/transcript/events/tail/theme/motion, CLI auth/main, and research LoCoMo/benchmark paths. No statement of completeness or absence of further issues is warranted.

Priority: P1 = security boundary/data integrity issue to address first; P2 = material correctness/reliability defect; P3 = lower-impact usability defect. Conditional findings state their prerequisites.

## Findings

### 1. P1 — MCP tool arguments can elevate execution to full host mode

**Locations:** `machina/mcp_server/__init__.py:368`, `:384`; `machina/cli/__init__.py:240`, `:283`, `:293`; `machina/permissions/policy.py:858`.

`machina_chat` accepts model-supplied `mode="full"` and forwards it as CLI `--mode`. The child changes its effective execution default, creates a session, and calls `grant_full_default`. The resulting full session grant passes policy before sensitive-capability checks. An MCP caller can therefore select broader host authority than the operator's configured ask/sandbox mode without a separate Machina approval.

This applies when the MCP tool is exposed to an agent that is not intentionally entrusted with human administrator authority. The server has no operator-configured maximum mode. Fix by keeping authority in server configuration/human control, validating requested modes against a ceiling, and preventing tool arguments from minting full grants.

### 2. P1 — Full-mode consent text contradicts actual sensitive-action policy

**Locations:** `machina/permissions/policy.py:858`; `ui/src/features/mode.ts:56`; `README.md:250`; `SECURITY.md:45`.

The policy explicitly allows a full session grant before `_sensitive_capabilities`, including sensitive operations. The UI tells users that credentials, elevation and destructive system operations still ask, and README states that changing mode does not grant blanket sensitive consent. Users can approve full mode under a materially narrower description than the authority actually granted.

Align the policy and the activation disclosure. If sensitive actions are meant to remain gated, evaluate them before the full-grant fast path; if full mode intentionally bypasses them, disclose that accurately before activation.

### 3. P1, conditional — Atomic file writes can escape a validated parent directory

**Locations:** `machina/tools/file.py:509`, `:523`, `:529`, `:267`, `:289`.

The writer validates an opened target, closes that descriptor, then creates a temporary file and replaces the destination using pathnames. A concurrent local actor can rename the validated workspace subdirectory and replace it with a symlink to an outside directory before temporary-file creation. Both operations then resolve outside the approved workspace. Leaf `O_NOFOLLOW` on the discarded probe does not retain the parent's identity.

Prerequisite: another process can modify a workspace ancestor while a write is in progress. Anchor traversal, temporary creation, and replacement to validated directory descriptors; use platform-specific equivalents where needed.

### 4. P1 — Cancelled file writes can overwrite later edits

**Locations:** `machina/tools/file.py:546`, `:285`, `:289`; `machina/core/agent.py:1033`.

File writes run in `asyncio.to_thread`. Cancelling the awaiting coroutine on timeout/turn cancellation does not stop the worker thread. The agent can mark the operation cancelled and release turn ownership while the worker later performs `os.replace`. On a delayed filesystem, an old turn can overwrite edits from a newer turn or a user.

Keep execution ownership until the worker's effects have finished. Track and join the actual worker operation during cancellation rather than treating cancellation of the await as cancellation of the filesystem write.

### 5. P1 — Batched conversation history does not preserve event order

**Locations:** `machina/core/context.py:263`, `:107`, `:530`; `machina/db/schema.sql:71`; `machina/core/agent.py:1274`.

Batched chunks receive random UUIDs and share the database's transaction-time `created_at`. Retrieval orders ties by UUID, which is stable but unrelated to insertion/event order. A user's message, tool results and final answer in one batch can therefore be replayed out of order. Resumed prompts, exports and memory consolidation consume that ordering.

Persist a durable event ordinal or another ordering key assigned in original event order. UUID tie-breaking alone cannot recover chronology.

### 6. P2 — Final history persistence errors are swallowed and the pending history is lost

**Locations:** `machina/core/agent.py:1324`, `:1339`, `:1704`, `:1722`.

`_flush_pending_chunks` logs insertion errors and returns. The successful completion path then returns its response and discards the local pending list. The comment's promised later retry cannot happen after that return. A transient DB failure can permanently omit executed tools and assistant output from history while the caller sees successful completion.

Make persistence degradation visible and retain failed writes in a durable retry mechanism, or fail the completion appropriately. Do not imply that keeping a local list is sufficient recovery.

### 7. P2 — Scheduled jobs ignore the agent selected in the job

**Locations:** `machina/core/scheduler.py:1085`, `:1099`, `:1105`; `machina/core/agent_factory.py:102`.

For an existing session, `_build_agent` passes the session unchanged to the factory, which selects `session.agent_id`. `job.agent_name` is used only when the session cannot be loaded. A job configured for a restricted specialist on a harness-owned session executes the harness definition/tools instead, while job metadata reports the specialist.

Honor the selected identity while retaining the session's authority restrictions, or reject incompatible job/session identities during creation.

### 8. P2 — The UI can display ask mode while the session has full authority

**Locations:** `ui/src/features/mode.ts:20`; `machina/gateway/features/mode.py:21`; `ui/src/app.ts:293`, `:335` and session-switch paths.

Bare `/mode` requests `mode.get` without a session ID, so the gateway returns the global default rather than current session authority. After `/mode full`, `/mode` can report `ask`, and App copies that result into the footer. Switching sessions also does not refresh the effective mode.

Pass the active session ID for status and refresh authority whenever the active session changes. A permission indicator must reflect the authority governing execution.

### 9. P2 — The installer writes unescaped paths into executable shell code

**Locations:** `install.sh:524`, `:500`.

The generated shim interpolates the absolute checkout path into a double-quoted shell word without escaping it as a literal. A valid installation path containing `$`, backticks, quotes or backslashes is reinterpreted when `machina` launches, producing wrong paths or command execution. Windows PATH registration similarly places `windir` inside PowerShell single quotes without escaping apostrophes, breaking paths such as an `O'Connor` user directory.

Serialize paths using correct shell/PowerShell literal quoting, or pass path data separately from executable code.

### 10. P2 — Multiline menu entries can select a different session for deletion

**Locations:** `ui/src/components/Menu.ts:158`, `:267`, `:149`; `ui/src/features/sessions.ts:219`.

Rendering expands embedded newlines into physical rows, but mouse hit testing assumes one row per item. Session titles accept embedded newlines. Clicking the second line of the first title can check the second session, which the deletion picker then deletes after confirmation. The viewport calculation also budgets items rather than physical rows.

Use the same physical-row-to-item mapping for layout and hit testing, or normalize menu labels to one line.

### 11. P2 — Ollama response cache does not identify the server

**Locations:** `machina/core/provider/ollama.py:40`, `:95`.

Instances accept different `base_url` values, but cache scope includes only model/request/caller identity. Identical calls to different servers within the cache TTL can return the first server's response without contacting the intended endpoint. This also affects an endpoint change while a matching cache entry remains live.

Include the normalized endpoint in cache identity, as the direct provider already does.

### 12. P2 — Streaming parsers discard data fields without a following space

**Locations:** `machina/core/provider/direct.py:253`; `anthropic.py:424`; `chatgpt.py:543`; `copilot.py:287`; `openrouter.py:422`, `:530`.

These parsers recognize only the literal prefix `data: `. An endpoint emitting `data:{...}` or `data:[DONE]` has its events ignored, yielding empty/truncated streams despite sending data. The same narrow parsing is duplicated across providers.

Centralize SSE field parsing, permit the optional space after the colon, and assemble event data consistently.

### 13. P2 — Rate limiter configuration does not update live providers

**Locations:** `machina/core/provider/rate_limit.py:65`, `:75`; `machina/core/provider/direct.py:64` and other provider constructors.

Providers retain bucket objects captured at construction. `reload_rate_limiters` replaces registry entries rather than updating those objects, so live providers retain old settings and newly constructed instances use separate buckets. No production caller of the reload function was found, so normal runtime configuration changes do not trigger even that partial refresh.

Use shared stable bucket identities and update them safely, or resolve the active bucket at request time; connect runtime configuration changes to the refresh path.

### 14. P2 — Public custom-port and IPv6 HTTP requests use the wrong Host authority

**Location:** `machina/security/fetch.py:235`.

TCP connection setup uses the parsed port, but `Host` contains only the hostname. Explicit nondefault ports are omitted and IPv6 literals lack brackets. Servers that validate authority can reject requests, affecting web retrieval/crawling and URL-based imports.

Format Host using the effective hostname/port with IPv6 brackets and correct default-port handling.

### 15. P2 — Cancelling MCP chat leaves the child agent running

**Location:** `machina/mcp_server/__init__.py:474`.

The async handler awaits a synchronous process runner through `to_thread` without a cancellation stop handle. Cancelling the MCP request stops the await, while the worker and agent subprocess can continue modifying files and spending provider budget until completion or the 300-second child deadline.

Retire the process tree explicitly when the request is cancelled, and wait for retirement before declaring the operation stopped.

### 16. P2 — A stale GitHub CLI token import can overwrite newer Copilot authentication

**Locations:** `machina/security/copilot_auth.py:257`, `:268`, `:269`.

The existing-store check happens before subprocess/account work, followed by unconditional token publication. A device login or logout occurring in between can be overwritten by the stale import; the import can recreate a store removed during logout. This path does not use the generation/fingerprint fencing already used by device publication.

Capture and revalidate store generation under the publication lock. Preserve intentional future re-import from `gh` while preventing already-in-flight work from winning over a newer account decision.

### 17. P2 — Removing a provider can erase a replacement provider's credential

**Locations:** `machina/security/providers.py:403`, `:413`, `:419`, `:423`.

Removal releases the provider store lock before deleting its `.env` and process-environment key. A concurrent process can recreate the provider with a fresh key in that gap; the old removal then deletes the replacement credential. The provider remains registered but unusable.

Keep store and credential cleanup in the same serialization boundary or bind cleanup to the credential generation being removed.

### 18. P2 — RAG source credentials can persist in audit events

**Locations:** `machina/rag/pipeline.py:228`, `:231`; `machina/core/provider/audit.py:58`, `:138`, `:147`; `machina/observability/audit.py:88`.

Indexing audits the original source before source redaction. Audit string sanitization handles selected token formats and database URLs, but not HTTP URL userinfo or generic query credentials. A source such as `https://alice:example-passphrase@example.invalid/document` or a URL carrying an unrecognized `api_key` can persist intact in `audit_events`. The log record sanitizer may redact the rendered log line, but it does not modify the separate entry passed to audit persistence.

Apply the shared complete redactor to source metadata before both emission and persistence. Treat URI userinfo and credential query parameters explicitly.

### 19. P2 — Rejected skill updates can partially change the live skill

**Locations:** `machina/skills/registry.py:869`, `:873`, `:875`, `:879`, `:896`.

`update_skill` mutates the shared Skill object one field at a time before validating later fields and before writing the file. A valid description plus invalid content/triggers raises after changing the description in memory. A disk write failure leaves all proposed fields live even though the operation failed and persistence did not succeed. `set_enabled` similarly changes the live flag before the write.

Validate an owned candidate snapshot first, write it atomically, then publish it into the live registry. Rejected operations should leave the previous skill intact.

### 20. P2, conditional — Benchmark copies can modify their original fixture through symlinks

**Locations:** `machina/research/head_to_head.py:265`, `:575`, `:579`.

Benchmark copies preserve symlinks. The editable-source allowlist follows file symlinks and excludes those paths from integrity hashes. A source link into the original fixture or an external directory can therefore let a harness edit the original target, contaminating subsequent runs while the run's integrity check overlooks the change.

This applies to fixtures containing such links; ordinary independent files are unaffected. Reject escaping symlinks or materialize verified content into independent run files.

### 21. P3 — Optional masked API-key input rejects empty submission

**Locations:** `ui/src/features/settings.ts:152`; `ui/src/components/Form.ts:223`.

Provider setup says to leave the API key empty for keyless local servers, but SecretForm rejects every blank Enter submission. Escape happens to fall through as an empty key, making cancellation act as the undocumented way to proceed.

Support an explicit optional secret field or an explicit keyless selection, and keep Escape as cancellation.

### 22. P3 — UI memory commands cannot address older memories, even by full UUID

**Location:** `ui/src/features/memory.ts:91` and the corresponding share path.

Forget/share resolve IDs only against the newest 500 memories, including when the input is a complete UUID. Older valid owned IDs are reported as unavailable despite being directly addressable by the backend. Learning approval/revert has a similar latest-100 lookup restriction.

Send complete IDs directly to ownership-checking backend methods; use bounded prefix lookup only for abbreviated IDs and disclose its scope.

## Additional concerns requiring focused follow-up

1. **Consolidation cursor lock scope:** `machina/core/agent.py:1426` reads the watermark before `machina/memory/consolidator.py:173` takes its advisory lock, and persists the new watermark after that lock is released (`agent.py:1482`). Cross-process workers can use the same stale cursor and may publish checkpoints out of order. Confirm with a controlled interleaving; move cursor read/extraction/checkpoint commit into one serialized operation or use monotonic checkpoint comparison.
2. **Chunk adjacency after recursive splitting:** `machina/rag/chunker.py` recursively emits a long separator-delimited part, clears `current_text`, and can discard the separator before the following part; `_apply_overlap` then concatenates a prior suffix with that next part. Inspect with a long uninterrupted word followed by a short word and a small chunk size: overlap can create text that was never contiguous in the source. Carry source offsets or retained separators through recursive emissions.
3. **Latent embedding-backfill starvation:** `machina/memory/store.py:360` repeatedly selects a bounded NULL-embedding batch without a progress cursor or failure marker. Permanently skipped rows can monopolize the batch. No production caller was found, so this is currently a latent maintenance-method issue.

## Suggested fix order

1. Close MCP authority escalation and align full-mode consent with actual policy.
2. Retain filesystem identity and execution ownership through write/cancellation completion.
3. Preserve durable conversation ordering and make history persistence failures recoverable/visible.
4. Correct scheduled agent identity and session mode indicators.
5. Fence credential publication/removal, fix audit redaction, and retire cancelled MCP children.
6. Resolve installer/menu defects and provider/network interoperability issues.

No fixes, commits, dependency changes, or test changes were made during this review.

---

## Fix status (2026-10-15)

Plan: `docs/verification/FIX_PLAN_ROUND5_2026-10-15.md`. Every lane reproduced its
finding adversarially first (refute-and-stop otherwise; none of the fixed items
were refuted). Evidence under
`docs/verification/proofs/codebase_review_round5_2026-10-15/`.

| # | Finding | Status | Commit |
|---|---|---|---|
| 1 | MCP tool args elevate to full mode | FIXED — operator ceiling (`MCP_MAX_MODE` tighten-only, fail-closed) | 22081dd |
| 2 | Full-mode consent text contradicts policy | FIXED — disclosure rewritten to exact grant semantics (fast path stands, comment-pinned) | 8b50144 (+22081dd) |
| 3 | Atomic writes escape validated dir | FIXED — dir-fd anchored (openat/renameat); real-renamer adversary contained | b169b12 |
| 4 | Cancelled writes overwrite later edits | FIXED — worker joined before ownership release (agent cancel paths already await) | 3f030ae |
| 5 | History batched order not preserved | IN FLIGHT — lane pending; status updated on completion | — |
| 6 | Final flush errors swallowed | FIXED — PB-1 log-and-continue + retention | bceadb4 |
| 7 | Scheduler ignores job agent | FIXED — identity replace, caps retained | 49e592c |
| 8 | UI shows ask while full | FIXED — session-scoped mode status + switch refresh | 8b50144 |
| 9 | Installer unescaped paths | FIXED — shell/PowerShell literal quoting | e40c42d |
| 10 | Menu multiline misclick | FIXED — shared physical-row map | c77d6c0 |
| 11 | Ollama cache not server-scoped | FIXED — endpoint in cache identity | 297dca4 |
| 12 | SSE parsers drop `data:` w/o space | FIXED — one shared space-tolerant parser | 297dca4 |
| 13 | Rate-limit config not live | FIXED — per-access registry resolve | 297dca4 |
| 14 | Host authority ports/IPv6 | FIXED — RFC 9110 authority formatting | 37b8daf |
| 15 | Cancelled MCP chat leaves child | FIXED — stop event + bounded shielded join | 22081dd |
| 16 | Stale gh import overwrites auth | FIXED — generation + snapshot fence | d8aba97 |
| 17 | Provider removal deletes replacement key | FIXED — one serialization boundary | d8aba97 |
| 18 | RAG source credentials in audit | FIXED — shared URL-credential redaction at emission + persistence | bf8dd12 |
| 19 | Rejected skill update mutates live | FIXED — validate→write→publish | 7a0f4df |
| 20 | Benchmark symlink escape | FIXED — fail-closed symlink gates (both copies) | f53476e |
| 21 | Optional masked input rejects blank | FIXED — explicit keyless path | 896d3ac |
| 22 | Memory ops cannot address older IDs | FIXED — full-UUID direct, abbreviations bounded | 896d3ac |
| F1 | Consolidation cursor stale across workers | FIXED — read+extract+checkpoint serialized | e163394 |
| F2 | Chunk overlap fabricates spans | FIXED — source offsets; adjacency-gated overlap | 2c62179 |
| F3 | Backfill starvation (latent) | FIXED — dead method deleted (no callers) | 42dc04f |

Follow-ups recorded while fixing: `/keys` parity test now reads the Python
allowlist (011f1ba); ratchet delta documented (a424c26); R5-A disposition comment
pins the full-grant fast path; `context_chunks.document_source` raw-userinfo
storage flagged as a residual outside lane scope (see R5-N evidence).
