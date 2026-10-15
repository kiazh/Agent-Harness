"""Evidence tests for the 2026-10-15 final-review gate reconciliation.

Four no-mistakes-gate failures were reconciled against earlier pins. Each test
here pins the reconciled invariant directly (fails on the pre-reconciliation
code, passes after), independent of the file that originally hosted it:

1. ``chaos``: a failed context flush retains its batch; the turn-close cleanup
   retries it, so the turn neither fails nor silently drops the batch.
2. ``handoff``: the keyset pagination cursor is the 3-key
   ``(created_at, event_seq, id)`` tuple, so readers pass ``before_seq``.
3. ``integration_permissions``: an owned provider is closed exactly once
   across both close paths (public wrapper + inner helper).
4. ``provider_close_visibility``: parent cancellation cancels the in-flight
   close (counted as ``provider.close.cancelled``), bounded-joins it, and
   re-raises CancelledError.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from machina.core.agent import ReActAgent
from machina.core.agent_factory import _close_owned_provider, close_agent_provider
from machina.core.metrics import MetricsCollector
from machina.core.models import LLMResponse, Session

# ─── 1. Retained-on-failure flush is retried, not dropped ──────────────────


async def test_failed_flush_batch_is_retained_and_retried_at_turn_close():
    session = Session(id=uuid.uuid4(), title="s", context_budget=8000)
    provider = AsyncMock()
    provider.complete = AsyncMock(
        return_value=LLMResponse(content="test", model="test", usage={"total_tokens": 10})
    )

    with patch("machina.core.agent.session_manager") as mock_sm:
        mock_sm.get = AsyncMock(return_value=session)
        mock_sm.update_activity = AsyncMock()
        with patch("machina.core.agent.context_manager") as mock_cm:
            batches: list[list[dict]] = []

            async def _flush(chunks):
                batches.append(list(chunks))  # snapshot: the live list is cleared
                if len(batches) == 2:
                    raise Exception("Connection lost")

            mock_cm.add_chunks_batch = AsyncMock(side_effect=_flush)
            mock_cm.get_latest_images = AsyncMock(return_value=[])
            mock_cm.get_recent_context = AsyncMock(return_value=[])

            agent = ReActAgent(provider=provider)
            response = await agent.run(session.id, "test", verbose=False)

    assert response is not None and response.content == "test"
    # Two in-turn flushes plus one turn-close retry; the retried batch is the
    # exact batch whose flush failed.
    assert len(batches) == 3
    assert batches[2] == batches[1]


# ─── 2. 3-key keyset pagination cursor ─────────────────────────────────────


async def test_get_all_chunks_advances_the_three_key_cursor():
    from machina.core.context import ContextManager
    from machina.core.models import ContextChunk

    mgr = ContextManager()
    base = datetime(2026, 1, 1, tzinfo=UTC)
    store = [
        ContextChunk(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            agent_id="h",
            chunk_type="user_message",
            payload={"content": f"m{i}"},
            token_count=1,
            created_at=base,  # identical timestamps: event_seq must break the tie
            event_seq=i,
        )
        for i in range(600)
    ]
    cursors: list[int | None] = []

    async def _page(
        session_id,
        limit=500,
        before_time=None,
        before_id=None,
        before_seq=None,
        chunk_type=None,
    ):
        cursors.append(before_seq)
        ordered = sorted(store, key=lambda c: (c.created_at, c.event_seq, str(c.id)), reverse=True)
        if before_time is not None:
            ordered = [
                c
                for c in ordered
                if (c.created_at, c.event_seq, str(c.id))
                < (before_time, before_seq, str(before_id))
            ]
        return ordered[:limit]

    mgr.get_chunks_before = _page  # type: ignore[method-assign]
    out = await mgr.get_all_chunks(uuid.uuid4())

    assert cursors[0] is None  # legacy first page keeps the 2-key predicate
    assert all(seq is not None for seq in cursors[1:])  # reader advances the ordinal
    ids = [c.id for c in out]
    assert len(ids) == len(set(ids)), "no duplicate pages"
    assert len(out) == 600, "same-timestamp batch is not skipped"


# ─── 3. Owned provider closed exactly once across both paths ───────────────


async def test_owned_provider_closed_once_across_both_paths():
    closed: list[bool] = []

    class _P:
        async def close(self):
            closed.append(True)

    class _A:
        provider = _P()
        _owns_provider = True

    agent = _A()
    await close_agent_provider(agent)
    await close_agent_provider(agent)  # ownership already consumed
    await _close_owned_provider(agent, timeout=1.0)  # inner seam agrees
    assert closed == [True]
    assert agent._owns_provider is False


# ─── 4. Cancellation cancels + bounded-joins the in-flight close ───────────


async def test_parent_cancellation_cancels_and_joins_inner_close(monkeypatch):
    import machina.core.metrics as metrics_module

    collector = MetricsCollector()
    monkeypatch.setattr(metrics_module, "metrics", collector)

    started, cancelled = asyncio.Event(), asyncio.Event()

    async def close():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    agent = SimpleNamespace(
        _owns_provider=True, agent_id="test-agent", provider=SimpleNamespace(close=close)
    )
    task = asyncio.create_task(close_agent_provider(agent))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The inner close observed cancellation (not the cleanup timeout) and the
    # join held before the error propagated.
    await asyncio.wait_for(cancelled.wait(), 0.5)
    assert collector._counters["provider.close.cancelled"] == 1
