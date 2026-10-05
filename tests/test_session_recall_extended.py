"""Extended tests for ah.core.session_recall — discover() and window()."""
from __future__ import annotations

import uuid

import pytest

from ah.core.context import ContextManager
from ah.core.serialization import payload_to_msgpack
from ah.core.session_recall import SessionRecall


async def test_discover_live_chunks_only(db_pool, monkeypatch):
    """discover() finds matches in live context_chunks only."""
    monkeypatch.setattr("ah.core.context.db", db_pool)
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    context = ContextManager()
    recall = SessionRecall()
    agent = f"recall-live-{uuid.uuid4()}"
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'No match here') RETURNING id",
        agent,
    )
    try:
        chunk = await context.add_chunk(
            session_id, agent, "user_message", {"content": "zebralope alpha evidence"}
        )
        hits = await recall.discover(agent, "zebralope")
        assert len(hits) == 1
        assert hits[0].source == "live"
        assert hits[0].chunk_id == chunk.id
        assert hits[0].session_id == session_id
        assert "zebralope" in hits[0].preview
    finally:
        await db_pool.execute("DELETE FROM context_chunks WHERE session_id = $1", session_id)
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session_id)


async def test_discover_archive_only(db_pool, monkeypatch):
    """discover() finds matches in context_archive only."""
    monkeypatch.setattr("ah.core.context.db", db_pool)
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    context = ContextManager()
    recall = SessionRecall()
    agent = f"recall-arch-{uuid.uuid4()}"
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'No match') RETURNING id",
        agent,
    )
    chunk_id = uuid.uuid4()
    try:
        await context.archive_chunk(
            session_id,
            chunk_id,
            payload_to_msgpack({"content": "quartzotter archived evidence"}),
            agent_id=agent,
            chunk_type="user_message",
        )
        hits = await recall.discover(agent, "quartzotter")
        assert len(hits) == 1
        assert hits[0].source == "archive"
        assert hits[0].chunk_id == chunk_id
        assert hits[0].session_id == session_id
    finally:
        await db_pool.execute("DELETE FROM context_archive WHERE session_id = $1", session_id)
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session_id)


async def test_discover_title_only(db_pool, monkeypatch):
    """discover() finds matches in session titles only."""
    monkeypatch.setattr("ah.core.context.db", db_pool)
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    recall = SessionRecall()
    agent = f"recall-title-{uuid.uuid4()}"
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'Mercuryfalcon project notes') RETURNING id",
        agent,
    )
    try:
        hits = await recall.discover(agent, "mercuryfalcon")
        assert len(hits) == 1
        assert hits[0].source == "title"
        assert hits[0].chunk_id is None
        assert hits[0].session_id == session_id
        assert hits[0].title == "Mercuryfalcon project notes"
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session_id)


async def test_discover_all_three_sources(db_pool, monkeypatch):
    """discover() returns hits from live, archive, and title in one query."""
    monkeypatch.setattr("ah.core.context.db", db_pool)
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    context = ContextManager()
    recall = SessionRecall()
    agent = f"recall-all3-{uuid.uuid4()}"
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'Granitekingfisher log') RETURNING id",
        agent,
    )
    live_chunk_id = uuid.uuid4()
    arch_chunk_id = uuid.uuid4()
    try:
        # Live chunk
        await context.add_chunk(
            session_id, agent, "user_message", {"content": "granitekingfisher live data"}
        )
        # Archive chunk
        await context.archive_chunk(
            session_id,
            arch_chunk_id,
            payload_to_msgpack({"content": "granitekingfisher old data"}),
            agent_id=agent,
            chunk_type="assistant_message",
        )
        hits = await recall.discover(agent, "granitekingfisher")
        sources = {hit.source for hit in hits}
        assert sources == {"live", "archive", "title"}
        # Title hit has chunk_id=None
        title_hits = [h for h in hits if h.source == "title"]
        assert len(title_hits) == 1
        assert title_hits[0].chunk_id is None
    finally:
        await db_pool.execute("DELETE FROM context_archive WHERE session_id = $1", session_id)
        await db_pool.execute("DELETE FROM context_chunks WHERE session_id = $1", session_id)
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session_id)


