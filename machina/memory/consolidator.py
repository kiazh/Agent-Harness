"""Memory consolidator — extract durable memories from conversation chunks.

Pipeline: raw chunks → LLM extraction → importance scoring → dedup → write
"""

from __future__ import annotations

import inspect
import json
import logging
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager

import asyncpg

from machina.core.context import NULL_EVENT_SEQ_ASC, context_manager
from machina.core.models import ContextChunk
from machina.core.provider import LLMProvider, audit_log
from machina.db.lazy import LazyDb
from machina.memory.approval import PendingMemory
from machina.memory.models import MemoryEntry
from machina.memory.scorer import ImportanceScorer
from machina.memory.store import MemoryStore, memory_store
from machina.observability.diagnostics import record_failure
from machina.plugins.registry import plugin_registry

__all__ = ["ConsolidationResult", "MemoryConsolidator"]

db = LazyDb()


class ConsolidationResult:
    """Typed consolidation outcome (AH-AUDIT-037).

    status: "complete" (all writes durable or queued for approval —
    checkpoint valid),
    "empty" (successful extraction with zero memories — checkpoint valid,
    the range was fully processed), "partial" (some writes failed —
    checkpoint None, the range stays retryable), "failed" (extraction
    failed — checkpoint None, nothing advances).
    Only "complete"/"empty" carry a checkpoint, and only for the fully
    processed contiguous input. Retries are idempotent via normalized-
    content dedup; concurrent consolidations share the LP-14 single-flight.
    """

    def __init__(
        self,
        status: str,
        entries: list | None = None,
        checkpoint: dict | None = None,
        chunks_processed: int = 0,
    ) -> None:
        self.status = status
        self.entries = list(entries or [])
        self.checkpoint = checkpoint
        self.chunks_processed = chunks_processed

    def __bool__(self) -> bool:
        return bool(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self):
        return iter(self.entries)


logger = logging.getLogger(__name__)

# Deduplication threshold — cosine similarity above this means "duplicate"
DEFAULT_DEDUP_THRESHOLD = 0.85

# Maximum chunks to process in a single consolidation run
MAX_CHUNKS_PER_CONSOLIDATION = 100


def _cosine_similarity(a: list[float] | None, b: list[float] | None) -> float:
    """Cosine similarity for intra-batch dedup (0.0 when either is missing)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _normalize(text: str) -> str:
    """Normalize memory content for duplicate detection."""
    return " ".join(str(text).lower().split()).strip(" .!?,;:()\"'")


# System prompt for memory extraction
EXTRACTION_SYSTEM_PROMPT = """You are a memory extraction system. Given a conversation, extract durable memories that would be useful in future conversations.

Extract:
- User preferences ("user prefers concise responses")
- Decisions ("chose PostgreSQL over MongoDB for the project")
- Facts ("user's account ID is 4782, subscription tier: Pro")
- Task outcomes ("deployment failed due to port conflict")

Do NOT extract:
- Small talk and greetings
- Redundant confirmations ("ok", "thanks")
- Ephemeral state (weather readings, stock prices)

Return a JSON array of objects with these fields:
- content: the memory content (string)
- category: one of "preference", "decision", "fact", "event", "transient"
- importance: float between 0 and 1
- explicitly_important: boolean (true if user explicitly said "remember this")

