"""Integration and algorithm tests for the second research implementation phase."""

from __future__ import annotations

import json
import uuid

import pytest

from machina.memory.identity import AgentBelief, IdentityGate
from machina.memory.policy import GroupRelativeTrainer, MemoryPolicy
from machina.memory.rl import MemoryAction, MemoryState
from machina.research.identity_eval import evaluate_identity_scenarios, load_identity_scenarios
from machina.research.identity_eval import main as identity_main
from machina.research.locomo import (
    Conversation,
    DialogueTurn,
    Question,
    evaluate_archive_retrieval,
    evaluate_retrieval,
    evaluate_session_recall,
    load_locomo,
)
from machina.research.train_memory_policy import load_scored_states, train_from_file
from machina.soulspec.schema import SoulSpec


def _state(task: str) -> MemoryState:
    return MemoryState(conversation=[], current_memory=[], task=task, budget_remaining=4000)


def test_group_relative_policy_learns_distinct_actions_and_round_trips(tmp_path):
    states = [_state("store fact"), _state("retrieve fact")]
    policy = MemoryPolicy()

    def reward(state: MemoryState, action: MemoryAction) -> float:
        desired = MemoryAction.STORE if state.task.startswith("store") else MemoryAction.RETRIEVE
        return 1.0 if action == desired else -1.0

    report = GroupRelativeTrainer(seed=12).train(policy, states, reward, epochs=60)
    assert report.final_expected_reward > report.initial_expected_reward + 0.4
    assert policy.choose(states[0]) == MemoryAction.STORE
    assert policy.choose(states[1]) == MemoryAction.RETRIEVE
    path = tmp_path / "policy.json"
    policy.save(path)
    assert MemoryPolicy.load(path).probabilities(states[0]) == policy.probabilities(states[0])


def test_policy_masks_actions_without_target_memory():
    probabilities = MemoryPolicy().probabilities(_state("delete a memory"))
    assert MemoryAction.DELETE not in probabilities
    assert MemoryAction.UPDATE not in probabilities
    assert sum(probabilities.values()) == pytest.approx(1.0)


def test_group_relative_policy_handles_underflowed_action_probability():
    state = _state("retrieve fact")
    policy = MemoryPolicy()
    policy.weights[MemoryAction.STORE][0] = -1000.0

    def reward(_state: MemoryState, action: MemoryAction) -> float:
        return 1.0 if action == MemoryAction.RETRIEVE else -1.0

    report = GroupRelativeTrainer(seed=12).train(policy, [state], reward, epochs=2)
    assert report.final_expected_reward >= report.initial_expected_reward
    assert sum(policy.probabilities(state).values()) == pytest.approx(1.0)


def test_scored_rollout_training_writes_policy_and_holdout_report(tmp_path):
    data_path = tmp_path / "rollouts.jsonl"
    rows = []
    for task in ("store fact", "retrieve fact", "store preference", "retrieve preference"):
        desired = "store" if task.startswith("store") else "retrieve"
        rows.append(
            {
                "state": {
                    "task": task,
                    "conversation": [],
                    "budget_remaining": 4000,
                    "current_memory_count": 0,
                },
                "outcomes": {
                    action: {
                        "relevance": 1.0 if action == desired else 0.0,
                        "task_success": action == desired,
                        "persona_consistency": 1.0,
                    }
                    for action in ("store", "retrieve", "noop")
                },
            }
        )
    data_path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    assert len(load_scored_states(data_path)) == 4
    output_path = tmp_path / "trained.json"
    report = train_from_file(data_path, output_path, epochs=30, seed=4, holdout_fraction=0.25)
    assert (
        report["training"]["final_expected_reward"] > report["training"]["initial_expected_reward"]
    )
    assert report["holdout_examples"] == 1
    assert MemoryPolicy.load(output_path)
    rows[0]["outcomes"].pop("noop")
    data_path.write_text(json.dumps(rows[0]), encoding="utf-8")
    with pytest.raises(ValueError, match="missing outcomes"):
        load_scored_states(data_path)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"outcomes": []}, "outcomes must be an object"),
        ({"outcomes": {"store": {"relevance": "high"}}}, "relevance must be a finite number"),
    ],
)
def test_scored_rollout_rejects_malformed_outcomes_with_row_number(tmp_path, change, message):
    row = {
        "state": {
            "task": "store fact",
            "conversation": [],
            "budget_remaining": 4000,
        },
        "outcomes": {
            action: {"task_success": True, "persona_consistency": 1.0}
            for action in ("store", "retrieve", "noop")
        },
    }
    if isinstance(change["outcomes"], dict):
        row["outcomes"]["store"].update(change["outcomes"]["store"])
    else:
        row.update(change)
    path = tmp_path / "invalid.jsonl"
    path.write_text(json.dumps(row), encoding="utf-8")
    with pytest.raises(ValueError, match=f"invalid training row 1: {message}"):
        load_scored_states(path)


