"""Durable RSI behavior against the dedicated test database."""

import asyncio
import uuid

import pytest

from ah.memory.growth import GrowthStore


@pytest.fixture
async def rsi_store(db_pool):
    owner = "rsi-test-" + uuid.uuid4().hex
    store = GrowthStore(db_pool)
    try:
        yield store, owner
    finally:
        await db_pool.execute("DELETE FROM agent_growth WHERE agent_id = $1", owner)


async def test_rsi_versions_persona_and_restores_it_after_restart(rsi_store, db_pool):
    store, owner = rsi_store
    await store.teach(owner, "style", "tone", "Be concise")
    first = await store.rsi_status(owner)
    assert first["revision"] == 1 and first["trial"] is None
    await store.teach(owner, "style", "tone", "Be warmer", origin="agent")
    restarted = GrowthStore(db_pool)
    second = await restarted.rsi_status(owner)
    assert second["revision"] == 2 and second["trial"]["revision"] == 2
    assert await restarted.rollback(owner, 1)
    assert (await store.get(owner))["notes"] == {"style:tone": "Be concise"}
    restored = await store.rsi_status(owner)
    assert restored["revision"] == 3 and restored["trial"] is None
    assert restored["history"][0]["reason"] == "rollback"


@pytest.mark.parametrize("reward,kept", [(-1, False), (1, True)])
async def test_trial_evaluation_uses_three_human_ratings_of_that_revision(rsi_store, reward, kept):
    store, owner = rsi_store
    await store.teach(owner, "style", "tone", "Be concise")
    stale = await store.begin(owner, None, "implement Python feature", 4000)
    await store.complete(owner, stale["id"])
    await store.teach(owner, "style", "tone", "Be warmer", origin="agent")
    assert await store.feedback(owner, stale["id"], reward)
    assert (await store.rsi_status(owner))["trial"]["ratings"] == 0
    for index in range(3):
        episode = await store.begin(owner, None, "implement Python feature", 4000)
        await store.complete(owner, episode["id"])
        assert await store.feedback(owner, episode["id"], reward)
        if index < 2:
            assert (await store.rsi_status(owner))["trial"] is not None
    status = await store.rsi_status(owner)
    assert status["trial"] is None
    assert (await store.get(owner))["notes"]["style:tone"] == (
        "Be warmer" if kept else "Be concise"
    )
    assert status["history"][0]["status"] == ("stable" if kept else "rolled_back")


async def test_human_correction_supersedes_trial_and_old_negative_ratings(rsi_store):
    store, owner = rsi_store
    await store.teach(owner, "style", "tone", "Be concise")
    await store.teach(owner, "style", "tone", "Be warmer", origin="agent")
    episodes = [await store.begin(owner, None, "task", 4000) for _ in range(3)]
    await store.teach(owner, "style", "tone", "Use the tone I explicitly requested")
    for episode in episodes:
        await store.complete(owner, episode["id"])
        await store.feedback(owner, episode["id"], -1)
    assert (await store.get(owner))["notes"]["style:tone"] == "Use the tone I explicitly requested"
    assert (await store.rsi_status(owner))["trial"] is None


async def test_operational_evidence_is_once_only_and_human_rating_overrides_proxy(rsi_store):
    store, owner = rsi_store
    episode = await store.begin(owner, None, "fix Python bug", 4000)
    outcome = {
        "terminal": "completed",
        "tools": [{"name": "file_read", "ok": True}],
        "tokens": 50,
        "iterations": 2,
    }
    await asyncio.gather(
        *[
            store.complete(owner, episode["id"], outcome=outcome, skills=["ecc-python-patterns"])
            for _ in range(2)
        ]
    )
    status = await store.rsi_status(owner)
    assert status["observations"] == 1
    assert status["automatic_observations"] == 1 and status["human_ratings"] == 0
    assert status["skill_weights"]["ecc-python-patterns"] > 1
    assert status["tool_weights"]["file_read"] > 1
    ratings = await asyncio.gather(*[store.feedback(owner, episode["id"], -1) for _ in range(2)])
    assert sorted(ratings) == [False, True]
    rated = await store.rsi_status(owner)
    assert rated["observations"] == 1 and rated["human_ratings"] == 1
    assert rated["skill_weights"]["ecc-python-patterns"] < 1
    assert rated["tool_weights"]["file_read"] < 1
    next_turn = await store.begin(owner, None, "fix Python bug", 4000)
    assert next_turn["rsi"]["tool_weights"]["file_read"] < 1


