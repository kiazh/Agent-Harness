"""Gateway feature methods (sessions, context, memory, skills, config, profiles, status)
exercised against the real test database."""

from __future__ import annotations

import json
import os
import uuid

import pytest

from ah.core.config import config
from ah.core.context import context_manager
from ah.core.session import session_manager
from ah.db.connection import db
from ah.gateway.server import INVALID_PARAMS, NOT_FOUND, Gateway
from tests.test_gateway import Harness

pytestmark = pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_DATABASE_URL"),
    reason="AGENT_HARNESS_TEST_DATABASE_URL not set",
)


@pytest.fixture
async def h():
    harness = Harness()
    assert "result" in await harness.call("initialize")
    yield harness
    await harness.gateway.close()


async def new_session(h: Harness, title: str = "feature test") -> str:
    return (await h.call("session.create", {"title": title}))["result"]["session"]["id"]


async def add_turn(sid: str, user: str, assistant: str) -> None:
    session_id = uuid.UUID(sid)
    await context_manager.add_chunk(session_id, "harness", "user_message", {"content": user}, 5)
    await context_manager.add_chunk(
        session_id, "harness", "assistant_message", {"content": assistant}, 5
    )


def result(response: dict) -> dict:
    assert "result" in response, response
    return response["result"]


def test_every_feature_method_is_registered():
    from ah.gateway import features

    methods = Gateway(lambda frame: None)._methods
    assert set(features.METHODS) <= set(methods)


class TestSessions:
    async def test_recall_search_and_window_are_scoped_to_current_agent(self, h):
        tag = f"quartzotter{uuid.uuid4().hex[:8]}"
        sid = await new_session(h, "Recall owner")
        other = await session_manager.create(title="Other agent", agent_id=f"other-{tag}")
        try:
            own_chunk = await context_manager.add_chunk(
                uuid.UUID(sid),
                "harness",
                "user_message",
                {"content": f"{tag} own evidence", "attachment": b"\x00\xff"},
            )
            other_chunk = await context_manager.add_chunk(
                other.id, other.agent_id, "user_message", {"content": f"{tag} private"}
            )
            hits = result(await h.call("session.recall", {"sessionId": sid, "query": tag}))["hits"]
            assert [(hit["sessionId"], hit["chunkId"]) for hit in hits] == [
                (sid, str(own_chunk.id))
            ]
            window = result(
                await h.call(
                    "session.recall.window",
                    {
                        "sessionId": sid,
                        "targetSessionId": sid,
                        "chunkId": str(own_chunk.id),
                    },
                )
            )["messages"]
            assert [item["payload"]["content"] for item in window] == [f"{tag} own evidence"]
            assert window[0]["payload"]["attachment"] == {"$base64": "AP8="}
            json.dumps(window)
            hidden = result(
                await h.call(
                    "session.recall.window",
                    {
                        "sessionId": sid,
                        "targetSessionId": str(other.id),
                        "chunkId": str(other_chunk.id),
                    },
                )
            )["messages"]
            assert hidden == []
            invalid = await h.call("session.recall", {"query": tag})
            assert invalid["error"]["code"] == INVALID_PARAMS
        finally:
            await session_manager.delete(uuid.UUID(sid))
            await session_manager.delete(other.id)

    async def test_rename_goal_search_fork_delete(self, h):
        tag = uuid.uuid4().hex[:8]
        sid = await new_session(h)
        renamed = result(
            await h.call("session.rename", {"sessionId": sid, "title": f"zebra {tag}"})
        )
        assert renamed["session"]["title"] == f"zebra {tag}"

        goal = result(await h.call("session.setGoal", {"sessionId": sid, "goal": "ship it"}))
        assert goal["goal"] == "ship it"

        found = result(await h.call("session.search", {"query": tag}))["sessions"]
        assert sid in {s["id"] for s in found}

        await add_turn(sid, "hello", "hi there")
        fork = result(await h.call("session.fork", {"sessionId": sid, "title": "branch"}))[
            "session"
        ]
        assert fork["id"] != sid and fork["title"] == "branch"
        history = result(await h.call("session.resume", {"sessionId": fork["id"]}))["history"]
        assert [e["content"] for e in history] == ["hello", "hi there"]

        assert result(await h.call("session.delete", {"sessionId": fork["id"]}))["deleted"] is True
        missing = await h.call("session.resume", {"sessionId": fork["id"]})
        assert missing["error"]["code"] == NOT_FOUND

    async def test_export_markdown(self, h):
        sid = await new_session(h, "export me")
        await add_turn(sid, "what is 2+2?", "4")
        markdown = result(await h.call("session.export", {"sessionId": sid}))["markdown"]
        assert "# Session: export me" in markdown
        assert markdown.index("what is 2+2?") < markdown.index("### Assistant")

    async def test_rename_requires_title(self, h):
        sid = await new_session(h)
        response = await h.call("session.rename", {"sessionId": sid, "title": "  "})
        assert response["error"]["code"] == INVALID_PARAMS


