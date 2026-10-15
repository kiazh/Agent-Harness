# Whole-codebase review — 2026-10-14

Reviewed checkout: `fdb83e5` plus the existing uncommitted changes. This review covers committed application code as well as the working tree; it is not limited to a diff. No application code, tests, index, commits, or branches were changed. This report and its evidence directory are the review deliverables.

**Assessment: fix the confirmed security and execution-ownership issues before release.** There are 16 newly reported findings and four previously reported RAG findings retained below. Existing tests pass, but the focused reproductions expose gaps outside those assertions. No P0 issue was established.

## Verification and scope

| Check | Actual result |
| --- | --- |
| Backend, excluding DB/UI/workflow-trial markers | 3,340 passed, 1 skipped, 1 xfailed, 312 deselected; 203.76 seconds |
| DB selection, excluding UI/workflow-trial markers | 303 passed, 3,351 deselected; 107.69 seconds |
| Combined backend passes across those separate selections | 3,643; these were two runs, not one combined coverage run |
| UI `npm run check`, including real gateway and fake OAuth wire fixtures | 347 passed, zero failed/skipped; TypeScript and ESLint passed |
| Ruff check, `machina/ tools/` | Passed |
| Ruff format check, `machina/ tools/` | Passed; 217 files |
| Bandit, medium/high threshold, excluding CLI as CI does | No medium/high findings |
| Wheel and sdist build | Passed into a temporary directory |
| Release artifact checks | Passed: isolated wheel imports, 71 bundled ECC agents, resource hashes, clean source archive |
| `uv lock --check` | **Failed:** the lockfile needs updating; the network-enabled check resolved 130 packages |
| `git diff --check` before report creation | Passed |

DB checks used only `postgresql://postgres:postgres@localhost:5432/machina_test`. Credential demonstrations used synthetic values and temporary workspaces. Provider wire tests used `httpx.MockTransport`; no paid provider was called. A scheduler reproduction used a temporary test-database session and deleted it afterward. The pool reproduction wrote no database rows.

The review traced security/permission classification and enforcement; file/search/terminal/web tools; MCP/OAuth and network boundaries; agent execution, providers, usage, delegation, scheduling, runtime cleanup; sessions/context/database/memory/RAG; skill installation, runtime, workflows and learning; observability; gateway/HTTP/CLI/UI; SoulSpec, plugins, repomap; installer and release packaging. Three independent reviewers covered separate subsystems, and their findings were checked against current code and executable reproductions. Historical audit documents were used as leads, not as proof that old findings still exist.

This is a risk-focused review across the codebase, not a claim that every line or every deployment configuration has been exhaustively verified. Live model quality, real external OAuth services, Docker execution, native Windows behavior, hosted CI, deployment, and backup/restore were not tested. Workflow-trial tests were excluded. No coverage percentage is claimed for these runs.

## New findings

### R01 · P1 · Recursive searches bypass credential-read approval