Return ONLY the JSON array, no other text."""


class MemoryConsolidator:
    """Consolidates raw context chunks into durable long-term memories.

    Follows the research-backed pipeline:
    1. Extract candidate memories via LLM
    2. Score importance
    3. Embed candidates (redaction-first) so vector dedup can see them
    4. Deduplicate against existing memories
    5. Write new memories
    """

    def __init__(
        self,
        llm_provider: LLMProvider | None = None,
        importance_scorer: ImportanceScorer | None = None,
        store: MemoryStore | None = None,
        dedup_threshold: float = DEFAULT_DEDUP_THRESHOLD,
    ) -> None:
        self.llm = llm_provider
        self.scorer = importance_scorer or ImportanceScorer()
        self.store = store or memory_store
        self.dedup_threshold = dedup_threshold

    async def consolidate_session(
        self,
        session_id: uuid.UUID,
        agent_id: str,
        since: tuple | None = None,
        *,
        cursor_reader: Callable[[], Awaitable[tuple | None]] | None = None,
        checkpoint_writer: Callable[[dict], Awaitable[None]] | None = None,
    ) -> ConsolidationResult:
        """Consolidate a session's context chunks into long-term memories.

        With *since*=(created_at, id) only newer chunks are extracted
        (LP-14 cursor, ascending continuation); without it the OLDEST
        window is processed first (FM5 W6) so a first-run backlog larger
        than one page stays reachable via subsequent ascending cursor
        runs. Returns a typed result — callers advance the durable
        checkpoint ONLY from result.checkpoint (fully processed contiguous
        input), never from an optimistic attribute.

        Cross-instance single-flight (FM5 W5): consolidations for one
        session are serialized with a transaction-scoped PostgreSQL
        advisory lock taken on a DEDICATED connection outside the shared
        pool (R10), so two processes cannot extract the same range
        concurrently and the lock can never consume a connection the
        protected body needs. When no dedicated connection can be
        established (database unavailable, no DSN) the lock is skipped
        (the caller's in-process single-flight still applies) and
        consolidation proceeds.

        Cursor serialization (R5-F1): when *cursor_reader* is supplied the
        cursor is resolved INSIDE the advisory lock (overriding *since*), and
        when *checkpoint_writer* is supplied the fully-processed checkpoint is
        committed inside the same lock. Caller-managed cursor reads/persists
        (the pre-R5-F1 contract) run OUTSIDE the lock, letting two workers read
        one stale cursor and republish an older checkpoint after a newer one
        lands; routing both through this call makes read+extract+commit one
        serialized operation so the watermark can only move forward.
        """
        if not getattr(db, "connected", False):
            result = await self._consolidate_session_impl(
                session_id, agent_id, since, cursor_reader=cursor_reader
            )
            await self._commit_checkpoint(result, checkpoint_writer)
            return result

        entered_impl = False

        try:
            async with self._advisory_lock(session_id) as locked:
                if not locked:
                    logger.debug(
                        "No dedicated lock connection for session %s; "
                        "consolidating without a cross-process lock",
                        session_id,
                    )
                # Any exception from here on comes from the consolidation
                # body: never re-run it (double extraction/writes).
                entered_impl = True
                result = await self._consolidate_session_impl(
                    session_id, agent_id, since, cursor_reader=cursor_reader
                )
                await self._commit_checkpoint(result, checkpoint_writer)
                return result
        except Exception as e:
            if entered_impl:
                raise
            record_failure("consolidator.consolidate_session.lock", e)
            result = await self._consolidate_session_impl(
                session_id, agent_id, since, cursor_reader=cursor_reader
            )
            await self._commit_checkpoint(result, checkpoint_writer)
            return result

    @staticmethod
    async def _commit_checkpoint(
        result: ConsolidationResult,
        checkpoint_writer: Callable[[dict], Awaitable[None]] | None,
    ) -> None:
        """Advance the durable watermark while the serialization lock is held.

        Called from inside the advisory-lock scope so the checkpoint write is
        ordered against every other worker's cursor read/advance for this
        session. No-op without a writer or a fully-processed checkpoint.
        """
        if checkpoint_writer is None:
            return
        checkpoint = getattr(result, "checkpoint", None)
        if checkpoint:
            await checkpoint_writer(checkpoint)

    @asynccontextmanager
    async def _advisory_lock(self, session_id: uuid.UUID) -> AsyncGenerator[bool, None]:
        """Transaction-scoped cross-process lock on a dedicated connection (R10).

        The lock connection is deliberately NOT taken from the shared pool:
        the protected consolidation body acquires pool connections of its own
        (context, memory, usage), so a pool-held lock would consume a
        connection that the work then waits for — deadlocking a one-connection
        pool and starving the default pool under concurrent consolidations.

        Yields True once the advisory lock is held, False when no dedicated
        connection can be established (no DSN / test double); callers then
        proceed with only the in-process single-flight, the same degradation
        as an unavailable database.
        """
        dsn = getattr(db, "dsn", None)
        if not isinstance(dsn, str) or not dsn:
            yield False
            return
        conn = await asyncpg.connect(dsn, timeout=10)
        try:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"memory_consolidate:{session_id}",
                )
                yield True
        finally:
            await conn.close()

    async def _consolidate_session_impl(
        self,
        session_id: uuid.UUID,
        agent_id: str,
        since: tuple | None = None,
        *,
        cursor_reader: Callable[[], Awaitable[tuple | None]] | None = None,
    ) -> ConsolidationResult:
        """Consolidation body; the caller owns any serialization lock.

        When *cursor_reader* is supplied it is evaluated here — i.e. under the
        caller's lock — so the extraction window is resolved from the current
        durable cursor rather than a value the caller read before locking.
        """
        if cursor_reader is not None:
            since = await cursor_reader()

        # Step 0: Get context chunks (cursor-filtered when resuming).
        if since is not None:
            # Cursor shape: legacy ``(created_at, id)`` or the HELD-55 F1
            # ``(created_at, event_seq, id)``. Both resolve; a missing seq keeps
            # the legacy 2-key predicate path (no reinterpretation of a range
            # stored before the ordinal existed).
            since_time = since[0]
            since_id = since[-1]
            since_seq = since[1] if len(since) >= 3 else None
            cursor_kwargs: dict = {
                "since_time": since_time,
                "since_id": since_id,
                "limit": MAX_CHUNKS_PER_CONSOLIDATION,
            }
            if since_seq is not None:
                cursor_kwargs["since_seq"] = since_seq
            chunks = await context_manager.get_chunks_since(session_id, **cursor_kwargs)
        else:
            # FM5 W6: oldest-first so the backlog drains in bounded pages.
            try:
                pending = context_manager.get_chunks_since(
                    session_id,
                    since_time=None,
                    since_id=None,
                    limit=MAX_CHUNKS_PER_CONSOLIDATION,
                )
            except AttributeError:
                # Legacy context doubles without the since API.
                pending = None
            if pending is not None and inspect.isawaitable(pending):
                chunks = await pending
            else:
                chunks = await context_manager.get_chunks(
                    session_id, limit=MAX_CHUNKS_PER_CONSOLIDATION
                )
        if not chunks:
            logger.debug("No chunks to consolidate for session %s", session_id)
            return ConsolidationResult("empty", [], None, 0)

        # Checkpoint identity: the maximum (created_at, event_seq, id) actually
        # processed — never a positional guess, and the SAME 3-key the window
        # is ordered on (HELD-55 F1). A NULL event_seq (legacy row) sorts last
        # via the bigint-max sentinel, matching `NULLS LAST` in the query.
        # Initial newest-first windows (DESC) and ascending cursor
        # continuations both resolve correctly.

        def _checkpoint_for(processed: list) -> dict:
            latest = max(
                processed,
                key=lambda c: (
                    c.created_at.isoformat() if getattr(c, "created_at", None) else "",
                    (
                        NULL_EVENT_SEQ_ASC
                        if getattr(c, "event_seq", None) is None
                        else int(c.event_seq)
                    ),
                    str(getattr(c, "id", "")),
                ),
            )
            checkpoint = {
                "id": str(latest.id),
                "at": latest.created_at.isoformat() if latest.created_at else None,
            }
            checkpoint["seq"] = (
                NULL_EVENT_SEQ_ASC
                if getattr(latest, "event_seq", None) is None
                else int(latest.event_seq)
            )
            return checkpoint

        # Step 1: Format conversation for LLM
        conversation = self._format_conversation(chunks)

        # Step 2: Extract candidate memories via LLM
        try:
            candidates = await self._extract_memories(conversation, session_id, agent_id)
        except Exception:
            logger.exception("Memory extraction failed for session %s", session_id)
            # Failed extraction: no checkpoint — the range stays retryable.
            return ConsolidationResult("failed", [], None, len(chunks))
        if not candidates:
            logger.debug("No memories extracted from session %s", session_id)
            # Successful empty extraction differs from failure: the range
            # was fully processed, so the checkpoint validly advances.
            return ConsolidationResult("empty", [], _checkpoint_for(chunks), len(chunks))

        await plugin_registry.dispatch("on_memory_extract", candidates)

        # Step 3: Score importance
        for candidate in candidates:
            candidate.importance = self.scorer.score(candidate)

        eligible = [c for c in candidates if c.importance >= 0.2]

        # Step 3.5: Redaction-before-embedding ingestion (AH-AUDIT-038) —
        # BEFORE dedup so the vector comparison in step 4 can actually see the
        # candidates. LLM extraction returns embedding=None; comparing first
        # would leave only exact-text dedup and let paraphrases above the
        # dedup threshold through as duplicates. Redacted content is embedded
        # through the same query embedder, so candidate and query embeddings
        # share an enforced space. No-key operation is preserved (embedding
        # stays None, keyword retrieval unaffected); embedding failures never
        # drop the memory.
        try:
            from machina.memory.embeddings import embed_query as _embed_query
            from machina.memory.redaction import redact_secrets as _redact_mem

            for candidate in eligible:
                if candidate.embedding:
                    continue
                try:
                    redacted = _redact_mem(candidate.content or "").text
                    if not redacted.strip():
                        continue
                    candidate.embedding = await _embed_query(redacted[:2000])
                except Exception as _boundary_error:
                    # Optional fallback preserves the primary outcome; report no payload.
                    record_failure("consolidator.candidate_embedding", _boundary_error)
                    candidate.embedding = None
        except Exception as _boundary_error:
            # Optional fallback preserves the primary outcome; report no payload.
            record_failure("consolidator.consolidate_session", _boundary_error)

        # Step 4: Deduplicate against existing memories (batch)
        new_memories: list[MemoryEntry] = []
        candidates_with_embedding = [c for c in eligible if c.embedding]
        candidates_without_embedding = [c for c in eligible if not c.embedding]

        # Existing memories are needed by both dedup paths; fetch once.
        existing_memories = (
            await self.store.search(agent_id=agent_id, limit=1000) if eligible else []
        )

        # Batch check for duplicates using SQL vector similarity
        # instead of O(n*m) nested loop cosine similarity.
        if candidates_with_embedding:
            # Use search_by_embedding for each candidate — this leverages
            # the HNSW index for O(log n) similarity search per candidate,
            # avoiding the O(n*m) nested loop over all existing memories.
            # Intra-batch seen tracking prevents duplicates within this batch.
            seen_norms: set[str] = {_normalize(m.content) for m in existing_memories}
            seen_embeddings: list[list[float]] = []
            for candidate in candidates_with_embedding:
                is_duplicate = False
                key = _normalize(candidate.content)
                if not key:
                    logger.warning("Skipping empty normalized memory content (batch dedup)")
                    continue
                if key in seen_norms:
                    match = next(
                        (m for m in existing_memories if _normalize(m.content) == key),
                        None,
                    )
                    if match is not None and getattr(match, "id", None) is not None:
                        try:
                            await self.store.update_access(match.id)
                        except Exception as _boundary_error:
                            # Optional fallback preserves the primary outcome; report no payload.
                            record_failure("consolidator.consolidate_session", _boundary_error)
                    is_duplicate = True
                else:
                    # Intra-batch embedding similarity against already-accepted batch items
                    for prev_emb in seen_embeddings:
                        if (
                            _cosine_similarity(candidate.embedding, prev_emb)
                            >= self.dedup_threshold
                        ):
                            is_duplicate = True
                            break
                if is_duplicate:
                    continue
                # Use the store's vector search with a tight threshold
                similar = await self.store.search_by_embedding(
                    embedding=candidate.embedding,
                    agent_id=agent_id,
                    limit=5,
                )
                for existing, similarity in similar:
                    if similarity >= self.dedup_threshold:
                        await self.store.update_access(existing.id)
                        is_duplicate = True
                        break
                if not is_duplicate:
                    seen_norms.add(key)
                    if candidate.embedding:
                        seen_embeddings.append(candidate.embedding)
                    new_memories.append(candidate)

        # Candidates whose embedding is still missing (no provider, embedding
        # failure, or empty content) stay on normalized-content dedup against
        # stored memories and within this batch.
        seen = {_normalize(m.content) for m in existing_memories}
        for candidate in candidates_without_embedding:
            key = _normalize(candidate.content)
            if not key:
                logger.warning("Skipping empty normalized memory content")
                continue
            if key in seen:
                match = next((m for m in existing_memories if _normalize(m.content) == key), None)
                if match is not None and getattr(match, "id", None) is not None:
                    try:
                        await self.store.update_access(match.id)
                    except Exception as _boundary_error:
                        # Optional fallback preserves the primary outcome; report no payload.
                        record_failure("consolidator.consolidate_session", _boundary_error)
                continue
            seen.add(key)
            new_memories.append(candidate)

        # Step 5: Write new memories
        written: list[MemoryEntry] = []
        pending_approval = 0
        write_failures = 0
        for memory in new_memories:
            try:
                entry = await self.store.add(
                    session_id=session_id,
                    agent_id=agent_id,
                    content=memory.content,
                    category=memory.category,
                    importance=memory.importance,
                    embedding=memory.embedding,
                    explicitly_important=memory.explicitly_important,
                    base_strength=memory.base_strength,
                )
                if isinstance(entry, PendingMemory):
                    # H07: approval-routed writes are queued, not durable;
                    # they must not masquerade as written MemoryEntry rows.
                    # The submission itself succeeded, so the checkpoint may
                    # advance (re-running would duplicate the queued rows).
                    pending_approval += 1
                else:
                    written.append(entry)
            except Exception as e:
                write_failures += 1
                logger.exception("Failed to write memory: %s", e)

        audit_log(
            "memory_consolidate",
            session_id=str(session_id),
            agent_id=agent_id,
            chunks_processed=len(chunks),
            candidates_extracted=len(candidates),
            new_memories_written=len(written),
            pending_approval=pending_approval,
        )

        logger.info(
            "Consolidated session %s: %d chunks → %d candidates → %d new memories"
            " (%d pending approval)",
            session_id,
            len(chunks),
            len(candidates),
            len(written),
            pending_approval,
        )
        if write_failures:
            # Partial persistence: no checkpoint — retry stays idempotent
            # via normalized-content dedup. Advancing here would skip the
            # failed range.
            return ConsolidationResult("partial", written, None, len(chunks))
        return ConsolidationResult("complete", written, _checkpoint_for(chunks), len(chunks))

    async def consolidate_from_text(
        self,
        text: str,
        session_id: uuid.UUID | None = None,
        agent_id: str = "harness",
    ) -> list[MemoryEntry]:
        """Consolidate memories directly from text (bypasses context chunks).

        Useful for testing or direct memory injection. Extraction failure
        yields [] here (no watermark exists for text injection).
        """
        try:
            candidates = await self._extract_memories(text, session_id, agent_id)
        except Exception as _boundary_error:
            # Optional fallback preserves the primary outcome; report no payload.
            record_failure("consolidator.consolidate_text", _boundary_error)
            return []
        if not candidates:
            return []

        for candidate in candidates:
            candidate.importance = self.scorer.score(candidate)

        # Dedup (same policy as consolidate_session): normalized-content check
        # against stored memories + within this batch, plus vector check when
        # embeddings are present.
        eligible = [c for c in candidates if c.importance >= 0.2]
        if not eligible:
            return []
        existing_memories = await self.store.search(agent_id=agent_id, limit=1000)
        seen = {_normalize(m.content) for m in existing_memories}
        seen_embeddings: list[list[float]] = []
        new_memories: list[MemoryEntry] = []
        for memory in eligible:
            key = _normalize(memory.content)
            if not key:
                logger.warning("Skipping empty normalized memory content (from_text)")
                continue
            if key in seen:
                match = next((m for m in existing_memories if _normalize(m.content) == key), None)
                if match is not None and getattr(match, "id", None) is not None:
                    try:
                        await self.store.update_access(match.id)
                    except Exception as _boundary_error:
                        # Optional fallback preserves the primary outcome; report no payload.
                        record_failure("consolidator.consolidate_from_text", _boundary_error)
                continue
            is_duplicate = False
            if memory.embedding:
                for prev_emb in seen_embeddings:
                    if _cosine_similarity(memory.embedding, prev_emb) >= self.dedup_threshold:
                        is_duplicate = True
                        break
                if not is_duplicate:
                    try:
                        similar = await self.store.search_by_embedding(
                            embedding=memory.embedding,
                            agent_id=agent_id,
                            limit=5,
                        )
                        for existing, similarity in similar:
                            if similarity >= self.dedup_threshold:
                                await self.store.update_access(existing.id)
                                is_duplicate = True
                                break
                    except Exception:
                        logger.warning("search_by_embedding failed during dedup", exc_info=True)
            if is_duplicate:
                continue
            seen.add(key)
            if memory.embedding:
                seen_embeddings.append(memory.embedding)
            new_memories.append(memory)

        written: list[MemoryEntry] = []
        pending_approval = 0
        for memory in new_memories:
            try:
                entry = await self.store.add(
                    session_id=session_id,
                    agent_id=agent_id,
                    content=memory.content,
                    category=memory.category,
                    importance=memory.importance,
                    embedding=memory.embedding,
                    explicitly_important=memory.explicitly_important,
                    base_strength=memory.base_strength,
                )
                if isinstance(entry, PendingMemory):
                    # H07: queued for approval, not a durable write.
                    pending_approval += 1
                else:
                    written.append(entry)
            except Exception as e:
                logger.exception("Failed to write memory: %s", e)

        if pending_approval:
            logger.info(
                "Consolidated %d memories from text; %d routed to approval",
                len(written),
                pending_approval,
            )
        return written

    def _format_conversation(self, chunks: list[ContextChunk]) -> str:
        """Format context chunks into a conversation string for LLM extraction."""
        lines: list[str] = []
        for chunk in chunks:
            chunk_type = chunk.chunk_type
            payload = chunk.payload

            if chunk_type == "user_message":
                content = payload.get("content", "")
                lines.append(f"User: {content}")
            elif chunk_type == "assistant_message":
                content = payload.get("content", "")
                lines.append(f"Assistant: {content}")
            elif chunk_type == "tool_call":
                tool = payload.get("tool", "unknown")
                args = json.dumps(payload.get("args", {}), default=str)
                result_preview = payload.get("result_preview", "")
                lines.append(f"Tool Call: {tool}({args})")
                if result_preview:
                    lines.append(f"Tool Result: {result_preview[:200]}")
            elif chunk_type == "result":
                content = str(payload)[:200]
                lines.append(f"Result: {content}")

        return "\n".join(lines)

    async def _extract_memories(
        self,
        conversation: str,
        session_id: uuid.UUID | None = None,
        agent_id: str = "harness",
    ) -> list[MemoryEntry]:
        """Use LLM to extract candidate memories from conversation.

        Returns list of MemoryEntry objects (not yet persisted). Raises on
        extraction failure (no provider, provider error, malformed output,
        schema-invalid array elements) so callers distinguish failed
        extraction (retryable, no checkpoint) from successful empty
        extraction (checkpoint valid).
        """
        if not self.llm:
            raise RuntimeError("no LLM provider available for memory extraction")

        try:
            from machina.core.usage import usage_store

            response = await usage_store.complete_call(
                self.llm,
                session_id,
                agent_id,
                messages=[
                    {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                    {"role": "user", "content": conversation},
                ],
                tools=[],
            )
            # Parse LLM response as JSON array
            content = response.content.strip()
            # Handle markdown code blocks
            if content.startswith("```"):
                lines = content.split("\n")
                # Remove first line (```json or ```) and last line (```)
                content = "\n".join(lines[1:-1])

            items = json.loads(content)
            if not isinstance(items, list):
                logger.warning("LLM returned non-list for memory extraction: %s", type(items))
                # Wrong-shape JSON ({} or a scalar) is as untrustworthy as
                # malformed JSON: a retryable extraction failure — never an
                # empty success that advances the checkpoint.
                raise RuntimeError(
                    "memory extraction parse failed: expected a JSON array, "
                    f"got {type(items).__name__}"
                )

            memories: list[MemoryEntry] = []
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    # Schema-invalid element: a retryable extraction failure
                    # (like malformed JSON), never a silent skip that lets the
                    # caller advance the checkpoint past unprocessed input.
                    raise RuntimeError(
                        "memory extraction parse failed: array item "
                        f"{index} is {type(item).__name__}, expected an object"
                    )
                try:
                    content = item["content"]
                    if not isinstance(content, str):
                        raise TypeError(f"content must be a string, got {type(content).__name__}")
                    memory = MemoryEntry(
                        id=uuid.uuid4(),
                        session_id=None,  # Will be set on write
                        agent_id="harness",  # Will be overridden
                        content=content,
                        category=item.get("category", "fact"),
                        importance=float(item.get("importance", 0.5)),
                        explicitly_important=bool(item.get("explicitly_important", False)),
                        # LLM extraction provides no embeddings; the
                        # consolidation pipeline embeds candidates before
                        # dedup (step 3.5), so they participate in vector
                        # retrieval and cosine dedup.
                        embedding=None,
                    )
                except (KeyError, TypeError, ValueError) as e:
                    # Schema-invalid elements are as untrustworthy as malformed
                    # JSON: fail the extraction so the range stays retryable
                    # instead of silently dropping memories and advancing the
                    # watermark past them (R09).
                    raise RuntimeError(
                        f"memory extraction parse failed: array item {index} is schema-invalid: {e}"
                    ) from e
                memories.append(memory)

            return memories

        except json.JSONDecodeError as e:
            logger.exception("Failed to parse LLM memory extraction response as JSON: %s", e)
            raise RuntimeError(f"memory extraction parse failed: {e}") from e
        except RuntimeError:
            raise
        except Exception as e:
            logger.exception("Memory extraction failed: %s", e)
            raise RuntimeError(f"memory extraction failed: {e}") from e
