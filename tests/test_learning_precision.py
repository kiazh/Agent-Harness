"""Post-turn skill-learning proposals are measured for precision, not just staged.

The reviewer in ``machina.skills.learning`` auto-publishes validated proposals
(or stages them when auto-apply is off/capped/suppressed) into
``learning_reviews`` with one of seven statuses. This module measures how much
of what it proposes is actually useful:

* ``acceptance_rate`` — of the proposals *made*, what fraction were accepted.
* ``precision``       — of the proposals a human *decided* on, the accepted share.
* ``recall_proxy``    — of review attempts, the share that yielded a proposal.
* ``usage_rate``      — of accepted proposals, the share later used (relevance).

Auto-apply health is the same data viewed from the publish side: of the
reviews the reviewer published itself, the share later reverted or rejected.
Crossing ``AUTO_APPLY_SUPPRESS_MIN_SAMPLE`` with a rate above
``AUTO_APPLY_SUPPRESS_BAD_RATE`` flips the reviewer to stage-only.

Zero-denominator conventions: every ratio is ``None`` (not ``0.0``) when its
denominator is empty, so "no data" is never confused with "zero precision".
"""

from __future__ import annotations

import json
import uuid

import pytest

from machina.core.models import LLMResponse
from machina.core.session import SessionManager
from machina.skills.learning import (
    AUTO_APPLY_SUPPRESS_MIN_SAMPLE,
    LearningReviewer,
    compute_accepted_usage_rate,
    compute_auto_apply_revert_rate,
    compute_auto_apply_suppressed,
    compute_learning_precision,
)
from machina.skills.registry import SkillRegistry

# ─── Pure metric over statuses (no DB) ─────────────────────────────────────


def test_all_accepted_is_full_precision_and_acceptance():
    metrics = compute_learning_precision(["approved", "approved", "approved"])
    assert metrics.proposals == 3
    assert metrics.accepted == 3
    assert metrics.acceptance_rate == pytest.approx(1.0)
    assert metrics.precision == pytest.approx(1.0)
    assert metrics.recall_proxy == pytest.approx(1.0)


def test_all_rejected_is_zero_precision_and_acceptance():
    metrics = compute_learning_precision(["rejected", "rejected"])
    assert metrics.proposals == 2
    assert metrics.accepted == 0
    assert metrics.acceptance_rate == pytest.approx(0.0)
    assert metrics.precision == pytest.approx(0.0)


def test_mixed_proposals_split_acceptance_precision_and_recall():
    metrics = compute_learning_precision(
        ["approved", "approved", "rejected", "pending", "none", "error"]
    )
    assert metrics.proposals == 4  # approved + rejected + pending
    assert metrics.accepted == 2
    assert metrics.rejected == 1
    assert metrics.pending == 1
    assert metrics.no_skill == 1
    assert metrics.errors == 1
    assert metrics.review_attempts == 6  # every terminal outcome, incl. none/error
    # of proposals made -> accepted share
    assert metrics.acceptance_rate == pytest.approx(2 / 4)
    # of decided proposals -> accepted share (pending excluded)
    assert metrics.precision == pytest.approx(2 / 3)
    # of review attempts -> proposals yielded
    assert metrics.recall_proxy == pytest.approx(4 / 6)


def test_in_flight_reviews_are_not_counted_as_attempts_or_proposals():
    metrics = compute_learning_precision(["approved", "reviewing"])
    assert metrics.proposals == 1
    assert metrics.review_attempts == 1
    assert metrics.acceptance_rate == pytest.approx(1.0)
    assert metrics.recall_proxy == pytest.approx(1.0)


def test_zero_proposals_returns_none_ratios_without_dividing_by_zero():
    metrics = compute_learning_precision(["none", "none", "error"])
    assert metrics.proposals == 0
    assert metrics.acceptance_rate is None
    assert metrics.precision is None
    # Attempts happened, but none yielded a proposal.
    assert metrics.recall_proxy == pytest.approx(0.0)


def test_empty_input_returns_all_none_ratios():
    metrics = compute_learning_precision([])
    assert metrics.review_attempts == 0
    assert metrics.proposals == 0
    assert metrics.acceptance_rate is None
    assert metrics.precision is None
    assert metrics.recall_proxy is None


