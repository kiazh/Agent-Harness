"""Context compression — rolling compaction and manual compression.

Provides:
- ContextCompressor: compresses older context chunks while preserving tool call/result pairs
- RollingCompaction: monitors token usage and triggers compression when threshold exceeded
- CompressionConfig: configurable threshold and target ratio
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from ah.core.assembler import get_token_count
from ah.core.models import ContextChunk

__all__ = [
    "CompressionConfig",
    "CompressionResult",
    "ContextCompressor",
    "RollingCompaction",
]

logger = logging.getLogger(__name__)


def _run_awaitable(awaitable: Any) -> Any:
    """Run *awaitable* to completion from synchronous code.

    ``ContextCompressor.compress`` is synchronous, but its optional LLM
    summarization step is a coroutine. A bare ``asyncio.run`` raises
    "asyncio.run() cannot be called from a running event loop" whenever
    ``compress()`` is invoked from async code (the CLI does exactly that).
    Use ``asyncio.run`` when no loop is running; otherwise execute the
    coroutine on a dedicated worker thread that owns its own event loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, awaitable).result()


@dataclass
class CompressionConfig:
    """Configuration for context compression.

    Attributes:
        enabled: Whether compression is active.
        threshold: Fraction of context_budget that triggers compression (0.0-1.0).
        target_ratio: Target fraction of context_budget after compression (0.0-1.0).
        preserve_recent: Number of most-recent chunks to always preserve uncompressed.
        llm_summarize: Whether to use LLM for summarization (falls back to truncation if False).
    """

    enabled: bool = True
    threshold: float = 0.8
    target_ratio: float = 0.5
    preserve_recent: int = 3
    llm_summarize: bool = True


@dataclass
class CompressionResult:
    """Result of a compression operation.

    Attributes:
        compressed_chunks: The compressed/summarized chunks.
        original_count: Number of original chunks compressed.
        original_tokens: Total tokens in original chunks.
        compressed_tokens: Total tokens in compressed chunks.
        compression_ratio: Ratio of compressed to original tokens (0.0-1.0).
        method: Compression method used ("llm_summarize" or "truncate").
    """

    compressed_chunks: list[ContextChunk]
    original_count: int
    original_tokens: int
    compressed_tokens: int
    compression_ratio: float
    method: str


