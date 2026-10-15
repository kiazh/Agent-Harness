# Final Review — line pass: memory + connectors slice

- Date: 2026-10-15
- Scope (post-fix state, every line read): `machina/memory/{redaction,store,consolidator,approval,shared_bus,identity,persona,forgetting,scorer,user_profile,embeddings,models}.py` and `machina/connectors/{oauth,models,oauth_callback,policy,execution,manager,registry,store,mcp_client,service}.py`. Read-only dependencies consulted to verify cross-module contracts: `connectors/oauth_network.py`, `oauth_models.py`, `oauth_store.py`, `oauth_auth.py`, `http_transport.py`, `core/context.py`, `core/agent.py`, `db/schema.sql`, `gateway/features/connector_oauth.py`.
- Method: static line-by-line; callsite verification; **no runtime execution** (per lane rules). A falsification attempt is recorded per finding.
- Corpus fixtures with synthetic secrets are ignored by design.
- Prior lanes acknowledged as fixed and not re-reported: R4-016/089 redaction, R5-N/Nb source+audit redaction, R5-F1 cursor lock scope, R5-F2/HELD lanes (chunker/context), R4-089 memory bullets.

## Findings (severity-ranked)

### F1 — P2 — Consolidation cursor boundary and page order disagree on the tie-break; a page split inside one transaction's `created_at` group silently drops chunks
`machina/memory/consolidator.py:277-284, 307-321` ↔ `machina/core/context.py:618-633`

`ConsolidationResult`'s watermark is the **maximum `(created_at, id)`** of the page actually processed (`_checkpoint_for`, `consolidator.py:310-321`), and the *resumed* window is fetched with that tuple as a keyset bound:

```
get_chunks_since(..., since_time, since_id, limit=100)
  WHERE (COALESCE(created_at, epoch), id) > (COALESCE($2, epoch), $3)      -- boundary uses id
  ORDER BY created_at ASC, event_seq ASC NULLS LAST, id ASC               -- order uses event_seq
```

`machina/core/context.py:623-625`. The two disagree whenever rows share `created_at`. That is the **normal** case for a batch: `created_at TIMESTAMPTZ DEFAULT now()` (`db/schema.sql:69`) and `add_chunks_batch` inserts the whole turn in **one statement** (`context.py:101-105`), so every row of a batch carries one value of `now()` — HELD-55 documents exactly this ("the whole batch shares one transaction created_at", `context.py:259-267, 102-104`) and its probe asserts `distinct_created_at=1`.

Mechanism: page 1 is the oldest 100 rows ordered by `(created_at, event_seq, id)`. When the 100th row falls inside a batch `B`, the page contains only `B`'s prefix (by `event_seq`). The checkpoint is the **max id** in the page (a `uuid4`, i.e. unrelated to `event_seq`). Page 2 then returns `(created_at, id) > (T_B, max_id)`, which excludes the *unprocessed* tail of `B` whose random ids are `<= max_id` (≈ 5/6 of a same-timestamp remainder, since `max_id` is the max of ~100 random ids). Those chunks are never re-fetched: their `(created_at, id)` is at or below the watermark, so consolidation permanently skips them (silent memory loss; no duplicate/re-extraction, because the boundary is id-ordered).

Reachability: any session whose chunk count exceeds `MAX_CHUNKS_PER_CONSOLIDATION = 100` (`consolidator.py:73`) and whose 100-multiple boundary falls inside a batch. The agent flushes one batch per turn / per tool batch (`core/agent.py:1799, 1748`), so once a session passes 100 chunks a straddling batch is the norm.

Severity: P2 — silent, permanent loss of memory-extraction input on long sessions. Not a security boundary failure.

Falsification attempt: the finding is void if `created_at` is unique per row (no ties) — contradicted by `DEFAULT now()` and HELD-55's own `distinct_created_at=1` probe; or if a page boundary can never sit inside a tied group — contradicted for any session with `> 100` chunks; or if the checkpoint were the *order-last* row `(created_at, event_seq, id)` rather than the max id — it is not (`consolidator.py:311-317`). Consistent fix: make the boundary predicate and the `ORDER BY` use the same key (e.g. `(created_at, event_seq, id)` with `event_seq` added to the cursor), or compute the checkpoint from the page's `ORDER BY` key rather than `max(id)`.

### F2 — P2 — Pattern screener bounds nested repetition *counts* but not atom/body size
`machina/connectors/policy.py:95-96, 100-171, 216-220`

