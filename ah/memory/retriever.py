"""Memory retriever — hybrid search with re-ranking.

Pipeline: dense vector search + sparse keyword search → merge → re-rank → top-K
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any

from ah.core.provider import LLMProvider
from ah.core.usage import usage_store
from ah.db.connection import db
from ah.memory.models import MemoryEntry, RetrievedMemory
from ah.memory.persona import EmotionTopology, PersonaMemoryStore
from ah.memory.store import MemoryStore, memory_store

__all__ = ["MemoryRetriever"]

logger = logging.getLogger(__name__)

# Default number of results to return
DEFAULT_TOP_K = 5

# Weight for dense vs sparse in hybrid scoring
DENSE_WEIGHT = 0.7
SPARSE_WEIGHT = 0.3

# Minimum similarity threshold for dense results
MIN_SIMILARITY_THRESHOLD = 0.3


class MemoryRetriever:
    """Hybrid retrieval for long-term memory.

    Combines dense vector search with sparse keyword matching,
    then re-ranks the merged candidates.
    """

    def __init__(
        self,
        store: MemoryStore | None = None,
        llm_provider: LLMProvider | None = None,
        top_k: int = DEFAULT_TOP_K,
        rerank: bool = True,
        persona_store: PersonaMemoryStore | None = None,
    ) -> None:
        self.store = store or memory_store
        self.llm = llm_provider
        self.top_k = top_k
        self.rerank = rerank
        self.emotion_topology = EmotionTopology()
        self.persona_store = persona_store or PersonaMemoryStore()

    async def retrieve(
        self,
        query: str,
        agent_id: str | None = None,
        category: str | None = None,
        date_range: tuple[datetime, datetime] | None = None,
        query_embedding: list[float] | None = None,
        session_id: uuid.UUID | None = None,
        emotion: str | None = None,
    ) -> list[RetrievedMemory]:
        """Retrieve relevant memories for a query.

        Args:
            query: The search query text.
            agent_id: Filter by agent ID.
            category: Filter by memory category.
            date_range: Optional (start, end) datetime tuple.
            query_embedding: Pre-computed embedding for the query. If None,
                only sparse keyword search is performed.

        Returns:
            List of RetrievedMemory objects, sorted by relevance score.
        """
        # Step 1: Dense vector search (if embedding available)
        dense_results: list[tuple[MemoryEntry, float]] = []
        if query_embedding:
            dense_results = await self.store.search_by_embedding(
                embedding=query_embedding,
                agent_id=agent_id,
                category=category,
                limit=self.top_k * 2,  # Get more candidates for re-ranking
            )
            # Filter by minimum similarity
            dense_results = [(m, s) for m, s in dense_results if s >= MIN_SIMILARITY_THRESHOLD]

        # Step 2: Sparse keyword search
        sparse_results = await self._keyword_search(
            query=query,
            agent_id=agent_id,
            category=category,
            limit=self.top_k * 2,
        )

        # Defense in depth: never pass quarantined rows to the agent, even if
        # a custom store or an older query returned them.
        dense_results = [(m, s) for m, s in dense_results if not m.quarantined]
        sparse_results = [(m, s) for m, s in sparse_results if not m.quarantined]

        # Step 3: Merge and deduplicate
        candidates = self._merge_results(dense_results, sparse_results)

        # Step 4: Apply date range filter
        if date_range:
            start, end = date_range
            candidates = [
                rm
                for rm in candidates
                if rm.memory.created_at and start <= rm.memory.created_at <= end
            ]

        # Step 5: Re-rank
        if self.rerank and len(candidates) > 1:
            candidates = await self._rerank(query, candidates, session_id, agent_id)

        # Step 6: Emotion-weighted persona memory retrieval (if emotion specified)
        if emotion:
            candidates = await self._retrieve_with_emotion(candidates, emotion, agent_id)

        # Update access stats for retrieved memories (batch)
        ids_to_update = [rm.memory.id for rm in candidates[: self.top_k]]
        if ids_to_update:
            try:
                await self.store.batch_update_access(ids_to_update)
            except Exception:
                pass  # Don't fail retrieval due to access update failure

        return candidates[: self.top_k]

    async def _retrieve_with_emotion(
        self,
        candidates: list[RetrievedMemory],
        emotion: str,
        agent_id: str | None = None,
    ) -> list[RetrievedMemory]:
        """Retrieve persona memories weighted by emotional state.

        Fetches persona memories for the factual memories in candidates,
        scores them using EmotionTopology, and merges results.
        """
        try:
            # Get fact IDs from candidates
            fact_ids = [rm.memory.id for rm in candidates]
            if not fact_ids:
                return candidates

            # Fetch persona memories for these facts
            persona_memories = await self.persona_store.search_persona_for_facts(
                fact_ids, agent_id, 10
            )

            if not persona_memories:
                return candidates

            # Score persona memories by emotion
            scored_persona = self.emotion_topology.retrieve_for_emotion(
                persona_memories, emotion, limit=self.top_k * 2
            )

            # Merge: boost factual candidates that have high-scoring persona memories
            persona_boost: dict[uuid.UUID, tuple[float, str]] = {}
            for pm, score in scored_persona:
                previous = persona_boost.get(pm.fact_id)
                if previous is None or score > previous[0]:
                    persona_boost[pm.fact_id] = (score, pm.interpretation)

            for rm in candidates:
                if rm.memory.id in persona_boost:
                    score, interpretation = persona_boost[rm.memory.id]
                    rm.score += score * 0.3
                    rm.persona_interpretation = interpretation

            # Re-sort by updated score
            candidates.sort(key=lambda rm: rm.score, reverse=True)
            return candidates

        except Exception:
            logger.warning("Emotion-weighted retrieval failed, returning candidates unchanged")
            return candidates

    async def retrieve_by_embedding(
        self,
        embedding: list[float],
        agent_id: str | None = None,
        category: str | None = None,
        limit: int = 5,
    ) -> list[RetrievedMemory]:
        """Retrieve memories by embedding similarity only."""
        results = await self.store.search_by_embedding(
            embedding=embedding,
            agent_id=agent_id,
            category=category,
            limit=limit,
        )
        retrieved = [RetrievedMemory(memory=m, score=s, source="dense") for m, s in results]
        # Update access (batch)
        ids_to_update = [rm.memory.id for rm in retrieved]
        if ids_to_update:
            try:
                await self.store.batch_update_access(ids_to_update)
            except Exception:
                pass
        return retrieved

    async def _keyword_search(
        self,
        query: str,
        agent_id: str | None = None,
        category: str | None = None,
        limit: int = 10,
    ) -> list[tuple[MemoryEntry, float]]:
        """Keyword-based search using PostgreSQL full-text search (FTS).

        Uses to_tsvector/plainto_tsquery for efficient indexed text search
        instead of ILIKE which causes full table scans.
        """
        # Extract keywords from query (simple tokenization)
        keywords = self._extract_keywords(query)
        if not keywords:
            return []

        # Build FTS query using to_tsvector and plainto_tsquery
        conditions = ["quarantined = FALSE"]
        params: list[Any] = []
        param_idx = 1

        if agent_id is not None:
            conditions.append(f"agent_id = ${param_idx}")
            params.append(agent_id)
            param_idx += 1

        if category is not None:
            conditions.append(f"category = ${param_idx}")
            params.append(category)
            param_idx += 1

        # Use FTS: to_tsvector('english', content) @@ plainto_tsquery('english', $N)
        # This leverages the GIN index on to_tsvector for fast full-text search
        conditions.append(f"to_tsvector('english', content) @@ plainto_tsquery('english', ${param_idx})")
        params.append(" ".join(keywords))
        param_idx += 1

        where_clause = " AND ".join(conditions) if conditions else "TRUE"

        params.append(limit)
        rows = await db.fetch(
            f"""
            SELECT id, session_id, agent_id, content, category, importance,
                   created_at, last_accessed, access_count, embedding,
                   explicitly_important, base_strength
            FROM memories
            WHERE {where_clause}
            ORDER BY importance DESC, created_at DESC
            LIMIT ${param_idx}
            """,
            *params,
        )

        # Score based on keyword match count
        results: list[tuple[MemoryEntry, float]] = []
        for row in rows:
            entry = self.store._row_to_entry(row)
            match_count = sum(1 for kw in keywords if kw.lower() in entry.content.lower())
            score = match_count / len(keywords) if keywords else 0.0
            results.append((entry, score))

        return results

    def _merge_results(
        self,
        dense_results: list[tuple[MemoryEntry, float]],
        sparse_results: list[tuple[MemoryEntry, float]],
    ) -> list[RetrievedMemory]:
        """Merge dense and sparse results, deduplicating by memory ID.

        Combined score = DENSE_WEIGHT * dense_score + SPARSE_WEIGHT * sparse_score
        """
        merged: dict[uuid.UUID, RetrievedMemory] = {}

        for entry, score in dense_results:
            merged[entry.id] = RetrievedMemory(
                memory=entry,
                score=score * DENSE_WEIGHT,
                source="dense",
            )

        for entry, score in sparse_results:
            sparse_score = score * SPARSE_WEIGHT
            if entry.id in merged:
                # Memory found in both — combine scores
                existing = merged[entry.id]
                existing.score += sparse_score
                existing.source = "hybrid"
            else:
                merged[entry.id] = RetrievedMemory(
                    memory=entry,
                    score=sparse_score,
                    source="sparse",
                )

        # Sort by score descending
        return sorted(merged.values(), key=lambda rm: rm.score, reverse=True)

    async def _rerank(
        self,
        query: str,
        candidates: list[RetrievedMemory],
        session_id: uuid.UUID | None = None,
        agent_id: str | None = None,
    ) -> list[RetrievedMemory]:
        """Re-rank candidates using LLM (if available) or score-based ranking.

        Without an LLM, falls back to score-based ranking with a
        recency boost.
        """
        if not self.llm or len(candidates) <= 1:
            return candidates

        try:
            # Build prompt for LLM re-ranking
            candidate_texts = []
            for i, rm in enumerate(candidates):
                candidate_texts.append(
                    f"[{i}] (score={rm.score:.3f}, category={rm.memory.category}) "
                    f"{rm.memory.content[:200]}"
                )

            rerank_prompt = f"""Given the following query and candidate memories, re-rank them by relevance.