class ContextCompressor:
    """Compresses context chunks while preserving tool call/result pairs.

    The compressor identifies tool_call chunks and their corresponding results,
    keeping them together during compression. Older chunks are summarized or
    truncated to fit within the target token budget.
    """

    def __init__(self, config: CompressionConfig | None = None) -> None:
        self.config = config or CompressionConfig()

    def compress(
        self,
        chunks: list[ContextChunk],
        session_id: uuid.UUID,
        agent_id: str,
        llm_provider: Any = None,
    ) -> CompressionResult:
        """Compress a list of context chunks.

        Strategy:
        1. Identify tool_call/result pairs and keep them together
        2. Preserve the most recent N chunks uncompressed
        3. Compress older chunks using LLM summarization or truncation
        4. Return the compressed chunk list

        Sync compatibility: when called without a running event loop this
        runs summarization inline; when called from async code prefer
        :meth:`acompress` which awaits directly without blocking the loop
        (AH-011).
        """
        if not chunks:
            return CompressionResult(
                compressed_chunks=[],
                original_count=0,
                original_tokens=0,
                compressed_tokens=0,
                compression_ratio=0.0,
                method="none",
            )

        # Calculate total tokens
        original_tokens = sum(c.token_count for c in chunks)
        if original_tokens == 0:
            original_tokens = sum(get_token_count(str(c.payload)) for c in chunks)

        # Split into recent (preserve) and old (compress), keeping tool
        # pairs atomic across the boundary (AH-012).
        preserve_count = self.config.preserve_recent
        if len(chunks) <= preserve_count:
            # Nothing to compress
            return CompressionResult(
                compressed_chunks=chunks,
                original_count=0,
                original_tokens=original_tokens,
                compressed_tokens=original_tokens,
                compression_ratio=1.0,
                method="none",
            )

        recent_chunks, old_chunks = self._split_preserve_recent(chunks, preserve_count)

        # Compress old chunks
        if self.config.llm_summarize and llm_provider is not None:
            try:
                compressed = _run_awaitable(
                    self._llm_summarize(old_chunks, session_id, agent_id, llm_provider)
                )
                method = "llm_summarize"
            except Exception:
                try:
                    compressed = self._truncate_compress(old_chunks, session_id, agent_id)
                    method = "truncate"
                except Exception:
                    # If all compression fails, preserve original chunks to avoid context loss
                    compressed = old_chunks
                    method = "none"
        else:
            try:
                compressed = self._truncate_compress(old_chunks, session_id, agent_id)
                method = "truncate"
            except Exception:
                # If truncation fails, preserve original chunks to avoid context loss
                compressed = old_chunks
                method = "none"

        # Combine: compressed old chunks + preserved recent chunks
        all_compressed = compressed + recent_chunks
        compressed_tokens = sum(c.token_count for c in all_compressed)
        if compressed_tokens == 0:
            compressed_tokens = sum(get_token_count(str(c.payload)) for c in all_compressed)

        ratio = compressed_tokens / original_tokens if original_tokens > 0 else 0.0

        return CompressionResult(
            compressed_chunks=all_compressed,
            original_count=len(old_chunks),
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            compression_ratio=ratio,
            method=method,
        )

    async def acompress(
        self,
        chunks: list[ContextChunk],
        session_id: uuid.UUID,
        agent_id: str,
        llm_provider: Any = None,
    ) -> CompressionResult:
        """Async compression (AH-011): awaits LLM summarization directly.

        Same contract as :meth:`compress` but never blocks the event loop
        with ``Future.result()`` on a worker thread.
        """
        if not chunks:
            return CompressionResult(
                compressed_chunks=[],
                original_count=0,
                original_tokens=0,
                compressed_tokens=0,
                compression_ratio=0.0,
                method="none",
            )
        original_tokens = sum(c.token_count for c in chunks)
        if original_tokens == 0:
            original_tokens = sum(get_token_count(str(c.payload)) for c in chunks)
        preserve_count = self.config.preserve_recent
        if len(chunks) <= preserve_count:
            return CompressionResult(
                compressed_chunks=chunks,
                original_count=0,
                original_tokens=original_tokens,
                compressed_tokens=original_tokens,
                compression_ratio=1.0,
                method="none",
            )
        recent_chunks, old_chunks = self._split_preserve_recent(chunks, preserve_count)
        if self.config.llm_summarize and llm_provider is not None:
            try:
                compressed = await self._llm_summarize(
                    old_chunks, session_id, agent_id, llm_provider
                )
                method = "llm_summarize"
            except Exception:
                try:
                    compressed = self._truncate_compress(old_chunks, session_id, agent_id)
                    method = "truncate"
                except Exception:
                    compressed = old_chunks
                    method = "none"
        else:
            try:
                compressed = self._truncate_compress(old_chunks, session_id, agent_id)
                method = "truncate"
            except Exception:
                compressed = old_chunks
                method = "none"
        all_compressed = compressed + recent_chunks
        compressed_tokens = sum(c.token_count for c in all_compressed)
        if compressed_tokens == 0:
            compressed_tokens = sum(get_token_count(str(c.payload)) for c in all_compressed)
        ratio = compressed_tokens / original_tokens if original_tokens > 0 else 0.0
        return CompressionResult(
            compressed_chunks=all_compressed,
            original_count=len(old_chunks),
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            compression_ratio=ratio,
            method=method,
        )

    def _split_preserve_recent(
        self, chunks: list[ContextChunk], preserve_count: int
    ) -> tuple[list[ContextChunk], list[ContextChunk]]:
        """Split newest-first *chunks*, keeping tool pairs atomic (AH-012).

        If the preserve boundary cuts a tool_call/result pair, the mate is
        pulled into the preserved side so calls and results are never
        separated.
        """
        recent = chunks[:preserve_count]
        old = chunks[preserve_count:]
        if not old or not recent:
            return recent, old
        pairs = self._identify_tool_pairs(chunks)
        recent_idx = set(range(len(recent)))
        # Any old chunk paired with a recent chunk forces its mate to move.
        # Conversely any recent chunk paired with an old chunk pulls the mate.
        to_preserve = set(recent_idx)
        changed = True
        while changed:
            changed = False
            for i, j in pairs.items():
                if (i in to_preserve) != (j in to_preserve):
                    if i not in to_preserve or j not in to_preserve:
                        if i not in to_preserve or j not in to_preserve:
                            pass
                    # Pull both sides into preserved set.
                    if i not in to_preserve or j not in to_preserve:
                        to_preserve.add(i)
                        to_preserve.add(j)
                        changed = True
        if len(to_preserve) == len(recent_idx):
            return recent, old
        # Rebuild preserving original newest-first order.
        new_recent_idx = sorted(to_preserve)
        # Only allow growth (never shrink below preserve_count).
        if len(new_recent_idx) < preserve_count:
            return recent, old
        recent2 = [chunks[i] for i in new_recent_idx]
        old2 = [chunks[i] for i in range(len(chunks)) if i not in to_preserve]
        return recent2, old2

    def _identify_tool_pairs(self, chunks: list[ContextChunk]) -> dict[int, int]:
        """Identify tool_call/result pairs, grouped atomically (AH-012).

        Preference order for pairing:
        1. Explicit call IDs (payload ``call_id``/``id``/``tool_call_id``)
           matching across chunks — handles repeated same-name calls.
        2. Same-chunk call+result (payload has ``result_preview``).
        3. Legacy fallback: match separate ``result`` chunks to the most
           recent unmatched ``tool_call`` with the same tool name.
        Returns a dict mapping chunk index -> paired chunk index.
        """
        pairs: dict[int, int] = {}

        def _call_id(payload: dict) -> str | None:
            for k in ("call_id", "tool_call_id", "id"):
                v = payload.get(k)
                if isinstance(v, str) and v:
                    return v
            # Nested args may carry the provider tool_call id.
            args = payload.get("args")
            if isinstance(args, dict):
                for k in ("call_id", "tool_call_id", "id"):
                    v = args.get(k)
                    if isinstance(v, str) and v:
                        return v
            return None

        # 1. Pair by explicit call ID.
        id_to_indices: dict[str, list[int]] = {}
        for i, chunk in enumerate(chunks):
            if chunk.chunk_type in ("tool_call", "result"):
                cid = _call_id(chunk.payload)
                if cid:
                    id_to_indices.setdefault(cid, []).append(i)
        for cid, idxs in id_to_indices.items():
            # Pair indices sharing one call ID (usually exactly 2).
            for a in idxs:
                for b in idxs:
                    if a != b and a not in pairs and b not in pairs:
                        pairs[a] = b
                        pairs[b] = a

        # Build a map of tool_name -> list of indices
        tool_call_indices: dict[str, list[int]] = {}
        for i, chunk in enumerate(chunks):
            if i in pairs:
                continue
            if chunk.chunk_type == "tool_call":
                # The agent stores a call and its result in ONE chunk
                # (payload has "result_preview"). Treat that as a complete,
                # self-contained pair so it gets pair priority.
                if "result_preview" in chunk.payload:
                    pairs[i] = i
                    continue
                tool_name = chunk.payload.get("tool", "")
                if tool_name:
                    tool_call_indices.setdefault(tool_name, []).append(i)

        # Match separate result chunks to their tool calls
        for i, chunk in enumerate(chunks):
            if i in pairs:
                continue
            if chunk.chunk_type == "result":
                # Prefer call-ID match already handled; fall back to name.
                tool_name = chunk.payload.get("tool", "")
                if tool_name and tool_name in tool_call_indices:
                    # Find the most recent unmatched tool_call for this tool
                    for call_idx in reversed(tool_call_indices[tool_name]):
                        if call_idx not in pairs:
                            pairs[call_idx] = i
                            pairs[i] = call_idx
                            break

        return pairs

    async def _llm_summarize(
        self,
        chunks: list[ContextChunk],
        session_id: uuid.UUID,
        agent_id: str,
        llm_provider: Any,
    ) -> list[ContextChunk]:
        """Use LLM to summarize older context chunks.

        Falls back to truncation if LLM call fails.
        """
        try:
            # Build a text representation of the chunks
            text_parts = []
            for chunk in chunks:
                text_parts.append(self._chunk_to_text(chunk))

            summary_prompt = (
                "Summarize the following conversation context concisely. "
                "Preserve key facts, decisions, and tool results. "
                "Keep it under 200 words.\n\n" + "\n".join(text_parts)
            )

            response = await llm_provider.complete(
                messages=[{"role": "user", "content": summary_prompt}],
                temperature=0.3,
                max_tokens=500,
            )

            summary_text = response.content

            # Create a single summary chunk. It replaces older chunks, so it
            # takes their newest timestamp — keeping it ordered *before* the
            # preserved recent chunks instead of jumping to "now".
            summary_kwargs = {}
            stamps = [c.created_at for c in chunks if c.created_at is not None]
            if stamps:
                summary_kwargs["created_at"] = max(stamps)
            summary_chunk = ContextChunk(
                id=uuid.uuid4(),
                session_id=session_id,
                agent_id=agent_id,
                chunk_type="compression_summary",
                payload={
                    "content": summary_text,
                    "original_count": len(chunks),
                    "original_tokens": sum(c.token_count for c in chunks),
                },
                token_count=get_token_count(summary_text),
                **summary_kwargs,
            )
            return [summary_chunk]

        except Exception as e:
            logger.warning("LLM summarization failed, falling back to truncation: %s", e)
            raise

    def _truncate_compress(
        self,
        chunks: list[ContextChunk],
        session_id: uuid.UUID,
        agent_id: str,
    ) -> list[ContextChunk]:
        """Truncate older chunks to fit within target budget.

        Keeps tool pairs atomic (AH-012): paired call/result chunks are
        selected or truncated as a unit, never split.
        """
        # Sort by importance: tool_call/result pairs first, then by recency
        pairs = self._identify_tool_pairs(chunks)

        # Build atomic groups: each pair is one unit; singles are one unit.
        seen: set[int] = set()
        groups: list[list[int]] = []
        for i in range(len(chunks)):
            if i in seen:
                continue
            mate = pairs.get(i)
            if mate is not None and mate != i and mate not in seen:
                groups.append([i, mate] if i < mate else [mate, i])
                seen.add(i)
                seen.add(mate)
            else:
                groups.append([i])
                seen.add(i)

        # Build a priority score for each group
        def group_priority(g: list[int]) -> int:
            score = 0
            for idx in g:
                chunk = chunks[idx]
                if idx in pairs:
                    score += 100
                score += len(chunks) - idx
                if chunk.chunk_type == "memory":
                    score += 50
            # Average to avoid biasing larger groups, then boost pairs.
            return score

        groups.sort(key=group_priority, reverse=True)

        # Select groups that fit within target budget. Budgets count the FINAL
        # serialized representation (5.7): effective tokens fall back to the
        # real tokenizer when stored counts are 0/underestimated, and per-field
        # minima keep tool protocol structure intact.
        def _effective(c: ContextChunk) -> int:
            if c.token_count and c.token_count > 0:
                return c.token_count
            try:
                return max(1, get_token_count(str(c.payload)))
            except Exception:
                return 1

        target_tokens = int(sum(_effective(c) for c in chunks) * self.config.target_ratio)
        selected: list[ContextChunk] = []
        current_tokens = 0

        for g in groups:
            g_tokens = sum(_effective(chunks[i]) for i in g)
            if current_tokens + g_tokens <= target_tokens:
                for i in sorted(g):
                    selected.append(chunks[i])
                current_tokens += g_tokens
            else:
                # Try to truncate the group as a unit within remaining budget.
                remaining = target_tokens - current_tokens
                if remaining > 0:
                    # Split remaining evenly across group members.
                    per = max(1, remaining // len(g))
                    truncated_group = []
                    for i in sorted(g):
                        t = self._truncate_chunk(chunks[i], per)
                        if t is not None:
                            truncated_group.append(t)
                    # Only keep the group if ALL members fit (atomicity).
                    t_tokens = sum(t.token_count for t in truncated_group)
                    if (
                        len(truncated_group) == len(g)
                        and current_tokens + t_tokens <= target_tokens
                    ):
                        selected.extend(truncated_group)
                        current_tokens += t_tokens
                    # Else drop the whole group (never keep half a pair).

        # Sort back to original order (by created_at)
        selected.sort(key=lambda c: c.created_at, reverse=True)
        dropped = len(chunks) - len(selected)
        if dropped > 0:
            # Dropped chunks are evicted (archived by the caller); log for audit.
            logger.info(
                "Truncate compression dropped %d of %d chunks (target %d tokens)",
                dropped,
                len(chunks),
                target_tokens,
            )
        return selected

    def _truncate_chunk(self, chunk: ContextChunk, max_tokens: int) -> ContextChunk | None:
        """Truncate a chunk's payload to fit within max_tokens (AH-013/AH-014, 5.7).

        Uses tokenizer-based truncation (not 4-chars-per-token) and verifies
        the FINAL serialized representation fits, wrapper overhead included.
        Tool protocol fields (tool/call identity, result keys) are preserved —
        truncation shrinks values in place and never rewrites a call into an
        arbitrary ``{"content": ...}`` blob. Impossible budgets (<= 0, or
        smaller than the empty-payload overhead) return None: a defined
        no-fit result, never an oversized payload labeled guaranteed. Clears
        the embedding when content changes (AH-014).
        """
        from ah.core.assembler import TokenCounter as _TC

        if max_tokens <= 0:
            return None
        counter = _TC()
        # Tool protocol chunks keep their shape: truncate values, keep keys.
        is_tool = chunk.chunk_type in ("tool_call", "result")
        # Deep copy so nested dict truncation never aliases the original chunk.
        payload = copy.deepcopy(chunk.payload)
        truncated = False

        def _truncate_str(s: str, budget_tokens: int) -> str:
            if budget_tokens <= 0:
                return ""
            if get_token_count(s) <= budget_tokens:
                return s
            return counter.truncate(s, budget_tokens)

        # Budget per string field: split evenly (at least 1 token each).
        str_keys = [k for k, v in payload.items() if isinstance(v, str)]
        # Rough per-field budget; the final verify loop enforces the total.
        per_field = max(1, max_tokens // max(1, len(str_keys) or 1))
        for key in str_keys:
            value = payload[key]
            if get_token_count(value) > per_field:
                payload[key] = _truncate_str(value, per_field)
                truncated = True
        for key, value in payload.items():
            if isinstance(value, dict):
                for k2, v2 in list(value.items()):
                    if isinstance(v2, str) and get_token_count(v2) > per_field:
                        value[k2] = _truncate_str(v2, per_field)
                        truncated = True
            elif isinstance(value, list):
                # Nested lists/maps: truncate string leaves in place.
                for item in value:
                    if isinstance(item, dict):
                        for k3, v3 in list(item.items()):
                            if isinstance(v3, str) and get_token_count(v3) > per_field:
                                item[k3] = _truncate_str(v3, per_field)
                                truncated = True

        if not truncated:
            try:
                current = get_token_count(str(payload))
            except Exception:
                current = max_tokens + 1
            if current <= max_tokens:
                return chunk
            truncated = True  # needs shrinking below

        new_tokens = get_token_count(str(payload))
        if new_tokens > max_tokens:
            if is_tool:
                # Preserve protocol: drop bulky previews first, keep identity.
                slim = copy.deepcopy(payload)
                for drop_key in ("result_preview", "result", "content"):
                    if drop_key in slim and isinstance(slim[drop_key], str):
                        slim[drop_key] = ""
                        if get_token_count(str(slim)) <= max_tokens:
                            payload = slim
                            new_tokens = get_token_count(str(payload))
                            truncated = True
                            break
                if new_tokens > max_tokens:
                    return None  # no-fit: identity would not survive
            else:
                # Reserve wrapper overhead first, then budget content fields
                # against the remainder (5.7: count the final serialized
                # representation, not just field values).
                try:
                    skeleton = {k: ("" if isinstance(v, str) else v) for k, v in payload.items()}
                    overhead = get_token_count(str(skeleton))
                except Exception:
                    overhead = 5
                content_budget = max_tokens - overhead
                if content_budget < 1:
                    return None  # defined no-fit: overhead alone exceeds budget
                fields = [k for k, v in payload.items() if isinstance(v, str) and v]
                if not fields:
                    return None
                per = max(1, content_budget // len(fields))
                for k in fields:
                    payload[k] = _truncate_str(payload[k], per)
                truncated = True
                new_tokens = get_token_count(str(payload))
                if new_tokens > max_tokens:
                    # One last proportional squeeze; else admit no-fit.
                    per2 = max(1, per - (new_tokens - max_tokens) // len(fields) - 1)
                    for k in fields:
                        payload[k] = _truncate_str(payload[k], per2)
                    new_tokens = get_token_count(str(payload))
                    if new_tokens > max_tokens:
                        return None

        # Final guarantee: never return a chunk exceeding the bound.
        if get_token_count(str(payload)) > max_tokens:
            return None

        return ContextChunk(
            id=chunk.id,
            session_id=chunk.session_id,
            agent_id=chunk.agent_id,
            chunk_type=chunk.chunk_type,
            payload=payload,
            token_count=new_tokens,
            # AH-014: payload changed → embedding is stale. Clear it so
            # vector search cannot disagree with stored content.
            embedding=None if truncated else chunk.embedding,
            created_at=chunk.created_at,
            accessed_at=chunk.accessed_at,
        )

    def _chunk_to_text(self, chunk: ContextChunk) -> str:
        """Convert a chunk to text for summarization."""
        chunk_type = chunk.chunk_type
        payload = chunk.payload

        if chunk_type == "tool_call":
            tool = payload.get("tool", "unknown")
            args = payload.get("args", {})
            args_str = ", ".join(f"{k}={v}" for k, v in args.items())
            text = f"[Tool Call] {tool}({args_str})"
            preview = payload.get("result_preview", "")
            if preview:
                text += f"\n[Result] {str(preview)[:200]}"
            return text
        elif chunk_type == "result":
            status = payload.get("status", "ok")
            result = payload.get("result", "")
            if isinstance(result, str) and len(result) > 200:
                result = result[:200] + "..."
            return f"[Result] {status}: {result}"
        elif chunk_type == "user_message":
            return f"[User] {payload.get('content', '')}"
        elif chunk_type == "assistant_message":
            return f"[Assistant] {payload.get('content', '')}"
        elif chunk_type == "memory":
            return f"[Memory] {payload.get('content', '')}"
        else:
            return f"[{chunk_type}] {str(payload)[:200]}"


class RollingCompaction:
    """Monitors token usage and triggers compression when threshold is exceeded.

    Used by the agent to automatically compress context when it grows too large.
    """

    def __init__(self, config: CompressionConfig | None = None) -> None:
        self.config = config or CompressionConfig()
        self._last_compression_tokens: int = 0
        self._compression_count: int = 0

    def should_compress(self, current_tokens: int, context_budget: int) -> bool:
        """Check if compression should be triggered.

        Args:
            current_tokens: Current total token count for the session.
            context_budget: The session's context budget.

        Returns:
            True if compression should be triggered.
        """
        if not self.config.enabled:
            return False

        threshold_tokens = int(context_budget * self.config.threshold)

        # Trigger if we're over threshold and haven't recently compressed
        if current_tokens >= threshold_tokens:
            # Don't compress again until we've grown since last compression
            growth_since_last = current_tokens - self._last_compression_tokens
            if growth_since_last >= threshold_tokens * 0.2:  # At least 20% growth
                return True

        return False

    def record_compression(self, tokens_after: int) -> None:
        """Record that compression happened at this token count."""
        self._last_compression_tokens = tokens_after
        self._compression_count += 1

    @property
    def compression_count(self) -> int:
        """Number of times compression has been triggered."""
        return self._compression_count

    def reset(self) -> None:
        """Reset compaction state."""
        self._last_compression_tokens = 0
        self._compression_count = 0