`_screen_pattern_budget` charges `group_expansion[-1]` only for counted quantifiers *inside* a group (`pending`, lines 137-147) or an uncounted inner repetition (`elif group_expansion`, 148-149). A plain grouped atom whose body has **no** counted quantifier is popped with `closed == 0` (lines 168-170), so `pending` becomes `None` and a following `{n}` is never attributed (`elif group_expansion:` with the just-popped stack now empty → no charge). `(<~193 literal chars>){10000}` therefore passes `_check_schema`'s 200-char cap (line 218) and `_screen_pattern_budget`, even though it is the same compile-time expansion class the module documents at lines 85-94 (`a{100000000}` freezes compilation; the `regex` timeout covers **matching only**, not compilation — line 36). Body length is unbounded relative to the count: `~193 × 10_000 ≈ 1.9e6` nodes, ~200× the "cheap (<=10k nodes per count)" assumption at line 90, compiled synchronously on the event loop from a **server-supplied** schema (`validate_arguments` → `_check_schema` → `_validate` → `regex.search`, lines 386-406, 274-285). Amplification is bounded (~2e6), not unbounded.

Severity: P2 — untrusted-input synchronous event-loop stall (same class as the mitigated `a{100000000}`), bounded ~200×.

Falsification attempt: void if the `regex` engine stores a repeat node instead of unrolling a counted group's body — contradicted by the module's own justification (lines 85-94: counted repetitions are unrolled at COMPILE time, which is why nested counts are multiplicative and scale with body size). The existing tests (`tests/test_connectors_policy.py`) exercise only single-char atoms and small bodies, so this dimension is untested. Fix: charge `pending`/`group_expansion` by atom/body length (or cap `count × pattern_len`), not only by nested counts.

### F3 — P3 — OAuth generation guard does not cover a *completed* logout (grant-resurrection window)
`machina/connectors/oauth.py:522-526, 557-595` ↔ `machina/connectors/oauth_store.py:366-404, 487-493`

The "Generation guard: logout/remove before exchange completes" captures `expected_gen = current_generation(binding)` **after** the browser wait and before the token exchange (line 526). `current_generation` returns `max(grant_gen, tombstone_gen)` (`oauth_store.py:487-493`). If `forget()`'s tombstone commits before line 526, `expected_gen == T`; the later `save(record, expected_generation=T)` then sees `floor = max(current_gen, T) = T == expected_generation` with `record.generation = T + 1 == floor + 1` (`oauth_store.py:389-404`) and commits a fresh grant, undoing the logout.

Falsification attempt: `forget()` cancels every operation task for the connector (`oauth.py:938-945`) before retiring credentials, and `_run_operation` re-checks `operation["state"] == "cancelled"` under the lock (519-521) and cannot cross the exchange/`save` awaits without a `CancelledError` being delivered (re-raised, 627-637). The window therefore needs the task to reach line 526 *after* the tombstone while never observing cancellation — a narrow interleave, hence P3 rather than P2. A durable fix is a CAS that rejects when a tombstone exists at/after the captured write (require `floor == expected_gen and tombstone_gen <= expected_gen`).

### F4 — P3 — `OAuthCoordinator.forget()` accepts `owner` but never checks it
`machina/connectors/oauth.py:933-936`

`forget(self, connector_id, owner, *, expected_generation, revoke)` reads `config = await self._resolve_config(connector_id)` and never references `owner`; the connector/owner authorization that `cancel`/`status`/`authorization_url` enforce via `_validate_handle_config` (`oauth.py:841-853`) is absent. The only production caller enforces owner first (`gateway/features/connector_oauth.py:28-39, 193-197`), so this is latent.

Falsification attempt: void if a caller exists that reaches `forget` without a prior owner check. Today none does (the sole route is `mcp_oauth_forget` → `_authorized_owner`), so defense-in-depth only. It becomes a real gap if a future caller invokes `forget` directly (the parameter implies an intended in-method check).

### F5 — P3 — `StreamingSecretRedactor.flush()` silently drops the withheld prefix of an oversized open PEM
`machina/memory/redaction.py:862-872, 890-898`

In the `_pem_open` branch, `flush()` sets `_buf = ""`, `_pem_prefix = ""` and returns `""` (lines 892-898). `_pem_prefix` is withheld text that preceded the oversized unterminated block (set at line 869) and was never emitted; it is discarded rather than emitted. Fail-closed (no secret leaks — the block body was replaced by the marker at line 871), but silent content loss on the streamed output.

