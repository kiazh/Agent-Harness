# Final review — core runtime slice (line-by-line, post-fix)

Date: 2026-10-15
Reviewer: subagent (read-only pass; no runtime execution, no edits except this file)
Scope (read every line, current HEAD `1e1edbf`):
`machina/core/{agent.py, orchestrator.py, turn_flow.py, turns.py, session.py,
session_mode.py, context.py, compression.py, assembler.py, prompt.py,
scheduler.py, agent_factory.py, child_process.py, execution_context.py,
progress.py, usage.py, models.py}`

Method: full read of each slice file; every candidate verified by reading its
callsites/callees (and, where a "recent fix" invariant was involved, the
introducing commit diff). No tests were run. `tests/` fixtures were ignored by
instruction.

Focus areas requested: logic/edge cases; ordering/race hazards;
cancellation/ownership; error paths that swallow/misclassify; invariants broken
by the recent fixes (flush log-and-continue, cursor serialization, ordinal
assignment, max_iterations removal leftovers); dead fields/branches; docstrings
that lie.

---

## Confirmed findings (severity-ranked)

### F1 — P2 — HELD-55 ordinal is used for ordering but not for the consolidation *cursor*; events can be permanently skipped

**Files/lines**
- `machina/core/context.py:618-632` — `get_chunks_since`:
  - predicate stays on the UUID: `(COALESCE(created_at, epoch), id) > (COALESCE($2, epoch), $3::uuid)`
  - but the window order was changed by HELD-55 to: `ORDER BY ... event_seq ASC NULLS LAST, id ASC`
- `machina/memory/consolidator.py:310-321` — `_checkpoint_for` returns the watermark as `max(processed, key=(created_at.isoformat(), id))` (i.e. by `(created_at, id)`, never `event_seq`).
- Writer: `machina/core/context.py:262-277` (`add_chunks_batch`) + `machina/db/connection.py:24-34` migration. HELD-55 commit `97c6197` changed the ORDER BY in `get_chunks_since` but left the cursor predicate and the checkpoint key on `id`.

**Why it is a defect**
The consolidation window is selected as a *prefix of the `(created_at, event_seq, id)` order*, but the cursor it advances to (`_checkpoint_for`) and the next window's filter (the `(created_at, id)` predicate) are on `id`. When a same-`created_at` batch (one `add_chunks_batch` transaction → identical `created_at`, random UUIDs, distinct `event_seq`) straddles the `LIMIT MAX_CHUNKS_PER_CONSOLIDATION` boundary (`=100`), the max id in the processed prefix can exceed the ids of unprocessed rows in the same batch. Those rows then fail `(created_at, id) > checkpoint` forever — a permanent silent skip of consolidation input (memories never extracted from those turns).

This is precisely the class the HELD-55 schema comment claims to fix for consolidation (`machina/db/schema.sql:99-105`): "… (resumed prompts, exports, consolidation). event_seq is assigned in original event order". The *ordering* was fixed; the *pagination cursor* was not, so the fix is only half-applied on this reader.

**Falsification attempt**
- Would be refuted if the window always ends exactly at a batch boundary (no `created_at` group split by `LIMIT`). With `MAX_CHUNKS_PER_CONSOLIDATION=100` and per-flush batches of ~1-10 chunks, a split needs the 100th row to fall inside a batch — reachable whenever the consolidation backlog exceeds 100 (e.g. long session, consolidation disabled then enabled, or a resumed/imported session). Confirmed reachable in *principle*.
- Would be refuted if, within one `created_at` group, `id` order always matched `event_seq` order. It does not: `chunk_ids = [uuid.uuid4() ...]` (`context.py:273`). So `max-by-id != last-by-event_seq` generically. Confirmed.
- Would be refuted if the older behaviour were already inconsistent (i.e. pre-existing, not a regression). Pre-HELD-55, `get_chunks_since` ordered by `created_at ASC, id ASC` — matching the `(created_at, id)` predicate — so the window was a clean prefix and the checkpoint was the window's last row; no skip. HELD-55 introduced the order/predicate mismatch. Confirmed regression from the fix.
- Not the same as HELD-F1/R5-F1 (`docs/.../R5-F1_consolidation_cursor.md`), which fixed the *stale read before the lock* (watermark regression). F1 here is a *skip* caused by the id/event_seq key mismatch, and is not covered by any existing ledger entry (`grep event_seq docs/` → only the HELD-55 proof).