def test_locomo_evidence_metrics_and_invalid_ids(tmp_path):
    path = tmp_path / "locomo.json"
    path.write_text(
        json.dumps(
            [
                {
                    "conversation": {
                        "speaker_a": "Ada",
                        "session_1": [
                            {"dia_id": "D1:1", "speaker": "Ada", "text": "I moved to Toronto."},
                            {"dia_id": "D1:2", "speaker": "Ben", "text": "That is great."},
                        ],
                    },
                    "qa": [
                        {
                            "question": "What happened in Toronto?",
                            "answer": "Toronto",
                            "evidence": ["D1:1"],
                            "category": 1,
                        },
                        {"question": "adversarial", "evidence": [], "category": 5},
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    dataset = load_locomo(path)
    metrics = evaluate_retrieval(dataset, k=1)
    assert metrics.questions == 1
    assert metrics.evidence_recall == metrics.hit_rate == metrics.mean_reciprocal_rank == 1
    assert metrics.mean_retrieved_bytes == len(b"I moved to Toronto.")
    with pytest.raises(ValueError, match="unknown dialogue"):
        evaluate_retrieval(dataset, lambda question, turns, k: ["missing"])


async def test_archive_backend_scores_dense_and_cleans_up(db_pool, monkeypatch):
    monkeypatch.setattr("machina.core.context.db", db_pool)
    monkeypatch.setattr("machina.core.session.db", db_pool)

    # Deterministic fake embeddings (1536-dim storage space) keep this test
    # hermetic while exercising the real archive indexing + dense SQL path.
    async def fake_embed_texts(texts):
        return [[0.1] * 1536 for _ in texts]

    async def fake_embed_query(text):
        return [0.1] * 1536

    monkeypatch.setattr("machina.memory.embeddings.embed_texts", fake_embed_texts)
    monkeypatch.setattr("machina.memory.embeddings.embed_query", fake_embed_query)
    dataset = [
        Conversation(
            turns=(DialogueTurn("D1:1", "Ada", "The telescope observed a nebula"),),
            questions=(Question("Which telescope observed a nebula?", ("D1:1",)),),
        )
    ]
    before = await db_pool.fetchval("SELECT COUNT(*) FROM context_archive")
    metrics = await evaluate_archive_retrieval(dataset, k=1)
    assert metrics.hit_rate == 1
    assert metrics.search_mode == "hybrid x1"
    # Dense and keyword component paths are both measured, not inferred.
    assert metrics.components["dense"]["questions"] == 1
    assert metrics.components["keyword-only"]["questions"] == 1
    assert metrics.components["hybrid"]["hit_rate"] == 1
    assert await db_pool.fetchval("SELECT COUNT(*) FROM context_archive") == before


async def test_archive_backend_falls_back_to_keyword_without_embedder(db_pool, monkeypatch):
    monkeypatch.setattr("machina.core.context.db", db_pool)
    monkeypatch.setattr("machina.core.session.db", db_pool)

    async def no_embed_texts(texts):
        return None

    async def no_embed_query(text):
        return None

    monkeypatch.setattr("machina.memory.embeddings.embed_texts", no_embed_texts)
    monkeypatch.setattr("machina.memory.embeddings.embed_query", no_embed_query)
    dataset = [
        Conversation(
            turns=(DialogueTurn("D1:1", "Ada", "The telescope observed a nebula"),),
            questions=(Question("Which telescope observed a nebula?", ("D1:1",)),),
        )
    ]
    before = await db_pool.fetchval("SELECT COUNT(*) FROM context_archive")
    metrics = await evaluate_archive_retrieval(dataset, k=1)
    assert metrics.hit_rate == 1
    assert metrics.search_mode == "keyword-only x1"
    assert await db_pool.fetchval("SELECT COUNT(*) FROM context_archive") == before


async def test_session_recall_backend_scores_across_sessions_and_cleans_up(db_pool, monkeypatch):
    monkeypatch.setattr("machina.core.context.db", db_pool)
    monkeypatch.setattr("machina.core.session.db", db_pool)
    monkeypatch.setattr("machina.core.session_recall.db", db_pool)
    dataset = [
        Conversation(
            turns=(
                DialogueTurn("D1:1", "Ada", "The telescope observed a nebula"),
                DialogueTurn("D1:2", "Ben", "The orchid blooms at sunrise"),
            ),
            questions=(Question("Which telescope observed a nebula?", ("D1:1",)),),
        )
    ]
    before = await db_pool.fetchval("SELECT COUNT(*) FROM context_archive")
    metrics, latency_ms = await evaluate_session_recall(dataset, k=1, turns_per_session=1)
    assert metrics.hit_rate == 1
    assert latency_ms >= 0
    assert await db_pool.fetchval("SELECT COUNT(*) FROM context_archive") == before


async def test_session_recall_title_only_backend_scores_matched_session(db_pool, monkeypatch):
    """Title-only hits map to their session's evidence, not to a chunk id.

    Regression for F11: title hits carry ``chunk_id=None``, so the old ranking
    (built from the chunk→dialogue table) was always empty. With one turn per
    session the production-parity title is the evidence itself, so a title-only
    search must now surface it (previously a structural zero).
    """
    monkeypatch.setattr("machina.core.context.db", db_pool)
    monkeypatch.setattr("machina.core.session.db", db_pool)
    monkeypatch.setattr("machina.core.session_recall.db", db_pool)
    dataset = [
        Conversation(
            turns=(
                DialogueTurn("D1:1", "Ada", "The telescope observed a nebula"),
                DialogueTurn("D1:2", "Ben", "The orchid blooms at sunrise"),
            ),
            questions=(Question("Which telescope observed a nebula?", ("D1:1",)),),
        )
    ]
    metrics, latency_ms = await evaluate_session_recall(
        dataset, k=1, turns_per_session=1, title_only=True
    )
    assert metrics.hit_rate == 1
    assert latency_ms >= 0


async def test_longitudinal_identity_transition_quarantines_causal_memory(db_pool, monkeypatch):
    monkeypatch.setattr("machina.memory.identity.db", db_pool)
    gate = IdentityGate()
    agent = f"identity-sequence-{uuid.uuid4()}"
    baseline = AgentBelief(agent, {"reliability": 1.0}, {}, {})
    await gate.save_beliefs(baseline)
    memory_id = await db_pool.fetchval(
        "INSERT INTO memories (agent_id, content, category) "
        "VALUES ($1, 'shared claim', 'fact') RETURNING id",
        agent,
    )
    try:
        bad = AgentBelief(agent, {"reliability": 0.0}, {}, {})
        transition = await gate.observe_transition(bad, causal_memory_ids=[memory_id])
        assert not transition.accepted
        assert transition.drift_score == 1.0
        assert transition.quarantined_count == 1
        assert (await gate._load_beliefs(agent)).known_facts["reliability"] == 1.0
        assert await db_pool.fetchval("SELECT quarantined FROM memories WHERE id = $1", memory_id)
        good = AgentBelief(agent, {"reliability": 0.9}, {}, {})
        accepted = await gate.observe_transition(good)
        assert accepted.accepted
        assert accepted.to_version == 2
        history = await db_pool.fetch(
            "SELECT contained, from_version, to_version FROM agent_belief_history "
            "WHERE agent_id = $1 ORDER BY created_at",
            agent,
        )
        assert [(r["contained"], r["from_version"], r["to_version"]) for r in history] == [
            (True, 1, 1),
            (False, 1, 2),
        ]
    finally:
        await db_pool.execute("DELETE FROM agent_belief_history WHERE agent_id = $1", agent)
        await db_pool.execute("DELETE FROM memories WHERE agent_id = $1", agent)
        await db_pool.execute("DELETE FROM agent_beliefs WHERE agent_id = $1", agent)


async def test_labeled_identity_sequence_evaluation_cleans_up(db_pool, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr("machina.research.identity_eval.db", db_pool)
    monkeypatch.setattr("machina.memory.identity.db", db_pool)
    dataset = Path(__file__).resolve().parents[1] / "research/identity_scenarios_example.json"
    before = await db_pool.fetchval("SELECT COUNT(*) FROM agent_belief_history")
    result = await evaluate_identity_scenarios(load_identity_scenarios(dataset))
    assert result.transitions == 3
    assert result.attack_rejection_rate == 1
    assert result.benign_acceptance_rate == 1
    assert result.quarantined_memories == 1
    assert await db_pool.fetchval("SELECT COUNT(*) FROM agent_belief_history") == before


def test_identity_evaluator_requires_explicit_database_selection(monkeypatch):
    monkeypatch.setenv("MACHINA_TEST_DATABASE_URL", "postgresql://test/benchmark")
    monkeypatch.setattr("sys.argv", ["identity_eval", "scenarios.json"])

    def unexpected_run(coro):
        coro.close()
        pytest.fail("benchmark accessed a database without an explicit selection")

    monkeypatch.setattr("machina.research.identity_eval.asyncio.run", unexpected_run)
    with pytest.raises(SystemExit) as exc:
        identity_main()
    assert exc.value.code == 2


def test_soulspec_v05_package_preserves_manifest_and_declared_files(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    manifest = {
        "specVersion": "0.5",
        "name": "research-helper",
        "displayName": "Research Helper",
        "version": "1.2.3",
        "description": "Finds sources",
        "author": {"name": "Ada", "github": "ada"},
        "license": "Apache-2.0",
        "tags": ["research"],
        "category": "work/research",
        "files": {"soul": "SOUL.md", "identity": "IDENTITY.md"},
        "examples": {"good": "examples/good.md"},
        "compatibility": {"frameworks": ["openclaw"], "minTokenContext": 4096},
        "allowedTools": ["browser"],
        "recommendedSkills": [{"name": "search", "required": True}],
        "disclosure": {"summary": "Find and verify sources"},
        "environment": "virtual",
        "customMetadata": {"preserve": True},
    }
    (source / "soul.json").write_text(json.dumps(manifest), encoding="utf-8")
    (source / "SOUL.md").write_text("# Soul\nBe careful.\n", encoding="utf-8")
    (source / "IDENTITY.md").write_bytes(b"Identity\r\n")
    (source / "examples").mkdir()
    (source / "examples/good.md").write_bytes(b"Example\r\n")
    # ``SoulSpec.from_package`` is the real validation seam (R4-106): it raises
    # on an invalid manifest, which the very next call exercises.
    spec = SoulSpec.from_package(source)
    destination = tmp_path / "destination"
    spec.write_package(destination)
    exported = json.loads((destination / "soul.json").read_text(encoding="utf-8"))
    extension = exported.pop("x-machina")
    assert exported == manifest
    assert extension["soulSpec"] == spec.to_dict()
    assert SoulSpec.from_package(destination).to_dict() == spec.to_dict()
    assert "Be careful." in (destination / "SOUL.md").read_text()
    assert (destination / "IDENTITY.md").read_bytes() == b"Identity\r\n"
    assert (destination / "examples/good.md").read_bytes() == b"Example\r\n"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "Bad Name"),
        ("version", "one"),
        ("license", "GPL-3.0"),
        ("allowedTools", "browser"),
        ("recommendedSkills", [{"name": "search", "required": "yes"}]),
        ("environment", "ocean"),
    ],
)
def test_soulspec_rejects_invalid_manifest_fields(tmp_path, field, value):
    spec = SoulSpec(
        name="valid-name",
        persona=SoulSpec.Persona(name="Valid", description="Valid description", system_prompt="Go"),
    )
    spec.write_package(tmp_path, author="Ada")
    path = tmp_path / "soul.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest[field] = value
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        SoulSpec.from_package(tmp_path)


def test_soulspec_rejects_optional_file_escape(tmp_path):
    spec = SoulSpec(
        name="valid-name",
        persona=SoulSpec.Persona(name="Valid", description="Valid description", system_prompt="Go"),
    )
    spec.write_package(tmp_path, author="Ada")
    path = tmp_path / "soul.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"]["identity"] = "../private.txt"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        SoulSpec.from_package(tmp_path)
