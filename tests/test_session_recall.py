"""Cross-session transcript discovery over live and archived context."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid

import pytest

from ah.core.context import ContextManager
from ah.core.serialization import payload_to_msgpack
from ah.core.session_recall import SessionRecall
from ah.tools import registry
from ah.tools.agents import current_agent_id, current_session_id


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
        partial = await recall.discover(agent, "where did quartzotter migration happen")
        assert {hit.chunk_id for hit in partial if hit.chunk_id} == {live.id, archived.id}
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
        assert (
            await db_pool.fetchval(
                "SELECT resurrection_count FROM context_archive WHERE chunk_id = $1", middle.id
            )
            == 0
        )
    finally:
        await db_pool.execute("DELETE FROM context_archive WHERE session_id = $1", session_id)
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session_id)


async def test_agent_recall_tools_derive_scope_from_active_session(db_pool, monkeypatch):
    monkeypatch.setattr("ah.core.context.db", db_pool)
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    monkeypatch.setattr("ah.tools.agents.db", db_pool)
    agent = f"recall-{uuid.uuid4()}"
    other_agent = f"other-{uuid.uuid4()}"
    tag = f"mercuryfalcon{uuid.uuid4().hex[:8]}"
    own = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'Own') RETURNING id", agent
    )
    other = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'Other') RETURNING id", other_agent
    )
    try:
        own_chunk = await ContextManager().add_chunk(
            own, agent, "user_message", {"content": f"{tag} my text"}
        )
        other_chunk = await ContextManager().add_chunk(
            other, other_agent, "user_message", {"content": f"{tag} secret"}
        )
        token = current_session_id.set(own)
        agent_token = current_agent_id.set(agent)
        try:
            discovered = await registry.execute("session_recall", query=tag)
            assert str(own_chunk.id) in discovered
            assert str(other_chunk.id) not in discovered
            assert "secret" not in discovered
            window = await registry.execute(
                "session_recall_window", session_id=str(own), chunk_id=str(own_chunk.id)
            )
            assert "my text" in window
            hidden = await registry.execute(
                "session_recall_window", session_id=str(other), chunk_id=str(other_chunk.id)
            )
            assert "secret" not in hidden
            mismatch_token = current_agent_id.set(other_agent)
            try:
                with pytest.raises(Exception, match="does not belong"):
                    await registry.execute("session_recall", query=tag)
            finally:
                current_agent_id.reset(mismatch_token)
        finally:
            current_agent_id.reset(agent_token)
            current_session_id.reset(token)
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = ANY($1::uuid[])", [own, other])


async def test_archived_recall_survives_a_new_process(db_pool, monkeypatch):
    monkeypatch.setattr("ah.core.context.db", db_pool)
    agent = f"restart-{uuid.uuid4()}"
    tag = f"granitekingfisher{uuid.uuid4().hex[:8]}"
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'Restart test') RETURNING id",
        agent,
    )
    chunk_id = uuid.uuid4()
    try:
        await ContextManager().archive_chunk(
            session_id,
            chunk_id,
            payload_to_msgpack({"content": f"{tag} persisted evidence"}),
            agent_id=agent,
            chunk_type="user_message",
        )
        script = """
import asyncio
import os
from ah.core.session_recall import SessionRecall
from ah.db.connection import Database
import ah.core.session_recall as recall_module

async def main():
    connection = Database(dsn=os.environ["AH_RECALL_TEST_DSN"])
    await connection.connect()
    recall_module.db = connection
    try:
        hits = await SessionRecall().discover(os.environ["AH_RECALL_TEST_AGENT"], os.environ["AH_RECALL_TEST_QUERY"])
        print(str(hits[0].chunk_id) if hits else "none")
    finally:
        await connection.close()

asyncio.run(main())
"""
        environment = dict(os.environ)
        environment.update(
            AH_RECALL_TEST_DSN=db_pool.dsn,
            AH_RECALL_TEST_AGENT=agent,
            AH_RECALL_TEST_QUERY=tag,
        )
        process = subprocess.run(
            [sys.executable, "-c", script],
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert process.returncode == 0, process.stderr
        assert process.stdout.strip() == str(chunk_id)
        assert await db_pool.fetchval(
            "SELECT resurrection_count FROM context_archive WHERE chunk_id = $1", chunk_id
        ) == 0
    finally:
        await db_pool.execute("DELETE FROM context_archive WHERE session_id = $1", session_id)
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session_id)