**Suggested correction (not applied):** filter and checkpoint on `event_seq` (e.g. `(created_at, event_seq, id) > ($2, $3, $4)` and a checkpoint key of `(created_at, event_seq, id)`), keeping `id` only as the final tiebreak.

---

### F2 — P3 — `run()` drops the cancellation-safe wrapper that `run_stream()` has around the immediate user-message flush

**Files/lines**
- `machina/core/agent.py:1655-1657` (`run`): comment says "persist the user message immediately so cancellation during the first LLM call cannot lose it", then `await self._flush_pending_chunks(pending_chunks)` with **no** `except asyncio.CancelledError` guard.
- `machina/core/agent.py:1863-1871` (`run_stream`): the identical immediate flush **is** wrapped in `try/except asyncio.CancelledError → _flush_pending_cancellation_safe(...) → raise`.

**Why it is a defect**
`_flush_pending_chunks` is DB I/O (`await context_manager.add_chunks_batch`). If cancellation is delivered during that await in `run()`, `CancelledError` propagates (the await is not shielded) and the just-built `pending_chunks` list (holding the user message) is dropped with no interruption marker. `run_stream` handles exactly this. The asymmetry is an oversight, not a documented difference.

**Falsification attempt**
- Refuted if the flush were synchronous/atomic w.r.t. cancellation (`_flush_pending_chunks` would then have no await point). It awaits `add_chunks_batch` (`agent.py:1334-1336`). Confirmed.
- Refuted if some outer try/except in `run()` covered it. `instrument_run` catches `Exception`, not `BaseException`, so `CancelledError` is not absorbed (`agent.py:98-119`). Confirmed.
- Impact is bounded (only the current user message, on a narrow window; clients typically resend). Hence P3.

---

### F3 — P3 — Multi-approval pauses are dropped by pause derivation (`needs_approvals` never read)

**Files/lines**
- `machina/core/models.py:124-140` — `AgentResponse.__post_init__`: builds `seen` from `self.needs_approval`, then only `pause = entry.get("needs_approval")` (singular). It never inspects `entry["needs_approvals"]`.
- `machina/core/agent.py:1313-1315` — the tool entry stores *both* `needs_approvals` (the full delegated list) and `needs_approval = needs_approvals[0]`.
- Callsite `machina/core/approval_resume.py:65` — `pauses = [entry["needs_approval"] for entry in made if "needs_approval" in entry]` (also singular), then passes it explicitly to `AgentResponse(...)`.

**Why it is a defect**
The `__post_init__` docstring (`models.py:117-123`) claims: "Every construction site is covered automatically: entries carrying a needs_approval marker contribute their pause record." For a delegated tool that paused N>1 approvals (`_needs_approvals`), only the first contributes. Any `AgentResponse` built from `tool_calls` without an explicit full `needs_approval` loses pauses 2..N. `approval_resume.resume_action_stream` is a live site that structurally loses them (it extracts only the singular field from `made`). The two agent loops happen to avoid the loss because they pass `needs_approval=collect_pauses(batch)` explicitly (`agent.py:1802-1810`, `2077-2089`).

