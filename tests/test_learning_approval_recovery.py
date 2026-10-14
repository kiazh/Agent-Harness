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


class CommitFailingDB(ApprovalDB):
    """ApprovalDB whose transaction exit (COMMIT) fails.

    asyncpg surfaces a failed COMMIT on the transaction context manager's
    exit, after the UPDATE body already ran. The double applies the UPDATE
    immediately, so rollback is modeled by restoring the pre-transaction row
    and raising from ``__aexit__``.
    """

    def __init__(
        self,
        row: dict,
        *,
        fail_commits: int = 1,
        error_factory=lambda: OSError("synthetic DB commit failure"),
    ):
        super().__init__(row)
        self.fail_commits = fail_commits
        self.error_factory = error_factory

    @asynccontextmanager
    async def transaction(self):
        snapshot = (dict(self.row), self.commits)
        yield
        if self.fail_commits:
            self.fail_commits -= 1
            row, commits = snapshot
            self.row.clear()
            self.row.update(row)
            self.commits = commits
            raise self.error_factory()


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


async def test_commit_failure_does_not_keep_a_live_skill_and_retry_completes(monkeypatch, tmp_path):
    """Re-verification: compensation must also cover the transaction exit.

    A COMMIT that raises after the UPDATE is outside the old try boundary, so
    ``review-demo`` stayed enabled while the durable review was still pending.
    """
    review_id = uuid.uuid4()
    database = CommitFailingDB(_row(review_id), fail_commits=1)
    monkeypatch.setattr("machina.skills.learning.db", database)
    reviewer = LearningReviewer(SkillRegistry(tmp_path / "skills"))

    with pytest.raises(OSError, match="commit"):
        await reviewer.approve(review_id, agent_id="harness")

    # The commit never happened: the durable review is still pending, and no
    # enabled skill may outlive the failed approval.
    assert database.row["status"] == "pending"
    assert _fresh_registry(tmp_path).list_skills() == []

    approved = await reviewer.approve(review_id, agent_id="harness")
    assert approved["status"] == "approved"
    assert database.commits == 1
    skills = _fresh_registry(tmp_path).list_skills()
    assert [skill.name for skill in skills] == ["review-demo"]
    assert skills[0].source == f"learning-review:{review_id}"
    assert skills[0].enabled is True


async def test_cancelled_commit_exit_removes_the_staged_skill(monkeypatch, tmp_path):
    review_id = uuid.uuid4()
    database = CommitFailingDB(_row(review_id), error_factory=asyncio.CancelledError)
    monkeypatch.setattr("machina.skills.learning.db", database)
    reviewer = LearningReviewer(SkillRegistry(tmp_path / "skills"))

    with pytest.raises(asyncio.CancelledError):
        await reviewer.approve(review_id, agent_id="harness")

    assert database.row["status"] == "pending"
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