Falsification attempt: reachable only when a `-----BEGIN … PRIVATE KEY-----` block exceeds `MAX_BUFFERED_PEM = 32 KiB` (line 518) and its `END` never arrives before `flush()`; the emitted marker plus a short prefix loss is the documented bounded behavior, so this is a correctness/quality residual, not a leak.

### F6 — Q — `MemoryStore.add` drops an explicit `connection` when routing to the approval gate
`machina/memory/store.py:82-110`

When the gate is enabled and `bypass_approval` is False, `add` returns the pending `MemoryEntry` (lines 100-110) and never uses the `connection` argument. A caller that supplies a transaction connection but does **not** pass `bypass_approval=True` would get a pending row written on the shared `db` (inside `gate.submit`), not a write inside the caller's transaction — a surprising contract.

Falsification attempt: no live caller does this — `approval.approve` (`approval.py:247-261`), `_approve_all_locked` (`approval.py:406-420`) and `shared_bus.deliver` (`shared_bus.py:120-134`) all pass `bypass_approval=True`, and `consolidator` passes no connection (`consolidator.py:467-476`). Latent only; Q.

### F7 — Q — `execute_call` concurrency-wait timeout can leak a semaphore permit
`machina/connectors/execution.py:230-241`

`await asyncio.wait_for(semaphore.acquire(), timeout=…)` releases the permit only on the success path (`finally: semaphore.release()`, line 281). If the timeout fires after `Semaphore.acquire()` has decremented `_value` but before `wait_for` delivers the result (the classic `wait_for` cancellation race), the `TimeoutError` path raises `ConnectorToolError` without releasing, leaking one permit. Repeated occurrences permanently shrink the per-connection `_harness_call_limit` (`config.concurrency`, default 4), eventually stalling the connector until reconnect.

Falsification attempt: void if `asyncio.wait_for` can never raise `TimeoutError` for an `acquire()` that already completed. Modern CPython (`asyncio.timeout`-based `wait_for`) is designed to return the completed result rather than convert to `TimeoutError`, so the race is version-dependent and unconfirmed without runtime execution — reported as Q (latent). A defensive `try/finally` around the acquire (release when the semaphore was in fact acquired) removes the exposure regardless.

### F8 — Q — Connector inventory file I/O (blocking flock + read/write) runs on the event loop
`machina/connectors/service.py:69-70, 148, 171` ↔ `machina/connectors/store.py:34-60, 62-92, 107-135`

`ConnectorService._refresh_locked` calls `self.store.load()` and `_configure_locked`/`_remove_locked` call `store.configure`/`store.remove` directly from `async def`. `ConnectorStore` is fully synchronous: `load()` does `stat()` + `read_text()`, and `configure`/`remove` take a **blocking** `fcntl.flock(LOCK_EX)`/`msvcrt.locking` (`store.py:48, 56`) and fsync (`store.py:156-165`). Under contention with another worker the blocking flock stalls the whole event loop; the whole operation is local and small, so impact is latency rather than correctness.

Falsification attempt: void if a `to_thread`/executor wrapper exists on the async side — there is none in `service.py`; the calls are direct (lines 70, 148, 171). Not a correctness defect (Q) for the current single-installation, small-file workload.

## Hot spots examined and found sound (no finding)