async def test_claimed_success_and_unfinished_turns_do_not_create_positive_evidence(rsi_store):
    store, owner = rsi_store
    unfinished = await store.begin(owner, None, "task", 4000)
    assert not await store.feedback(owner, unfinished["id"], 1)
    claimed = await store.begin(owner, None, "task", 4000)
    await store.complete(
        owner,
        claimed["id"],
        outcome={"terminal": "completed", "tools": [], "tokens": 12, "iterations": 1},
    )
    status = await store.rsi_status(owner)
    assert status["automatic_observations"] == 0
    assert status["skill_weights"] == {} and status["tool_weights"] == {}


async def test_rsi_bounds_history_and_does_not_restore_another_agent(rsi_store):
    store, owner = rsi_store
    for index in range(55):
        await store.teach(owner, "style", "tone", f"Tone {index}")
    status = await store.rsi_status(owner)
    assert status["revision"] == 55 and len(status["history"]) <= 50
    assert not await store.rollback(owner, 1)
    assert not await store.rollback(owner + "other", 55)
    for invalid in (-1, True, "55"):
        with pytest.raises(ValueError):
            await store.rollback(owner, invalid)


async def test_first_agent_trial_can_restore_original_baseline(rsi_store):
    store, owner = rsi_store
    await store.teach(owner, "style", "tone", "Trial tone", origin="agent")
    assert await store.rollback(owner, 0)
    assert (await store.get(owner))["notes"] == {}
    assert (await store.rsi_status(owner))["trial"] is None


@pytest.mark.parametrize("operation", ["teach", "confirm", "forget", "install"])
async def test_human_choice_cannot_be_undone_by_stale_trial_ratings(
    rsi_store, monkeypatch, operation
):
    from ah.core.config import config
    from ah.memory.growth import initial_policy

    store, owner = rsi_store
    await store.teach(owner, "style", "tone", "Stable")
    await store.teach(owner, "style", "tone", "Trial", origin="agent")
    episodes = [await store.begin(owner, None, "task", 4000) for _ in range(3)]
    monkeypatch.setattr(config, "rsi_enabled", operation == "confirm")
    if operation in {"teach", "confirm"}:
        await store.teach(owner, "style", "tone", "Trial" if operation == "confirm" else "Human")
    elif operation == "forget":
        await store.forget(owner, "style:tone")
    else:
        await store.install_policy(owner, initial_policy().to_dict())
    chosen = (await store.get(owner))["notes"]
    monkeypatch.setattr(config, "rsi_enabled", True)
    for episode in episodes:
        await store.complete(owner, episode["id"])
        await store.feedback(owner, episode["id"], -1)
    assert (await store.get(owner))["notes"] == chosen
    assert (await store.rsi_status(owner))["trial"] is None


@pytest.mark.parametrize("reward", [0, -1, 1])
async def test_human_reward_replaces_automatic_memory_update(rsi_store, db_pool, reward):
    import json

    from ah.memory.policy import RetrievalPolicy
    from ah.memory.rl import MemoryAction

    store, owner = rsi_store
    episode = await store.begin(owner, None, "fix Python bug", 4000)
    expected = RetrievalPolicy.from_dict((await store.get(owner))["policy"])
    row = await db_pool.fetchrow("SELECT * FROM growth_episodes WHERE id = $1", episode["id"])
    await store.complete(
        owner,
        episode["id"],
        outcome={"terminal": "completed", "tools": [{"name": "file_read", "ok": True}]},
    )
    assert await store.feedback(owner, episode["id"], reward)
    expected.observe_features(
        tuple(json.loads(row["features"])), MemoryAction(row["action"]), reward, row["probability"]
    )
    actual = (await store.get(owner))["policy"]
    for action, weights in expected.to_dict().items():
        assert actual[action] == pytest.approx(weights, abs=1e-14)