Query: {query}

Candidates:
{chr(10).join(candidate_texts)}

Return a JSON array of indices in order of relevance (most relevant first).
Return ONLY the JSON array, no other text."""

            response = await asyncio.wait_for(
                usage_store.complete_call(
                    self.llm,
                    session_id,
                    agent_id or "harness",
                    messages=[
                        {"role": "system", "content": "You are a memory re-ranking system."},
                        {"role": "user", "content": rerank_prompt},
                    ],
                    tools=[],
                ),
                timeout=10.0,
            )

            # Parse response
            content = response.content.strip()
            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(lines[1:-1])

            indices = json.loads(content)
            if not isinstance(indices, list):
                return candidates

            # Reorder candidates
            reranked = []
            for idx in indices:
                if isinstance(idx, int) and 0 <= idx < len(candidates):
                    reranked.append(candidates[idx])

            # Add any candidates not included in LLM response
            for rm in candidates:
                if rm not in reranked:
                    reranked.append(rm)

            return reranked

        except Exception as e:
            logger.warning("LLM re-ranking failed, falling back to score-based: %s", e)
            return candidates

    def _extract_keywords(self, query: str) -> list[str]:
        """Extract keywords from a query (simple tokenization).

        Removes stop words and short tokens.
        """
        stop_words = {
            "a",
            "an",
            "the",
            "is",
            "are",
            "was",
            "were",
            "be",
            "been",
            "being",
            "have",
            "has",
            "had",
            "do",
            "does",
            "did",
            "will",
            "would",
            "could",
            "should",
            "may",
            "might",
            "can",
            "this",
            "that",
            "these",
            "those",
            "i",
            "you",
            "he",
            "she",
            "it",
            "we",
            "they",
            "what",
            "which",
            "who",
            "whom",
            "when",
            "where",
            "why",
            "how",
            "all",
            "each",
            "every",
            "both",
            "few",
            "more",
            "most",
            "other",
            "some",
            "such",
            "no",
            "not",
            "only",
            "own",
            "same",
            "so",
            "than",
            "too",
            "very",
            "just",
            "and",
            "but",
            "if",
            "or",
            "because",
            "as",
            "until",
            "while",
            "of",
            "at",
            "by",
            "for",
            "with",
            "about",
            "against",
            "between",
            "into",
            "through",
            "during",
            "before",
            "after",
            "above",
            "below",
            "to",
            "from",
            "up",
            "down",
            "in",
            "out",
            "on",
            "off",
            "over",
            "under",
            "again",
            "further",
            "then",
            "once",
            "here",
            "there",
            "tell",
            "me",
            "my",
            "your",
            "his",
            "her",
            "its",
            "our",
            "their",
        }

        # Tokenize and filter
        tokens = re.findall(r"\b[a-zA-Z]+\b", query.lower())
        keywords = [t for t in tokens if len(t) > 2 and t not in stop_words]
        return keywords
