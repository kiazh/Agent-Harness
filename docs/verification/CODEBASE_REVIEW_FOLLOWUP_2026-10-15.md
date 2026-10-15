# Source review follow-up — 2026-10-15

Baseline: `1e1edbf36c7e2df0283ed80be943949df2067f21`, branch `main`. The tracked worktree was clean at review start. This review includes current fixes and focuses on additional defects rather than repeating the previous report.

Tests were excluded as requested. No tests, application probes, fixes, commits, or dependency changes were performed. All findings are based on source inspection; concurrency scenarios below have not been demonstrated by a runtime reproduction in this pass.

## Findings

### F01 — P1, conditional: directory-fd write protection still permits an ancestor swap before opening

**Source:** [file.py:280](/Users/shadow/machina/machina/tools/file.py:280), [file.py:282](/Users/shadow/machina/machina/tools/file.py:282), [file.py:291](/Users/shadow/machina/machina/tools/file.py:291).

The new pinned-directory implementation closes the previous post-open pathname race, but its directory acquisition still checks containment by pathname before `lstat` and `open`. For a target `workspace/a/b/file`, another local process can replace ancestor `a` with a symlink to an outside tree after `_realpath_inside_base(parent)` returns but before `lstat(parent)`. Both `lstat` and `open` then identify the outside directory `b`. The inode comparison succeeds, and `O_NOFOLLOW` only rejects a symlink at the final component `b`. Subsequent descriptor-relative writes are anchored to the wrong directory.

**Impact:** a workspace-scoped write can affect an outside file when a concurrent process controls a workspace ancestor. This is a remaining gap in the previous finding's fix, with an earlier race window.

**Fix direction:** open the trusted workspace root and traverse each component relative to the preceding directory descriptor with no-follow semantics. Retain that chain through parent creation and final replacement.

### F02 — P1: cancelling a tool batch loses records of already-completed effects

**Source:** [agent.py:1133](/Users/shadow/machina/machina/core/agent.py:1133), [agent.py:1151](/Users/shadow/machina/machina/core/agent.py:1151), [agent.py:1268](/Users/shadow/machina/machina/core/agent.py:1268), [agent.py:1274](/Users/shadow/machina/machina/core/agent.py:1274).

Tool completions are emitted as each task finishes, but context recording, `tool_calls_made` bookkeeping and approval completion happen in Phase D, after the entire batch. If a fast write finishes while another tool is still running, cancelling the turn skips Phase D. The completed write is not in `pending_chunks`, so cancellation flushing cannot recover it. An approved action that already ran can remain in the claimed state.

**Impact:** filesystem effects survive while their durable conversation record and approval outcome are missing, undermining recovery and truthful cancellation reporting.

**Fix direction:** record each completed action and retire its approval before emitting completion. Keep provider-message ordering separate from durable effect bookkeeping; make cancellation finalize any completed items not yet recorded.

### F03 — P2: failed connector rollback overwrites another worker's newer configuration

**Source:** [management.py:238](/Users/shadow/machina/machina/connectors/management.py:238), [management.py:243](/Users/shadow/machina/machina/connectors/management.py:243), [management.py:246](/Users/shadow/machina/machina/connectors/management.py:246), [management.py:212](/Users/shadow/machina/machina/connectors/management.py:212).

`add_and_connect` saves a configuration and then awaits connection/discovery. Its configuration lock is local to one service instance. Another process can save a newer configuration during that await. If the first connection fails, `_compensate` unconditionally removes the connector or restores its prior fields, destroying the newer successful update.

**Impact:** approved connector configuration can disappear or revert to obsolete endpoints/commands under concurrent administration.

**Fix direction:** give the durable configuration a revision and compensate only if the current revision still belongs to this operation. Use compare-and-swap for restoration/removal.

### F04 — P2: agent connector installation bypasses the existing sandbox stdio restriction

**Source:** [management.py:233](/Users/shadow/machina/machina/connectors/management.py:233), [management.py:246](/Users/shadow/machina/machina/connectors/management.py:246), [mcp_client.py:381](/Users/shadow/machina/machina/connectors/mcp_client.py:381).

The agent `mcp_connect` management path validates the approved request but does not reject a stdio connector in sandbox mode before launching it. The MCP client launches stdio servers on the host. Existing connector connection/invocation paths explicitly prohibit this combination.

**Impact:** an approved install from a sandbox session can start host code even though the corresponding existing connector cannot be connected or invoked in that mode. The proposal identifies the backend as host; this is inconsistent enforcement rather than a falsely labeled container execution.

**Fix direction:** enforce the same sandbox transport rule at the common launch boundary, or define and consistently enforce an explicit host-execution exception.

### F05 — P2: connector store locking can block the entire event loop

**Source:** [store.py:48](/Users/shadow/machina/machina/connectors/store.py:48), [service.py:148](/Users/shadow/machina/machina/connectors/service.py:148).