class TestContext:
    async def test_get_and_compress(self, h):
        sid = await new_session(h)
        for i in range(8):
            await add_turn(sid, f"question {i} " + "x" * 200, f"answer {i} " + "y" * 200)

        ctx = result(await h.call("context.get", {"sessionId": sid, "limit": 5}))
        assert len(ctx["chunks"]) == 5
        assert ctx["totalTokens"] > 0
        assert ctx["chunks"][0]["preview"].startswith("answer 7")

        original = config.compression_llm_summarize
        config.compression_llm_summarize = False  # truncation only: no LLM call in tests
        try:
            out = result(await h.call("context.compress", {"sessionId": sid}))
        finally:
            config.compression_llm_summarize = original
        assert out["compressed"] is True
        assert out["method"] == "truncate"
        assert out["compressedTokens"] < out["originalTokens"]

    async def test_compress_empty_session(self, h):
        sid = await new_session(h)
        assert result(await h.call("context.compress", {"sessionId": sid})) == {"compressed": False}


class TestMemory:
    async def test_session_scoped_memory_operations_keep_agents_separate(self, h):
        tag = f"amberheron{uuid.uuid4().hex[:8]}"
        own_sid = await new_session(h)
        other_agent = f"other-{uuid.uuid4()}"
        other_session = await session_manager.create(title="Other memory", agent_id=other_agent)
        try:
            own = result(await h.call("memory.add", {
                "sessionId": own_sid, "content": f"{tag} own", "category": "fact",
            }))["memory"]
            other = result(await h.call("memory.add", {
                "sessionId": str(other_session.id), "content": f"{tag} private", "category": "fact",
            }))["memory"]
            listed = result(await h.call("memory.list", {"sessionId": own_sid}))["memories"]
            assert own["id"] in {item["id"] for item in listed}
            assert other["id"] not in {item["id"] for item in listed}
            found = result(await h.call("memory.search", {
                "sessionId": own_sid, "query": "amberheron",
            }))["results"]
            assert [item["id"] for item in found] == [own["id"]]
            denied = await h.call("memory.forget", {"sessionId": own_sid, "id": other["id"]})
            assert denied["error"]["code"] == NOT_FOUND
            assert result(await h.call("memory.forget", {
                "sessionId": str(other_session.id), "id": other["id"],
            }))["deleted"] is True
        finally:
            await db.execute("DELETE FROM memories WHERE agent_id IN ('harness', $1) AND content LIKE $2", other_agent, f"{tag}%")
            await session_manager.delete(uuid.UUID(own_sid))
            await session_manager.delete(other_session.id)

    async def test_share_uses_session_owner_and_rejects_other_agents_memory(self, h, monkeypatch):
        from ah.memory.store import memory_store

        monkeypatch.setenv("AGENT_HARNESS_PROVENANCE_KEY", "test-only-provenance-key")
        sid = await new_session(h)
        other_agent = f"other-{uuid.uuid4()}"
        recipient = f"recipient-{uuid.uuid4()}"
        own = await memory_store.add(None, "harness", "The beacon is green", "fact")
        other = await memory_store.add(None, other_agent, "The beacon is private", "fact")
        try:
            shared = result(await h.call("memory.share", {
                "sessionId": sid, "id": str(own.id), "recipientAgent": recipient,
                "publisherAgent": other_agent,
            }))
            assert shared["status"] == "accepted"
            assert len(await memory_store.search(agent_id=recipient)) == 1
            denied = await h.call("memory.share", {
                "sessionId": sid, "id": str(other.id), "recipientAgent": recipient,
            })
            assert denied["error"]["code"] == NOT_FOUND
        finally:
            await memory_store.delete(own.id)
            await memory_store.delete(other.id)
            await db.execute("DELETE FROM memories WHERE agent_id = $1", recipient)
            await session_manager.delete(uuid.UUID(sid))

    async def test_add_list_search_forget(self, h):
        tag = uuid.uuid4().hex[:10]
        added = result(
            await h.call(
                "memory.add", {"content": f"User prefers kelp {tag}", "category": "preference"}
            )
        )["memory"]
        assert added["category"] == "preference"

        listed = result(await h.call("memory.list", {"category": "preference", "limit": 500}))
        assert added["id"] in {m["id"] for m in listed["memories"]}

        hits = result(await h.call("memory.search", {"query": f"kelp {tag}"}))["results"]
        assert hits and hits[0]["id"] == added["id"]

        assert result(await h.call("memory.forget", {"id": added["id"]}))["deleted"] is True
        assert result(await h.call("memory.forget", {"id": added["id"]}))["deleted"] is False

    async def test_add_validates(self, h):
        bad = await h.call("memory.add", {"content": "x", "category": "gossip"})
        assert bad["error"]["code"] == INVALID_PARAMS
        bad = await h.call("memory.add", {"content": "x", "importance": 3})
        assert bad["error"]["code"] == INVALID_PARAMS

    async def test_approval_flow(self, h, monkeypatch):
        from ah.memory.approval import memory_approval_gate

        monkeypatch.setattr(memory_approval_gate, "enabled", True)
        keep = await memory_approval_gate.submit(content="keep this fact", category="fact")
        drop = await memory_approval_gate.submit(content="drop this fact", category="fact")

        pending_ids = {
            p["id"] for p in result(await h.call("memory.pending", {"limit": 500}))["pending"]
        }
        assert {str(keep.id), str(drop.id)} <= pending_ids

        approved = result(await h.call("memory.approve", {"id": str(keep.id)}))["memory"]
        assert approved["content"] == "keep this fact"
        assert result(await h.call("memory.reject", {"id": str(drop.id), "note": "nope"})) == {
            "rejected": True
        }

        again = await h.call("memory.approve", {"id": str(keep.id)})
        assert again["error"]["code"] == NOT_FOUND

        stats = result(await h.call("memory.stats"))
        assert stats["approval"]["approved"] >= 1 and stats["approval"]["rejected"] >= 1

        await memory_approval_gate.submit(content="bulk reject me", category="event")
        assert result(await h.call("memory.rejectAll"))["count"] >= 1
        assert result(await h.call("memory.approveAll"))["count"] == 0


