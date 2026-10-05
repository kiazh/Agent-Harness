"""Post-turn skill-learning proposals are measured for precision, not just staged.

The reviewer in ``ah.skills.learning`` stages proposals into ``learning_reviews``
with one of six statuses. This module measures how much of what it proposes is
actually useful:

* ``acceptance_rate`` — of the proposals *made*, what fraction were accepted.
* ``precision``       — of the proposals a human *decided* on, the accepted share.
* ``recall_proxy``    — of review attempts, the share that yielded a proposal.
* ``usage_rate``      — of accepted proposals, the share later used (relevance).

Zero-denominator conventions: every ratio is ``None`` (not ``0.0``) when its
denominator is empty, so "no data" is never confused with "zero precision".
"""

from __future__ import annotations

import uuid

import pytest

from ah.core.session import SessionManager
from ah.skills.learning import (
    LearningReviewer,
    compute_accepted_usage_rate,
    compute_learning_precision,
)
from ah.skills.registry import SkillRegistry

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


# ─── Integration: reads real proposal records from the database ────────────


async def test_precision_reads_real_proposal_records_from_db(db_pool, monkeypatch, tmp_path):
    monkeypatch.setattr("ah.skills.learning.db", db_pool)
    monkeypatch.setattr("ah.core.session.db", db_pool)
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
    monkeypatch.setattr("ah.skills.learning.db", db_pool)
    monkeypatch.setattr("ah.core.session.db", db_pool)
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