**Falsification attempt**
- Refuted if entries never carried plural pauses into an implicit-derivation site. `DelegationToolResult.needs_approval` can be a list (`agent.py:1041-1043`) and `tool_entry["needs_approvals"]` holds it (`agent.py:1313-1315`); `approval_resume.py:63` feeds exactly those entries to `_execute_tool_calls_stream` and then derives `pauses` from the singular key. Confirmed for that path.
- Refuted if every construction site passes explicit pauses. `grep 'AgentResponse(' machina/` → `approval_resume.py:72` passes an explicit-but-singular list; `turn_flow.py:100` passes none (stagnation; no pauses). So the trap is live at one site. Confirmed, latent elsewhere.

---

### F4 — P3 — `Orchestrator.delegate` leaks the owned child provider and orphans the child session if `_record_start` fails

**File/lines**
- `machina/core/orchestrator.py:309-321` — the build is guarded (`except BaseException: archive(child.id); raise`), but `message_id = await self._record_start(...)` at line 322 sits **outside** that guard.
- The provider-cleanup `finally` (join background tasks + `close_agent_provider`) begins only at `orchestrator.py:374`, inside the `try` that starts at `orchestrator.py:329` — so it is never reached if `_record_start` raises.
- Related: `machina/core/agent_factory.py:137-188` — `llm = get_provider(...)` is created before a non-guarded `MachinaAdapter.to_soulspec(definition).to_storage_dict()` (line 185); a raise there leaks `llm` (no try/finally closes it).

**Why it is a defect**
`_record_start` is a bare `INSERT INTO agent_messages` (`orchestrator.py:564-579`). On a DB error there, the freshly built, provider-owning child agent (`_default_agent` → `build_agent_for_session`, `_owns_provider=True`; `_owns_factory_agents` defaults True, `orchestrator.py:96`) is dropped without `close_agent_provider`, and the child session created at `orchestrator.py:279-288` is never archived (the archive path only runs on build failure). This is an error-path resource/session leak.

**Falsification attempt**
- Refuted if `_record_start` could not raise (single INSERT). Any DB outage/contention makes it raise. Confirmed reachable.
- Refuted if an outer caller closes the provider. `delegate`'s callers (`run_sequential`/`run_parallel`, the delegate tool) do not receive the agent object and never close it. Confirmed.
- Low probability (requires a DB failure at one specific call) → P3.

---

## Clean-but-suspicious (checked; do NOT re-raise as findings)

These were examined closely and found benign or by-design; listed to prevent duplicate reports in later passes.