def test_execution_status_controls_reward_even_when_response_claims_success():
    from ah.core.models import AgentResponse
    from ah.memory.rsi import operational_reward, summarize_outcome

    response = AgentResponse(
        content="session request budget exceeded (1/1)",
        tool_calls=[{"tool": "read_file", "outcome": "completed"}],
        outcome="failed",
    )
    assert operational_reward(summarize_outcome(response)) == -0.5


@pytest.mark.parametrize("restore", ["manual", "trial", "import_baseline"])
async def test_restored_policy_keeps_provenance_for_later_human_rating(rsi_store, restore):
    from ah.memory.growth import initial_policy

    store, owner = rsi_store
    episode = await store.begin(owner, None, "fix Python", 4000)
    original = (await store.get(owner))["policy"]
    await store.complete(
        owner,
        episode["id"],
        outcome={"terminal": "completed", "tools": [{"name": "read_file", "ok": True}]},
    )
    if restore == "import_baseline":
        await store.install_policy(owner, initial_policy().to_dict())
        assert await store.rollback(owner, 0)
    else:
        await store.teach(owner, "style", "tone", "Stable")
        await store.teach(owner, "style", "tone", "Trial", origin="agent")
        if restore == "manual":
            assert await store.rollback(owner, 1)
        else:
            for _ in range(3):
                trial = await store.begin(owner, None, "task", 4000)
                await store.complete(owner, trial["id"])
                await store.feedback(owner, trial["id"], -1)
    assert await store.feedback(owner, episode["id"], 0)
    actual = (await store.get(owner))["policy"]
    for action, weights in original.items():
        assert actual[action] == pytest.approx(weights, abs=1e-14)


async def test_restoration_excludes_proxy_updates_after_the_checkpoint(rsi_store):
    store, owner = rsi_store
    await store.teach(owner, "style", "tone", "Stable")
    original = (await store.get(owner))["policy"]
    episode = await store.begin(owner, None, "fix Python", 4000)
    await store.complete(
        owner,
        episode["id"],
        outcome={"terminal": "completed", "tools": [{"name": "read_file", "ok": True}]},
    )
    assert await store.rollback(owner, 1)
    assert await store.feedback(owner, episode["id"], 0)
    assert (await store.get(owner))["policy"] == original


@pytest.mark.parametrize("management", ["restore", "import"])
async def test_inflight_completion_after_policy_replacement_can_be_human_corrected(
    rsi_store, management
):
    store, owner = rsi_store
    await store.teach(owner, "style", "tone", "Stable")
    original = (await store.get(owner))["policy"]
    episode = await store.begin(owner, None, "fix Python", 4000)
    if management == "restore":
        assert await store.rollback(owner, 1)
    else:
        await store.install_policy(owner, original)
    await store.complete(
        owner,
        episode["id"],
        outcome={"terminal": "completed", "tools": [{"name": "read_file", "ok": True}]},
    )
    assert await store.feedback(owner, episode["id"], 0)
    for action, weights in original.items():
        assert (await store.get(owner))["policy"][action] == pytest.approx(weights, abs=1e-14)


def test_failed_shell_exit_has_typed_status_even_with_output(tmp_path):
    import sys

    from ah.tools.terminal import _run_bounded

    result = _run_bounded(
        [sys.executable, "-c", "print('test failed'); raise SystemExit(1)"], 3, str(tmp_path)
    )
    assert result.is_error is True
    assert "test failed" in result


def test_rsi_outcome_summary_ignores_raw_response_and_redacts_tool_identifiers():
    from ah.core.models import AgentResponse
    from ah.memory.rsi import summarize_outcome

    secret = "sk-or-v1-" + "a" * 50
    summary = summarize_outcome(
        AgentResponse(
            content="I succeeded " + secret,
            tool_calls=[
                {"tool": "file_read", "result": secret, "args": {"secret": secret}},
                {"tool": "terminal", "result": "Error: failed " + secret},
            ],
            tokens_used=30,
            iterations=2,
        )
    )
    assert summary["tools"] == [
        {"name": "file_read", "ok": True},
        {"name": "terminal", "ok": False},
    ]
    assert secret not in str(summary)
    assert summary["terminal"] == "completed"
