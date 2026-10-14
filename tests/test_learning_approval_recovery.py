"""R11: a failed skill approval must not publish a live skill or block retry.

2026-10-14 review evidence: with an injected UPDATE failure ``approve()``
left ``review-demo`` enabled while the review stayed ``pending``, and the
retry failed with ``skill already exists``.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

import pytest

from machina.skills.learning import LearningReviewer
from machina.skills.registry import SkillRegistry

CONTENT = "Check inputs. Run a focused verification. Record the result."


def _row(review_id: uuid.UUID, *, name: str = "review-demo") -> dict:
    return {
        "id": review_id,
        "agent_id": "harness",
        "session_id": uuid.uuid4(),
        "status": "pending",
        "name": name,
        "description": "Review demonstration",
        "content": CONTENT,
        "triggers": [],
        "reason": "",
    }


class ApprovalDB:
    """Minimal learning_reviews double: SELECT returns the row, UPDATE commits."""

    def __init__(self, row: dict, *, fail_updates: int = 0, raise_cancelled: bool = False):
        self.row = dict(row)
        self.fail_updates = fail_updates
        self.raise_cancelled = raise_cancelled
        self.commits = 0

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchrow(self, query, *args):
        if query.startswith("UPDATE"):
            if self.raise_cancelled:
                raise asyncio.CancelledError()
            if self.fail_updates:
                self.fail_updates -= 1
                raise OSError("synthetic DB status update failure")
            self.commits += 1
            self.row["status"] = "approved"
            return dict(self.row)
        return dict(self.row)


def _fresh_registry(tmp_path) -> SkillRegistry:
    registry = SkillRegistry(tmp_path / "skills")
    registry.load_all()
    return registry


async def test_failed_approval_leaves_no_live_skill_and_retry_completes(monkeypatch, tmp_path):
    review_id = uuid.uuid4()
    database = ApprovalDB(_row(review_id), fail_updates=1)
    monkeypatch.setattr("machina.skills.learning.db", database)
    reviewer = LearningReviewer(SkillRegistry(tmp_path / "skills"))

    with pytest.raises(OSError):
        await reviewer.approve(review_id, agent_id="harness")

    # The staged publication was compensated: a fresh registry discovers
    # nothing, and the review is still pending in the durable store.
    assert _fresh_registry(tmp_path).list_skills() == []
    assert database.row["status"] == "pending"

    approved = await reviewer.approve(review_id, agent_id="harness")
    assert approved["status"] == "approved"
    assert database.commits == 1
    skills = _fresh_registry(tmp_path).list_skills()
    assert [skill.name for skill in skills] == ["review-demo"]
    assert skills[0].source == f"learning-review:{review_id}"
    assert skills[0].enabled is True


async def test_cancelled_commit_removes_the_staged_skill(monkeypatch, tmp_path):
    review_id = uuid.uuid4()
    database = ApprovalDB(_row(review_id), raise_cancelled=True)
    monkeypatch.setattr("machina.skills.learning.db", database)
    reviewer = LearningReviewer(SkillRegistry(tmp_path / "skills"))

    with pytest.raises(asyncio.CancelledError):
        await reviewer.approve(review_id, agent_id="harness")

    assert _fresh_registry(tmp_path).list_skills() == []


async def test_retry_adopts_the_same_reviews_interrupted_artifact(monkeypatch, tmp_path):
    review_id = uuid.uuid4()
    row = _row(review_id)
    registry = SkillRegistry(tmp_path / "skills")
    # Simulate a crash between publication and commit: the exact reviewed
    # artifact exists, tagged with the owning review, review still pending.
    registry.create_skill(
        row["name"],
        row["description"],
        row["content"],
        triggers=row["triggers"],
        source=f"learning-review:{review_id}",
        source_type="learned",
    )
    monkeypatch.setattr("machina.skills.learning.db", ApprovalDB(row))

    approved = await LearningReviewer(registry).approve(review_id, agent_id="harness")

    assert approved["status"] == "approved"
    skills = _fresh_registry(tmp_path).list_skills()
    assert [skill.name for skill in skills] == ["review-demo"]
    assert skills[0].enabled is True