1. `agent.py:1324-1342` `_flush_pending_chunks` log-and-continue (PB-1): on failure the list is intentionally retained; `add_chunks_batch` is a single `INSERT … UNNEST` (atomic), so no partial write/duplicate. On the terminal flush of a turn the retained chunks are dropped, but that is the documented fail-open trade-off. `_flush_pending_cancellation_safe` (agent.py:1344-1383) clears the original after a failed inner flush, but the stack is unwinding anyway and an interruption marker is written. Not a bug.
2. `agent.py:285-286` "unbounded tool calls" comment + both `while True` loops: no `max_iterations` remains in source (only schema-drop comments at `db/schema.sql:280-283`, `db/soulspec_schema.sql:3-7`). No leftover gate. Clean.
3. `turn_flow.stagnation_step` (turn_flow.py:53-90): `flush`/`record_stagnation_state` are outside the guarded `try`. Verified against the pre-extraction block (`git show 6a1131a^:machina/core/agent.py:1756-1772`): the original also only guarded import+fold and left the write unguarded. Behaviour-preserving; the module docstring's "any failure skips the hook" refers to the import, which is accurate.
4. `context.py:262-277` ordinal assignment: `nextval` batch + `sorted(...)` + `zip(..., strict=True)` gives the i-th event the i-th smallest ordinal regardless of `nextval` evaluation order; column/args order in `_ADD_CHUNKS_BATCH(_WITH_IMAGES)_SQL` (`context.py:101-119`) matches `(id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, search_text, document_source, event_seq)` and the `$11/$12/$13` image params. Correct.
5. `turns.py` claim/release: token-gated `_local_release`/`_db_release`, `_live_claim_where`, `_finish_fencing_ok`, `renew_lease`/`finish` strict fencing (`scheduler.py:342-378`, `444-485`) all fail closed on a missing token for claimed rows. Consistent.
6. `scheduler.run_due_once` timeout/ownership-loss/quarantine paths (`scheduler.py:609-761`): `loss_wait` is cancelled+joined on every exit; `_quarantine_run` retains the effect and its lease, and the `terminated` callback defers `_finish_run`; `_finish_run` is reached exactly once. Token fencing prevents a stale finish. Clean.
7. `agent_factory.merge_authority` / `cap_child_authority` / `check_mode_cap` / `definition_tool_cap` (`agent_factory.py:195-277`): `None`-vs-`[]` semantics handled by key presence; `[]` never folds to unrestricted. Matches the docstrings.
8. `session_mode.resolve_effective_mode` falls back to the in-process registry when the DB is not connected (`session_mode.py:269-273`), which can return a previously DB-observed mode. This is the documented "explicitly DB-less execution" path, not an accidental cache; `db.connected` is False only in DB-less/degraded mode. By design.
9. `session.py:_row_to_session` (session.py:428-452) does not guard `msgpack.unpackb` of `state_msgpack`, unlike `update_state_fields` (session.py:196-219). A corrupt row makes `get`/`get_fresh` raise; it is inconsistent with the "never silently discard" recovery in `update_state_fields`, but raising is fail-loud rather than fail-open. Suspicious, not clearly a defect — flagging for the session slice, not as a core-runtime finding.
10. `usage.py:106-108` `UsageStore.complete_call` omits `max_tokens` when it equals the magic default `4096`, so a configured `max_tokens == 4096` is indistinguishable from "unset" and the provider default applies. Cosmetic/latent; the value is re-derived consistently elsewhere (`prompt.py:144-147`, `agent.py:498-501`). Not raised.
11. `child_process.run_bounded` (`child_process.py:173-348`): reader threads, group-liveness, deadline+cancel+stdout-cap kills, and "spawn never raises" all verified against the callers' contracts; no ownership gap found.
12. `compression._truncate_chunk` and `_split_preserve_recent` (`compression.py`): atomic tool-pair handling, defined no-fit (`None`), embedding cleared on change, and epoch-floor sorting all match their docstrings.

---

## Coverage

Read in full (current tree):
`agent.py` (2110), `orchestrator.py` (620), `turn_flow.py` (106), `turns.py` (624),
`session.py` (461), `session_mode.py` (341), `context.py` (1392),
`compression.py` (852), `assembler.py` (233), `prompt.py` (430),
`scheduler.py` (1126), `agent_factory.py` (330), `child_process.py` (348),
`execution_context.py` (122), `progress.py` (124), `usage.py` (400),
`models.py` (140).

Cross-file callsites verified for findings: `memory/consolidator.py:139-337`,
`db/connection.py:24-34`, `db/schema.sql:99-112`, `approval_resume.py:43-77`,
`core/background_tasks.py`, and the pre-extraction block
`git show 6a1131a^:machina/core/agent.py:1756-1772`.

Verdict: 4 confirmed findings (1×P2, 3×P3). No P1 found.

---

# Second pass — 2026-10-15 (independent re-read; NEW findings only)

A second, independent line-by-line read of the same slice at HEAD `1e1edbf`.
The pass above is preserved verbatim. Below: two new confirmed findings and a
list of lower-severity/dead-code observations. The request for this pass was
explicitly *new* issues in the current code, so the first pass's F1–F4 are not
re-argued here. I independently re-verified F1 (the `event_seq` order /
`(created_at, id)` cursor mismatch in `get_chunks_since` ↔
`consolidator._checkpoint_for`) and consider it confirmed.

## New confirmed findings