Locations: [permissions/policy.py:159](../../machina/permissions/policy.py#L159), [policy.py:170](../../machina/permissions/policy.py#L170), [tools/builtins.py:503](../../machina/tools/builtins.py#L503), [tools/file.py:135](../../machina/tools/file.py#L135).

The policy recognizes only some recursive grep spellings and treats ordinary ripgrep traversal as safe because hidden files are excluded. Visible files such as `credentials` and `production.env` are still sensitive. `search_files` also uses a narrower private-path blacklist than the permission policy.

The real broker/tool reproduction makes `read_file('credentials')` and `terminal('cat .env')` wait for `credential_access` approval. The same synthetic values are returned without approval by `search_files(pattern='REVIEW_TOKEN', path='.')` and `terminal('rg REVIEW_TOKEN .')`. `grep -d recurse REVIEW_TOKEN .` also reads the hidden `.env` without approval. This exposes credential material to the agent/provider through supposedly routine project reads.

Fix: share one sensitive-path definition and enforce it against every opened search result. Recursive terminal reads need review unless their effective read set is enforceably constrained. Proof: [security.py](proofs/codebase_review_2026-10-14/security.py), [security.log](proofs/codebase_review_2026-10-14/security.log).

### R02 · P1 · Recursive copy bypasses security-settings approval

Locations: [permissions/policy.py:238](../../machina/permissions/policy.py#L238), [permissions/commands.py:165](../../machina/permissions/commands.py#L165).

Sensitive destination classification checks explicit operands rather than the descendants a recursive command changes. In a scratch workspace, `cp source/.git/config target/.git/config` requires `security_settings` approval, while `cp -R source/. target` is permitted as routine. The latter actually overwrites `target/.git/config` with attacker-controlled fixture bytes.

A routine directory copy can consequently replace Git configuration/hooks or `.machina` settings protected against direct edits. Require review for recursive directory writes, or enforce sensitive destination checks for every copied descendant. Proof: [security.py](proofs/codebase_review_2026-10-14/security.py).

### R03 · P1 · RAG splits multiline secrets before redacting them

Location: [rag/pipeline.py:236](../../machina/rag/pipeline.py#L236), with per-chunk redaction at line 242.

`index_document()` chunks the raw document before applying the secret redactor. A private-key PEM block spanning chunks no longer has both delimiters in any one chunk, so the redactor misses its body. With a synthetic multiline key, the real default 512-token splitter, and a spy embedder/captured INSERT arguments, whole-document redaction removes the key, but pipeline ingestion produces six chunks and retains its raw body in five embedding inputs and five serialized storage payloads.

The exposure reaches an external embedding provider when one is configured, and persists sensitive bytes. Redact the complete document content before splitting; keep per-chunk checks as additional protection. This is separate from the previously repaired streaming PEM issue. Proof: [rag_pem.py](proofs/codebase_review_2026-10-14/rag_pem.py).

### R04 · P1 · Headless CLI chat bypasses session turn ownership

Location: [cli/__init__.py:198](../../machina/cli/__init__.py#L198).

`chat --session ID` invokes `agent.run_stream()` directly without acquiring or renewing the shared session claim. The agent itself does not acquire it. A CliRunner reproduction holds a real local coordinator claim for another transport, yet production CLI dispatch still enters the injected agent stream under that foreign owner.

A CLI prompt can interleave transcript writes and tool effects with an active gateway/REST/job turn. Route headless execution through the shared ownership lifecycle and reject occupied sessions. The reproduction validates the real coordinator and CLI call boundary with mocked DB/provider services; it is not a real cross-process database run. Proof: [cli.py](proofs/codebase_review_2026-10-14/cli.py).

### R05 · P1 · CLI deletion bypasses active-turn and mutation guards

Location: [cli/__init__.py:542](../../machina/cli/__init__.py#L542).

The CLI calls `session_manager.delete()` without the mutation-claim protocol used by gateway and REST deletion. That service deletes unconditionally. The reproduction holds an active local turn claim, invokes confirmed CLI deletion, and observes the destructive service call and success output despite the foreign owner. Database deletion was mocked to avoid destroying data.

A session and its context can disappear while its tools/provider continue running. Apply the existing mutation claim, active-turn recheck, and owner-matched release protocol. Proof: [cli.py](proofs/codebase_review_2026-10-14/cli.py).

### R06 · P1 · Anthropic thinking/tool continuations discard required signed blocks

Locations: [provider/anthropic.py:132](../../machina/core/provider/anthropic.py#L132), [anthropic.py:405](../../machina/core/provider/anthropic.py#L405), [core/prompt.py:158](../../machina/core/prompt.py#L158).

The adapter enables extended thinking from `reasoning_effort`, but normalizes responses to text/tool calls and drops `thinking`/`redacted_thinking` blocks and signatures. The streaming parser also ignores those block types. The next tool-result request reconstructs an assistant message containing only `tool_use`, while thinking remains enabled. A mocked-wire response containing `thinking + tool_use` reproduces that exact outgoing shape through the real provider and prompt helper.

Anthropic requires the original signed thinking blocks to accompany the tool continuation; its official cookbook demonstrates the missing-block request failing with HTTP 400. This conclusion combines a verified local wire-shape defect with the documented API contract; no live-provider rejection was solicited. Preserve native content blocks/signatures through responses, message assembly, streaming accumulation and persistence without modifying them. Source: [Anthropic tool-use thinking cookbook](https://platform.claude.com/cookbook/extended-thinking-extended-thinking-with-tool-use). Proof: [providers.py](proofs/codebase_review_2026-10-14/providers.py).

### R07 · P2 · Terminal deadline succeeds while a descendant keeps running

Locations: [tools/terminal.py:231](../../machina/tools/terminal.py#L231), [terminal.py:151](../../machina/tools/terminal.py#L151).

Both the timer and `_kill_tree()` skip termination when the launcher has exited, even if its process group still contains children. With an exact command approved through a fake human handler, real `terminal(timeout=1)` executes `(sleep 2; exec >/dev/null 2>&1; sleep 2; echo survived > late.txt) &`. It returns after about two seconds with exit code zero and `is_error=False`. The descendant creates `late.txt` after the tool has returned.

This breaks deadline/effect-lifetime guarantees and can permit late writes after the caller believes execution ended. Retain process-group identity independently of the launcher and terminate remaining descendants on deadline/cancellation, with explicit completion semantics for background work. Proof: [security.py](proofs/codebase_review_2026-10-14/security.py), [security.log](proofs/codebase_review_2026-10-14/security.log).

### R08 · P2 · Anthropic and Ollama accept truncated streams as complete

Locations: [provider/anthropic.py:424](../../machina/core/provider/anthropic.py#L424), [provider/ollama.py:330](../../machina/core/provider/ollama.py#L330).

Unlike the repaired OpenAI-compatible transport, these adapters do not require their terminal marker. An Anthropic text-delta stream without `message_stop`, or an Ollama frame with `done:false` followed by clean EOF, emits `['text', 'done']` and a successful final response containing only `partial answer`.

A proxy/server disconnect can silently truncate answers and allow the agent to treat a partial response as authoritative. Require a protocol-specific terminal marker before producing the final response or accepting accumulated tool calls; otherwise raise a typed provider failure and retain conservative accounting. Proof: [providers.py](proofs/codebase_review_2026-10-14/providers.py).

### R09 · P2 · Invalid memory-array entries permanently advance the checkpoint

Locations: [memory/consolidator.py:604](../../machina/memory/consolidator.py#L604), [core/agent.py:1464](../../machina/core/agent.py#L1464).

The parser rejects a top-level object but logs/skips malformed array entries. `[{'category':'preference','importance':0.9}]` with missing `content` becomes an empty successful extraction. The production agent checkpoint path advances `mem_consolidated_up_to` past the remembered preference. Calling consolidation twice then invokes extraction only once because the input is now skipped.

The same silent loss occurs for malformed elements in otherwise valid arrays. Treat schema-invalid elements as retryable extraction failure and do not mark the input range fully processed. Proof: [memory_watermark.py](proofs/codebase_review_2026-10-14/memory_watermark.py), [memory_and_skill.py](proofs/codebase_review_2026-10-14/memory_and_skill.py).

### R10 · P2 · Memory consolidation starves its own database pool

Location: [memory/consolidator.py:168](../../machina/memory/consolidator.py#L168).

Consolidation holds an advisory-lock connection/transaction while its implementation calls context, memory and usage APIs that acquire additional pool connections. A single-connection pool cannot consolidate even an empty session. Ten simultaneous distinct-session consolidations can occupy the default ten connections and all wait for a second acquisition until timeout.

The real test-DB proof uses a nonexistent session: the direct implementation returns `empty`, while the public locked method times out with a one-connection asyncpg pool. A bounded-pool fixture also reproduces ten simultaneous failures. Thread the owned connection through the work or redesign serialization so it does not consume every connection needed by the protected operation. Proof: [memory_pool.py](proofs/codebase_review_2026-10-14/memory_pool.py), [memory_and_skill.py](proofs/codebase_review_2026-10-14/memory_and_skill.py).

### R11 · P2 · Failed skill approval publishes an active skill and blocks retry

Location: [skills/learning.py:364](../../machina/skills/learning.py#L364).

`approve()` writes an enabled skill into the live registry before updating/committing the review status. An UPDATE failure or cancellation leaves the file active while the durable review remains pending. A second approval fails because the skill already exists.

Using the real registry in a temporary directory and an injected UPDATE failure, a fresh registry discovers `review-demo` after the failed approval; retry raises `skill already exists`. Stage publication with recoverable reconciliation and recognize artifacts owned by the same review, so retries can complete consistently. Proof: [memory_and_skill.py](proofs/codebase_review_2026-10-14/memory_and_skill.py).

### R12 · P2 · Quarantined scheduler effects outlive the unrenewed session claim

Location: [core/scheduler.py:804](../../machina/core/scheduler.py#L804), with release only at line 969.

Jobs acquire a session-turn claim but never renew it. Ordinary jobs time out after 300 seconds, within the 360-second claim TTL. However, cancellation-resistant effects are explicitly quarantined and retain their job lease for longer. The separate session claim still expires, allowing another interactive turn to claim that session while the old job continues effects.

A real test-DB reproduction compresses the timing to a one-second session TTL and 0.15-second job timeout. The injected agent resists cancellation; the runner retains one quarantined task; after expiry a second real `try_begin_turn()` succeeds while that task remains active. This is an accelerated lease-expiry proof, not a six-minute production-duration run. Renew the session claim throughout effects and quarantine, and fence/cancel effects if that ownership is lost. Proof: [scheduler.py](proofs/codebase_review_2026-10-14/scheduler.py).

### R13 · P2 · Turn cleanup ignores its release bound and misreports uncertain ownership

Location: [core/turn_engine.py:519](../../machina/core/turn_engine.py#L519).

The first `await shield(release)` has no configured release timeout. After cancellation, the bounded re-wait catches timeout without setting `release_failed=True`. Gateway and SSE use that flag to decide whether to emit normal completion or `turn.cleanup_pending`.

An injected blocked `end_turn()` exceeds a configured 0.02-second release bound. Cancelling cleanup then returns `TurnClose(release_failed=False)` while the release is still blocked and the simulated claim remains held. Apply the deadline on every release path and mark timeout/cancellation uncertainty as release failure; retain/observe the outstanding task. Proof: [turn_release.py](proofs/codebase_review_2026-10-14/turn_release.py).

### R14 · P2 · Headless CLI hides failed/approval terminal outcomes and exits zero

Location: [cli/__init__.py:206](../../machina/cli/__init__.py#L206).

The CLI stores the final response but does not inspect `outcome`, `needs_approval`, or final content that was not emitted as text deltas. Real error and budget paths use terminal failed responses without text frames. A failed fixture response with content `PROVIDER FAILURE MUST BE VISIBLE` exits zero and prints the session identifier without the diagnostic. Approval pauses similarly lose actionable terminal information.

Handle terminal outcome explicitly, display error/approval information and any missing canonical content, and provide documented failure/paused exit statuses. Proof: [cli.py](proofs/codebase_review_2026-10-14/cli.py).

### R15 · P2 · Session navigation loses pending approval cards

Locations: [ui/src/app.ts:335](../../ui/src/app.ts#L335), [app.ts:1028](../../ui/src/app.ts#L1028), [app.ts:1269](../../ui/src/app.ts#L1269).

Navigation deletes approval dialogs, hidden-session approval frames are discarded, and returning does not reload/reconstruct pending approvals. The production `App` reproduction moves from one card with `awaiting_approval` status to zero cards and `working` status after switching away and back; the turn remains in flight and there are zero recovery queries.

The backend remains paused while the visible UI suggests progress. Manual `/approvals` is a workaround. Retain per-session approval evidence separately from interactive decisions, or reload matching live approvals when returning. Proof: [ui_approval_navigation.ts](proofs/codebase_review_2026-10-14/ui_approval_navigation.ts); execution instructions are in the evidence README.

### R16 · P2 · Dependency lock omits declared embedding/reranking extras

Location: [uv.lock:31](../../uv.lock#L31), [uv.lock:80](../../uv.lock#L80).

The lock lists only `secrets`, `connectors`, `workflow-trial`, and `dev`; it lacks the declared `embeddings-local`, `embeddings-onnx`, and `rerank` extras and their model2vec/fastembed dependencies. `uv lock --check` with network access resolves 130 packages and exits one because the lockfile needs updating. The offline attempt failed earlier due to missing cached metadata; that was not used as evidence of staleness.

Locked synchronization cannot reproduce the declared project/extras until the lock is regenerated. Update it from the final manifest and verify locked installation for the new extras. Proof: [lock_check.log](proofs/codebase_review_2026-10-14/lock_check.log).

> **R4-120 annotation (2026-10-14) — finding fixed, no longer holds:** the current
> `uv.lock` declares `embeddings-local` (model2vec), `embeddings-onnx` (fastembed),
> and `rerank` (fastembed) at `uv.lock:1289-1296`, `provides-extras` lists all of
> them (`uv.lock:1339`), and `uv lock --check --offline` resolves 130 packages
> cleanly. The lock was regenerated after this review; do not re-raise R16
> without re-running the check.

## Previously reported working-tree findings

These four were established in the initial diff review and remain present. They are retained for a single consolidated inventory rather than counted as new findings above.

### R17 · P1 · Cohere pipeline identity mismatches on every request

Locations: [reranker.py:604](../../machina/rag/reranker.py#L604), [tools/rag.py:76](../../machina/tools/rag.py#L76).

The configured reranker identity uses a 16-character credential digest; the bound Cohere identity uses 64 characters. Consecutive `get_rag_pipeline()` calls with identical configuration return different pipelines. This repeatedly drops search/model caches and constructs new clients/embedders. Share one identity calculation for both paths; test reuse with the real Cohere backend marker rather than a fake missing that marker.

### R18 · P2 · Adding reranking can route an existing embedding model to an incompatible backend

Location: [embedder.py:693](../../machina/rag/embedder.py#L693).

With `embedding_backend=auto`, installing fastembed via `Machina[rerank]` unconditionally selects ONNX and forwards an existing explicit model identifier unchanged. Actual fastembed 0.9 rejects `minishlab/potion-base-4M` and `text-embedding-3-small` before any download, so previously working model2vec/API configurations degrade to keyword-only. Consider model compatibility during automatic selection or preserve the existing model's backend. `potion-base-8M` is supported by fastembed and was explicitly ruled out as an example of this incompatibility.

### R19 · P2 · Changing RRF weights does not invalidate the shared pipeline

Location: [tools/rag.py:95](../../machina/tools/rag.py#L95).

Weights are read only when constructing a pipeline and omitted from shared identity. Changing dense weight from 1.0 to 0.25 keeps the same pipeline with actual weight 1.0; a freshly constructed pipeline uses 0.25. Include both weights in configuration invalidation and retire stale cached search results.

### R20 · P2 · Configured reranker timeout is never applied

Location: [reranker.py:583](../../machina/rag/reranker.py#L583).

The factory does not read/pass `reranker_timeout`. Configuring 77 seconds still builds an HTTP backend with 10 seconds and Cohere with 30 seconds. Carry the timeout through settings, construction and identity changes so runtime updates take effect.

## Suggested correction order

1. Close credential reads, recursive protected writes, and whole-document secret leakage (R01–R03).
2. Make CLI execution/deletion use the shared coordinator (R04–R05), then repair provider continuation/termination and process lifetime (R06–R08).
3. Repair durable checkpoint/publication consistency and ownership cleanup (R09–R15).
4. Synchronize the dependency lock and resolve the four existing RAG integration findings (R16–R20).

For each correction, turn the corresponding executable reproduction into a behavioral regression test. Passing the existing suite alone does not exercise these failing cases. Re-run the relevant suite after fixes, then the combined release checks on the final candidate.