def test_usage_rate_over_accepted_skills():
    assert compute_accepted_usage_rate(["a", "b", "c"], {"a": 3, "b": 0, "c": 1}) == pytest.approx(
        2 / 3
    )
    assert compute_accepted_usage_rate(["a"], {"a": 0}) == pytest.approx(0.0)
    # No accepted skills -> undefined, not zero.
    assert compute_accepted_usage_rate([], {}) is None
    assert compute_accepted_usage_rate([None, ""], {}) is None


# ─── Auto-apply health: revert rate and the bounded auto-disable ───────────


def test_auto_apply_revert_rate_counts_only_bad_outcomes():
    assert compute_auto_apply_revert_rate(["approved", "approved"]) == pytest.approx(0.0)
    assert compute_auto_apply_revert_rate(
        ["reverted", "rejected", "approved", "approved"]
    ) == pytest.approx(0.5)
    assert compute_auto_apply_revert_rate(["reverted"]) == pytest.approx(1.0)
    # Nothing auto-applied -> undefined, not zero (zero-denominator convention).
    assert compute_auto_apply_revert_rate([]) is None


def test_auto_apply_suppression_needs_sample_and_rate():
    # Below the sample floor even a fully bad history stays on (no signal).
    below = ["reverted"] * (AUTO_APPLY_SUPPRESS_MIN_SAMPLE - 1)
    assert compute_auto_apply_suppressed(below) is False
    # At the floor, exactly 30% bad is not "above" the bar.
    floor = ["reverted"] * 3 + ["approved"] * (AUTO_APPLY_SUPPRESS_MIN_SAMPLE - 3)
    assert compute_auto_apply_suppressed(floor) is False
    # Above the bar with enough sample suppresses.
    above = ["reverted"] * 4 + ["approved"] * (AUTO_APPLY_SUPPRESS_MIN_SAMPLE - 4)
    assert compute_auto_apply_suppressed(above) is True
    # Nothing auto-applied -> not suppressed.
    assert compute_auto_apply_suppressed([]) is False


class ReviewProvider:
    model = "test-reviewer"

    def __init__(self, content: str) -> None:
        self.content = content

    async def complete(self, **kwargs):
        return LLMResponse(content=self.content, model=self.model, usage={"total_tokens": 30})


def _review_candidate(name: str) -> str:
    return json.dumps(
        {
            "name": name,
            "description": "Auto-apply health test skill",
            "triggers": ["autoapply"],
            "content": "Check health and readiness before declaring the task complete.",
        }
    )


# ─── Integration: reads real proposal records from the database ────────────


async def test_precision_reads_real_proposal_records_from_db(db_pool, monkeypatch, tmp_path):
    monkeypatch.setattr("machina.skills.learning.db", db_pool)
    monkeypatch.setattr("machina.core.session.db", db_pool)
    agent_id = f"precision-{uuid.uuid4()}"
    session = await SessionManager().create(title="precision", agent_id=agent_id)
    reviewer = LearningReviewer(registry=SkillRegistry(tmp_path / "skills"))
    statuses = ["approved", "approved", "rejected", "pending", "none", "error"]
    try:
        for index, status in enumerate(statuses):
            await db_pool.execute(
                "INSERT INTO learning_reviews (session_id, agent_id, turn_hash, status) "
                "VALUES ($1, $2, $3, $4)",
                session.id,
                agent_id,
                f"hash-{index}",
                status,
            )
        report = await reviewer.precision(agent_id)
        assert report["proposals"] == 4
        assert report["accepted"] == 2
        assert report["review_attempts"] == 6
        assert report["acceptance_rate"] == pytest.approx(0.5)
        assert report["precision"] == pytest.approx(2 / 3)
        assert report["recall_proxy"] == pytest.approx(4 / 6)
        # An unrelated agent sees no proposals, not zero precision.
        empty = await reviewer.precision(f"precision-{uuid.uuid4()}")
        assert empty["proposals"] == 0
        assert empty["precision"] is None
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