### N1 — P2 — Growth/RSI ContextVars set *inside* the stream generator do not survive per-step-task drivers, so `_complete_growth` is skipped for the HTTP streaming transport

**Files/lines**
- `machina/core/agent.py:91-93` — `_growth_episode`, `_rsi_choice`, `_rsi_skills` are module `ContextVar`s.
- `machina/core/agent.py:574-576` — `_prepare_context` resets them (`_growth_episode.set(None)`, …).
- `machina/core/agent.py:644-649` — growth path sets `_growth_episode.set(decision["id"])` and `_rsi_choice` while building the prompt, i.e. **inside** the `run_stream` generator body.
- `machina/core/agent.py:132-140` — the `@instrument_stream` wrapper reads it on the terminal event: `if event.type == "done": … await self._complete_growth(event.response, session_id=…)`.
- `machina/core/agent.py:1564-1570` — `_complete_growth` starts with `episode_id = _growth_episode.get(); if episode_id is None: return`.
- Driver that breaks it: `machina/api/routes/streaming.py:76-95` (`_owned_step` → `asyncio.ensure_future(awaitable)`), used per-step at `streaming.py:197` (`await _owned_step(stream.__anext__(), remaining)`), after only `begin_turn_scope()` is set in the driver context (`streaming.py:163`).
- Driver that works: `machina/gateway/server.py:989` drives `async for turn_event in engine.stream()` **inline in one task**.

**Why it is a defect**
`asyncio.ensure_future` creates a `Task`, which snapshots the current context at
creation. The SSE route resumes `TurnEngine.stream` (and through it the
`instrument_stream` wrapper and the raw `run_stream`) in a **fresh task per
step**. `_prepare_context` therefore runs — and sets `_growth_episode` — in the
*step-1 task's* context, which is discarded when that task ends. The terminal
`done` event is yielded in a *later* task whose context is a copy of the
route's driver context, which never held the value. `_growth_episode.get()`
returns `None`, so `_complete_growth` is a no-op: the `growth_episodes` row
inserted by `growth_store.begin` (`machina/memory/growth.py:187-196`) is never
marked `completed`, `agent_growth.interactions` is never incremented, RSI
evidence (`rsi.finish`) is never recorded, and the `rsi_updated` growth notice
never fires. With `personality_growth_enabled` defaulting `True`
(`machina/core/config.py:129`) this affects every HTTP streaming turn.

The code base already knows this class of bug: the SSE route sets the turn-mode
snapshot in the *driver* context before creating step tasks precisely because a
ContextVar set inside the generator "would not survive across steps"
(`streaming.py:150-163`, and the warning in `turn_engine.py:293-298`). The
growth ContextVars were not given the same treatment.

**Falsification attempt**
- Refuted if the SSE driver resumed the generator in a single task. It does not: `_owned_step` wraps every `stream.__anext__` in its own `asyncio.ensure_future` (`streaming.py:77,197`). Confirmed.
- Refuted if a generator shares its caller's context object rather than the resuming task's. It resumes in the *task's* context (Task snapshots context at creation; mutations in a child task do not propagate back). The route's own turn-scope comment depends on exactly this. Confirmed.
- Refuted if growth completion happens anywhere else on this path. `_complete_growth` has only two call sites (`agent.py:112`, `:135`), both inside the wrappers; the SSE transport registers no growth listener and never sets the ContextVar. Confirmed.
- Refuted if the existing test covers it. `tests/test_personality_growth.py:70-87` sets `_growth_episode` and drains the stream with a plain `async for` (one task) — it cannot exercise per-step tasks. Confirmed latent.
- Scope: gateway/TUI and `run()` are single-task and unaffected. Background learning is degraded, not the answer path → P2.

### N2 — P2 — HELD-55's `event_seq` is dropped at the read boundary and re-derived in id order by compaction write-back and fork, so history reorders after any auto/explicit compaction (or fork)

