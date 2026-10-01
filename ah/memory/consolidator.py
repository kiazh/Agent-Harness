"""Memory consolidator — extract durable memories from conversation chunks.

Pipeline: raw chunks → LLM extraction → importance scoring → dedup → write
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from ah.core.context import context_manager
from ah.core.models import ContextChunk
from ah.core.provider import LLMProvider, audit_log
from ah.memory.models import MemoryEntry
from ah.memory.scorer import ImportanceScorer
from ah.memory.store import MemoryStore, memory_store

__all__ = ["MemoryConsolidator"]

logger = logging.getLogger(__name__)

# Deduplication threshold — cosine similarity above this means "duplicate"
DEFAULT_DEDUP_THRESHOLD = 0.85

# Maximum chunks to process in a single consolidation run
MAX_CHUNKS_PER_CONSOLIDATION = 100

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
    3. Deduplicate against existing memories
    4. Write new memories
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
    ) -> list[MemoryEntry]:
        """Consolidate a session's context chunks into long-term memories.

        Returns list of newly created MemoryEntry objects.
        """
        # Step 0: Get context chunks
        chunks = await context_manager.get_chunks(session_id, limit=MAX_CHUNKS_PER_CONSOLIDATION)
        if not chunks:
            logger.debug("No chunks to consolidate for session %s", session_id)
            return []

        # Step 1: Format conversation for LLM
        conversation = self._format_conversation(chunks)

        # Step 2: Extract candidate memories via LLM
        candidates = await self._extract_memories(conversation)
        if not candidates:
            logger.debug("No memories extracted from session %s", session_id)
            return []

        # Step 3: Score importance
        for candidate in candidates:
            candidate.importance = self.scorer.score(candidate)

        # Step 4: Deduplicate against existing memories
        new_memories: list[MemoryEntry] = []
        for candidate in candidates:
            # Skip low-importance memories
            if candidate.importance < 0.2:
                continue

            # Check for similar existing memories
            if candidate.embedding:
                similar = await self.store.search_by_embedding(
                    embedding=candidate.embedding,
                    agent_id=agent_id,
                    limit=1,
                )
                if similar and similar[0][1] > self.dedup_threshold:
                    # Duplicate found — update access instead of creating new
                    existing_entry, similarity = similar[0]
                    await self.store.update_access(existing_entry.id)
                    logger.debug(
                        "Duplicate memory detected (similarity=%.2f), updated access for %s",
                        similarity,
                        existing_entry.id,
                    )
                    continue

            new_memories.append(candidate)

        # Step 5: Write new memories
        written: list[MemoryEntry] = []
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
                written.append(entry)
            except Exception as e:
                logger.error("Failed to write memory: %s", e)

        audit_log(
            "memory_consolidate",
            session_id=str(session_id),
            agent_id=agent_id,
            chunks_processed=len(chunks),
            candidates_extracted=len(candidates),
            new_memories_written=len(written),
        )

        logger.info(
            "Consolidated session %s: %d chunks → %d candidates → %d new memories",
            session_id,
            len(chunks),
            len(candidates),
            len(written),
        )
        return written

    async def consolidate_from_text(
        self,
        text: str,
        session_id: uuid.UUID | None = None,
        agent_id: str = "harness",
    ) -> list[MemoryEntry]:
        """Consolidate memories directly from text (bypasses context chunks).

        Useful for testing or direct memory injection.
        """
        candidates = await self._extract_memories(text)
        if not candidates:
            return []

        for candidate in candidates:
            candidate.importance = self.scorer.score(candidate)

        written: list[MemoryEntry] = []
        for memory in candidates:
            if memory.importance < 0.2:
                continue
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
                written.append(entry)
            except Exception as e:
                logger.error("Failed to write memory: %s", e)

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

    async def _extract_memories(self, conversation: str) -> list[MemoryEntry]:
        """Use LLM to extract candidate memories from conversation.

        Returns list of MemoryEntry objects (not yet persisted).
        """
        if not self.llm:
            logger.warning("No LLM provider available for memory extraction")
            return []

        try:
            response = await self.llm.complete(
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
                return []

            memories: list[MemoryEntry] = []
            for item in items:
                try:
                    memory = MemoryEntry(
                        id=uuid.uuid4(),
                        session_id=None,  # Will be set on write
                        agent_id="harness",  # Will be overridden
                        content=item["content"],
                        category=item.get("category", "fact"),
                        importance=float(item.get("importance", 0.5)),
                        explicitly_important=bool(item.get("explicitly_important", False)),
                    )
                    memories.append(memory)
                except (KeyError, ValueError) as e:
                    logger.warning("Invalid memory item from LLM: %s (error: %s)", item, e)
                    continue

            return memories

        except json.JSONDecodeError as e:
            logger.error("Failed to parse LLM memory extraction response as JSON: %s", e)
            return []
        except Exception as e:
            logger.error("Memory extraction failed: %s", e)
            return []