async def test_discover_no_matches(db_pool, monkeypatch):
    """discover() returns empty list when nothing matches."""
    monkeypatch.setattr("ah.core.context.db", db_pool)
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    recall = SessionRecall()
    agent = f"recall-none-{uuid.uuid4()}"
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'Something else') RETURNING id",
        agent,
    )
    try:
        await ContextManager().add_chunk(
            session_id, agent, "user_message", {"content": "unrelated content"}
        )
        hits = await recall.discover(agent, "nonexistent")
        assert hits == []
    finally:
        await db_pool.execute("DELETE FROM context_chunks WHERE session_id = $1", session_id)
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session_id)


async def test_discover_agent_scoping(db_pool, monkeypatch):
    """discover() does not return other agents' sessions."""
    monkeypatch.setattr("ah.core.context.db", db_pool)
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    context = ContextManager()
    recall = SessionRecall()
    agent_a = f"recall-a-{uuid.uuid4()}"
    agent_b = f"recall-b-{uuid.uuid4()}"
    session_a = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'Agent A session') RETURNING id",
        agent_a,
    )
    session_b = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'Agent B session') RETURNING id",
        agent_b,
    )
    try:
        await context.add_chunk(
            session_a, agent_a, "user_message", {"content": "sharedkeyword alpha"}
        )
        await context.add_chunk(
            session_b, agent_b, "user_message", {"content": "sharedkeyword beta"}
        )
        hits_a = await recall.discover(agent_a, "sharedkeyword")
        assert all(hit.session_id == session_a for hit in hits_a)
        assert all(hit.session_id != session_b for hit in hits_a)

        hits_b = await recall.discover(agent_b, "sharedkeyword")
        assert all(hit.session_id == session_b for hit in hits_b)
        assert all(hit.session_id != session_a for hit in hits_b)
    finally:
        await db_pool.execute("DELETE FROM context_chunks WHERE session_id = ANY($1::uuid[])", [session_a, session_b])
        await db_pool.execute("DELETE FROM sessions WHERE id = ANY($1::uuid[])", [session_a, session_b])


async def test_discover_agent_scoping_archive(db_pool, monkeypatch):
    """discover() does not return other agents' archived chunks."""
    monkeypatch.setattr("ah.core.context.db", db_pool)
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    context = ContextManager()
    recall = SessionRecall()
    agent_a = f"recall-arch-a-{uuid.uuid4()}"
    agent_b = f"recall-arch-b-{uuid.uuid4()}"
    session_a = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'Session A') RETURNING id",
        agent_a,
    )
    session_b = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'Session B') RETURNING id",
        agent_b,
    )
    try:
        await context.archive_chunk(
            session_a,
            uuid.uuid4(),
            payload_to_msgpack({"content": "archkeyword alpha"}),
            agent_id=agent_a,
            chunk_type="user_message",
        )
        await context.archive_chunk(
            session_b,
            uuid.uuid4(),
            payload_to_msgpack({"content": "archkeyword beta"}),
            agent_id=agent_b,
            chunk_type="user_message",
        )
        hits_a = await recall.discover(agent_a, "archkeyword")
        assert all(hit.session_id == session_a for hit in hits_a)

        hits_b = await recall.discover(agent_b, "archkeyword")
        assert all(hit.session_id == session_b for hit in hits_b)
    finally:
        await db_pool.execute("DELETE FROM context_archive WHERE session_id = ANY($1::uuid[])", [session_a, session_b])
        await db_pool.execute("DELETE FROM sessions WHERE id = ANY($1::uuid[])", [session_a, session_b])


async def test_discover_empty_agent_id_raises(db_pool, monkeypatch):
    """discover() raises ValueError for empty agent_id."""
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    recall = SessionRecall()
    with pytest.raises(ValueError, match="agent_id is required"):
        await recall.discover("", "query")
    with pytest.raises(ValueError, match="agent_id is required"):
        await recall.discover("   ", "query")


async def test_discover_limit_validation(db_pool, monkeypatch):
    """discover() raises ValueError for out-of-range limit."""
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    recall = SessionRecall()
    with pytest.raises(ValueError, match="limit must be between 1 and 100"):
        await recall.discover("agent", "query", limit=0)
    with pytest.raises(ValueError, match="limit must be between 1 and 100"):
        await recall.discover("agent", "query", limit=101)
    with pytest.raises(ValueError, match="limit must be between 1 and 100"):
        await recall.discover("agent", "query", limit=-1)