POSIX connector writes acquire `flock(LOCK_EX)` synchronously, without a deadline. Async configuration/removal methods call these writes directly. If another process holds the lock while stalled, the waiting process blocks its event-loop thread, including unrelated requests, cancellation and health handling.

**Impact:** contention on one connector store can freeze an entire API/gateway worker for an unbounded period.

**Fix direction:** acquire locks nonblocking with a bounded retry/deadline outside the event loop, and return a typed busy/store-unavailable result when the bound expires.

### F06 — P2: OAuth refresh takes locks in conflicting orders

**Source:** [service.py:65](/Users/shadow/machina/machina/connectors/service.py:65), [oauth.py:1206](/Users/shadow/machina/machina/connectors/oauth.py:1206), [oauth.py:1368](/Users/shadow/machina/machina/connectors/oauth.py:1368).

A service refresh holds the configuration lock while refreshing cached authorization. When a token refresh is in flight, cache refresh waits for that operation's credential lock. The token-refresh owner holds the credential lock and, after receiving tokens, calls `_resolve_config`, which waits for the configuration lock. The circular wait lasts until the bounded refresh-coalescing wait expires; the cache path then treats the in-flight record as unavailable and removes cached authorization.

**Impact:** concurrent requests stall and can report missing authorization/reauthorization despite a valid token refresh.

**Fix direction:** establish one lock order and avoid waiting for credential refresh while holding the configuration lock. Capture and later revalidate configuration revisions instead.

### F07 — P2: HTTP streaming drops growth state between generator steps

**Source:** [streaming.py:77](/Users/shadow/machina/machina/api/routes/streaming.py:77), [streaming.py:197](/Users/shadow/machina/machina/api/routes/streaming.py:197), [agent.py:644](/Users/shadow/machina/machina/core/agent.py:644), [agent.py:1567](/Users/shadow/machina/machina/core/agent.py:1567).

HTTP streaming creates a fresh task for every `__anext__` call. Growth episode and RSI ContextVars are set inside the first step's task, so they do not propagate back to the driver and into later tasks. The done step reads the default episode ID and skips `_complete_growth`. Even a short completion normally yields usage before done, separating preparation and completion into different tasks.

**Impact:** successful HTTP turns can leave growth episodes incomplete, preventing normal feedback and adaptation; related RSI state is lost between steps.

**Fix direction:** store turn state explicitly on an owned per-run object, or drive the entire generator from one persistent task. Avoid relying on ContextVar writes propagating from child tasks to their creator.

### F08 — P2: shared RAG pipelines retain old embedding credentials and failed backend selection

**Source:** [rag.py:83](/Users/shadow/machina/machina/tools/rag.py:83), [rag.py:145](/Users/shadow/machina/machina/tools/rag.py:145), [embedder.py:92](/Users/shadow/machina/machina/rag/embedder.py:92), [embedder.py:129](/Users/shadow/machina/machina/rag/embedder.py:129).

Pipeline identity includes embedding backend/model/base URL but not credential identity or effective backend availability. Its independently constructed OpenAI embedder captures an API key and Authorization header at initialization. Rotating/clearing the key does not rebuild the pipeline. Similarly, a pipeline constructed without usable API credentials can stay keyword-only after a key is added, if the other identity components remain unchanged. The separate shared-embedder helper includes a credential digest, but the RAG pipeline does not use that helper.

**Impact:** RAG can continue sending requests with a superseded key, or fail to enable dense retrieval after credentials are supplied, until another identity component changes or the process restarts.

**Fix direction:** include resolved embedding selection and a nonsecret credential fingerprint in pipeline identity, and close replaced owned clients safely.

### F09 — P2: persistent CLI model/provider changes can write the old value back

**Source:** [CLI:1627](/Users/shadow/machina/machina/cli/__init__.py:1627), [CLI:1637](/Users/shadow/machina/machina/cli/__init__.py:1637), [config.py:507](/Users/shadow/machina/machina/core/config.py:507), [config.py:537](/Users/shadow/machina/machina/core/config.py:537).

After `config.set`, persistent model/provider updates synchronize the environment shadow using `config.get(key)`. That getter still prefers the old `MACHINA_MODEL`/`MACHINA_PROVIDER` environment value over the newly saved attribute. With `MACHINA_MODEL=old`, `machina config-set model new --persist` can save YAML `new` and then write `old` back into `.env`, while reporting success.

**Impact:** a documented default-setting command appears successful but does not change effective behavior in a common installed configuration.

**Fix direction:** synchronize the validated newly assigned value, then read the effective setting for confirmation after the environment is updated.

### F10 — P2: forced setup merges the old environment instead of replacing it

**Source:** [CLI:1401](/Users/shadow/machina/machina/cli/__init__.py:1401), [CLI:1508](/Users/shadow/machina/machina/cli/__init__.py:1508), [CLI:1527](/Users/shadow/machina/machina/cli/__init__.py:1527).

