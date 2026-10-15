# Holistic source review — 2026-10-15

Final snapshot: `64fc75fb6715f628709d2f3ad8162170c69cbdbd`. Application source coverage was recorded at `20a1faf4809584aa0d63e78b198d4e89604721f7`; the subsequent commits changed skill reference text, documentation, and excluded tests. The four skill text changes were read and application source hashes were rechecked unchanged. This includes the extensions/harnesses/theme/skill changes and the lazy memory import fix committed while review was running. Application files were not modified by this review. Tests were excluded as requested.

Result: **24 findings: 5 P1 and 19 P2.** Coverage manifests record **305 fully read files / 87,543 lines** across application Python/SQL, UI TypeScript, and selected installer/build/CI configuration. Final hash reconciliation found no changed reviewed files and no uncovered application Python, SQL, or UI TypeScript files. Generated `uv.lock` and `ui/package-lock.json` were explicitly excluded. New/changed first-party skill reference text was also reviewed separately.

## Method and limits

Read application source in bounded line ranges, followed calls across subsystem boundaries, and reread changed files or changed sections against the current snapshot. Earlier complete reads were carried forward only for unchanged file contents. Per-file coverage and SHA256 evidence are in [the coverage directory](proofs/holistic_review_2026-10-15/). Coverage describes source inspection, not a runtime correctness guarantee. Generated files, dependencies, and test bodies are excluded.

Two isolated diagnostics used synthetic data: streaming redaction and retrieval failure handling. Neither used real credentials, a database, network services, or the test suite. Their outputs are in [isolated_diagnostics.txt](proofs/holistic_review_2026-10-15/isolated_diagnostics.txt). Other findings below are source traces, with their triggering conditions stated explicitly.

Priority: P1 warrants prompt correction because of disclosure or persistent availability risk; P2 is a concrete functional or reliability defect under the stated conditions.

## Findings

### H01 — P1: cancellation can hang before session ownership is released

