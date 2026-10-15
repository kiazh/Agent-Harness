"""Install skills from a pasted GitHub repo link, in two human-gated steps.

``repo_stage`` clones (network) and reports a digest-bound plan without
installing or executing anything. ``repo_activate`` installs validated
skill content (filesystem) only for the exact reviewed digest. MCP
servers found in a repo are returned as configuration drafts for human
/mcp review; installers, hooks, and package installs never run.
"""

from __future__ import annotations

import asyncio
import logging
import shutil

from machina.skills import repo_install
from machina.skills.registry import skill_registry
from machina.tools.base import registry
from machina.tools.file import _run_worker_joined

logger = logging.getLogger(__name__)


def skills_dir_for_install():
    return skill_registry.skills_dir


def _one_line(text: object, limit: int) -> str:
    # Upstream names/descriptions/reasons are untrusted: collapse newlines
    # so a crafted value cannot spoof protocol lines such as DIGEST:.
    return " ".join(str(text or "").split())[:limit]


@registry.register(
    name="repo_stage",
    description=(
        "Stage a GitHub repo link for skill installation. Clones shallowly and "
        "returns a reviewable plan with a digest: skills found, skipped tests, "
        "detected agents/commands/MCP drafts, and items that never run. Installs "
        "nothing and executes no repo code. Call repo_activate only after the "
        "human confirms the exact digest."
    ),
    parameters={
        "type": "object",
        "properties": {"url": {"type": "string", "description": "https://github.com link"}},
        "required": ["url"],
        "additionalProperties": False,
    },
    effects=("net", "fs.write"),
)
async def repo_stage(url: str) -> str:
    try:
        source = repo_install.parse_repo_url(url)
    except repo_install.RepoInstallError as exc:
        return f"Error: {exc}"
    clone_parent = None
    try:
        # Clone and staged-plan publication are filesystem effects, so their
        # workers are joined on cancellation (HELD-44 semantics via
        # _run_worker_joined): a cancelled stage must not detach a worker that
        # finishes writing after turn ownership is released. The scan read has
        # no effects and stays on bare to_thread (same read/write split as
        # tools/file.py).
        repo_dir, revision = await _run_worker_joined(lambda: repo_install.clone_repo(source))
        clone_parent = repo_dir.parent
    except repo_install.RepoInstallError as exc:
        return f"Error: {exc}"
    except OSError:
        logger.warning("repo staging clone failed", exc_info=True)
        return "Error: staging failed (filesystem error)"
    try:
        plan = await asyncio.to_thread(repo_install.scan_repo, repo_dir, source, revision)
        await _run_worker_joined(lambda: repo_install.write_staged_plan(plan, repo_dir))
    except repo_install.RepoInstallError as exc:
        return f"Error: {exc}"
    except OSError:
        logger.warning("repo staging write failed", exc_info=True)
        return "Error: staging failed (filesystem error)"
    finally:
        if clone_parent is not None:
            # Cleanup is also joined so the discarded clone is gone before
            # cancellation is reported.
            await _run_worker_joined(lambda: shutil.rmtree(clone_parent, ignore_errors=True))
    summary = plan.summary()
    lines = [
        f"Staged {source.url} at {revision[:12]} (plan {plan.digest[:16]}...).",
        f"Skills: {len(summary['skills'])} installable, {len(summary['invalid_skills'])} rejected.",
    ]
    for skill in summary["skills"][:10]:
        lines.append(
            f"- {_one_line(skill['name'], 80)}: "
            f"{_one_line(skill['description'], 100)} ({skill['files']} files)"
        )
    if len(summary["skills"]) > 10:
        lines.append(f"- ... and {len(summary['skills']) - 10} more (see digest plan)")
    if summary["invalid_skills"]:
        lines.append("Rejected (never installed):")
        for item in summary["invalid_skills"][:10]:
            lines.append(f"- {_one_line(item['path'], 80)}: {_one_line(item['reason'], 120)}")
    if summary["mcp_drafts"]:
        lines.append("MCP servers detected (drafts for human /mcp review, not configured):")
        for draft in summary["mcp_drafts"][:5]:
            lines.append(f"- {_one_line(draft['name'], 64)}: {_one_line(draft['note'], 140)}")
    for key, label in (
        ("agents", "Agent docs (inventory only, not wired)"),
        ("commands", "Command docs (inventory only, not wired)"),
        ("plugins", "Plugin files (inventory only, not installed)"),
        ("never_run", "Never executed (installers/hooks/manifests)"),
    ):
        if summary[key]:
            lines.append(f"{label}: {len(summary[key])} file(s)")
    if summary.get("unreadable_inventory"):
        lines.append(
            "Warning: inventory unreadable for "
            f"{len(summary['unreadable_inventory'])} file(s); "
            "the draft list above is a lower bound."
        )
    lines.append(
        "No skills were installed and no repo code ran; the reviewed clone was "
        "discarded and only the staging copy remains. To install, have the human "
        f"confirm digest {plan.digest} and call repo_activate with it."
    )
    return "\n".join(lines) + f"\nDIGEST:{plan.digest}"


@registry.register(
    name="repo_activate",
    description=(
        "Install staged repo skills for an exact reviewed digest from repo_stage. "
        "Re-verifies every file, validates SKILL.md files, and refuses on any "
        "change, conflict, or validation failure. Never executes repo code."
    ),
    parameters={
        "type": "object",
        "properties": {"digest": {"type": "string", "description": "Plan digest from repo_stage"}},
        "required": ["digest"],
        "additionalProperties": False,
    },
    effects=("fs.write",),
)
async def repo_activate(digest: str) -> str:
    try:
        detail, staged = await asyncio.to_thread(repo_install.load_staged_plan, digest)
        skills_dir = skills_dir_for_install()
        # The install is the publishing filesystem effect: join its worker so a
        # cancelled activation cannot install skills after ownership releases.
        installed = await _run_worker_joined(
            lambda: repo_install.activate_plan(detail, staged, skills_dir)
        )
    except repo_install.RepoInstallError as exc:
        return f"Error: {exc}"
    except OSError:
        logger.warning("repo activation I/O failed", exc_info=True)
        return "Error: activation failed (filesystem error)"
    if not installed:
        return "No skills to install from this plan. Nothing was changed."
    try:
        skill_registry.load_all(refresh=True)
    except Exception:
        logger.warning("skill registry refresh failed after install", exc_info=True)
        return (
            f"Installed {len(installed)} skill(s): {', '.join(installed)}, but the "
            "skill registry needs a reload before they are visible. "
            "No repo code was executed."
        )
    return (
        f"Installed {len(installed)} skill(s): {', '.join(installed)}. "
        "No repo code was executed. MCP drafts (if any) still need human /mcp review."
    )