async def test_discover_query_too_long_raises(db_pool, monkeypatch):
    """discover() raises ValueError for query > 200 chars."""
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    recall = SessionRecall()
    with pytest.raises(ValueError, match="query must be at most 200 characters"):
        await recall.discover("agent", "a" * 201)


async def test_discover_empty_query_returns_empty(db_pool, monkeypatch):
    """discover() returns empty list for empty/whitespace query."""
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    recall = SessionRecall()
    assert await recall.discover("agent", "") == []
    assert await recall.discover("agent", "   ") == []


async def test_discover_stopword_only_query_returns_empty(db_pool, monkeypatch):
    """discover() returns empty list when query is all stopwords."""
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    recall = SessionRecall()
    assert await recall.discover("agent", "the and or") == []


async def test_discover_respects_limit(db_pool, monkeypatch):
    """discover() respects the limit parameter."""
    monkeypatch.setattr("ah.core.context.db", db_pool)
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    context = ContextManager()
    recall = SessionRecall()
    agent = f"recall-limit-{uuid.uuid4()}"
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'Limit test') RETURNING id",
        agent,
    )
    try:
        for i in range(5):
            await context.add_chunk(
                session_id, agent, "user_message", {"content": f"limitword{i} evidence"}
            )
        hits = await recall.discover(agent, "evidence", limit=3)
        assert len(hits) <= 3
    finally:
        await db_pool.execute("DELETE FROM context_chunks WHERE session_id = $1", session_id)
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session_id)


async def test_discover_deduplicates_live_and_archive(db_pool, monkeypatch):
    """discover() deduplicates when same chunk_id exists in both live and archive."""
    monkeypatch.setattr("ah.core.context.db", db_pool)
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    context = ContextManager()
    recall = SessionRecall()
    agent = f"recall-dedup-{uuid.uuid4()}"
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'Dedup test') RETURNING id",
        agent,
    )
    chunk_id = uuid.uuid4()
    try:
        # Add a live chunk
        await context.add_chunk(
            session_id, agent, "user_message", {"content": "dedupkeyword evidence"}
        )
        # Also archive a chunk with the same chunk_id (simulating edge case)
        await context.archive_chunk(
            session_id,
            chunk_id,
            payload_to_msgpack({"content": "dedupkeyword archived"}),
            agent_id=agent,
            chunk_type="user_message",
        )
        hits = await recall.discover(agent, "dedupkeyword")
        # Should have hits but no duplicate chunk_ids from same source
        chunk_ids = [h.chunk_id for h in hits if h.chunk_id is not None]
        assert len(chunk_ids) == len(set(chunk_ids))
    finally:
        await db_pool.execute("DELETE FROM context_archive WHERE session_id = $1", session_id)
        await db_pool.execute("DELETE FROM context_chunks WHERE session_id = $1", session_id)
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session_id)


async def test_window_clamping_at_start(db_pool, monkeypatch):
    """window() clamps to available chunks when before exceeds available history."""
    monkeypatch.setattr("ah.core.context.db", db_pool)
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    context = ContextManager()
    recall = SessionRecall()
    agent = f"recall-clamp-{uuid.uuid4()}"
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'Clamp test') RETURNING id",
        agent,
    )
    try:
        first = await context.add_chunk(
            session_id, agent, "user_message", {"content": "first"}
        )
        second = await context.add_chunk(
            session_id, agent, "assistant_message", {"content": "second"}
        )
        # Request before=10 but only 1 chunk exists before 'second'
        window = await recall.window(agent, session_id, second.id, before=10, after=0)
        assert len(window) == 2
        assert window[0].chunk_id == first.id
        assert window[1].chunk_id == second.id
    finally:
        await db_pool.execute("DELETE FROM context_chunks WHERE session_id = $1", session_id)
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session_id)


async def test_window_clamping_at_end(db_pool, monkeypatch):
    """window() clamps to available chunks when after exceeds available future."""
    monkeypatch.setattr("ah.core.context.db", db_pool)
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    context = ContextManager()
    recall = SessionRecall()
    agent = f"recall-clamp2-{uuid.uuid4()}"
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'Clamp test') RETURNING id",
        agent,
    )
    try:
        first = await context.add_chunk(
            session_id, agent, "user_message", {"content": "first"}
        )
        second = await context.add_chunk(
            session_id, agent, "assistant_message", {"content": "second"}
        )
        # Request after=10 but only 1 chunk exists after 'first'
        window = await recall.window(agent, session_id, first.id, before=0, after=10)
        assert len(window) == 2
        assert window[0].chunk_id == first.id
        assert window[1].chunk_id == second.id
    finally:
        await db_pool.execute("DELETE FROM context_chunks WHERE session_id = $1", session_id)
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session_id)