Locations: [turn_engine.py:338](../../machina/core/turn_engine.py#L338), [turn_engine.py:508](../../machina/core/turn_engine.py#L508), [background_tasks.py:131](../../machina/core/background_tasks.py#L131).

`_close_source()` cancels the active driver, then joins it and calls `source.aclose()` without a deadline. `await_joined()` shields the operation and continues waiting after further cancellation. If a tool/provider suppresses cancellation or its finalizer hangs, turn timeout cannot force progress through this stage. Claim release and renewal shutdown occur afterward, so the session can remain busy indefinitely. The bounded background cleanup later in `close()` does not bound this earlier join.

Correction: impose a deadline on source shutdown, quarantine work that cannot settle, and guarantee claim/renewal cleanup independently of source completion.

### H02 — P1: streaming redaction emits the tail of long secret-shaped tokens

Location: [redaction.py:533](../../machina/memory/redaction.py#L533).

Complete secret patterns accept unbounded token lengths, while partial-prefix holdback patterns stop at 200 characters for `sk-` keys and 500 for bearer tokens. Once a still-growing token passes that bound, the current prefix can be redacted and emitted; later chunks no longer carry the identifying prefix and can pass through raw. The synthetic diagnostic fed `sk-` + 210 `a` characters, then 80 `b` characters and a delimiter. Batch redaction removed the whole key; streaming emitted all 80 trailing `b` characters.

Correction: retain secret-token recognition state through its delimiter, including after emitting a replacement marker; do not depend on a finite prefix buffer to recognize an unbounded token.

### H03 — P2: scheduled jobs selecting another agent fail ownership validation

Locations: [scheduler.py:1113](../../machina/core/scheduler.py#L1113), [agent.py:563](../../machina/core/agent.py#L563).

For a job whose `agent_name` differs from the existing session owner, the scheduler replaces `session.agent_id` only on an in-memory copy and constructs the selected agent. Running that agent reloads the durable session and rejects its original owner with `PermissionError`. Selecting a specialist on an existing differently owned session therefore fails, despite the scheduler's apparent override.

Correction: use an authorized child session/delegation flow or explicitly reconcile persisted ownership with the execution model.

### H04 — P2: compaction summaries are omitted or reduced to 100 characters in subsequent prompts

Locations: [compression.py:529](../../machina/core/compression.py#L529), [assembler.py:153](../../machina/core/assembler.py#L153), [assembler.py:200](../../machina/core/assembler.py#L200).

The compressor deliberately timestamps a summary before the preserved recent chunks. The assembler consumes only the newest three recent chunks, so preserving three recent entries places the summary outside the selected window immediately. If a summary does reach either recent or retrieved context, `_compress_chunk()` has no `compression_summary` case and renders only the first 100 characters of the payload dictionary. An ordinary continuation need not match archived text, so archive keyword recall does not guarantee recovery of the compacted context. Export supports summaries, but prompt construction does not give them equivalent treatment.

Correction: reserve a prompt section for the latest validated summary/checkpoint and render its content explicitly within the token budget.

### H05 — P2: explicit semantic memory searches never request dense retrieval

Locations: [tools/memory.py:163](../../machina/tools/memory.py#L163), [api/routes/memory.py:72](../../machina/api/routes/memory.py#L72), [gateway/features/memory.py:50](../../machina/gateway/features/memory.py#L50), [retriever.py:156](../../machina/memory/retriever.py#L156).

All three explicit search surfaces pass query text without `query_embedding`. The retriever starts dense search only when that argument is supplied; it does not generate an embedding internally. These paths therefore remain keyword-only even with a working embedding backend and stored vectors. The API describes semantic similarity, while ordinary agent preparation correctly calls `embed_query()` first. Synonym/paraphrase searches can miss memories that normal agent retrieval would find.

Correction: share embedding preparation across search entry points and report the actual retrieval mode.

### H06 — P2: total memory retrieval failure is reported as a valid empty result

Locations: [retriever.py:170](../../machina/memory/retriever.py#L170), [tools/memory.py:171](../../machina/tools/memory.py#L171).

The retriever gathers search exceptions, logs them, and leaves the corresponding result sets empty. If every attempted branch fails—including the only keyword branch on explicit search—it returns an ordinary empty list. The tool responds “No memories found”; API/gateway callers also return ordinary empty results. A synthetic `ConnectionError` from the keyword branch reproduced `[]`. This hides outages and can cause decisions based on a false claim that no relevant memory exists.

Correction: preserve partial success, but raise a typed unavailable error when all attempted retrieval branches fail.

### H07 — P2: approval-gated memory writes return the wrong kind of ID as stored success

Locations: [store.py:82](../../machina/memory/store.py#L82), [tools/memory.py:103](../../machina/tools/memory.py#L103), [approval.py:264](../../machina/memory/approval.py#L264).

When approval is enabled, `MemoryStore.add()` returns a normal `MemoryEntry` whose ID belongs to `pending_memories`. Callers label it “Memory stored” or return an ordinary memory object without pending status. Approval later creates a different real memory ID. Consumers cannot reliably use the originally returned ID for normal recall/update/share, even after approval, and cannot distinguish pending from completed writes from the result type.

Correction: return a discriminated pending/stored outcome and expose both pending ID and approved memory ID when applicable.

### H08 — P2: stored memory vectors have no embedding-space identity

Locations: [db/schema.sql:188](../../machina/db/schema.sql#L188), [store.py:292](../../machina/memory/store.py#L292), [memory/embeddings.py:20](../../machina/memory/embeddings.py#L20).

Memory rows store vectors but no provider/model/embedding-space version. Queries use the currently configured shared embedder and compare against all matching stored vectors. Switching to a different model with the same stored dimensions leaves old vectors accepted by SQL but semantically incompatible with new queries; similarity ranking and embedding-based duplicate decisions become unreliable. Sharing one current embedder does not migrate older persisted vectors.

Correction: persist an embedding-space identifier, filter compatible rows, and re-embed or fall back for older rows after configuration changes.

### H09 — P2: delegated harness prompts can become command options

Location: [interop.py:1387](../../machina/interop.py#L1387).

`run_task()` appends prompt text directly to the external CLI argv without an option terminator or stdin-based prompt handling. A task such as `--help` is interpreted as a CLI flag by supported harnesses; a zero exit code is then labeled `ok=True` despite no task execution. Other leading-option prompts can alter CLI behavior. This is argument interpretation, not shell interpolation.

Correction: use each harness's documented literal prompt mechanism, with an option boundary where supported, and distinguish task completion from command success.

### H10 — P2: valid saved max_tokens values are rejected during execution

Locations: [config.py:357](../../machina/core/config.py#L357), [provider/transport.py:242](../../machina/core/provider/transport.py#L242).

Configuration accepts any positive `max_tokens`, but the common provider transport rejects values above 32768. Saving 65536 succeeds and then calls through this transport fail before reaching the provider. Validation and execution disagree.

Correction: use one provider-aware validation policy at configuration and request time.

### H11 — P2: the configured temperature is not forwarded to agent calls

Locations: [agent.py:378](../../machina/core/agent.py#L378), [agent.py:442](../../machina/core/agent.py#L442).

Both the ordinary completion and streaming paths forward `max_tokens` but omit the configured temperature, leaving the provider default in effect. Changing the exposed setting therefore does not change normal agent sampling.

Correction: pass the validated configured temperature through both paths.

### H12 — P2: Copilot SSE error frames can end as successful empty or partial responses

Location: [provider/copilot.py:288](../../machina/core/provider/copilot.py#L288).

With an HTTP 200 stream, frames without `choices` are ignored, including JSON error objects. A later `[DONE]` sets successful terminal state. The error can disappear into a normal empty/partial completion instead of being surfaced to the caller.

Correction: recognize error payloads before processing choices and propagate a typed provider error while preserving partial-output accounting.

### H13 — P1: OAuth connector connection recursively acquires the configuration lock

Locations: [execution.py:170](../../machina/connectors/execution.py#L170), [mcp_client.py:469](../../machina/connectors/mcp_client.py#L469), [oauth.py:109](../../machina/connectors/oauth.py#L109), [service.py:87](../../machina/connectors/service.py#L87).

Authorized connection holds `service._configuration_lock` while awaiting `connect_and_discover()`. An OAuth HTTP connection performs authorization preflight, which refreshes the coordinator's auth cache and resolves configuration through the same service. `service.refresh()` then waits on the already-held non-reentrant lock. The ordinary runtime service/coordinator wiring takes this path, so the connection stalls until an outer timeout rather than completing authorization.

Correction: take a validated immutable configuration/auth snapshot before dispatch, and avoid calling back into lock-taking service refresh while holding the configuration lock.

### H14 — P1: recursive dereferencing copy bypasses containment and credential checks

Location: [policy.py:723](../../machina/permissions/policy.py#L723).

Top-level operand checks do not cover all files reached by recursive copy. The descendant scan classifies protected `.git`/`.machina` components, but does not establish workspace containment or sensitive-file classification for each dereferenced descendant. With an existing workspace `source/data` symlink to an outside private file, routine `cp -RL source clone` copies its contents into a regular workspace file. Later permitted reads of `clone/data` expose the contents. No pathname race is needed.

Correction: classify the actual dereferenced source tree for containment and credential access, or require explicit review of recursive symlink-following copies.

### H15 — P2: stdio stderr drains can exhaust the shared thread executor

Location: [mcp_client.py:115](../../machina/connectors/mcp_client.py#L115).

Every live stdio connector submits a blocking `os.read` on its stderr pipe to the default executor. Quiet servers keep one worker occupied for their connection lifetime. Enough simultaneous connectors occupy all workers and prevent unrelated `to_thread` work—including filesystem and credential operations—from starting. The output byte cap does not release a blocked read.

Correction: use asynchronous pipe readiness or a separately bounded I/O facility that cannot consume the application's shared executor.

### H16 — P2: OAuth cancellation can be followed by credential publication

Locations: [oauth.py:591](../../machina/connectors/oauth.py#L591), [oauth.py:924](../../machina/connectors/oauth.py#L924), [oauth_store.py:433](../../machina/connectors/oauth_store.py#L433).

Cancel marks the operation cancelled and calls `task.cancel()`, but does not fence credential publication or join its result. If the flow is awaiting the store's `to_thread` commit, cancelling the coroutine does not stop the worker. That worker can publish the authorization after cancellation has already been reported to the user.

Correction: make cancellation invalidate the publication generation under the same commit synchronization, or join and reconcile any in-progress commit before reporting the final cancellation outcome.

### H17 — P2: OAuth index publication failure can delete the credential it references

Locations: [oauth_store.py:116](../../machina/connectors/oauth_store.py#L116), [oauth_store.py:412](../../machina/connectors/oauth_store.py#L412), [oauth_store.py:423](../../machina/connectors/oauth_store.py#L423).

`_atomic_write()` replaces the index and then fsyncs its directory. If that post-rename fsync raises, the caller treats publication as uncommitted and deletes the new encrypted envelope. The already-replaced index now points to a missing file. This is an I/O-failure case, rather than ordinary successful execution, but it defeats the intended crash-safe credential update.

Correction: track the publication boundary and never remove an envelope that the visible index can reference; reconcile the index before rollback cleanup.

### H18 — P2: file search validates a path and later reopens it without descriptor protection

Locations: [builtins.py:561](../../machina/tools/builtins.py#L561), [builtins.py:576](../../machina/tools/builtins.py#L576), [builtins.py:422](../../machina/tools/builtins.py#L422).

The parent resolves and classifies a file, then sends its original pathname to the search subprocess, which uses ordinary `open()`. A concurrent replacement of the file or an ancestor with a symlink can redirect that later read outside the workspace or into a private file. The descriptor-based traversal improvements in ordinary file tools do not protect this separate search implementation.

Correction: open through protected descriptor traversal and pass the opened input, or apply equivalent no-follow ancestor validation at the actual read boundary.

### H19 — P2: active RAG reranker clients are not closed at runtime shutdown

Locations: [runtime.py:258](../../machina/core/runtime.py#L258), [tools/rag.py:145](../../machina/tools/rag.py#L145).

Shutdown closes the shared embedder and the active pipeline's embedder, but does not close the active pipeline's reranker. The existing helper closes both components only for replaced pipeline generations. With an HTTP/Cohere reranker, the active async HTTP client remains open across runtime shutdown/restart and can retain sockets or outlive its owning event loop.

Correction: release the active pipeline through one lifecycle helper that closes all owned components and resets the shared generation state.

### H20 — P1: explicit compression can send a session to the wrong provider

Locations: [gateway/features/sessions.py:198](../../machina/gateway/features/sessions.py#L198), [services.py:169](../../machina/services.py#L169).

With LLM summarization enabled, explicit compression constructs a provider from gateway/global defaults instead of the session's pinned provider and model. A session using local Ollama or another private route can consequently have its conversation submitted to an external default provider during compression. The gateway supplies its defaults explicitly; CLI compression also falls back to global defaults. This affects actual transcript processing, not just the displayed model label.

Correction: resolve the session's effective provider/model using the same route resolver as ordinary turns, and apply explicit overrides only when the operation intentionally requests them.

### H21 — P2: long management requests block subsequent cancellation, approval and revocation RPCs

Location: [gateway/__main__.py:81](../../machina/gateway/__main__.py#L81).

The stdio dispatch loop awaits each `handle_line()` before reading the next queued request. Handlers that await slow harness execution, delegation, discovery or management work therefore block later `prompt.cancel`, approval resolution, `mode.revoke`, and shutdown requests from being dispatched. Running the handler's blocking work on a thread keeps the event loop available but does not free this serial request loop.

Correction: dispatch independent requests as tracked tasks, with explicit ordering only where shared-state mutations require it, and keep cancellation/control requests dispatchable.

### H22 — P2: the harness UI times out before the backend execution deadline

Locations: [harnesses.ts:116](../../ui/src/features/harnesses.ts#L116), [app.ts:337](../../ui/src/app.ts#L337), [gateway/features/interop.py:229](../../machina/gateway/features/interop.py#L229).

The UI gives `interop.run` 120 seconds, while the backend defaults to 300 seconds. A run lasting 120–300 seconds reports a UI timeout while its external process continues with host access. The eventual response has no pending UI request to receive it. Together with H21, the user also cannot dispatch subsequent control requests while the backend is still awaiting that run.

Correction: coordinate deadlines and cancellation across both sides, or return a job ID immediately and provide progress/result/cancel operations.

### H23 — P2: `/effort` reports a setting change that environment precedence can override

Locations: [providers_feature.py:210](../../machina/gateway/features/providers_feature.py#L210), [config.py:509](../../machina/core/config.py#L509).

The command updates config state and echoes the requested reasoning effort, but `config.get()` continues to prioritize `MACHINA_REASONING_EFFORT`, including an explicitly empty value. When that variable is present, `/effort high` can report success while subsequent provider calls keep using the environment's previous value.

Correction: expose the effective setting and its source, or support an explicit runtime override with consistent precedence.

### H24 — P2: delegate UI hides the paused approval continuation

Locations: [agents.ts:187](../../ui/src/features/agents.ts#L187), [gateway/features/agents.py:63](../../machina/gateway/features/agents.py#L63), [orchestrator.py:443](../../machina/core/orchestrator.py#L443).

The orchestrator can return `waiting_approval`, and the gateway supplies the child session ID plus `needsApproval` details. The `/delegate` UI handles only `error` specially and renders every other status as ordinary output, discarding the approval and child-session continuation information. A paused child is presented like a finished response, without the information needed to continue it from that interaction.

Correction: model paused delegation as a distinct UI state, render its approval actions, and preserve the child-session continuation target.

## Previously reported defects corrected in the current snapshot

Current source resolves the earlier completed-tool cancellation bookkeeping loss, child stdin supervision ordering, provider ownership leak on delegation startup failure, event-sequence pagination mismatch, and SSE growth ContextVar propagation problem. Connector rollback handling, blocking store mutation, sandbox management, the previously identified refresh-lock inversion, and MCP mode ceiling handling also received fixes and are not repeated as their old findings. H13 concerns a separate recursive configuration-lock acquisition. Additional older findings should be judged against this snapshot rather than assumed current from previous reports.

## Cross-cutting assessment

The recurring risks are mismatched contracts across boundaries: pending versus stored memory; empty results versus failed retrieval; saved configuration versus accepted provider arguments; selected agent versus durable session owner; compressed context versus prompt rendering; and cancellation intent versus joined cleanup. Centralizing these outcomes in explicit types and shared boundary helpers would prevent several defects together. Local hardening alone does not establish that the full caller-to-storage or caller-to-provider flow behaves correctly.