async def test_precision_reports_usage_rate_of_accepted_skills(db_pool, monkeypatch, tmp_path):
    monkeypatch.setattr("machina.skills.learning.db", db_pool)
    monkeypatch.setattr("machina.core.session.db", db_pool)
    agent_id = f"precision-{uuid.uuid4()}"
    session = await SessionManager().create(title="precision usage", agent_id=agent_id)
    registry = SkillRegistry(tmp_path / "skills")
    registry.create_skill("deploy-checks", "Checks", "Check health before success.")
    registry.record_use("deploy-checks")
    reviewer = LearningReviewer(registry=registry)
    try:
        await db_pool.execute(
            "INSERT INTO learning_reviews (session_id, agent_id, turn_hash, status, name) "
            "VALUES ($1, $2, $3, 'approved', $4)",
            session.id,
            agent_id,
            "used-hash",
            "deploy-checks",
        )
        await db_pool.execute(
            "INSERT INTO learning_reviews (session_id, agent_id, turn_hash, status, name) "
            "VALUES ($1, $2, $3, 'approved', $4)",
            session.id,
            agent_id,
            "unused-hash",
            "never-used",
        )
        report = await reviewer.precision(agent_id)
        assert report["accepted"] == 2
        assert report["accepted_with_usage"] == 1
        assert report["usage_rate"] == pytest.approx(0.5)
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


# ─── Auto-disable: the precision check flips the reviewer to stage-only ────


async def test_auto_apply_health_and_suppression_flip_to_stage_only(db_pool, monkeypatch, tmp_path):
    monkeypatch.setattr("machina.skills.learning.db", db_pool)
    monkeypatch.setattr("machina.core.session.db", db_pool)
    agent_id = f"precision-{uuid.uuid4()}"
    # Seeds live in their own session so the review session's per-session cap
    # is not consumed by the auto-apply health fixture.
    seed_session = await SessionManager().create(title="auto disable seeds", agent_id=agent_id)
    session = await SessionManager().create(title="auto disable", agent_id=agent_id)
    registry = SkillRegistry(tmp_path / "skills")
    reviewer = LearningReviewer(registry=registry)
    audits: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "machina.core.provider.audit.audit_log",
        lambda event, **kwargs: audits.append((event, kwargs)),
    )
    provider = ReviewProvider(_review_candidate("suppressed-skill"))
    try:
        for index in range(AUTO_APPLY_SUPPRESS_MIN_SAMPLE):
            await db_pool.execute(
                "INSERT INTO learning_reviews (session_id, agent_id, turn_hash, status, auto_applied) "
                "VALUES ($1, $2, $3, $4, TRUE)",
                seed_session.id,
                agent_id,
                f"seed-{index}",
                "reverted" if index < 4 else "approved",
            )
        health = await reviewer.auto_apply_health(agent_id)
        assert health["auto_applied"] == AUTO_APPLY_SUPPRESS_MIN_SAMPLE
        assert health["reverted_or_rejected"] == 4
        assert health["revert_rate"] == pytest.approx(0.4)
        assert health["suppressed"] is True

        proposal = await reviewer.review_turn(
            session.id,
            agent_id,
            "Document the deployment workflow and steps",
            "First do health checks then inspect logs and verify readiness.",
            provider,
        )
        assert proposal is not None and proposal["status"] == "pending"
        assert proposal["autoApplied"] is False
        assert registry.get("suppressed-skill") is None
        assert [event for event, _ in audits] == ["learning_auto_apply_suppressed"]

        # Opportunistic re-check: the flip persists and the audit stays one-time.
        second = await reviewer.review_turn(
            session.id,
            agent_id,
            "Document the second workflow and steps",
            "First review readiness then publish and verify the deployment.",
            provider,
        )
        assert second is not None and second["status"] == "pending"
        assert len(audits) == 1

        report = await reviewer.precision(agent_id)
        assert report["auto_applied"] == AUTO_APPLY_SUPPRESS_MIN_SAMPLE
        assert report["auto_apply_revert_rate"] == pytest.approx(0.4)
        assert report["auto_apply_suppressed"] is True
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", seed_session.id)