async def test_window_empty_when_no_chunks(db_pool, monkeypatch):
    """window() returns empty list when session has no chunks."""
    monkeypatch.setattr("ah.core.context.db", db_pool)
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    recall = SessionRecall()
    agent = f"recall-empty-{uuid.uuid4()}"
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'Empty test') RETURNING id",
        agent,
    )
    try:
        window = await recall.window(agent, session_id, uuid.uuid4(), before=5, after=5)
        assert window == []
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session_id)


async def test_window_ordering_chronological(db_pool, monkeypatch):
    """window() returns messages in chronological order."""
    monkeypatch.setattr("ah.core.context.db", db_pool)
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    context = ContextManager()
    recall = SessionRecall()
    agent = f"recall-order-{uuid.uuid4()}"
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'Order test') RETURNING id",
        agent,
    )
    try:
        # Add chunks with explicit time ordering
        c1 = await context.add_chunk(
            session_id, agent, "user_message", {"content": "msg1"}
        )
        c2 = await context.add_chunk(
            session_id, agent, "assistant_message", {"content": "msg2"}
        )
        c3 = await context.add_chunk(
            session_id, agent, "user_message", {"content": "msg3"}
        )
        c4 = await context.add_chunk(
            session_id, agent, "assistant_message", {"content": "msg4"}
        )
        c5 = await context.add_chunk(
            session_id, agent, "user_message", {"content": "msg5"}
        )
        # Stagger created_at to ensure ordering
        for i, c in enumerate([c1, c2, c3, c4, c5]):
            minutes = 5 - i
            await db_pool.execute(
                f"UPDATE context_chunks SET created_at = now() - interval '{minutes} minutes' WHERE id = $1",
                c.id,
            )
        # Window around c3 (middle)
        window = await recall.window(agent, session_id, c3.id, before=2, after=2)
        assert len(window) == 5
        assert [m.chunk_id for m in window] == [c1.id, c2.id, c3.id, c4.id, c5.id]
    finally:
        await db_pool.execute("DELETE FROM context_chunks WHERE session_id = $1", session_id)
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session_id)


async def test_window_before_zero(db_pool, monkeypatch):
    """window() with before=0 returns only anchor and after chunks."""
    monkeypatch.setattr("ah.core.context.db", db_pool)
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    context = ContextManager()
    recall = SessionRecall()
    agent = f"recall-b0-{uuid.uuid4()}"
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'Before zero') RETURNING id",
        agent,
    )
    try:
        c1 = await context.add_chunk(
            session_id, agent, "user_message", {"content": "first"}
        )
        c2 = await context.add_chunk(
            session_id, agent, "assistant_message", {"content": "second"}
        )
        c3 = await context.add_chunk(
            session_id, agent, "user_message", {"content": "third"}
        )
        window = await recall.window(agent, session_id, c2.id, before=0, after=1)
        assert len(window) == 2
        assert window[0].chunk_id == c2.id
        assert window[1].chunk_id == c3.id
    finally:
        await db_pool.execute("DELETE FROM context_chunks WHERE session_id = $1", session_id)
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session_id)


async def test_window_after_zero(db_pool, monkeypatch):
    """window() with after=0 returns only before chunks and anchor."""
    monkeypatch.setattr("ah.core.context.db", db_pool)
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    context = ContextManager()
    recall = SessionRecall()
    agent = f"recall-a0-{uuid.uuid4()}"
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'After zero') RETURNING id",
        agent,
    )
    try:
        c1 = await context.add_chunk(
            session_id, agent, "user_message", {"content": "first"}
        )
        c2 = await context.add_chunk(
            session_id, agent, "assistant_message", {"content": "second"}
        )
        c3 = await context.add_chunk(
            session_id, agent, "user_message", {"content": "third"}
        )
        window = await recall.window(agent, session_id, c2.id, before=1, after=0)
        assert len(window) == 2
        assert window[0].chunk_id == c1.id
        assert window[1].chunk_id == c2.id
    finally:
        await db_pool.execute("DELETE FROM context_chunks WHERE session_id = $1", session_id)
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session_id)