- **Redaction batch↔stream divergence**: partial table `_PARTIAL_RES` (`redaction.py:533-592`) mirrors the complete patterns incl. the R4-016 left-boundary rule (`sk-` glued matches withheld in neither world); every emission is re-redacted (`redact_secrets(safe)`, line 860) so a frontier that legitimately lands at end-of-buffer still redacts whole; `_stream_frontier` word-boundary pullback (732-737) and unanchored overlap expansion (740-751) prevent splitting a complete match; digit-run hold (`_DIGIT_RUN_RE`, 611) is end-terminated by the first non-digit/space/dash, matching R4-089; configured-secret handling retains partial suffixes and never splits a complete value (847-856). Luhn gate applied identically in the emitted prefix (357-366). No divergence found beyond F5.
- **Approval routing / bypass**: `_approval_gate` seam (`store.py:30-42`) resolves the same singleton; `is_enabled()` OR-composes the flag and `memory_approval_enabled` (`approval.py:119-130`); approve/approve_all/reject take `FOR UPDATE` + transaction (`approval.py:234-287, 369-448`); the `unnest`-paired batch update aligns `pending_ids`/`memory_ids` positionally (`approval.py:424-440`); `bypass_approval=True` only on the three completion paths. Gate routing re-redacts inside `submit` (`approval.py:151-153`), so no raw secret reaches `pending_memories`.
- **`update_access` quarantine**: single (`store.py:356-369`) and batch (`371-382`) both filter `quarantined = FALSE`; retrieval/search/get also exclude quarantined by default (`219-343`).
- **Shared-bus receipts/dedup**: one durable receipt per `(source, recipient)` (`shared_bus.py:63-73, 150-176`); the missing-provenance rejection is deliberately **not** cached (`retryable`, 97-106); copy + receipt commit atomically; `ON CONFLICT DO NOTHING` + re-fetch handles the concurrent winner (164-176); re-delivery is blocked by `provenance.source_agent != source.agent_id` (107).
- **Consolidator serialization (R5-F1)**: `cursor_reader` evaluated inside the advisory lock (`consolidator.py:273-274`) and `checkpoint_writer` commits inside the same lock scope incl. the no-DB and lock-unavailable fallbacks (177-210); `entered_impl` prevents body re-run (184-210); agent callsite routes the built-in consolidator through the serialized contract (`core/agent.py:1516-1535`). (The cursor *key* itself is F1.)
- **Connector OAuth refresh state machine**: `_owned_refresh_lock` records the owning task and `refresh_auth_cache` coalesces on an in-flight refresh without self-deadlock (`oauth.py:1196-1233`); crash journals (`refresh_state != "idle"`) fail closed (1209-1211, 1279-1281, 1297-1299); rotation preserves `grant_id`/`generation` only for an unchanged scope set and otherwise raises (1340-1385); store CAS rejects forward skips (`oauth_store.py:389-404`).
- **OAuth callback**: exact-path + duplicate-Host rejection + `_consumed` once-only delivery, with invalid requests (wrong state/path/host, non-ASCII state guarded by `isascii()` before `compare_digest`) never consuming the legitimate attempt (`oauth_callback.py:203-241`); `iss` enforced only when advertised/required (231-241); socket count bounded and decremented in `finally` (136-139, 276-277).
- **Connector lifecycle**: `manager.connect` bounds retries and opens the circuit only on retryable `ConnectionFailed`, never on `ConfigError`/`AuthRequired` (`manager.py:106-141`); `set_tools` collision/rollover is per-connector (`registry.py:85-118`); `connect_and_discover` retires the connection on any `BaseException` (`service.py:216-223`); `service._refresh_locked` retires changed/removed connectors and invalidates OAuth with the old revision (`service.py:69-93`).
- **Thread-offload / DNS**: `run_dns_check` uses a bounded daemon-thread pool, releases its slot in the worker, and consumes late outcomes (`http_transport.py:17-72`); `pinned_http_transport` resolves once and dials only approved numeric IPs with the original SNI/Host (`http_transport.py:85-144`); OAuth index reads use `asyncio.to_thread` (`oauth.py:773, 996, 1033`); store reads/writes are atomic + fsynced (`oauth_store.py:100-131`).
- **Port validation**: `ConnectorConfig.validate` eagerly reads `parts.port` so `:99999`/`:abc` become a typed `ConfigError` (`connectors/models.py:179-189`); `OAuthSettings.from_dict` requires `port == 0 or 1024..65535` and rejects bool (`oauth_models.py:184-187`).

## Residual / observational (not defects)

- `_run_operation` computes `settings` from the pre-approval config read (`oauth.py:419`) and re-checks `config.revision` after approval (462-466); a same-revision oauth-settings change is impossible (any connector update bumps `revision`), so no gap.
- `preview()` releases `self._lock` across discovery, so its "replace older preview for this connector/owner" cleanup (`oauth.py:164-171`) only covers previews existing at entry; two concurrent previews for the same connector/owner can coexist, bounded by `OAUTH_MAX_PREVIEWS` (242-246). Resource-timing only.
- `_PEM_DASH_RE`/`_PEM_WORD_RE` are applied unconditionally in `_stream_frontier` (`redaction.py:775-780`) although the comment says "only while a block is open" — over-hold (emission lag on trailing `-`/`BEGIN`/`END`), never a leak.

## Verdict

No P1. Confirmed P2×2 (F1 cursor boundary/order mismatch; F2 screener body-size gap), P3×3 (F3 OAuth logout/commit race, F4 `forget` owner, F5 PEM flush prefix loss) and Q×3 (F6 gate connection, F7 semaphore race, F8 blocking inventory I/O). All remaining enumerated contracts verified sound via callsites.