**Files/lines**
- Read boundary drops the ordinal: `machina/core/models.py:46-59` (`ContextChunk` has no `event_seq`), `machina/core/serialization.py:25-46` (`row_to_chunk` never reads it). Every `get_*` reader therefore returns ordinal-less chunks.
- `machina/core/context.py:669-701` (`get_all_chunks`) pages through `get_chunks_before`, whose ORDER BY is `(COALESCE(created_at, epoch) DESC, id DESC)` — **id**, not `event_seq` (`context.py:578`, `:595`).
- Compaction consumes that order: `machina/services.py:159-160` (`all_chunks = await context_manager.get_all_chunks(...)`), `:181-193` (`acompress`), `:204-210` (`replace_chunks_by_ids(..., [c.id for c in chunks], result.compressed_chunks, ...)`).
- Write-back re-inserts without the ordinal: `machina/core/context.py:906-914` — INSERT columns are `(id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, search_text, created_at)`; `event_seq` is omitted, so the column DEFAULT `nextval('context_chunks_event_seq')` (`machina/db/connection.py:26-34`) assigns a *fresh* ordinal in physical insertion order.
- Compressor output order is id/timestamp based: `machina/core/compression.py:654-663` (`_truncate_compress` re-sorts `selected` by `(created_at, str(id))` DESC) and `:336-369` (`_finalize_result` = `compressed + recent_chunks`); the `recent_chunks` slice comes from `get_all_chunks` order (`:380-381`).
- Same omission in `machina/core/session.py:372-381` (`fork`): `INSERT … SELECT …` lists the same columns minus `event_seq` and has **no ORDER BY**.
- The HELD-55 commit `97c6197` updated the ORDER BY of `get_chunks`, `get_recent_context`, `get_chunks_since`, `get_transcript_chunks` only; it left `get_chunks_before`/`get_all_chunks` and the two re-insert paths on the id tiebreak.

