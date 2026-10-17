"""Memory retriever — hybrid search with re-ranking.

Pipeline: dense vector search + sparse keyword search → merge → re-rank → top-K

Ranking contract (FM5 W1/W3): relevance is primary. Keyword matches are
scored as a *fraction* of query keywords, catalog/echo content is demoted
(never dropped), and recency/importance only break ties.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any

from machina.core.exceptions import MachinaError
from machina.core.provider import LLMProvider
from machina.core.usage import usage_store
from machina.db.lazy import LazyDb
from machina.memory.authority import CATALOG_DEMOTION_FACTOR, authority_tier
from machina.memory.models import MemoryEntry, RetrievedMemory
from machina.memory.persona import EmotionTopology, PersonaMemoryStore
from machina.memory.store import MemoryStore, memory_store
from machina.observability.diagnostics import record_failure
from machina.observability.instrumentation import instrumented, log_event

__all__ = ["MemoryRetrievalError", "MemoryRetriever"]

logger = logging.getLogger(__name__)


class MemoryRetrievalError(MachinaError, RuntimeError):
    """Every attempted memory retrieval branch failed (H06).

    Partial success never raises — a caller that received some candidate sets
    keeps them. This error distinguishes a total retrieval outage from a
    genuinely empty result, so tool/API/gateway callers can surface an error
    instead of claiming that no relevant memory exists.
    """


# Default number of results to return
db = LazyDb()
DEFAULT_TOP_K = 5

# Weight for dense vs sparse in hybrid scoring
DENSE_WEIGHT = 0.7
SPARSE_WEIGHT = 0.3

# Minimum similarity threshold for dense results
MIN_SIMILARITY_THRESHOLD = 0.3

# Bound keyword extraction so the sparse SQL stays bounded (mirrors the
# 16-term cap in machina.core.text_search.build_or_tsquery).
MAX_KEYWORDS = 16

# Candidate over-fetch factor: the sparse query selects by match count before
# the SQL LIMIT and leaves headroom for Python-side authority demotion.
SPARSE_OVERFETCH = 8
SPARSE_FETCH_CEILING = 256


class MemoryRetriever:
    """Hybrid retrieval for long-term memory.

    Combines dense vector search with sparse keyword matching,
    then re-ranks the merged candidates.
    """

    @staticmethod
    async def prepare_query_embedding(query: str) -> list[float] | None:
        """Embed *query* for dense retrieval; None when no embedder (H05).

        Shared preparation for every explicit search entry point (tool, API,
        gateway), mirroring how ordinary agent preparation calls
        ``embed_query()``. Returning None keeps the search keyword-only
        without raising, so callers can report the mode honestly.
        """
        from machina.memory.embeddings import embed_query

        return await embed_query(query)

    def __init__(
        self,
        store: MemoryStore | None = None,
        llm_provider: LLMProvider | None = None,
        top_k: int = DEFAULT_TOP_K,
        rerank: bool = True,
        persona_store: PersonaMemoryStore | None = None,
        persona_enabled: bool | None = None,
        default_emotion: str | None = None,
    ) -> None:
        self.store = store or memory_store
        self.llm = llm_provider
        self.top_k = top_k
        self.rerank = rerank
        self.emotion_topology = EmotionTopology()
        self.persona_store = persona_store or PersonaMemoryStore()
        # None = resolve from config at call time so runtime toggles apply.
        self._persona_enabled_override = persona_enabled
        self._default_emotion_override = default_emotion

    def _persona_enabled(self, override: bool | None = None) -> bool:
        """Whether persona-conditioned retrieval applies (default-on)."""
        if override is not None:
            return override
        if self._persona_enabled_override is not None:
            return self._persona_enabled_override
        try:
            from machina.core.config import get_config

            return bool(get_config().get("persona_memory_enabled"))
        except Exception as _boundary_error:
            # Optional fallback preserves the primary outcome; report no payload.
            record_failure("retriever.persona_enabled", _boundary_error)
            return True

    def _default_emotion(self, override: str | None = None) -> str:
        """Fallback emotion when none is supplied (neutral-ish default)."""
        if override:
            return override
        if self._default_emotion_override:
            return self._default_emotion_override
        try:
            from machina.core.config import get_config

            configured = get_config().get("persona_default_emotion")
            if isinstance(configured, str) and configured.strip():
                return configured.strip()
        except Exception as _boundary_error:
            # Optional fallback preserves the primary outcome; report no payload.
            record_failure("retriever._default_emotion", _boundary_error)
        return "trust"

    @instrumented("memory", "retrieve")
    async def retrieve(
        self,
        query: str,
        agent_id: str | None = None,
        category: str | None = None,
        date_range: tuple[datetime, datetime] | None = None,
        query_embedding: list[float] | None = None,
        session_id: uuid.UUID | None = None,
        emotion: str | None = None,
        persona_id: str | None = None,
        persona_enabled: bool | None = None,
        default_emotion: str | None = None,
    ) -> list[RetrievedMemory]:
        """Retrieve relevant memories for a query.

        Ranking (FM5 W1/W3): candidates are ordered by demoted relevance
        first (keyword match fraction / dense similarity), with catalog and
        recall-echo content demoted by ``CATALOG_DEMOTION_FACTOR``. Recency
        and importance only break ties; they never outrank relevance.

        Args:
            query: The search query text.
            agent_id: Filter by agent ID.
            category: Filter by memory category.
            date_range: Optional (start, end) datetime tuple.
            query_embedding: Pre-computed embedding for the query. If None,
                only sparse keyword search is performed.
            session_id: Re-rank call attribution only. It is passed to the
                LLM re-ranker for usage accounting and does **not** scope or
                filter retrieval — memories are agent-scoped, not
                session-scoped. (N1)
            emotion: Explicit emotional state. When None, the configured
                ``persona_default_emotion`` is used.
            persona_id: Persona filter for persona memories. Defaults to
                ``agent_id`` when None.
            persona_enabled: Override for ``persona_memory_enabled`` config.
            default_emotion: Override for ``persona_default_emotion`` config.

        Returns:
            List of RetrievedMemory objects, sorted by relevance score.

        Raises:
            MemoryRetrievalError: When every attempted branch failed. Partial
                success returns the branches that responded; a genuinely
                empty result stays an empty list.
        """
        # Step 1 & 2: Run dense vector search and sparse keyword search concurrently
        dense_task = None
        if query_embedding:
            dense_task = self.store.search_by_embedding(
                embedding=query_embedding,
                agent_id=agent_id,
                category=category,
                date_range=date_range,
                limit=self.top_k * 2,  # Get more candidates for re-ranking
            )
        sparse_task = self._keyword_search(
            query=query,
            agent_id=agent_id,
            category=category,
            limit=self.top_k * 2,
            date_range=date_range,
        )

        tasks = [t for t in (dense_task, sparse_task) if t is not None]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        dense_results: list[tuple[MemoryEntry, float]] = []
        sparse_results: list[tuple[MemoryEntry, float]] = []
        failures: list[tuple[str, Exception]] = []

        idx = 0
        if dense_task is not None:
            dense_res = results[idx]
            idx += 1
            if isinstance(dense_res, Exception):
                logger.warning("Dense search failed: %s", dense_res)
                failures.append(("dense", dense_res))
            else:
                dense_results = [(m, s) for m, s in dense_res if s >= MIN_SIMILARITY_THRESHOLD]

        sparse_res = results[idx]
        if isinstance(sparse_res, Exception):
            logger.warning("Sparse search failed: %s", sparse_res)
            failures.append(("sparse", sparse_res))
        else:
            sparse_results = sparse_res

        # H06: partial success is preserved; only a total outage of every
        # attempted branch is a deliberate typed error, never an ordinary
        # empty list that callers could mistake for "no memories exist".
        if len(failures) == len(tasks):
            names = ", ".join(name for name, _ in failures)
            log_event(
                "memory.retrieve.failed",
                level="warning",
                agent_id=agent_id,
                persona_id=persona_id,
                query_chars=len(query),
                branches=names,
            )
            raise MemoryRetrievalError(
                f"all memory retrieval branches failed ({names})"
            ) from failures[0][1]

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

        # Step 6: Persona-conditioned retrieval (default-on).
        # Uses the explicit emotion, else the configured default. No-op when
        # disabled or when no persona rows exist for these facts.
        if self._persona_enabled(persona_enabled) and candidates:
            effective_emotion = emotion or self._default_emotion(default_emotion)
            if effective_emotion:
                candidates = await self._retrieve_with_emotion(
                    candidates,
                    effective_emotion,
                    agent_id,
                    persona_id if persona_id is not None else agent_id,
                )

        # Update access stats for retrieved memories (batch)
        ids_to_update = [rm.memory.id for rm in candidates[: self.top_k]]
        if ids_to_update:
            try:
                await self.store.batch_update_access(ids_to_update)
            except Exception:
                logger.warning("batch_update_access failed", exc_info=True)

        log_event(
            "memory.retrieve.summary",
            level="debug",
            agent_id=agent_id,
            persona_id=persona_id if persona_id is not None else agent_id,
            query_chars=len(query),
            dense_candidates=len(dense_results),
            sparse_candidates=len(sparse_results),
            merged_candidates=len(candidates),
            returned=min(len(candidates), self.top_k),
            failed_branches=len(failures),
            rerank=bool(self.rerank),
        )
        return candidates[: self.top_k]

    async def _retrieve_with_emotion(
        self,
        candidates: list[RetrievedMemory],
        emotion: str,
        agent_id: str | None = None,
        persona_id: str | None = None,
    ) -> list[RetrievedMemory]:
        """Retrieve persona memories weighted by emotional state.

        Fetches persona memories for the factual memories in candidates,
        scores them using EmotionTopology, and merges results.

        An unscoped request (``persona_id`` and ``agent_id`` both None) skips
        the persona merge entirely: querying with no persona filter would
        return every persona's private interpretation and attach it to a
        shared fact (P0-3). The factual candidates stay fully readable; only
        the persona-conditioned annotation is withheld.
        """
        try:
            # Get fact IDs from candidates
            fact_ids = [rm.memory.id for rm in candidates]
            if not fact_ids:
                return candidates

            # Fetch persona memories for these facts. persona_id defaults to
            # agent_id for backward compatibility. No resolved scope means no
            # safe persona filter, so withhold the persona merge rather than
            # reading across personas.
            persona_filter = persona_id if persona_id is not None else agent_id
            if persona_filter is None or not str(persona_filter).strip():
                logger.debug("unscoped retrieval: skipping persona interpretation merge")
                return candidates
            persona_memories = await self.persona_store.search_persona_for_facts(
                fact_ids, persona_filter, 10
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

    @instrumented("memory", "retrieve_by_embedding")
    async def retrieve_by_embedding(
        self,
        embedding: list[float],
        agent_id: str | None = None,
        category: str | None = None,
        limit: int = 5,
        emotion: str | None = None,
        persona_id: str | None = None,
        persona_enabled: bool | None = None,
        default_emotion: str | None = None,
    ) -> list[RetrievedMemory]:
        """Retrieve memories by embedding similarity, with persona boost by default."""
        results = await self.store.search_by_embedding(
            embedding=embedding,
            agent_id=agent_id,
            category=category,
            limit=limit,
        )
        retrieved = [RetrievedMemory(memory=m, score=s, source="dense") for m, s in results]
        if self._persona_enabled(persona_enabled) and retrieved:
            effective_emotion = emotion or self._default_emotion(default_emotion)
            if effective_emotion:
                retrieved = await self._retrieve_with_emotion(
                    retrieved,
                    effective_emotion,
                    agent_id,
                    persona_id if persona_id is not None else agent_id,
                )
                retrieved = retrieved[:limit]
        # Update access (batch)
        ids_to_update = [rm.memory.id for rm in retrieved]
        if ids_to_update:
            try:
                await self.store.batch_update_access(ids_to_update)
            except Exception:
                logger.warning("batch_update_access failed", exc_info=True)
        return retrieved

    async def _keyword_search(
        self,
        query: str,
        agent_id: str | None = None,
        category: str | None = None,
        limit: int = 10,
        date_range: tuple[datetime, datetime] | None = None,
    ) -> list[tuple[MemoryEntry, float]]:
        """Keyword-based search using PostgreSQL full-text search (FTS).

        Uses to_tsvector/plainto_tsquery for efficient indexed text search
        instead of ILIKE which causes full table scans.

        Candidate selection (FM5 W2): rows are ordered by keyword match count
        (relevance) *before* the LIMIT, not by importance. A bounded
        over-fetch leaves headroom for Python-side authority demotion, so a
        high-importance catalog can never pre-truncate a real instruction.
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

        if date_range is not None:
            conditions.append(f"created_at BETWEEN ${param_idx} AND ${param_idx + 1}")
            params.extend(date_range)
            param_idx += 2

        # FTS is exact on stemmed lexemes, so keep ILIKE substring matching
        # as well: queries that are a prefix of a longer indexed token
        # (e.g. a session tag with a random suffix) must still hit.
        like_conditions = []
        like_param_idxs = []
        for kw in keywords:
            like_conditions.append(f"content ILIKE ${param_idx}")
            like_param_idxs.append(param_idx)
            params.append(f"%{kw}%")
            param_idx += 1
        fts_param_idx = param_idx
        conditions.append(
            f"(to_tsvector('english', content) @@ plainto_tsquery('english', ${param_idx})"
            f" OR ({' OR '.join(like_conditions)}))"
        )
        params.append(" ".join(keywords))
        param_idx += 1

        where_clause = " AND ".join(conditions) if conditions else "TRUE"

        # Relevance expression: number of query keywords present. This exact
        # per-keyword count orders candidates before importance/tiebreaks.
        match_expr = " + ".join(
            f"(CASE WHEN content ILIKE ${idx} THEN 1 ELSE 0 END)" for idx in like_param_idxs
        )
        fetch_limit = min(max(limit * SPARSE_OVERFETCH, 32), SPARSE_FETCH_CEILING)
        params.append(fetch_limit)
        rows = await db.fetch(
            f"""
            SELECT id, session_id, agent_id, content, category, importance,
                   created_at, last_accessed, access_count, embedding,
                   explicitly_important, base_strength,
                   ({match_expr}) AS keyword_matches
            FROM memories
            WHERE {where_clause}
            ORDER BY keyword_matches DESC,
                     ts_rank(
                         to_tsvector('english', content),
                         plainto_tsquery('english', ${fts_param_idx})
                     ) DESC,
                     importance DESC, created_at DESC
            LIMIT ${param_idx}
            """,  # nosec B608 # Fixed SQL fragments; external values use bound parameters.
            *params,
        )

        # Score is the normalized keyword match fraction, recomputed in Python
        # so mocked stores and DB rows rank identically.
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

        Combined relevance = DENSE_WEIGHT * dense_score + SPARSE_WEIGHT *
        sparse_score (both normalized to [0, 1]). Catalog/recall-echo content
        is then demoted by ``CATALOG_DEMOTION_FACTOR`` — never dropped.

        Sort order (FM5 W1/W3): demoted relevance first, then authority tier,
        then recency, then importance, then Ebbinghaus strength. Recency and
        importance are tiebreakers only; they never outrank relevance.
        """
        from machina.memory.forgetting import ForgettingModel

        _forgetting = ForgettingModel()
        merged: dict[uuid.UUID, RetrievedMemory] = {}

        for entry, score in dense_results:
            merged[entry.id] = RetrievedMemory(
                memory=entry,
                score=max(0.0, min(1.0, float(score))) * DENSE_WEIGHT,
                source="dense",
            )

        for entry, score in sparse_results:
            sparse_score = max(0.0, min(1.0, float(score))) * SPARSE_WEIGHT
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

        # Authority demotion: catalog/echo content keeps its evidence but
        # cannot occupy the top slots over a real instruction.
        for rm in merged.values():
            if authority_tier(rm.memory.content) != 0:
                rm.score *= CATALOG_DEMOTION_FACTOR

        def _rank_key(rm: RetrievedMemory) -> tuple:
            memory = rm.memory
            reference = memory.last_accessed or memory.created_at
            try:
                timestamp = reference.timestamp() if reference else 0.0
            except Exception:
                timestamp = 0.0
            try:
                strength = _forgetting.current_strength(memory)
            except Exception:
                strength = 1.0
            return (
                -rm.score,
                authority_tier(memory.content),
                -timestamp,
                -memory.importance,
                -strength,
            )

        # Sort by rank key (relevance primary; recency/importance tiebreak)
        return sorted(merged.values(), key=_rank_key)

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
                    purpose="memory_rerank",
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

            # Reorder candidates; ignore out-of-range and repeated indices so
            # one memory can never be surfaced multiple times.
            reranked = []
            seen_indices: set[int] = set()
            for idx in indices:
                if isinstance(idx, int) and 0 <= idx < len(candidates) and idx not in seen_indices:
                    seen_indices.add(idx)
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

        # Tokenize and filter (bounded: see MAX_KEYWORDS)
        tokens = re.findall(r"\b[a-zA-Z]+\b", query.lower())
        keywords = [t for t in tokens if len(t) > 2 and t not in stop_words]
        return keywords[:MAX_KEYWORDS]
