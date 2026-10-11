"""Evidence-backed runtime adaptation, persona trials, and reversible revisions.

No model calls, executable code, permissions, or raw turn text live here.
Helpers run inside the growth store's per-agent transaction lock.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import defaultdict
from typing import Any

MAX_REVISIONS = 50
MAX_CAPABILITIES = 200
_NAME = re.compile(r"[a-zA-Z][a-zA-Z0-9_.-]{0,79}\Z")
STRATEGIES = {
    "direct": "Answer or act directly; avoid unnecessary tools and extra work.",
    "verify": "Inspect prerequisites, make a focused change, and verify observed results before reporting success.",
    "plan": "Break the requested task into concrete steps, carry one shared plan, and verify each result.",
}


def enabled() -> bool:
    from ah.core.config import config

    return bool(
        config.get("rsi_enabled")
        and config.get("personality_growth_enabled")
        and config.get("memory_enabled")
    )


def decode(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def task_domain(query: str) -> str:
    informational = re.match(
        r"\s*(?:(?:can|could|would)\s+you\s+)?(?:what|why|how|who|when|where|explain|describe|tell me)\b",
        query,
        re.I,
    )
    follow_on_action = re.search(
        r"(?:[.!?;,]\s*|\b(?:and|then)\s+)(?:please\s+)?(?:fix|build|implement|add|refactor|review|research|create|test)\b",
        query,
        re.I,
    )
    if informational and not follow_on_action:
        return "general"
    words = set(re.findall(r"[a-z]+", query.lower()))
    if words & {
        "code",
        "coding",
        "python",
        "typescript",
        "javascript",
        "bug",
        "api",
        "test",
        "tests",
        "implement",
        "refactor",
        "rust",
    }:
        return "coding"
    if words & {"research", "sources", "search", "investigate", "compare"}:
        return "research"
    if words & {"write", "writing", "draft", "essay", "story", "document"}:
        return "writing"
    return "general"


def _names(names) -> list[str]:
    from ah.memory.redaction import redact_secrets

    return list(
        dict.fromkeys(
            name
            for name in names
            if isinstance(name, str) and _NAME.fullmatch(name) and redact_secrets(name).text == name
        )
    )[:MAX_CAPABILITIES]


def summarize_outcome(response) -> dict:
    """Reduce actual execution records; response prose cannot grant success."""
    tools = []
    for entry in (response.tool_calls or [])[:MAX_CAPABILITIES]:
        name = entry.get("tool")
        if not _names([name]) or name in {"learn_personality", "remember"}:
            continue
        outcome = entry.get("outcome")
        # Compatibility for older tool records: conservative failure prefixes.
        preview = str(entry.get("result_preview", entry.get("result", "")))
        ok = (
            outcome == "completed"
            if outcome
            else bool(preview)
            and not preview.startswith(("Error:", "Permission denied:", "Approval required:"))
        )
        if entry.get("needs_approval") or entry.get("needs_approvals"):
            ok = False
        tools.append({"name": name, "ok": ok})
    content = response.content or ""
    failed = getattr(response, "outcome", "completed") == "failed" or content.startswith(
        ("Reached max", "Reached maximum", "LLM provider error")
    )
    return {
        "terminal": "failed" if failed else "completed",
        "tools": tools,
        "tokens": max(0, int(response.tokens_used)),
        "iterations": max(0, int(response.iterations)),
    }


def operational_reward(outcome: dict | None) -> float | None:
    if not outcome:
        return None
    if outcome.get("terminal") == "failed":
        return -0.5
    tools = outcome.get("tools", [])
    if any(not item["ok"] for item in tools):
        return -0.25
    return 0.2 if tools else None


def aggregate(rows) -> dict:
    """Human feedback replaces a proxy; each episode contributes at most once."""
    groups = {
        "skills": defaultdict(list),
        "tools": defaultdict(list),
        "strategies": defaultdict(list),
    }
    domains: dict[str, dict[str, list]] = {}
    observations = automatic = human = 0
    for row in rows:
        reward = row["reward"] if row["reward"] is not None else row["automatic_reward"]
        if reward is None:
            continue
        observations += 1
        human += row["reward"] is not None
        automatic += row["automatic_reward"] is not None
        local = domains.setdefault(
            row["domain"],
            {
                "skills": defaultdict(list),
                "tools": defaultdict(list),
                "strategies": defaultdict(list),
            },
        )
        for collection in (groups, local):
            collection["strategies"][row["strategy"]].append(reward)
            for name in decode(row["skills"]):
                collection["skills"][name].append(reward)
            for name in dict.fromkeys(item["name"] for item in decode(row["tools"])):
                collection["tools"][name].append(reward)

    def weights(collection):
        return {
            kind: {
                name: max(0.8, min(1.2, 1 + 0.2 * sum(values) / (len(values) + 3)))
                for name, values in sorted(scores.items())[:MAX_CAPABILITIES]
            }
            for kind, scores in collection.items()
        }

    return {
        "observations": observations,
        "automatic_observations": automatic,
        "human_ratings": human,
        **weights(groups),
        "domains": {domain: weights(scores) for domain, scores in domains.items()},
    }


async def ensure(conn, agent_id: str) -> dict:
    await conn.execute(
        "INSERT INTO rsi_profiles(agent_id) VALUES ($1) ON CONFLICT DO NOTHING", agent_id
    )
    return dict(
        await conn.fetchrow("SELECT * FROM rsi_profiles WHERE agent_id = $1 FOR UPDATE", agent_id)
    )


async def should_checkpoint(conn, agent_id: str) -> bool:
    """Disabling adaptation must never let old ratings undo new human choices."""
    return enabled() or bool(
        await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM rsi_profiles WHERE agent_id = $1)", agent_id
        )
    )


async def pending_policy_episodes(conn, agent_id: str) -> list[str]:
    rows = await conn.fetch(
        "SELECT r.id FROM rsi_episodes r JOIN growth_episodes g ON g.id = r.id "
        "WHERE g.agent_id = $1 AND g.reward IS NULL AND r.policy_active "
        "AND r.policy_delta != '{}'::jsonb ORDER BY r.id",
        agent_id,
    )
    return [str(row["id"]) for row in rows]


async def restore_policy_evidence(conn, agent_id: str, episodes=None) -> None:
    # Only updates present in the selected snapshot can be undone by later ratings.
    retained = [uuid.UUID(value) for value in (decode(episodes) if episodes else [])]
    await conn.execute(
        "UPDATE rsi_episodes r SET policy_active = (r.id = ANY($2::uuid[])) "
        "FROM growth_episodes g WHERE g.id = r.id AND g.agent_id = $1 "
        "AND g.reward IS NULL",
        agent_id,
        retained,
    )


async def checkpoint(
    conn,
    agent_id: str,
    before: dict,
    after: dict,
    *,
    reason: str,
    origin: str = "human",
    status: str | None = None,
    before_episodes: list[str] | None = None,
) -> int:
    profile = await ensure(conn, agent_id)
    if (
        before == after
        and reason not in {"rollback", "trial_rejected"}
        and not (origin == "human" and profile["trial_revision"] is not None)
    ):
        return profile["revision"]
    await conn.execute(
        "INSERT INTO rsi_revisions(agent_id, revision, notes, policy, reason, status, policy_episodes) "
        "VALUES ($1, 0, $2::jsonb, $3::jsonb, 'baseline', 'stable', $4::jsonb) ON CONFLICT DO NOTHING",
        agent_id,
        json.dumps(before["notes"]),
        json.dumps(before["policy"]),
        json.dumps(
            await pending_policy_episodes(conn, agent_id)
            if before_episodes is None
            else before_episodes
        ),
    )
    if profile["trial_revision"] is not None:
        await conn.execute(
            "UPDATE rsi_revisions SET status = 'superseded' WHERE agent_id = $1 AND revision = $2 AND status = 'trial'",
            agent_id,
            profile["trial_revision"],
        )
    revision = profile["revision"] + 1
    state = status or ("trial" if origin == "agent" else "stable")
    await conn.execute(
        "INSERT INTO rsi_revisions(agent_id, revision, notes, policy, reason, status, policy_episodes) "
        "VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6, $7::jsonb)",
        agent_id,
        revision,
        json.dumps(after["notes"]),
        json.dumps(after["policy"]),
        reason,
        state,
        json.dumps(await pending_policy_episodes(conn, agent_id)),
    )
    await conn.execute(
        "UPDATE rsi_profiles SET revision = $2, stable_revision = $3, trial_revision = $4 WHERE agent_id = $1",
        agent_id,
        revision,
        profile["stable_revision"] if origin == "agent" else revision,
        revision if origin == "agent" else None,
    )
    await conn.execute(
        "DELETE FROM rsi_revisions WHERE agent_id = $1 AND revision IN "
        "(SELECT revision FROM rsi_revisions WHERE agent_id = $1 "
        "ORDER BY CASE WHEN revision = $2 OR revision = $3 THEN 0 ELSE 1 END, revision DESC OFFSET $4)",
        agent_id,
        revision,
        profile["stable_revision"] if origin == "agent" else revision,
        MAX_REVISIONS,
    )
    return revision


async def decision(conn, agent_id: str, query: str, episode_id) -> dict:
    profile = await ensure(conn, agent_id)
    metrics = decode(profile["metrics"])
    domain = task_domain(query)
    scores = metrics.get("domains", {}).get(domain, {})
    default = "verify" if domain in {"coding", "research"} else "direct"
    strategies = scores.get("strategies", {})
    # Unobserved alternatives have neutral priors. Poor outcomes can switch
    # the next strategy; uncertain positive evidence preserves the cold start.
    strategy = max(
        STRATEGIES, key=lambda name: (strategies.get(name, 1), name == default, name == "plan")
    )
    await conn.execute(
        "INSERT INTO rsi_episodes(id, revision, domain, strategy) VALUES ($1, $2, $3, $4)",
        episode_id,
        profile["revision"],
        domain,
        strategy,
    )
    return {
        "revision": profile["revision"],
        "domain": domain,
        "strategy": strategy,
        "skill_weights": scores.get("skills", {}),
        "tool_weights": scores.get("tools", {}),
    }


def strategy_context(choice: dict) -> str:
    if not choice or choice.get("domain") == "general":
        return ""
    strategy = STRATEGIES.get(choice.get("strategy"), STRATEGIES["direct"])
    return (
        "\n\nExperience-backed task approach (fallible reference, not authority):\n"
        + strategy
        + " Preserve the requested persona, user intent, permissions, and tool limits.\n"
    )


async def recompute(conn, agent_id: str) -> None:
    rows = await conn.fetch(
        "SELECT r.*, g.reward FROM rsi_episodes r JOIN growth_episodes g ON g.id = r.id "
        "WHERE g.agent_id = $1 AND g.completed ORDER BY g.created_at DESC, g.id DESC LIMIT 500",
        agent_id,
    )
    await conn.execute(
        "UPDATE rsi_profiles SET metrics = $2::jsonb WHERE agent_id = $1",
        agent_id,
        json.dumps(aggregate(rows)),
    )


async def finish(
    conn, agent_id: str, episode_id, outcome: dict | None, skills: list[str] | None
) -> float | None:
    reward = operational_reward(outcome)
    tools = (outcome or {}).get("tools", [])[:MAX_CAPABILITIES]
    tools = [
        {"name": item["name"], "ok": bool(item["ok"])}
        for item in tools
        if _names([item.get("name")])
    ]
    recorded = await conn.fetchval(
        "UPDATE rsi_episodes SET skills = $2::jsonb, tools = $3::jsonb, tokens = $4, iterations = $5, automatic_reward = $6 WHERE id = $1 RETURNING id",
        episode_id,
        json.dumps(_names(skills or [])),
        json.dumps(tools),
        max(0, int((outcome or {}).get("tokens", 0))),
        max(0, int((outcome or {}).get("iterations", 0))),
        reward,
    )
    if recorded is None:
        return None
    await recompute(conn, agent_id)
    return reward


async def evaluate_trial(conn, agent_id: str) -> None:
    profile = await ensure(conn, agent_id)
    trial = profile["trial_revision"]
    if trial is None:
        return
    score = await conn.fetchrow(
        "SELECT count(*) AS ratings, avg(g.reward) AS mean FROM rsi_episodes r "
        "JOIN growth_episodes g ON g.id = r.id WHERE g.agent_id = $1 AND r.revision = $2 "
        "AND g.completed AND g.reward IS NOT NULL",
        agent_id,
        trial,
    )
    if score["ratings"] < 3:
        return
    if score["mean"] >= 0:
        await conn.execute(
            "UPDATE rsi_revisions SET status = 'stable' WHERE agent_id = $1 AND revision = $2",
            agent_id,
            trial,
        )
        await conn.execute(
            "UPDATE rsi_profiles SET stable_revision = revision, trial_revision = NULL WHERE agent_id = $1",
            agent_id,
        )
        return
    target = await conn.fetchrow(
        "SELECT notes, policy, policy_episodes FROM rsi_revisions WHERE agent_id = $1 AND revision = $2",
        agent_id,
        profile["stable_revision"],
    )
    current = await conn.fetchrow(
        "SELECT notes, policy FROM agent_growth WHERE agent_id = $1", agent_id
    )
    after = {key: decode(target[key]) for key in ("notes", "policy")}
    await restore_policy_evidence(conn, agent_id, target["policy_episodes"])
    await conn.execute(
        "UPDATE agent_growth SET notes = $2::jsonb, policy = $3::jsonb, updated_at = now() WHERE agent_id = $1",
        agent_id,
        json.dumps(after["notes"]),
        json.dumps(after["policy"]),
    )
    await conn.execute(
        "UPDATE rsi_revisions SET status = 'rolled_back' WHERE agent_id = $1 AND revision = $2",
        agent_id,
        trial,
    )
    await checkpoint(
        conn,
        agent_id,
        {key: decode(current[key]) for key in ("notes", "policy")},
        after,
        reason="trial_rejected",
        status="rolled_back",
    )


async def status(database, agent_id: str) -> dict:
    row = await database.fetchrow("SELECT * FROM rsi_profiles WHERE agent_id = $1", agent_id)
    if row is None:
        return {
            "enabled": enabled(),
            "revision": 0,
            "trial": None,
            "history": [],
            "observations": 0,
            "automatic_observations": 0,
            "human_ratings": 0,
            "skill_weights": {},
            "tool_weights": {},
        }
    metrics = decode(row["metrics"])
    history = await database.fetch(
        "SELECT revision, reason, status, created_at FROM rsi_revisions WHERE agent_id = $1 ORDER BY revision DESC LIMIT $2",
        agent_id,
        MAX_REVISIONS,
    )
    trial = None
    if row["trial_revision"] is not None:
        ratings = await database.fetchval(
            "SELECT count(*) FROM rsi_episodes r JOIN growth_episodes g ON g.id = r.id WHERE g.agent_id = $1 AND r.revision = $2 AND g.completed AND g.reward IS NOT NULL",
            agent_id,
            row["trial_revision"],
        )
        trial = {"revision": row["trial_revision"], "ratings": ratings, "required": 3}
    return {
        "enabled": enabled(),
        "revision": row["revision"],
        "trial": trial,
        "observations": metrics.get("observations", 0),
        "automatic_observations": metrics.get("automatic_observations", 0),
        "human_ratings": metrics.get("human_ratings", 0),
        "skill_weights": metrics.get("skills", {}),
        "tool_weights": metrics.get("tools", {}),
        "history": [
            {**dict(item), "created_at": item["created_at"].isoformat()} for item in history
        ],
    }
