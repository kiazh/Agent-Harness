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

from machina.skills import enrich, repo_install
from machina.skills.registry import log_skill_metadata_event, skill_registry
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

    def clone():
        nonlocal clone_parent
        repo_dir, revision = repo_install.clone_repo(source)
        clone_parent = repo_dir.parent
        return repo_dir, revision

    try:
        repo_dir, revision = await _run_worker_joined(clone)
        plan = await _run_worker_joined(lambda: repo_install.scan_repo(repo_dir, source, revision))

        def publish():
            nonlocal clone_parent
            try:
                repo_install.write_staged_plan(plan, repo_dir)
                return _stage_result(plan, source, revision)
            finally:
                shutil.rmtree(clone_parent)
                clone_parent = None

        result = await _run_worker_joined(publish, capture_result=True)
    except repo_install.RepoInstallError as exc:
        return f"Error: {exc}"
    except OSError:
        logger.warning("repo staging failed", exc_info=True)
        return "Error: staging failed (filesystem error)"
    finally:
        if clone_parent is not None:
            await _run_worker_joined(lambda: shutil.rmtree(clone_parent))
    return result


def _stage_result(plan, source, revision) -> str:
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
        "change, conflict, or validation failure. Never executes repo code. "
        "Each installed skill's v2 catalog metadata (task families, capabilities) "
        "is generated once on install; never regenerate it when already present."
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
        return await _run_worker_joined(
            lambda: _activate_result(detail, staged, skills_dir), capture_result=True
        )
    except repo_install.RepoInstallError as exc:
        return f"Error: {exc}"
    except OSError:
        logger.warning("repo activation I/O failed", exc_info=True)
        return "Error: activation failed (filesystem error)"


def _generate_installed_metadata(names: list[str]) -> list[dict[str, object]]:
    """One-time v2 metadata generation for freshly installed skills.

    Runs in the activation worker thread (no event loop), so the sync bridge
    is safe. Idempotent: a skill that already carries v2 metadata triggers no
    provider call. Returns one structured outcome per skill; a failed
    generation is reported to the caller and never turns the skill into a
    normal success (it stays on disk but unusable, retryable via
    ``machina skills enrich``).
    """
    outcomes: list[dict[str, object]] = []
    for name in names:
        skill = skill_registry.get(name)
        if skill is None:
            log_skill_metadata_event(name, "", status="failed", error_type="SkillMissing")
            outcomes.append(
                {
                    "name": name,
                    "metadataGenerated": False,
                    "usable": False,
                    "metadataError": "skill was not found after installation",
                }
            )
            continue
        if skill.skill_metadata is not None:
            log_skill_metadata_event(skill.name, skill.file_path, status="present")
            outcomes.append(
                {
                    "name": name,
                    "metadataGenerated": False,
                    "usable": True,
                    "metadataError": None,
                }
            )
            continue
        error_type = ""
        try:
            enrich.ensure_skill_metadata_sync(
                skill, agent_seam=enrich.default_agent_seam(), registry=skill_registry
            )
        except Exception as exc:  # reported below, never silent success
            error_type = type(exc).__name__
            logger.warning("skill metadata generation failed for %s", name, exc_info=True)
        usable = skill.skill_metadata is not None
        log_skill_metadata_event(
            skill.name,
            skill.file_path,
            status="generated" if usable else "failed",
            error_type="" if usable else (error_type or "SkillMetadataUnavailable"),
        )
        outcomes.append(
            {
                "name": name,
                "metadataGenerated": usable,
                "usable": usable,
                "metadataError": None
                if usable
                else (
                    f"metadata generation failed ({error_type or 'no schema-v2 metadata produced'})"
                ),
            }
        )
    return outcomes


def _activate_result(detail, staged, skills_dir) -> str:
    installed = repo_install.activate_plan(detail, staged, skills_dir)
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
    outcomes = _generate_installed_metadata(installed)
    lines = [
        f"Installed {len(installed)} skill(s): {', '.join(installed)}. "
        "No repo code was executed. MCP drafts (if any) still need human /mcp review."
    ]
    failed = [outcome for outcome in outcomes if not outcome["usable"]]
    for outcome in outcomes:
        if outcome["usable"]:
            detail_text = (
                "metadataGenerated: true"
                if outcome["metadataGenerated"]
                else "metadataGenerated: false; usable: true (metadata already present)"
            )
        else:
            detail_text = (
                "metadataGenerated: false; usable: false; "
                f"metadataError: {_one_line(outcome['metadataError'], 200)}"
            )
        lines.append(f"- {_one_line(outcome['name'], 80)}: {detail_text}")
    if failed:
        names = ", ".join(_one_line(outcome["name"], 80) for outcome in failed)
        lines.append(
            f"METADATA FAILURE: {names} installed but UNUSABLE (excluded from search and "
            "selection). Retry with `machina skills enrich "
            f"{_one_line(failed[0]['name'], 80)}` or `machina skills enrich --all`."
        )
    return "\n".join(lines)
