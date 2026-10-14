"""Stream C: recall precision filters (mocked DB)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from machina.core.session_recall import SessionRecall


def _row(session_id, chunk_id, preview, score, chunk_type, source="archive", occurred=None):
    return {
        "session_id": session_id,
        "chunk_id": chunk_id,
        "title": "t",
        "source": source,
        "preview": preview,
        "occurred_at": occurred or datetime.now(UTC),
        "score": score,
        "chunk_type": chunk_type,
    }


def _mock_db(monkeypatch, rows):
    fetch = AsyncMock(return_value=rows)
    monkeypatch.setattr("machina.core.session_recall.db", SimpleNamespace(fetch=fetch))
    return fetch


async def test_polluted_archive_returns_real_instruction_first(monkeypatch):
    sid = uuid.uuid4()
    rows = [
        _row(sid, uuid.uuid4(), "Skill tdd-guide [0:500/1200]: workflow steps red green", 0.9, "tool_call"),
        _row(sid, uuid.uuid4(), f"[live] prior | session={sid} chunk={uuid.uuid4()} | echoed catalog text", 0.8, "assistant_message"),
        _row(sid, uuid.uuid4(), "Deploy with read-only scope, do not edit files outside /tmp", 0.1, "user_message"),
    ]
    _mock_db(monkeypatch, rows)
    hits = await SessionRecall().discover("harness", "deploy scope files")
    assert hits[0].preview.startswith("Deploy")
    assert "Skill tdd-guide" not in hits[0].preview


async def test_content_hash_suppresses_repeated_catalog(monkeypatch):
    sid = uuid.uuid4()
    catalog = "Skill tdd-guide [0:500/1200]: workflow steps red green"
    rows = [
        _row(sid, uuid.uuid4(), catalog, 0.9, "tool_call"),
        _row(sid, uuid.uuid4(), catalog, 0.85, "tool_call"),
        _row(sid, uuid.uuid4(), "Deploy with read-only scope", 0.1, "user_message"),
    ]
    _mock_db(monkeypatch, rows)
    hits = await SessionRecall().discover("harness", "deploy workflow")
    previews = [h.preview for h in hits]
    assert previews.count(catalog) == 1
    assert hits[0].preview.startswith("Deploy")


async def test_session_filter(monkeypatch):
    a, b = uuid.uuid4(), uuid.uuid4()
    rows = [
        _row(a, uuid.uuid4(), "alpha instruction keep", 0.5, "user_message"),
        _row(b, uuid.uuid4(), "alpha instruction drop", 0.6, "user_message"),
    ]
    _mock_db(monkeypatch, rows)
    hits = await SessionRecall().discover("harness", "alpha", session_id=a)
    assert hits and all(h.session_id == a for h in hits)


async def test_role_type_filter(monkeypatch):
    sid = uuid.uuid4()
    rows = [
        _row(sid, uuid.uuid4(), "alpha tool output", 0.9, "tool_call"),
        _row(sid, uuid.uuid4(), "alpha user instruction", 0.1, "user_message"),
    ]
    _mock_db(monkeypatch, rows)
    hits = await SessionRecall().discover("harness", "alpha", chunk_types=["user_message"])
    assert hits and all("user instruction" in h.preview for h in hits)


async def test_recency_filter(monkeypatch):
    sid = uuid.uuid4()
    now = datetime.now(UTC)
    rows = [
        _row(sid, uuid.uuid4(), "alpha old instruction", 0.9, "user_message", occurred=now - timedelta(days=30)),
        _row(sid, uuid.uuid4(), "alpha new instruction", 0.1, "user_message", occurred=now),
    ]
    _mock_db(monkeypatch, rows)
    hits = await SessionRecall().discover("harness", "alpha", since=now - timedelta(days=1))
    assert len(hits) == 1
    assert "new instruction" in hits[0].preview


async def test_ownership_agent_id_forwarded(monkeypatch):
    sid = uuid.uuid4()
    rows = [_row(sid, uuid.uuid4(), "alpha instruction", 0.5, "user_message")]
    fetch = _mock_db(monkeypatch, rows)
    await SessionRecall().discover("owner-agent", "alpha")
    assert fetch.await_args.args[1] == "owner-agent"
