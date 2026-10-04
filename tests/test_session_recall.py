"""Cross-session transcript discovery over live and archived context."""

from __future__ import annotations

import uuid

from ah.core.context import ContextManager
from ah.core.session_recall import SessionRecall


async def test_discovery_searches_live_and_archive_without_cross_agent_leak(db_pool, monkeypatch):
    monkeypatch.setattr("ah.core.context.db", db_pool)
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    context = ContextManager()
    recall = SessionRecall()
    agent = f"recall-{uuid.uuid4()}"
    other_agent = f"other-{uuid.uuid4()}"
    sessions = []
    try:
        for owner, title in (
            (agent, "Ordinary meeting"),
            (agent, "Quartzotter project"),
            (other_agent, "Private meeting"),
        ):
            session_id = await db_pool.fetchval(
                "INSERT INTO sessions (agent_id, title) VALUES ($1, $2) RETURNING id",
                owner,
                title,
            )
            sessions.append(session_id)

        live = await context.add_chunk(
            sessions[0], agent, "user_message", {"content": "quartzotter live evidence"}
        )
        archived = await context.add_chunk(
            sessions[0], agent, "assistant_message", {"content": "quartzotter old evidence"}
        )
        archive_row = await db_pool.fetchrow(
            "SELECT payload_msgpack, created_at FROM context_chunks WHERE id = $1", archived.id
        )
        await context.archive_chunk(
            sessions[0],
            archived.id,
            archive_row["payload_msgpack"],
            agent_id=agent,
            chunk_type="assistant_message",
            created_at=archive_row["created_at"],
        )
        await db_pool.execute("DELETE FROM context_chunks WHERE id = $1", archived.id)
        await context.add_chunk(
            sessions[2], other_agent, "user_message", {"content": "quartzotter private"}
        )

        hits = await recall.discover(agent, "quartzotter")
        assert {(hit.source, hit.chunk_id) for hit in hits} == {
            ("live", live.id),
            ("archive", archived.id),
            ("title", None),
        }
        assert {hit.session_id for hit in hits} == {sessions[0], sessions[1]}
        assert all("private" not in hit.preview for hit in hits)
        assert (
            await db_pool.fetchval(
                "SELECT resurrection_count FROM context_archive WHERE chunk_id = $1", archived.id
            )
            == 0
        )
        assert await recall.discover(agent, "") == []
        assert await recall.discover(agent, "the and") == []
    finally:
        await db_pool.execute(
            "DELETE FROM context_archive WHERE session_id = ANY($1::uuid[])", sessions
        )
        await db_pool.execute("DELETE FROM sessions WHERE id = ANY($1::uuid[])", sessions)


async def test_window_reads_archived_anchor_in_order_and_enforces_agent_scope(db_pool, monkeypatch):
    monkeypatch.setattr("ah.core.context.db", db_pool)
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    context = ContextManager()
    recall = SessionRecall()
    agent = f"recall-{uuid.uuid4()}"
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'Recall window') RETURNING id",
        agent,
    )
    try:
        first = await context.add_chunk(
            session_id, agent, "user_message", {"content": "first message"}
        )
        middle = await context.add_chunk(
            session_id, agent, "assistant_message", {"content": "middle evidence"}
        )
        last = await context.add_chunk(
            session_id, agent, "user_message", {"content": "last message"}
        )
        await db_pool.execute(
            "UPDATE context_chunks SET created_at = now() - interval '3 minutes' WHERE id = $1",
            first.id,
        )
        await db_pool.execute(
            "UPDATE context_chunks SET created_at = now() - interval '2 minutes' WHERE id = $1",
            middle.id,
        )
        archive_row = await db_pool.fetchrow(
            "SELECT payload_msgpack, created_at FROM context_chunks WHERE id = $1", middle.id
        )
        await context.archive_chunk(
            session_id,
            middle.id,
            archive_row["payload_msgpack"],
            agent_id=agent,
            chunk_type="assistant_message",
            created_at=archive_row["created_at"],
        )
        await db_pool.execute("DELETE FROM context_chunks WHERE id = $1", middle.id)

        window = await recall.window(agent, session_id, middle.id, before=1, after=1)
        assert [message.chunk_id for message in window] == [first.id, middle.id, last.id]
        assert [message.source for message in window] == ["live", "archive", "live"]
        assert [message.payload["content"] for message in window] == [
            "first message",
            "middle evidence",
            "last message",
        ]
        assert await recall.window("another-agent", session_id, middle.id) == []
        assert await recall.window(agent, session_id, uuid.uuid4()) == []
        assert await db_pool.fetchval(
            "SELECT resurrection_count FROM context_archive WHERE chunk_id = $1", middle.id
        ) == 0
    finally:
        await db_pool.execute("DELETE FROM context_archive WHERE session_id = $1", session_id)
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session_id)