class TestSkills:
    async def test_learn_show_list_curator_delete(self, h, tmp_path, monkeypatch):
        from ah.skills.registry import skill_registry

        monkeypatch.setattr(skill_registry, "skills_dir", tmp_path / "skills")
        monkeypatch.setattr(skill_registry, "_skills", {})
        source = tmp_path / "deploy-notes.md"
        source.write_text("# Deploying\nRun the migrations first.", encoding="utf-8")

        learned = result(
            await h.call(
                "skills.learn", {"source": str(source), "name": "deploying", "triggers": ["deploy"]}
            )
        )["skill"]
        assert learned["name"] == "deploying" and learned["triggers"] == ["deploy"]

        shown = result(await h.call("skills.show", {"name": "deploying"}))["skill"]
        assert "migrations first" in shown["content"]
        assert "deploying" in {s["name"] for s in result(await h.call("skills.list"))["skills"]}
        assert "deploying" in result(await h.call("skills.curator"))["unused"]

        assert result(await h.call("skills.delete", {"name": "deploying"})) == {"deleted": True}
        assert (await h.call("skills.show", {"name": "deploying"}))["error"]["code"] == NOT_FOUND

    async def test_learn_reports_bad_source(self, h, tmp_path, monkeypatch):
        from ah.skills.registry import skill_registry

        monkeypatch.setattr(skill_registry, "skills_dir", tmp_path / "skills")
        response = await h.call("skills.learn", {"source": str(tmp_path / "missing.md")})
        assert "Source not found" in response["error"]["message"]


class TestConfigProfilesStatus:
    async def test_config_get_masks_secrets_and_set_updates(self, h, monkeypatch):
        snapshot = result(await h.call("config.get"))
        assert snapshot["config"]["openrouter_api_key"] in (True, False)
        assert "openrouter_api_key" in snapshot["secrets"]

        monkeypatch.setattr(config, "max_iterations", config.max_iterations)
        out = result(await h.call("config.set", {"key": "max_iterations", "value": "7"}))
        assert out["value"] == 7 and config.get("max_iterations") == 7
        flag = result(await h.call("config.set", {"key": "verbose", "value": "off"}))
        assert flag["value"] is False

    async def test_profiles(self, h):
        user = f"user-{uuid.uuid4().hex[:8]}"
        created = result(await h.call("profile.get", {"userId": user, "displayName": "Kia"}))[
            "profile"
        ]
        assert created["userId"] == user and created["displayName"] == "Kia"
        updated = result(
            await h.call("profile.set", {"userId": user, "key": "tone", "value": "concise"})
        )
        assert updated["profile"]["preferences"] == {"tone": "concise"}
        assert user in {
            p["userId"] for p in result(await h.call("profile.list", {"limit": 500}))["profiles"]
        }

    async def test_status(self, h):
        status = result(await h.call("status"))
        assert status["postgres"].startswith("PostgreSQL")
        assert status["sessions"] >= 0 and "read_file" in status["tools"]