async def test_window_both_zero(db_pool, monkeypatch):
    """window() with before=0 and after=0 returns only the anchor chunk."""
    monkeypatch.setattr("ah.core.context.db", db_pool)
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    context = ContextManager()
    recall = SessionRecall()
    agent = f"recall-both0-{uuid.uuid4()}"
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'Both zero') RETURNING id",
        agent,
    )
    try:
        await context.add_chunk(
            session_id, agent, "user_message", {"content": "first"}
        )
        anchor = await context.add_chunk(
            session_id, agent, "assistant_message", {"content": "anchor"}
        )
        await context.add_chunk(
            session_id, agent, "user_message", {"content": "third"}
        )
        window = await recall.window(agent, session_id, anchor.id, before=0, after=0)
        assert len(window) == 1
        assert window[0].chunk_id == anchor.id
    finally:
        await db_pool.execute("DELETE FROM context_chunks WHERE session_id = $1", session_id)
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session_id)


async def test_window_agent_scope_enforced(db_pool, monkeypatch):
    """window() returns empty for sessions owned by another agent."""
    monkeypatch.setattr("ah.core.context.db", db_pool)
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    context = ContextManager()
    recall = SessionRecall()
    agent_a = f"recall-scope-a-{uuid.uuid4()}"
    agent_b = f"recall-scope-b-{uuid.uuid4()}"
    session_a = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'Session A') RETURNING id",
        agent_a,
    )
    try:
        chunk = await context.add_chunk(
            session_a, agent_a, "user_message", {"content": "scoped content"}
        )
        # Agent B tries to read agent A's session
        window = await recall.window(agent_b, session_a, chunk.id, before=2, after=2)
        assert window == []
    finally:
        await db_pool.execute("DELETE FROM context_chunks WHERE session_id = $1", session_a)
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session_a)


async def test_window_empty_agent_id_raises(db_pool, monkeypatch):
    """window() raises ValueError for empty agent_id."""
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    recall = SessionRecall()
    with pytest.raises(ValueError, match="agent_id is required"):
        await recall.window("", uuid.uuid4(), uuid.uuid4())


async def test_window_before_out_of_range_raises(db_pool, monkeypatch):
    """window() raises ValueError for before > 20."""
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    recall = SessionRecall()
    with pytest.raises(ValueError, match="before and after must be between 0 and 20"):
        await recall.window("agent", uuid.uuid4(), uuid.uuid4(), before=21)


async def test_window_after_out_of_range_raises(db_pool, monkeypatch):
    """window() raises ValueError for after > 20."""
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    recall = SessionRecall()
    with pytest.raises(ValueError, match="before and after must be between 0 and 20"):
        await recall.window("agent", uuid.uuid4(), uuid.uuid4(), after=21)


async def test_window_negative_before_raises(db_pool, monkeypatch):
    """window() raises ValueError for negative before."""
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    recall = SessionRecall()
    with pytest.raises(ValueError, match="before and after must be between 0 and 20"):
        await recall.window("agent", uuid.uuid4(), uuid.uuid4(), before=-1)


async def test_window_negative_after_raises(db_pool, monkeypatch):
    """window() raises ValueError for negative after."""
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    recall = SessionRecall()
    with pytest.raises(ValueError, match="before and after must be between 0 and 20"):
        await recall.window("agent", uuid.uuid4(), uuid.uuid4(), after=-1)


async def test_window_includes_archived_chunks(db_pool, monkeypatch):
    """window() includes archived chunks in the window."""
    monkeypatch.setattr("ah.core.context.db", db_pool)
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    context = ContextManager()
    recall = SessionRecall()
    agent = f"recall-arch-win-{uuid.uuid4()}"
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'Archive window') RETURNING id",
        agent,
    )
    try:
        c1 = await context.add_chunk(
            session_id, agent, "user_message", {"content": "before arch"}
        )
        c2 = await context.add_chunk(
            session_id, agent, "assistant_message", {"content": "archived middle"}
        )
        c3 = await context.add_chunk(
            session_id, agent, "user_message", {"content": "after arch"}
        )
        # Archive the middle chunk
        archive_row = await db_pool.fetchrow(
            "SELECT payload_msgpack, created_at FROM context_chunks WHERE id = $1", c2.id
        )
        await context.archive_chunk(
            session_id,
            c2.id,
            archive_row["payload_msgpack"],
            agent_id=agent,
            chunk_type="assistant_message",
            created_at=archive_row["created_at"],
        )
        await db_pool.execute("DELETE FROM context_chunks WHERE id = $1", c2.id)

        window = await recall.window(agent, session_id, c2.id, before=1, after=1)
        assert len(window) == 3
        assert window[0].chunk_id == c1.id
        assert window[0].source == "live"
        assert window[1].chunk_id == c2.id
        assert window[1].source == "archive"
        assert window[2].chunk_id == c3.id
        assert window[2].source == "live"
    finally:
        await db_pool.execute("DELETE FROM context_archive WHERE session_id = $1", session_id)
        await db_pool.execute("DELETE FROM context_chunks WHERE session_id = $1", session_id)
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session_id)


