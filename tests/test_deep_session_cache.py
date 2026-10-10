"""Session snapshots must not expose mutable objects owned by the cache."""

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import msgpack
import pytest

from ah.core.session import SessionManager


class SessionRows:
    """Replace only the database boundary with durable MessagePack rows."""

    def __init__(self):
        self.session_id = uuid.uuid4()
        self.rows = {
            self.session_id: {
                "id": self.session_id,
                "title": "Durable session",
                "agent_id": "owner",
                "status": "active",
                "state_msgpack": msgpack.packb(
                    {"checkpoint": {"step": 1, "pending": ["review"]}},
                    use_bin_type=True,
                ),
                "goal": "Review changes",
                "model": "test-model",
                "provider": "test-provider",
                "context_budget": 8000,
                "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                "last_activity": datetime(2026, 1, 1, tzinfo=UTC),
            }
        }

    async def fetchrow(self, query, *args):
        if "INSERT INTO sessions" in query:
            title, agent_id, state, goal, model, provider, budget = args
            row = {
                **self.rows[self.session_id],
                "id": uuid.uuid4(),
                "title": title,
                "agent_id": agent_id,
                "state_msgpack": state,
                "goal": goal,
                "model": model,
                "provider": provider,
                "context_budget": budget,
            }
            self.rows[row["id"]] = row
            return row.copy()
        if "FROM sessions WHERE id = $1" in query:
            assert len(args) == 1
            row = self.rows.get(args[0])
            return row.copy() if row else None
        assert "WHERE status = 'active'" in query and not args
        return self.rows[self.session_id].copy()

    async def execute(self, query, *args):
        if "INSERT INTO context_chunks" in query:
            assert len(args) == 2 and all(sid in self.rows for sid in args)
            return "INSERT 0 0"
        assert "SET state_msgpack = $2" in query and len(args) == 2
        self.rows[args[0]]["state_msgpack"] = args[1]
        return "UPDATE 1"

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        yield


@pytest.fixture
def session_rows(monkeypatch):
    rows = SessionRows()
    monkeypatch.setattr("ah.core.session.db", rows)
    return rows


@pytest.mark.parametrize(
    "read", ["get", "cached_get", "create", "get_fresh", "get_last_active", "fork"]
)
@pytest.mark.parametrize("mutation", ["nested_dict", "nested_list", "authority"])
async def test_session_return_values_cannot_mutate_cached_snapshot(session_rows, read, mutation):
    # Reverting either cache boundary to shared/shallow copies must fail:
    # callers may edit their snapshot, but only an explicit write may persist it.
    manager = SessionManager()
    sid = session_rows.session_id
    if read == "cached_get":
        await manager.get(sid)
        session = await manager.get(sid)
    elif read == "create":
        session = await manager.create(
            title="Durable session",
            agent_id="owner",
            state={"checkpoint": {"step": 1, "pending": ["review"]}},
        )
    elif read == "get_last_active":
        session = await manager.get_last_active()
    else:
        session = await getattr(manager, read)(sid)
    assert session is not None

    if mutation == "nested_dict":
        session.state["checkpoint"]["step"] = 99
    elif mutation == "nested_list":
        session.state["checkpoint"]["pending"].append("unpersisted")
    else:
        session.agent_id = "intruder"

    for _ in range(2):
        cached = await manager.get(session.id)
        assert cached is not None
        assert cached.state == {"checkpoint": {"step": 1, "pending": ["review"]}}
        assert cached.agent_id == "owner"

    durable = await manager.get_fresh(session.id)
    assert durable is not None
    assert durable.state == {"checkpoint": {"step": 1, "pending": ["review"]}}
    assert durable.agent_id == "owner"


async def test_explicit_state_write_invalidates_the_cache(session_rows):
    manager = SessionManager()
    session = await manager.get(session_rows.session_id)
    session.state["checkpoint"]["step"] = 2
    await manager.update_state(session.id, session.state)

    cached = await manager.get(session.id)
    assert cached.state == {"checkpoint": {"step": 2, "pending": ["review"]}}
    durable = await manager.get_fresh(session.id)
    assert durable.state == {"checkpoint": {"step": 2, "pending": ["review"]}}