**Why it is a defect**
HELD-55's contract (schema comment `machina/db/schema.sql:99-112`) is "`event_seq`
is assigned in original event order … the UUID (id) stays the final fallback".
Compaction deletes and re-inserts every non-document chunk, so the ordinal is
re-derived from the *id-ordered* snapshot rather than the original event order;
for any `created_at`-tied group (one `add_chunks_batch` transaction: the same
turn's user/tool/assistant events) the recovered replay order is the UUID order,
which is exactly the defect HELD-55 fixed. `fork` does the same for a copied
session. Since `auto_compaction_enabled`/`compression_enabled`/`compression_llm_summarize`
all default `True` (`config.py:223,227,241`), a long session loses the fix after
its first auto-compaction; `get_recent_context` (the primary prompt reader) then
replays a batch shuffled. This also worsens first pass's F1: the consolidation
window still keys on `(created_at, id)` while the re-derived ordinals no longer
match original event order.

**Falsification attempt**
- Refuted if `replace_chunks_by_ids`/`fork` preserved `event_seq`. They cannot: `ContextChunk` has no such field (so the compressor could not carry it even if it wanted) and both INSERTs omit the column, so the DEFAULT fires. Confirmed.
- Refuted if `get_all_chunks` already ordered by `event_seq`, so the input order matched. It orders via `get_chunks_before` on `(created_at, id)` (`context.py:578,595`). Confirmed.
- Refuted if `_truncate_compress`/`_finalize_result` produced original event order for tied rows. They sort on `str(id)` (`compression.py:661`). Confirmed.
- Refuted if compaction replaced only a subset. `original_ids = [c.id for c in chunks]` and `chunks` excludes only documents (`services.py:160,206`), so all conversational rows are replaced. Confirmed.
- Reordering is only observable within a `created_at`-tied group (cross-batch order is preserved by `created_at`), i.e. exactly the case HELD-55 exists for. Severity P2 (normal-path, silent, defeats a landed fix); no data loss.

## Lower-severity / dead-code observations (Q — not raised as findings)

1. `agent.py:1357-1363` (`_flush_pending_cancellation_safe`) clears the caller's
   `pending_chunks` unconditionally after a *shielded* call to
   `_flush_pending_chunks`, which since PB-1 swallows non-cancel write errors
   (log-and-continue, `agent.py:1324-1342`). So the "retained for retry"
   invariant no longer holds on this path. In practice this path is terminal
   (the stack unwinds and `raise`s; see every call site), so no retry is lost —
   matches first-pass item 1, but the mechanism is now "clear always runs"
   rather than "the exception skips the clear". Worth a follow-up comment.
2. Dead checkpoint branch in compression: `_checkpoint_chunk`
   (`compression.py:49-80`) and the `method != "llm_summarize"` prepend in
   `_finalize_result` (`:354-357`) are only reachable when a caller passes
   `checkpoint=`, and the only production caller (`services.py:181-193`) passes
   none — `grep 'checkpoint=' machina/` finds no production site (tests only).
   If ever wired up, note `_checkpoint_chunk` does not set `created_at`, so the
   chunk defaults to `now()` and would sort *newest*, contradicting the
   ordering intent stated for the LLM path at `compression.py:529-535`.
3. `turns.py:559-624` `TurnOwnership` is constructed by `TurnEngine`
   (`turn_engine.py:216`) but only `start_renewal` is used; `stop_renewal`
   (`turns.py:613-624`) has no production caller (TurnEngine owns cancellation
   in `_release_claim`). Its `except (asyncio.CancelledError, Exception)` around
   `await asyncio.gather(...)` would also swallow a cancellation delivered
   during the join — latent only because it is dead.
4. `turns.py:124-125` `_live_claim_where` treats `claim_expires_at IS NULL` as
   *free*, while `_live_claim` (`:186-188`) and `owns_claim` (`:289-291`) treat
   a NULL expiry as *live*. No write path sets an owner without an expiry
   (`_db_release` nulls both), so it is currently unreachable — but the two
   predicates disagree in principle.
5. `usage.py:46-51` — the `except ValueError` in `_estimate_tokens` is dead:
   `get_token_count` already converts tiktoken's ValueError to the `len//4`
   fallback internally (`assembler.py:39-60`). `usage.py:106-108` also drops an
   explicit `max_tokens == 4096` (indistinguishable from unset) — cosmetic.
6. `agent.py:592-609` — the non-`pending_chunks` branch of `_prepare_context`
   inserts the user chunk *before* `get_recent_context` (`:610`), so the current
   message would appear both in "Recent Activity" and as the current query.
   Production always passes `pending_chunks` (`agent.py:1648-1653,1856-1861`),
   so this is reachable only from tests (`tests/test_skill_runtime.py:86,95`).
7. `agent_factory.py:52` and `:89` — source text is mojibake (`â€”` = the UTF-8
   bytes of `—` decoded as Latin-1), unlike the rest of the tree's real em
   dashes. Purely cosmetic docstring corruption, but concrete and current.

## Coverage (this pass)

Re-read in full: `agent.py`, `orchestrator.py`, `turn_flow.py`, `turns.py`,
`session.py`, `session_mode.py`, `context.py`, `compression.py`, `assembler.py`,
`prompt.py`, `scheduler.py`, `agent_factory.py`, `child_process.py`,
`execution_context.py`, `progress.py`, `usage.py`, `models.py`. Cross-checked
callsites: `api/routes/streaming.py:76-197,369-429`, `gateway/server.py:770-1009`,
`core/turn_engine.py:286-323`, `core/background_tasks.py`, `services.py:130-260,364-407`,
`memory/consolidator.py:139-337`, `memory/growth.py:174-369`,
`db/connection.py:20-34`, `db/schema.sql:59-112`, `tools/file.py:112-231`,
`serialization.py:25-46`.

Verdict (this pass): 2 new confirmed findings (2×P2) plus 7 Q observations.
No P1 found.