async def test_window_max_boundary_values(db_pool, monkeypatch):
    """window() accepts before=20 and after=20 (max boundary)."""
    monkeypatch.setattr("ah.core.context.db", db_pool)
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    context = ContextManager()
    recall = SessionRecall()
    agent = f"recall-max-{uuid.uuid4()}"
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'Max boundary') RETURNING id",
        agent,
    )
    try:
        chunks = []
        for i in range(3):
            c = await context.add_chunk(
                session_id, agent, "user_message", {"content": f"msg{i}"}
            )
            chunks.append(c)
        # Should not raise
        window = await recall.window(agent, session_id, chunks[1].id, before=20, after=20)
        assert len(window) == 3
    finally:
        await db_pool.execute("DELETE FROM context_chunks WHERE session_id = $1", session_id)
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session_id)


async def test_discover_preview_truncated(db_pool, monkeypatch):
    """discover() preview is truncated to 240 chars."""
    monkeypatch.setattr("ah.core.context.db", db_pool)
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    context = ContextManager()
    recall = SessionRecall()
    agent = f"recall-prev-{uuid.uuid4()}"
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'Preview test') RETURNING id",
        agent,
    )
    try:
        long_content = "previewword " + "x" * 500
        await context.add_chunk(
            session_id, agent, "user_message", {"content": long_content}
        )
        hits = await recall.discover(agent, "previewword")
        assert len(hits) == 1
        assert len(hits[0].preview) <= 240
    finally:
        await db_pool.execute("DELETE FROM context_chunks WHERE session_id = $1", session_id)
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session_id)


async def test_discover_multiple_sessions_same_agent(db_pool, monkeypatch):
    """discover() can return hits from multiple sessions of the same agent."""
    monkeypatch.setattr("ah.core.context.db", db_pool)
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    context = ContextManager()
    recall = SessionRecall()
    agent = f"recall-multi-{uuid.uuid4()}"
    session1 = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'Session one') RETURNING id",
        agent,
    )
    session2 = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'Session two') RETURNING id",
        agent,
    )
    try:
        await context.add_chunk(
            session1, agent, "user_message", {"content": "multisession alpha"}
        )
        await context.add_chunk(
            session2, agent, "user_message", {"content": "multisession beta"}
        )
        hits = await recall.discover(agent, "multisession")
        session_ids = {h.session_id for h in hits}
        assert session_ids == {session1, session2}
    finally:
        await db_pool.execute("DELETE FROM context_chunks WHERE session_id = ANY($1::uuid[])", [session1, session2])
        await db_pool.execute("DELETE FROM sessions WHERE id = ANY($1::uuid[])", [session1, session2])


async def test_window_nonexistent_chunk_returns_empty(db_pool, monkeypatch):
    """window() returns empty for a chunk_id not in the session."""
    monkeypatch.setattr("ah.core.context.db", db_pool)
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    recall = SessionRecall()
    agent = f"recall-nochunk-{uuid.uuid4()}"
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'No chunk') RETURNING id",
        agent,
    )
    try:
        await ContextManager().add_chunk(
            session_id, agent, "user_message", {"content": "existing"}
        )
        window = await recall.window(agent, session_id, uuid.uuid4(), before=2, after=2)
        assert window == []
    finally:
        await db_pool.execute("DELETE FROM context_chunks WHERE session_id = $1", session_id)
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session_id)


async def test_window_nonexistent_session_returns_empty(db_pool, monkeypatch):
    """window() returns empty for a session_id that doesn't exist."""
    monkeypatch.setattr("ah.core.session_recall.db", db_pool)
    recall = SessionRecall()
    window = await recall.window("agent", uuid.uuid4(), uuid.uuid4(), before=2, after=2)
    assert window == []