`setup --force` prints that it is overwriting `.env` and clears only its in-memory existing-values mapping. Since the file still exists, the seed/replacement branch is skipped, and `set_env_values` merges changes into the original contents. Noninteractive forced setup retains prior provider keys, model/provider choices and other settings; blank interactive answers also fail to remove old secrets.

**Impact:** configuration and credentials survive an operation advertised as starting over, leaving a materially different environment than the user requested.

**Fix direction:** build a fresh complete file for forced setup and replace it atomically with private permissions; retain merge semantics only for ordinary setup.

### F11 — P2: title-only retrieval evaluation is forced to score zero

**Source:** [locomo.py:512](/Users/shadow/machina/machina/research/locomo.py:512), [locomo.py:530](/Users/shadow/machina/machina/research/locomo.py:530), [session_recall.py:130](/Users/shadow/machina/machina/core/session_recall.py:130).

The title-only evaluation filters hits to `source == 'title'`, then creates rankings only from hits whose chunk UUID appears in a dialogue mapping. Title hits always have `chunk_id=None`, so all title-only rankings are empty even when a title matches. All temporary session titles are also the same generic `LoCoMo transcript`.

**Impact:** reported recall/MRR comparisons against title-only search use a baseline structurally incapable of retrieving an evidence ID, overstating measured improvement.

**Fix direction:** define a comparable session-to-evidence ranking and meaningful title-generation policy, or label this baseline unavailable rather than presenting its zero as a measured retrieval result.

### F12 — P2: delegation bookkeeping failure leaks the newly built child/provider

**Source:** [orchestrator.py:314](/Users/shadow/machina/machina/core/orchestrator.py:314), [orchestrator.py:322](/Users/shadow/machina/machina/core/orchestrator.py:322), [orchestrator.py:329](/Users/shadow/machina/machina/core/orchestrator.py:329).

The delegate creates its child session and builds an owned provider, then awaits `_record_start` before entering the execution cleanup `try/finally`. A DB error or cancellation during that insert skips cleanup. The provider is not closed and the child session is left active.

**Impact:** repeated bookkeeping failures accumulate orphan sessions and owned provider resources.

**Fix direction:** enter the ownership cleanup scope as soon as the child/session/provider is acquired. Include message-start recording within that scope, and archive/close on failure.

### F13 — P2: a named pipe can hang file validation on the event-loop thread

**Source:** [file.py:153](/Users/shadow/machina/machina/tools/file.py:153), [file.py:155](/Users/shadow/machina/machina/tools/file.py:155), [file.py:579](/Users/shadow/machina/machina/tools/file.py:579).

`resolve_path` opens every existing nondirectory path with blocking `O_RDONLY` to validate its identity. A POSIX FIFO without a writer blocks in that open. `read_file` calls `resolve_path` synchronously before its later `is_file` rejection and before moving actual reads to a thread. Other callers using the resolver inherit the same issue.

**Impact:** attempting to read a named pipe in a workspace can freeze the process's event loop; async tool timeouts cannot fire while that synchronous open is blocked.

**Fix direction:** reject unsupported file types before blocking operations, use nonblocking descriptor acquisition with post-open type checks, and keep filesystem operations off the event loop. Account for type swaps during validation.

### F14 — P3, latent helper defect: bounded subprocess stdin can block before supervision starts

**Source:** [child_process.py:272](/Users/shadow/machina/machina/core/child_process.py:272), [child_process.py:277](/Users/shadow/machina/machina/core/child_process.py:277), [child_process.py:305](/Users/shadow/machina/machina/core/child_process.py:305).

The bounded subprocess helper writes all stdin synchronously before starting its timeout timer or output-reader threads. Input larger than pipe capacity blocks indefinitely if the child does not read it. A child that writes enough output before reading stdin can also deadlock both directions. The cancellation polling loop has not started at that point.

**Impact:** the helper's general timeout/cancellation guarantee fails for such inputs. The production interop caller inspected currently supplies only a newline, so a present large-input production trigger was not established.

**Fix direction:** start deadline supervision and output draining before stdin delivery; send input concurrently with a cancellable/bounded writer.

## Scope and confidence

Newly completed or substantially expanded inspection covered child process handling; turn flow/engine/cleanup; orchestrator and runtime; agent tool batches, preparation and growth; connector management/service/store/manager, client lifecycle and OAuth refresh; UI transcript/events/tail and App event/navigation paths; CLI auth and most main commands; LoCoMo; remaining memory/agent/harness/research tools; file protections and the RAG pipeline's embedding lifecycle.

This was another focused source review, not a claim that every repository line has now been audited. Remaining coverage gaps include portions of large UI and runtime modules, auxiliary research/benchmark commands and some provider/tool implementations. No absence-of-bugs claim is made for inspected files.

Suggested order: fix F01/F02 first; then F03–F07/F13 (authority, worker responsiveness and durable state); then F08–F12 (credential/configuration/evaluation correctness); address F14 before expanding the helper's input use.
