"""M5b (H04 residual): the latest usable live compaction summary must not age
out of the live recent-context window.

The M5 lane fixed the prompt side (reserved ``## Compacted Context`` section)
but reported a residual: ``ContextCompressor`` stamps its summary *behind*
the preserved recent chunks (``compression.py``), so
``ContextManager.get_recent_context(session_id, limit=10)`` — the only live
source feeding the assembler from ``machina/core/agent.py`` — silently drops
the summary once ~7 newer chunks exist. The assembler can then only recover
it if archive keyword recall happens to surface an archived copy.

Contract under test: ``get_recent_context`` returns the unchanged newest-*
``limit`` window of ordinary chunks, plus at most ONE extra trailing entry —
the latest usable live ``compression_summary`` — and only when the window
itself carries no usable summary. The assembler's own token budget remains
authoritative for rendering; ordinary-chunk window semantics are untouched.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import machina.core.context as ctxmod
from machina.core.assembler import PromptAssembler, get_token_count
from machina.core.context import ContextManager
from machina.core.serialization import payload_to_msgpack
from tests.support.schema import isolated_schema

pytestmark = [pytest.mark.integration, pytest.mark.db]

BASE = datetime(2026, 1, 1, tzinfo=UTC)
SUMMARY_MARKER = "COMPACTED-M5B-MARKER: earlier turns decided X, Y, Z."


async def _session(database) -> uuid.UUID:
    return await database.fetchval("INSERT INTO sessions(title) VALUES ('m5b') RETURNING id")


async def _seed(
    database,
    session_id: uuid.UUID,
    label: str,
    *,
    chunk_type: str = "user_message",
    created_at: datetime,
    payload: dict[str, Any] | None = None,
    raw_payload: bytes | None = None,
) -> uuid.UUID:
    """Insert one chunk with an explicit, deterministic timestamp."""
    packed = (
        raw_payload
        if raw_payload is not None
        else payload_to_msgpack(payload if payload is not None else {"content": label})
    )
    return await database.fetchval(
        """
        INSERT INTO context_chunks
            (session_id, agent_id, chunk_type, payload_msgpack, token_count, created_at)
        VALUES ($1, 'harness', $2, $3, $4, $5)
        RETURNING id
        """,
        session_id,
        chunk_type,
        packed,
        4,
        created_at,
    )


async def _seed_aged_out_summary(database, session_id: uuid.UUID) -> None:
    """The reproduction: summary stamped at BASE, then 10 newer ordinary chunks."""
    await _seed(
        database,
        session_id,
        SUMMARY_MARKER,
        chunk_type="compression_summary",
        created_at=BASE,
        payload={"content": SUMMARY_MARKER, "original_count": 9, "original_tokens": 900},
    )
    for i in range(10):
        await _seed(
            database,
            session_id,
            f"turn {i}",
            created_at=BASE + timedelta(minutes=i + 1),
        )


async def test_summary_aged_out_of_the_window_still_reaches_the_assembler(db_pool, monkeypatch):
    """RED: after 10 newer chunks the newest-10 window no longer contains the
    summary, so the live source (and therefore the assembler) loses the only
    live record of pre-compaction state."""
    async with isolated_schema(db_pool) as (_schema, database):
        monkeypatch.setattr(ctxmod, "db", database)
        cm = ContextManager()
        sid = await _session(database)
        await _seed_aged_out_summary(database, sid)

        recent = await cm.get_recent_context(sid, limit=10)

        # Control: the ordinary newest-10 window is unchanged (newest first).
        plain = [r for r in recent if r["type"] != "compression_summary"]
        assert [r["payload"]["content"] for r in plain] == [
            f"turn {i}" for i in reversed(range(10))
        ]

        # The single latest usable live summary must still be carried.
        summaries = [r for r in recent if r["type"] == "compression_summary"]
        assert len(summaries) == 1, "latest live compaction summary fell out of the window"
        assert summaries[0]["payload"]["content"] == SUMMARY_MARKER
        assert recent[-1]["type"] == "compression_summary", (
            "the carried summary belongs at the tail: it is older than every window row"
        )

        prompt = PromptAssembler(session_budget=8000).assemble(
            system_prompt="You are a test agent.",
            goal=None,
            recent_chunks=recent,
            retrieved_chunks=[],
            query="continue the task",
        )
        assert "## Compacted Context" in prompt
        assert SUMMARY_MARKER in prompt

        # Control: ordinary recent-activity semantics are unchanged — only the
        # newest three chunks render there, and the carried summary does not
        # displace them.
        for label in ("turn 9", "turn 8", "turn 7"):
            assert label in prompt
        assert "turn 6" not in prompt


async def test_window_without_any_summary_is_unchanged(db_pool, monkeypatch):
    """Control: no summary present → exactly ``limit`` rows, newest first."""
    async with isolated_schema(db_pool) as (_schema, database):
        monkeypatch.setattr(ctxmod, "db", database)
        cm = ContextManager()
        sid = await _session(database)
        for i in range(12):
            await _seed(database, sid, f"turn {i}", created_at=BASE + timedelta(minutes=i))

        recent = await cm.get_recent_context(sid, limit=10)
        assert len(recent) == 10
        assert [r["payload"]["content"] for r in recent] == [
            f"turn {i}" for i in reversed(range(2, 12))
        ]

        prompt = PromptAssembler(session_budget=8000).assemble(
            system_prompt="You are a test agent.",
            goal=None,
            recent_chunks=recent,
            retrieved_chunks=[],
            query="continue the task",
        )
        assert "## Compacted Context" not in prompt, (
            "no summary present: the prompt must be unchanged"
        )
        assert "turn 11" in prompt and "turn 10" in prompt and "turn 9" in prompt
        assert "turn 8" not in prompt


async def test_summary_inside_the_window_is_not_duplicated(db_pool, monkeypatch):
    """Control: the normal post-compaction layout (summary behind 3 preserved
    chunks, still inside the window) is returned exactly once, in place."""
    async with isolated_schema(db_pool) as (_schema, database):
        monkeypatch.setattr(ctxmod, "db", database)
        cm = ContextManager()
        sid = await _session(database)
        await _seed(database, sid, "old turn", created_at=BASE - timedelta(minutes=1))
        await _seed(
            database,
            sid,
            SUMMARY_MARKER,
            chunk_type="compression_summary",
            created_at=BASE,
            payload={"content": SUMMARY_MARKER},
        )
        for i in range(3):
            await _seed(database, sid, f"turn {i}", created_at=BASE + timedelta(minutes=i + 1))

        recent = await cm.get_recent_context(sid, limit=10)
        assert [r["type"] for r in recent] == [
            "user_message",
            "user_message",
            "user_message",
            "compression_summary",
            "user_message",
        ]
        assert sum(1 for r in recent if r["type"] == "compression_summary") == 1


async def test_only_the_latest_usable_live_summary_is_carried(db_pool, monkeypatch):
    """Two live summaries outside the window: only the newest is carried."""
    async with isolated_schema(db_pool) as (_schema, database):
        monkeypatch.setattr(ctxmod, "db", database)
        cm = ContextManager()
        sid = await _session(database)
        await _seed(
            database,
            sid,
            "OLDER-SUMMARY",
            chunk_type="compression_summary",
            created_at=BASE,
            payload={"content": "OLDER-SUMMARY"},
        )
        await _seed(
            database,
            sid,
            "NEWER-SUMMARY",
            chunk_type="compression_summary",
            created_at=BASE + timedelta(minutes=1),
            payload={"content": "NEWER-SUMMARY"},
        )
        for i in range(10):
            await _seed(database, sid, f"turn {i}", created_at=BASE + timedelta(minutes=i + 2))

        recent = await cm.get_recent_context(sid, limit=10)
        summaries = [r for r in recent if r["type"] == "compression_summary"]
        assert len(summaries) == 1
        assert summaries[0]["payload"]["content"] == "NEWER-SUMMARY"


async def test_unusable_newest_summary_does_not_mask_older_usable_one(db_pool, monkeypatch):
    """A degenerate newest row (no content, no checkpoint) cannot hide the
    last usable summary; the assembler would otherwise render nothing."""
    async with isolated_schema(db_pool) as (_schema, database):
        monkeypatch.setattr(ctxmod, "db", database)
        cm = ContextManager()
        sid = await _session(database)
        await _seed(
            database,
            sid,
            "USABLE-SUMMARY",
            chunk_type="compression_summary",
            created_at=BASE,
            payload={"content": "USABLE-SUMMARY"},
        )
        await _seed(
            database,
            sid,
            "",
            chunk_type="compression_summary",
            created_at=BASE + timedelta(minutes=1),
            payload={"content": "   ", "original_count": 0},
        )
        for i in range(10):
            await _seed(database, sid, f"turn {i}", created_at=BASE + timedelta(minutes=i + 2))

        recent = await cm.get_recent_context(sid, limit=10)
        summaries = [r for r in recent if r["type"] == "compression_summary"]
        assert len(summaries) == 1
        assert summaries[0]["payload"]["content"] == "USABLE-SUMMARY"


async def test_corrupt_newest_summary_does_not_break_recovery(db_pool, monkeypatch):
    """Recovery reads rows the window never touched: a corrupt summary row is
    skipped instead of failing the turn or masking the last usable summary."""
    async with isolated_schema(db_pool) as (_schema, database):
        monkeypatch.setattr(ctxmod, "db", database)
        cm = ContextManager()
        sid = await _session(database)
        await _seed(
            database,
            sid,
            "USABLE-SUMMARY",
            chunk_type="compression_summary",
            created_at=BASE,
            payload={"content": "USABLE-SUMMARY"},
        )
        # 0xc1 is msgpack's reserved byte: unpacking always fails.
        await _seed(
            database,
            sid,
            "",
            chunk_type="compression_summary",
            created_at=BASE + timedelta(minutes=1),
            raw_payload=b"\xc1",
        )
        for i in range(10):
            await _seed(database, sid, f"turn {i}", created_at=BASE + timedelta(minutes=i + 2))

        recent = await cm.get_recent_context(sid, limit=10)
        summaries = [r for r in recent if r["type"] == "compression_summary"]
        assert len(summaries) == 1
        assert summaries[0]["payload"]["content"] == "USABLE-SUMMARY"


async def test_carried_summary_respects_the_assembler_token_budget(db_pool, monkeypatch):
    """Budget control: the carried entry goes through the same ``add()``
    accounting as every other section — a tiny budget still caps the prompt."""
    async with isolated_schema(db_pool) as (_schema, database):
        monkeypatch.setattr(ctxmod, "db", database)
        cm = ContextManager()
        sid = await _session(database)
        await _seed_aged_out_summary(database, sid)
        recent = await cm.get_recent_context(sid, limit=10)

        budget = 220
        prompt = PromptAssembler(session_budget=budget).assemble(
            system_prompt="You are a test agent.",
            goal=None,
            recent_chunks=recent,
            retrieved_chunks=[],
            query="continue the task",
        )
        assert get_token_count(prompt) <= budget + 5
        assert "## Compacted Context" in prompt
