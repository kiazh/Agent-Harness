"""Bounded post-turn skill review with self-publishing learned skills.

Validated proposals publish themselves as enabled learned skills (no standing
human approval gate) inside bounded caps; staged ``pending`` proposals remain
the fallback when auto-apply is off, capped, suppressed, or failed. A post-hoc
:meth:`LearningReviewer.revert` is the correction path, and a precision
auto-disable returns the reviewer to stage-only when auto-applied proposals
turn bad.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from machina.core.config import config
from machina.core.usage import usage_store
from machina.db.connection import db
from machina.memory.redaction import redact_secrets
from machina.observability.diagnostics import record_failure
from machina.skills.registry import Skill, SkillParser, SkillRegistry, skill_registry

logger = logging.getLogger(__name__)


def _audit(event_type: str, **kwargs: Any) -> None:
    """Emit one audit event, binding the core module lazily.

    The deferred import keeps this module's module-level ``machina.core``
    import count flat (architecture ratchet); the binding is resolved per call
    so tests can monkeypatch ``machina.core.provider.audit.audit_log``.
    """
    from machina.core.provider.audit import audit_log

    audit_log(event_type, **kwargs)


LEASE_SECONDS = 60  # A 'reviewing' row older than this is orphaned and re-claimable.

_SKILL_NAME = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
_TRIGGER = re.compile(
    r"\b(document|repeatable|workflow|procedure|runbook|remember how|automate|standardize|template|checklist|guideline|best practice|process|steps to|how to)\b",
    re.I,
)

# Precision auto-disable: once an agent has at least this many auto-applied
# reviews, a reverted/rejected share above the rate flips its reviewer back to
# stage-only. Evaluated opportunistically at review time (no background loop)
# from the durable review rows; the audit event fires once per suppression
# episode while the share stays above the threshold.
AUTO_APPLY_SUPPRESS_MIN_SAMPLE = 10
AUTO_APPLY_SUPPRESS_BAD_RATE = 0.30
_AUTO_APPLY_BAD_STATUSES = frozenset({"reverted", "rejected"})
# Process-level memo so the suppression audit fires once per episode, not on
# every review; cleared again when health recovers.
_auto_apply_suppressed_agents: set[str] = set()

# ``learning_reviews.status`` values that represent a proposal an agent made and
# a human could act on. ``reviewing`` is in-flight and never counted.
_PROPOSAL_STATUSES = frozenset({"pending", "approved", "rejected"})
_DECIDED_STATUSES = frozenset({"approved", "rejected"})
# Terminal non-proposal outcomes: the reviewer ran but staged nothing.
_NO_SKILL_STATUSES = frozenset({"none"})
_ERROR_STATUSES = frozenset({"error"})
_IN_FLIGHT_STATUSES = frozenset({"reviewing"})


@dataclass(frozen=True)
class LearningPrecisionMetrics:
    """Precision of post-turn skill proposals over a set of review statuses.

    Ratios use ``None`` — never ``0.0`` — when their denominator is empty, so
    "no proposals yet" cannot be mistaken for "proposals that were all wrong".
    """

    review_attempts: int
    proposals: int
    accepted: int
    rejected: int
    pending: int
    no_skill: int
    errors: int
    acceptance_rate: float | None
    precision: float | None
    recall_proxy: float | None
    accepted_with_usage: int = 0
    usage_rate: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_learning_precision(statuses: list[str]) -> LearningPrecisionMetrics:
    """Compute proposal precision from raw ``learning_reviews`` statuses.

    Definitions:
      * ``proposals``      — reviews that staged a skill (pending/approved/rejected).
      * ``acceptance_rate``— accepted / proposals; how much of what the agent
        proposed a human actually approved.
      * ``precision``      — accepted / decided (pending excluded); of the
        proposals a human ruled on, the accepted share. This is the headline
        metric — noise shows up as a low value.
      * ``recall_proxy``   — proposals / review_attempts; of the turns the
        reviewer spent budget on, the share that yielded any proposal.
    """
    counts: dict[str, int] = {}
    for status in statuses:
        counts[status] = counts.get(status, 0) + 1

    attempts = sum(count for status, count in counts.items() if status not in _IN_FLIGHT_STATUSES)
    proposals = sum(counts.get(status, 0) for status in _PROPOSAL_STATUSES)
    accepted = counts.get("approved", 0)
    rejected = counts.get("rejected", 0)
    pending = counts.get("pending", 0)
    decided = sum(counts.get(status, 0) for status in _DECIDED_STATUSES)
    no_skill = sum(counts.get(status, 0) for status in _NO_SKILL_STATUSES)
    errors = sum(counts.get(status, 0) for status in _ERROR_STATUSES)

    return LearningPrecisionMetrics(
        review_attempts=attempts,
        proposals=proposals,
        accepted=accepted,
        rejected=rejected,
        pending=pending,
        no_skill=no_skill,
        errors=errors,
        acceptance_rate=accepted / proposals if proposals else None,
        precision=accepted / decided if decided else None,
        recall_proxy=proposals / attempts if attempts else None,
    )


def compute_accepted_usage_rate(
    accepted_names: list[str | None], usage_by_name: dict[str, int]
) -> float | None:
    """Of the accepted skills, the share that have been used at least once.

    A skill that is approved but never triggered is latent noise, so this is the
    relevance proxy for precision. Returns ``None`` when nothing was accepted.
    """
    names = [name for name in accepted_names if name]
    if not names:
        return None
    used = sum(1 for name in names if usage_by_name.get(name, 0) > 0)
    return used / len(names)


def compute_auto_apply_revert_rate(statuses: list[str]) -> float | None:
    """Share of auto-applied reviews later reverted (or rejected after apply).

    The inputs are the statuses of ``auto_applied`` review rows only. ``None``
    — never ``0.0`` — when nothing was auto-applied, matching the
    zero-denominator convention of :func:`compute_learning_precision`.
    """
    if not statuses:
        return None
    bad = sum(1 for status in statuses if status in _AUTO_APPLY_BAD_STATUSES)
    return bad / len(statuses)


def compute_auto_apply_suppressed(statuses: list[str]) -> bool:
    """Whether auto-applied review *statuses* exceed the suppression bar.

    Both bounds must hold: at least ``AUTO_APPLY_SUPPRESS_MIN_SAMPLE``
    auto-applied reviews (small samples carry no signal) and a revert rate
    strictly above ``AUTO_APPLY_SUPPRESS_BAD_RATE``.
    """
    rate = compute_auto_apply_revert_rate(statuses)
    return (
        len(statuses) >= AUTO_APPLY_SUPPRESS_MIN_SAMPLE
        and rate is not None
        and rate > AUTO_APPLY_SUPPRESS_BAD_RATE
    )


class LearningReviewer:
    """Review a completed turn once and apply or stage a safe, read-before-write skill."""

    def __init__(self, registry: SkillRegistry | None = None) -> None:
        self.registry = registry or skill_registry

    @staticmethod
    def _serialize(row: Any) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "sessionId": str(row["session_id"]),
            "agentId": row["agent_id"],
            "status": row["status"],
            "name": row["name"],
            "description": row["description"],
            "triggers": json.loads(row["triggers"])
            if isinstance(row["triggers"], str)
            else row["triggers"],
            "content": row["content"],
            "reason": row["reason"],
            # Additive: whether the reviewer published this skill itself.
            # ``get`` keeps pre-upgrade / lightweight test doubles working.
            "autoApplied": bool(row.get("auto_applied", False)),
        }

    async def review_turn(
        self,
        session_id: uuid.UUID,
        agent_id: str,
        user_message: str,
        response: str,
        provider: Any,
        *,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """Spend at most one 512-token review on an explicit reusable workflow."""
        if not _TRIGGER.search(user_message) or len(response.split()) < 6:
            return None
        digest = hashlib.sha256((user_message + "\0" + response).encode()).hexdigest()
        max_reviews = max(0, int(config.get("learning_review_max_per_session")))
        if not max_reviews:
            return None
        # Serialize reviews for this session while deciding whether the cost
        # cap has room. Release the lock before the provider call.
        async with db.acquire() as conn:
            async with conn.transaction():
                owner = await conn.fetchval(
                    "SELECT agent_id FROM sessions WHERE id = $1 FOR UPDATE", session_id
                )
                if owner is None:
                    raise LookupError("session not found")
                if owner != agent_id:
                    raise PermissionError("session belongs to a different agent")
                existing = await conn.fetchrow(
                    "SELECT * FROM learning_reviews WHERE session_id = $1 AND turn_hash = $2 FOR UPDATE",
                    session_id,
                    digest,
                )
                if existing is not None:
                    if existing["status"] == "pending":
                        return self._serialize(existing)
                    if existing["status"] == "reviewing":
                        # Re-claim an orphaned 'reviewing' row (process died mid-call).
                        # FOR UPDATE SKIP LOCKED ensures only one concurrent caller wins.
                        claimed = await conn.fetchrow(
                            """UPDATE learning_reviews
                               SET lease_expires_at = now() + ($2 * interval '1 second')
                               WHERE id = (
                                   SELECT id FROM learning_reviews
                                   WHERE id = $1 AND status = 'reviewing'
                                     AND (lease_expires_at IS NULL OR lease_expires_at < now())
                                   FOR UPDATE SKIP LOCKED
                               )
                               RETURNING *""",
                            existing["id"],
                            LEASE_SECONDS,
                        )
                        if claimed is None:
                            # Another process claimed it first, or lease hasn't expired
                            return None
                        # Re-claimed — proceed with the LLM call below
                        row = claimed
                    else:
                        return None
                else:
                    count = await conn.fetchval(
                        "SELECT COUNT(*) FROM learning_reviews WHERE session_id = $1", session_id
                    )
                    if count >= max_reviews:
                        return None
                    row = await conn.fetchrow(
                        """INSERT INTO learning_reviews (session_id, agent_id, turn_hash, status, lease_expires_at)
                           VALUES ($1, $2, $3, 'reviewing', now() + ($4 * interval '1 second')) RETURNING *""",
                        session_id,
                        agent_id,
                        digest,
                        LEASE_SECONDS,
                    )

        # Redact before sending to the provider; bound both input and output.
        prompt = (
            "Suggest one reusable procedural skill from this completed turn, or return null. "
            "Return only JSON with name, description, triggers (array of short strings), "
            "and content (concrete steps). Do not include secrets, personal data, or "
            "instructions unrelated to the workflow. Do not invent actions not evidenced here.\n"
            f"User: {redact_secrets(user_message[:1200]).text}\n"
            f"Result: {redact_secrets(response[:1200]).text}\n"
        )
        if tool_calls:
            names = [str(call.get("tool", ""))[:60] for call in tool_calls[:8]]
            prompt += f"Tools used: {', '.join(names)}\n"
        try:
            completion = await asyncio.wait_for(
                usage_store.complete_call(
                    provider,
                    session_id,
                    agent_id,
                    [{"role": "user", "content": prompt}],
                    max_tokens=512,
                    temperature=0,
                ),
                timeout=20,
            )
            candidate = json.loads(completion.content)
            normalized = self._validate(candidate)
            if normalized is None:
                await db.execute(
                    "UPDATE learning_reviews SET status = 'none', reason = 'no valid novel skill', lease_expires_at = NULL WHERE id = $1",
                    row["id"],
                )
                return None
            name, description, triggers, content = normalized
            self.registry.load_all()
            if self.registry.get(name) is not None or self.registry.skill_file_exists(name):
                # CREATION ONLY: an existing name is dropped, never merged or
                # overwritten. Auto-apply must not edit existing skills.
                await db.execute(
                    "UPDATE learning_reviews SET status = 'none', reason = 'skill already exists', lease_expires_at = NULL WHERE id = $1",
                    row["id"],
                )
                return None
            if config.get("learning_review_auto_apply") and await self._auto_apply_allowed(
                agent_id
            ):
                applied = await self._auto_publish(row["id"], name, description, triggers, content)
                if applied is not None:
                    return self._serialize(applied)
                # The publish failed and its artifact was compensated; stage
                # the proposal for manual approval instead of losing it.
            staged = await db.fetchrow(
                """UPDATE learning_reviews SET status = 'pending', name = $2,
                          description = $3, triggers = $4::jsonb, content = $5,
                          lease_expires_at = NULL
                   WHERE id = $1 RETURNING *""",
                row["id"],
                name,
                description,
                json.dumps(triggers),
                content,
            )
            return self._serialize(staged)
        except Exception as exc:
            logger.warning(
                "Post-turn learning review failed for %s: %s", session_id, exc, exc_info=True
            )
            await db.execute(
                "UPDATE learning_reviews SET status = 'error', reason = $2, lease_expires_at = NULL WHERE id = $1",
                row["id"],
                type(exc).__name__,
            )
            return None

    async def auto_apply_health(self, agent_id: str) -> dict[str, Any]:
        """Auto-apply health for one agent, read from its durable review rows.

        ``auto_applied`` counts every review this agent auto-published;
        ``revert_rate`` is the reverted/rejected share of those (``None`` when
        nothing was auto-applied, per the module's zero-denominator
        convention); ``suppressed`` is the bounded auto-disable verdict.
        """
        rows = await db.fetch(
            "SELECT status FROM learning_reviews WHERE agent_id = $1 AND auto_applied = TRUE",
            agent_id,
        )
        statuses = [row["status"] for row in rows]
        return {
            "auto_applied": len(statuses),
            "reverted_or_rejected": sum(
                1 for status in statuses if status in _AUTO_APPLY_BAD_STATUSES
            ),
            "revert_rate": compute_auto_apply_revert_rate(statuses),
            "suppressed": compute_auto_apply_suppressed(statuses),
        }

    async def _auto_apply_allowed(self, agent_id: str) -> bool:
        """Whether this agent's next proposal may publish itself right now.

        Two bounded gates, both evaluated opportunistically at review time
        from the durable review rows: the rolling 24h per-agent cap (counting
        only auto-applied reviews) and the precision auto-disable (a
        reverted/rejected share above the bar falls back to stage-only).
        """
        health = await self.auto_apply_health(agent_id)
        if health["suppressed"]:
            if agent_id not in _auto_apply_suppressed_agents:
                # One audit per suppression episode, not per review.
                _auto_apply_suppressed_agents.add(agent_id)
                _audit(
                    "learning_auto_apply_suppressed",
                    agent_id=agent_id,
                    auto_applied=health["auto_applied"],
                    reverted_or_rejected=health["reverted_or_rejected"],
                    revert_rate=health["revert_rate"],
                    threshold=AUTO_APPLY_SUPPRESS_BAD_RATE,
                )
            return False
        _auto_apply_suppressed_agents.discard(agent_id)
        cap = max(0, int(config.get("learning_review_auto_apply_per_day")))
        if not cap:
            return False
        used = await db.fetchval(
            """SELECT COUNT(*) FROM learning_reviews
               WHERE agent_id = $1 AND auto_applied = TRUE
                 AND reviewed_at >= now() - interval '24 hours'""",
            agent_id,
        )
        return (used or 0) < cap

    async def _auto_publish(
        self,
        review_id: uuid.UUID,
        name: str,
        description: str,
        triggers: list[str],
        content: str,
    ) -> Any | None:
        """Publish a validated proposal as an enabled, source-tagged skill.

        Compensable like :meth:`approve`: the skill is written before the
        status commit, and any failure — a failed COMMIT included — removes
        the fresh artifact again, so a failed publish never leaves a live
        skill behind. Returns the approved row, or ``None`` after
        compensation so the caller can stage the proposal for manual
        approval instead of losing it.
        """
        source = f"learning-review:{review_id}"
        created = False
        applied = None
        try:
            async with db.acquire() as conn:
                async with conn.transaction():
                    self.registry.create_skill(
                        name,
                        description,
                        content,
                        triggers=triggers,
                        source=source,
                        source_type="learned",
                    )
                    created = True
                    applied = await conn.fetchrow(
                        "UPDATE learning_reviews SET status = 'approved', auto_applied = TRUE, "
                        "name = $2, description = $3, triggers = $4::jsonb, content = $5, "
                        "reviewed_at = now(), reason = 'auto-applied' WHERE id = $1 RETURNING *",
                        review_id,
                        name,
                        description,
                        json.dumps(triggers),
                        content,
                    )
        except BaseException as exc:
            # The review did not commit (or the skill write failed): drop this
            # review's artifact so it cannot outlive a non-approved review.
            if created:
                self._discard_artifact(name, source)
            if isinstance(exc, asyncio.CancelledError):
                raise
            record_failure("skills.learning.auto_apply", exc)
            return None
        try:
            _audit("learning_auto_applied", review_id=str(review_id), skill=name)
        except Exception as _audit_error:  # pragma: no cover - audit is best-effort
            record_failure("skills.learning.auto_apply.audit", _audit_error)
        return applied

    @staticmethod
    def _validate(candidate: Any) -> tuple[str, str, list[str], str] | None:
        if not isinstance(candidate, dict):
            return None
        name = candidate.get("name")
        description = candidate.get("description")
        content = candidate.get("content")
        triggers = candidate.get("triggers")
        if not isinstance(name, str) or not _SKILL_NAME.fullmatch(name):
            return None
        if not isinstance(description, str) or not 4 <= len(description) <= 300:
            return None
        if not isinstance(content, str) or not 20 <= len(content) <= 4000:
            return None
        if (
            not isinstance(triggers, list)
            or len(triggers) > 10
            or any(not isinstance(value, str) or not 2 <= len(value) <= 60 for value in triggers)
        ):
            return None
        content = redact_secrets(content).text
        description = redact_secrets(description).text
        triggers = [redact_secrets(value).text for value in triggers]
        try:
            SkillParser._validate_content(content)
            SkillParser._scan_injection(description)
            for _t in triggers:
                SkillParser._scan_injection(_t)
        except ValueError:
            return None
        return name, description, triggers, content

    async def list_reviews(self, agent_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = await db.fetch(
            "SELECT * FROM learning_reviews WHERE agent_id = $1 ORDER BY created_at DESC LIMIT $2",
            agent_id,
            min(max(limit, 1), 100),
        )
        return [self._serialize(row) for row in rows]

    async def precision(self, agent_id: str) -> dict[str, Any]:
        """Measure proposal precision for one agent from real review records.

        Reads every ``learning_reviews`` status for *agent_id* and folds them
        through the pure :func:`compute_learning_precision`, then adds the
        accepted-skill usage rate from registry telemetry and the auto-apply
        health used by the precision auto-disable. Never raises on an empty
        history: ratios come back as ``None`` per the documented convention.
        """
        rows = await db.fetch(
            "SELECT status, name, auto_applied FROM learning_reviews WHERE agent_id = $1",
            agent_id,
        )
        metrics = compute_learning_precision([row["status"] for row in rows])
        accepted_names = [row["name"] for row in rows if row["status"] == "approved"]
        auto_statuses = [row["status"] for row in rows if row["auto_applied"]]
        self.registry.load_all()
        usage_by_name = {skill.name: skill.usage_count for skill in self.registry.list_skills()}
        accepted_with_usage = sum(
            1 for name in accepted_names if name and usage_by_name.get(name, 0) > 0
        )
        report = metrics.to_dict()
        report["accepted_with_usage"] = accepted_with_usage
        report["usage_rate"] = compute_accepted_usage_rate(accepted_names, usage_by_name)
        report["auto_applied"] = len(auto_statuses)
        report["auto_apply_revert_rate"] = compute_auto_apply_revert_rate(auto_statuses)
        report["auto_apply_suppressed"] = compute_auto_apply_suppressed(auto_statuses)
        return report

    async def approve(self, review_id: uuid.UUID, *, agent_id: str) -> dict[str, Any]:
        """Approve a pending proposal and publish its skill exactly once.

        Publication is staged and compensable: the skill is written before the
        approval commit, and any failure or cancellation from the status write
        through the transaction exit (a failing COMMIT included) removes it
        again so a pending review never keeps a live skill. An artifact that
        provably belongs to this same review (matching source and staged
        payload) is adopted instead of refused, so an interrupted attempt can
        be completed by retrying approval.
        """
        source = f"learning-review:{review_id}"
        name: str | None = None
        try:
            async with db.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        "SELECT * FROM learning_reviews WHERE id = $1 FOR UPDATE", review_id
                    )
                    if row is None:
                        raise LookupError("learning proposal not found")
                    if row["agent_id"] != agent_id:
                        raise PermissionError("learning proposal belongs to a different agent")
                    if row["status"] != "pending":
                        return self._serialize(row)
                    triggers = (
                        json.loads(row["triggers"])
                        if isinstance(row["triggers"], str)
                        else (row["triggers"] or [])
                    )
                    name = row["name"]
                    self.registry.load_all()
                    staged = self._staged_artifact(row, triggers, source)
                    if staged is None:
                        self.registry.create_skill(
                            row["name"],
                            row["description"],
                            row["content"],
                            triggers=triggers,
                            source=source,
                            source_type="learned",
                        )
                    elif not staged.enabled:
                        # A crashed attempt left our artifact disabled; approval
                        # must publish it enabled.
                        self.registry.set_enabled(row["name"], True)
                    approved = await conn.fetchrow(
                        "UPDATE learning_reviews SET status = 'approved', reviewed_at = now() "
                        "WHERE id = $1 RETURNING *",
                        review_id,
                    )
                    return self._serialize(approved)
        except BaseException:
            # The review did not commit: the UPDATE, the transaction exit or
            # the COMMIT itself raised. Drop this review's artifact so it
            # cannot stay enabled against a pending review or block the retry
            # that re-publishes it.
            if name is not None:
                self._discard_artifact(name, source)
            raise

    def _staged_artifact(self, row: Any, triggers: Any, source: str) -> Skill | None:
        """Return this review's already-published artifact, or None if absent.

        Only an artifact whose provenance AND staged payload match the review
        row may be adopted; anything else conflicts with an unrelated skill
        and keeps the existing refusal.
        """
        name = row["name"]
        skill = self.registry.get(name)
        if skill is None:
            if self.registry.skill_file_exists(name):
                # The path exists but did not parse: not provably ours.
                raise ValueError("skill already exists; review a new proposal")
            return None
        if (
            skill.source != source
            or skill.description != row["description"]
            or skill.triggers != triggers
            or skill.content.strip() != str(row["content"]).strip()
        ):
            raise ValueError("skill already exists; review a new proposal")
        return skill

    def _discard_artifact(self, name: str, source: str) -> None:
        """Best-effort removal of this review's staged artifact after failure."""
        try:
            self.registry.load_all()
            skill = self.registry.get(name)
            if skill is None or skill.source != source:
                return
            self.registry.delete_skill(name)
        except Exception as exc:
            record_failure("skills.learning.approve_rollback", exc)

    async def revert(self, review_id: uuid.UUID, *, agent_id: str) -> dict[str, Any]:
        """Post-hoc review: undo an approved proposal and its published skill.

        Source-guarded: only the artifact whose ``source`` names this review is
        deleted; a foreign skill occupying the same name is merely disabled.
        The review row is then marked ``reverted``; any non-``approved`` status
        is an idempotent no-op. Ownership checks match :meth:`approve`, and the
        skill is acted on before the status commits so a failure leaves the
        skill off, never live against a reverted review.
        """
        source = f"learning-review:{review_id}"
        async with db.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM learning_reviews WHERE id = $1 FOR UPDATE", review_id
                )
                if row is None:
                    raise LookupError("learning proposal not found")
                if row["agent_id"] != agent_id:
                    raise PermissionError("learning proposal belongs to a different agent")
                if row["status"] != "approved":
                    return self._serialize(row)
                name = row["name"]
                removed = False
                if name:
                    self.registry.load_all()
                    skill = self.registry.get(name)
                    if skill is not None and skill.source == source:
                        removed = self.registry.delete_skill(name)
                    elif skill is not None:
                        self.registry.set_enabled(name, False)
                reverted = await conn.fetchrow(
                    "UPDATE learning_reviews SET status = 'reverted', reviewed_at = now() "
                    "WHERE id = $1 RETURNING *",
                    review_id,
                )
                _audit(
                    "learning_reverted",
                    review_id=str(review_id),
                    agent_id=agent_id,
                    skill=name,
                    removed=removed,
                )
                return self._serialize(reverted)

    async def reject(self, review_id: uuid.UUID, *, agent_id: str) -> dict[str, Any]:
        row = await db.fetchrow(
            """UPDATE learning_reviews SET status = 'rejected', reviewed_at = now()
               WHERE id = $1 AND agent_id = $2 AND status = 'pending' RETURNING *""",
            review_id,
            agent_id,
        )
        if row is None:
            current = await db.fetchrow("SELECT * FROM learning_reviews WHERE id = $1", review_id)
            if current is None:
                raise LookupError("learning proposal not found")
            if current["agent_id"] != agent_id:
                raise PermissionError("learning proposal belongs to a different agent")
            return self._serialize(current)
        return self._serialize(row)


learning_reviewer = LearningReviewer()
