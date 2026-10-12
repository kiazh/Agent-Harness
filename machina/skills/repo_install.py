"""Install skills from a GitHub repo link in two human-gated steps.

Stage (network) clones a shallow, submodule-free copy into an
installation-local staging directory and reports a digest-bound plan.
Activate (filesystem) re-verifies the digest, validates every SKILL.md
through the same injection scans as learned skills, and copies only
skill content into the skills directory with a provenance manifest.

Upstream code is never executed: install scripts, hooks, and package
installs are reported as inert inventory, never run. MCP servers found
in a repo produce configuration drafts for human /mcp review; nothing
is configured or connected automatically. Third-party agents/commands
are reported as inventory, not wired as executable authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ALLOWED_HOSTS = frozenset({"github.com"})
CLONE_TIMEOUT = 120
MAX_CLONE_BYTES = 100 * 1024 * 1024
MAX_FILES = 2000
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_SKILLS = 50

_OWNER_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_REPO_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_REF_RE = re.compile(r"[A-Za-z0-9._/-]{1,128}")

_SKILL_GLOB = "skills/*/SKILL.md"
# Skill resource directories that are never installed: test suites add
# attack surface without runtime value; fixtures elsewhere still copy.
_TEST_DIRS = {"tests", "test", "__tests__", "testing"}
_TEST_SUFFIXES = (
    ".test.js",
    ".test.ts",
    ".test.jsx",
    ".test.tsx",
    ".test.py",
    "_test.py",
    "_test.go",
)
_TEST_DIRS_LOWER = frozenset(d.lower() for d in _TEST_DIRS)
_TEST_SUFFIXES_LOWER = tuple(s.lower() for s in _TEST_SUFFIXES)

# Files that are inventoried and reported but never executed or installed.
_NEVER_RUN = {
    "install.sh",
    "install.ps1",
    "setup.sh",
    "setup.py",
    "Makefile",
    "package.json",
    "pyproject.toml",
}
_HOOK_DIRS = {"hooks", ".hooks", "githooks"}


class RepoInstallError(ValueError):
    """User-facing install failure; safe to show (no secrets, no paths outside staging)."""


@dataclass
class RepoSource:
    owner: str
    repo: str
    ref: str = ""
    url: str = ""

    @property
    def slug(self) -> str:
        return f"{self.owner}-{self.repo}".lower()


def parse_repo_url(url: str) -> RepoSource:
    """Accept only plain https GitHub repo links; rebuild the clone URL."""
    if not isinstance(url, str) or len(url) > 500:
        raise RepoInstallError("repo link must be a short text URL")
    text = url.strip()
    try:
        parts = urlparse(text)
    except ValueError:
        raise RepoInstallError("repo link is not a valid URL") from None
    if parts.scheme != "https" or (parts.hostname or "").lower() != "github.com":
        raise RepoInstallError("only https://github.com links are supported")
    if parts.username or parts.password or parts.port:
        raise RepoInstallError("repo links must not embed credentials or ports")
    segments = [seg for seg in parts.path.split("/") if seg not in ("", ".")]
    if len(segments) < 2:
        raise RepoInstallError("repo link must name an owner and repository")
    owner, repo = segments[0], segments[1]
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if (
        not _OWNER_RE.fullmatch(owner)
        or not _REPO_RE.fullmatch(repo)
        or set(owner) == {"."}
        or set(repo) == {"."}
        or ".." in (owner, repo)
    ):
        raise RepoInstallError("invalid owner or repository name")
    ref = ""
    if len(segments) > 2:
        # Tolerate /tree/<ref> links; everything else is rejected.
        if segments[2] != "tree" or len(segments) < 4:
            raise RepoInstallError("only repository root or /tree/<ref> links are supported")
        ref = "/".join(segments[3:])
        if not _REF_RE.fullmatch(ref) or ".." in ref.split("/"):
            raise RepoInstallError("invalid branch or tag reference")
    return RepoSource(owner=owner, repo=repo, ref=ref, url=f"https://github.com/{owner}/{repo}")


def staging_root() -> Path:
    return Path.home() / ".machina" / "repo-staging"


def _confined(root: Path, relative: str) -> Path:
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise RepoInstallError(f"unsafe path in repo: {relative}")
    path = root / relative
    try:
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise RepoInstallError(f"refusing symlink or escaping path: {relative}")
    except OSError as exc:
        raise RepoInstallError(f"unreadable path in repo: {relative}") from exc
    return path


def _git_env() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") and key not in ("GCM_INTERACTIVE", "SSH_ASKPASS")
    }
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "true",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_LFS_SKIP_SMUDGE": "1",
        }
    )
    return env


def _git(args: list[str], *, cwd: Path, timeout: int = 60) -> str:
    """Run a git plumbing command with scrubbed env and a hard timeout."""
    try:
        return subprocess.check_output(
            ["git", *args], cwd=cwd, text=True, timeout=timeout, env=_git_env()
        )
    except subprocess.TimeoutExpired as exc:
        raise RepoInstallError("repository operation timed out") from exc
    except subprocess.SubprocessError as exc:
        raise RepoInstallError("repository operation failed") from exc


def _abort_clone(parent: Path, message: str, cause: BaseException | None = None) -> None:
    """Remove a failed clone temp dir, then refuse with a path-free error."""
    shutil.rmtree(parent, ignore_errors=True)
    if cause is None:
        raise RepoInstallError(message)
    raise RepoInstallError(message) from cause


def clone_repo(source: RepoSource, *, _allow_file: bool = False) -> tuple[Path, str]:
    """Shallow-clone into a fresh temp dir; return (dir, revision).

    ``_allow_file`` exists only so tests can exercise cloning without
    network access; production callers must leave it False. The caller
    owns the temp parent and must remove it after staging (see repo_stage).

    Size is enforced after checkout: a hostile HEAD is fully fetched
    before refusal, bounded by the clone timeout and the size cap below.
    """
    if _allow_file:
        clone_url = source.url
    else:
        clone_url = f"https://github.com/{source.owner}/{source.repo}.git"
    parent = Path(tempfile.mkdtemp(prefix="machina-repo-stage-"))
    target = parent / "repo"
    args = ["git"]
    if not _allow_file:
        args += ["-c", "protocol.file.allow=never"]
    args += ["clone", "--depth", "1"]
    if not _allow_file:
        args += ["--no-recurse-submodules", "--no-tags"]
    if source.ref and not _allow_file:
        args += ["--branch", source.ref]
    args += [clone_url, str(target)]
    try:
        subprocess.run(
            args,
            check=True,
            capture_output=True,
            timeout=CLONE_TIMEOUT,
            env=_git_env(),
        )
    except subprocess.TimeoutExpired as exc:
        _abort_clone(parent, "clone timed out", exc)
    except subprocess.CalledProcessError as exc:
        _abort_clone(parent, "could not clone that repository", exc)
    try:
        revision = _git(["rev-parse", "HEAD"], cwd=target).strip()
    except RepoInstallError as exc:
        _abort_clone(parent, "cloned repository has no revision", exc)
    size = 0
    for path in target.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                size += path.stat().st_size
        except OSError:
            continue
        if size > MAX_CLONE_BYTES:
            _abort_clone(parent, "repository exceeds the clone size limit")
    return target, revision


def _tracked_files(repo: Path) -> list[str]:
    try:
        raw = subprocess.check_output(
            ["git", "ls-files", "-z"], cwd=repo, timeout=60, env=_git_env()
        )
    except subprocess.TimeoutExpired as exc:
        raise RepoInstallError("repository operation timed out") from exc
    except subprocess.SubprocessError as exc:
        raise RepoInstallError("cannot enumerate repository files") from exc
    return [item for item in raw.decode("utf-8", "replace").split("\0") if item]


@dataclass
class SkillPlan:
    name: str
    description: str
    root: str = ""
    files: dict[str, str] = field(default_factory=dict)
    skipped_tests: list[str] = field(default_factory=list)


@dataclass
class RepoPlan:
    source: RepoSource
    revision: str
    skills: list[SkillPlan] = field(default_factory=list)
    invalid_skills: list[dict[str, str]] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)
    mcp_drafts: list[dict[str, Any]] = field(default_factory=list)
    unreadable_inventory: list[str] = field(default_factory=list)
    never_run: list[str] = field(default_factory=list)
    skipped_oversize: list[str] = field(default_factory=list)
    digest: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "source": self.source.url,
            "revision": self.revision,
            "digest": self.digest,
            "skills": [
                {
                    "name": skill.name,
                    "description": skill.description,
                    "root": skill.root,
                    "files": len(skill.files),
                }
                for skill in self.skills
            ],
            "invalid_skills": self.invalid_skills,
            "agents": self.agents,
            "commands": self.commands,
            "plugins": self.plugins,
            "mcp_drafts": self.mcp_drafts,
            "unreadable_inventory": self.unreadable_inventory,
            "never_run": self.never_run,
            "skipped_oversize": self.skipped_oversize,
        }


def _is_test_path(relative: str) -> bool:
    # Case-insensitive: Tests/, FOO.TEST.PY and friends are hygiene
    # exclusions, not a security boundary (nothing executes; the digest
    # review is the gate).
    parts = Path(relative).parts
    if any(part.lower() in _TEST_DIRS_LOWER for part in parts[:-1]):
        return True
    name = parts[-1].lower() if parts else ""
    return name.endswith(_TEST_SUFFIXES_LOWER)


_TEXT_SUFFIXES = frozenset({".md", ".markdown", ".mdx", ".txt", ".rst"})


def _top(relative: str) -> str:
    parts = Path(relative).parts
    return parts[0] if parts else ""


def _in_hook_dir(relative: str) -> bool:
    return Path(relative).parts[0] in _HOOK_DIRS if Path(relative).parts else False


def _parse_skill_md(repo: Path, relative: str, SkillParser: Any) -> Any:
    """Size-check then parse a SKILL.md (parse itself reads unbounded)."""
    try:
        if (repo / relative).stat().st_size > MAX_FILE_BYTES:
            raise RepoInstallError(f"skill manifest exceeds size limit: {relative}")
        return SkillParser.parse(repo / relative)
    except (ValueError, OSError, UnicodeDecodeError) as exc:
        raise RepoInstallError(f"invalid skill manifest {relative}: {exc}") from exc


def _collect_members(
    repo: Path,
    members: list[str],
    entry: SkillPlan,
    plan: RepoPlan,
    SkillParser: Any,
) -> int:
    """Hash installable members; returns bytes added.

    Test suites, installer manifests, and hook directories are never
    installed (report-only inventory covers them separately). Companion
    text files get the same injection scan as SKILL.md; a match invalidates
    the whole skill fail-closed. Oversize/unreadable members are recorded
    with their reason instead of aborting the scan.
    """
    added = 0
    for relative in sorted(members):
        if _is_test_path(relative):
            entry.skipped_tests.append(relative)
            continue
        if Path(relative).name in _NEVER_RUN or _in_hook_dir(relative):
            continue
        try:
            data = _read_bounded(repo / relative)
        except RepoInstallError:
            plan.skipped_oversize.append(f"{relative} (exceeds size limit or unreadable)")
            continue
        if Path(relative).suffix.lower() in _TEXT_SUFFIXES:
            try:
                SkillParser._scan_injection(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise RepoInstallError(f"skill content failed safety scan: {relative}") from exc
        added += len(data)
        entry.files[relative] = hashlib.sha256(data).hexdigest()
    return added


def _read_bounded(path: Path, limit: int = MAX_FILE_BYTES) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise RepoInstallError(f"unreadable file: {path.name}") from exc
    if size > limit:
        raise RepoInstallError(f"file exceeds size limit: {path.name}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise RepoInstallError(f"unreadable file: {path.name}") from exc


def scan_repo(repo: Path, source: RepoSource, revision: str) -> RepoPlan:
    """Inventory a staged clone without executing anything in it."""
    from machina.skills.registry import SkillParser

    tracked = _tracked_files(repo)
    if len(tracked) > MAX_FILES:
        raise RepoInstallError("repository has too many files")
    for relative in tracked:
        _confined(repo, relative)
    plan = RepoPlan(source=source, revision=revision)
    total = 0
    skill_roots = sorted(
        {
            str(Path(p).parent)
            for p in tracked
            if Path(p).name == "SKILL.md"
            and len(Path(p).parts) == 3
            and Path(p).parts[0] == "skills"
        }
    )
    # Single-skill repos keep SKILL.md at the root; everything not otherwise
    # classified (agents/commands/plugins/hooks/installers/tests) belongs to it.
    root_skill = "SKILL.md" in tracked
    classified = {"agents", "commands", "plugins", ".opencode", "hooks", ".hooks", "githooks"}
    seen_names: set[str] = set()

    def _claim_name(name: str, path: str) -> str | None:
        safe = name.lower()
        if safe in seen_names:
            plan.invalid_skills.append(
                {"path": path, "reason": f"skill name collides (case-insensitive): {name}"}
            )
            return None
        seen_names.add(safe)
        return name

    for root in skill_roots[: MAX_SKILLS + 1]:
        if len(plan.skills) >= MAX_SKILLS:
            break
        try:
            skill = _parse_skill_md(repo, root + "/SKILL.md", SkillParser)
        except RepoInstallError as exc:
            plan.invalid_skills.append({"path": root + "/SKILL.md", "reason": str(exc)[:200]})
            continue
        name = _claim_name(skill.name, root + "/SKILL.md")
        if name is None:
            continue
        entry = SkillPlan(name=name, description=skill.description, root=root)
        try:
            total += _collect_members(
                repo,
                sorted(p for p in tracked if p == root + "/SKILL.md" or p.startswith(root + "/")),
                entry,
                plan,
                SkillParser,
            )
        except RepoInstallError as exc:
            plan.invalid_skills.append({"path": root + "/SKILL.md", "reason": str(exc)[:200]})
            continue
        if total > MAX_TOTAL_BYTES:
            raise RepoInstallError("skill content exceeds the total size limit")
        if not entry.files:
            plan.invalid_skills.append(
                {"path": root + "/SKILL.md", "reason": "no installable files"}
            )
            continue
        plan.skills.append(entry)
    if root_skill and len(plan.skills) < MAX_SKILLS:
        try:
            skill = _parse_skill_md(repo, "SKILL.md", SkillParser)
            raw_name = skill.name if skill.name not in ("repo", ".") else f"{source.slug}-skill"
        except RepoInstallError as exc:
            plan.invalid_skills.append({"path": "SKILL.md", "reason": str(exc)[:200]})
            skill = None
            raw_name = ""
        if skill is not None:
            name = _claim_name(raw_name, "SKILL.md")
            if name is not None:
                entry = SkillPlan(name=name, description=skill.description, root="")
                members = [
                    relative
                    for relative in sorted(tracked)
                    if relative == "SKILL.md"
                    or (_top(relative) not in classified and _top(relative) != "skills")
                ]
                try:
                    total += _collect_members(repo, members, entry, plan, SkillParser)
                except RepoInstallError as exc:
                    plan.invalid_skills.append({"path": "SKILL.md", "reason": str(exc)[:200]})
                    entry = None  # type: ignore[assignment]
                if entry is not None:
                    if total > MAX_TOTAL_BYTES:
                        raise RepoInstallError("skill content exceeds the total size limit")
                    if entry.files:
                        plan.skills.append(entry)
                    else:
                        plan.invalid_skills.append(
                            {"path": "SKILL.md", "reason": "no installable files"}
                        )
    recorded_oversize = {entry.split(" (", 1)[0] for entry in plan.skipped_oversize}
    for relative in tracked:
        parts = Path(relative).parts
        if not parts:
            continue
        if relative in recorded_oversize:
            continue
        top = parts[0]
        try:
            size = (repo / relative).stat().st_size
        except OSError:
            continue
        if size > MAX_FILE_BYTES:
            if relative not in {f for skill in plan.skills for f in skill.files}:
                plan.skipped_oversize.append(f"{relative} (exceeds size limit)")
            continue
        if top == "agents" and relative.endswith(".md") and len(parts) == 2:
            plan.agents.append(relative)
        elif top == "commands" and relative.endswith(".md"):
            plan.commands.append(relative)
        elif (
            top == ".opencode"
            and len(parts) > 1
            and parts[1] == "commands"
            and relative.endswith(".md")
        ):
            plan.commands.append(relative)
        elif top == "plugins":
            plan.plugins.append(relative if len(parts) <= 2 else parts[0] + "/" + parts[1])
        elif Path(relative).name in _NEVER_RUN or top in _HOOK_DIRS:
            if relative not in plan.never_run:
                plan.never_run.append(relative)
    plan.plugins = sorted(set(plan.plugins))
    plan.agents = sorted(plan.agents)[:100]
    plan.commands = sorted(plan.commands)[:200]
    plan.never_run = sorted(plan.never_run)[:100]
    plan.mcp_drafts, plan.unreadable_inventory = _mcp_drafts(repo, tracked, source)
    plan.digest = _digest_plan(plan)
    return plan


def _read_json(
    path: Path, limit: int = 256_000, *, label: str, unreadable: list[str]
) -> Any | None:
    """Bounded JSON read; records (never raises) when inventory is unreadable."""
    try:
        data = _read_bounded(path, limit)
    except RepoInstallError:
        unreadable.append(f"{label} (oversize or unreadable)")
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        unreadable.append(f"{label} (not valid JSON)")
        return None


def _mcp_drafts(
    repo: Path, tracked: list[str], source: RepoSource
) -> tuple[list[dict[str, Any]], list[str]]:
    """Describe MCP servers found in the repo for human /mcp review.

    Drafts are configuration starting points, never applied automatically.
    Endpoints and package names the repo does not state stay empty for the
    human to fill in; nothing here connects anywhere.
    """
    drafts: list[dict[str, Any]] = []
    unreadable: list[str] = []
    slug = re.sub(r"[^a-z0-9]+", "-", source.repo.lower()).strip("-") or "repo-server"
    if ".mcp.json" in tracked:
        config = _read_json(repo / ".mcp.json", label=".mcp.json", unreadable=unreadable)
        if isinstance(config, dict):
            servers = config.get("mcpServers", config.get("servers", {}))
            if isinstance(servers, dict):
                for name, entry in list(servers.items())[:10]:
                    if not isinstance(entry, dict):
                        continue
                    drafts.append(
                        {
                            "name": str(name)[:64],
                            "transport": "stdio",
                            "command": entry.get("command", ""),
                            "argv": entry.get("args", []),
                            "note": "Review command/args before saving with /mcp configure.",
                        }
                    )
    for manifest in ("package.json",):
        if manifest not in tracked:
            continue
        package = _read_json(repo / manifest, label=manifest, unreadable=unreadable)
        if not isinstance(package, dict):
            continue
        dependencies = package.get("dependencies", {})
        if not isinstance(dependencies, dict) or not any(
            "modelcontextprotocol" in str(dep)
            for dep in list(dependencies)
            + list(
                package.get("devDependencies", {}).keys()
                if isinstance(package.get("devDependencies"), dict)
                else []
            )
        ):
            continue
        bins = package.get("bin", {})
        if isinstance(bins, str):
            bins = {package.get("name", slug): bins}
        if isinstance(bins, dict) and bins:
            first = sorted(bins)[0]
            drafts.append(
                {
                    "name": slug,
                    "transport": "stdio",
                    "command": "npx",
                    "argv": ["-y", str(first)],
                    "note": "Verify the published package name and entry point, then save with /mcp configure. Never auto-connect.",
                }
            )
        else:
            drafts.append(
                {
                    "name": slug,
                    "transport": "stdio",
                    "command": "",
                    "argv": [],
                    "note": "MCP SDK dependency detected but no binary entry point found; inspect the repo and save with /mcp configure.",
                }
            )
    pyproject = repo / "pyproject.toml" if "pyproject.toml" in tracked else None
    if pyproject is not None:
        try:
            text = _read_bounded(pyproject, 256_000).decode("utf-8")
        except (RepoInstallError, UnicodeDecodeError):
            unreadable.append("pyproject.toml (oversize or unreadable)")
            text = ""
        if "modelcontextprotocol" in text or "mcp" in text.lower():
            scripts = re.findall(r"^(\w[\w-]*)\s*=", text, re.M)
            drafts.append(
                {
                    "name": slug,
                    "transport": "stdio",
                    "command": "uvx",
                    "argv": [source.repo] if scripts else [],
                    "note": "Possible Python MCP server; confirm the runnable package/entry point, then save with /mcp configure. Never auto-connect.",
                }
            )
    return drafts[:10], sorted(set(unreadable))[:10]


def _canonical_plan_bytes(source: str, revision: str, skills: list[dict[str, Any]]) -> bytes:
    """Canonical digest bytes shared by _digest_plan and activate_plan.

    Both sides must project skills to exactly {"name", "root", "files"} or
    digests diverge and activation refuses.
    """
    return json.dumps(
        {
            "source": source,
            "revision": revision,
            "skills": [
                {"name": skill["name"], "root": skill["root"], "files": skill["files"]}
                for skill in skills
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _digest_plan(plan: RepoPlan) -> str:
    return hashlib.sha256(
        _canonical_plan_bytes(
            plan.source.url,
            plan.revision,
            [
                {"name": skill.name, "root": skill.root, "files": skill.files}
                for skill in plan.skills
            ],
        )
    ).hexdigest()


_MAX_STAGED_ENTRIES = 10
_STAGING_TTL_SECONDS = 7 * 24 * 3600
_PLAN_JSON_MAX_BYTES = 1 * 1024 * 1024


def _staging_key(source: RepoSource, revision: str) -> str:
    # Sanitize: legal GitHub names may contain characters outside the key
    # alphabet, and the full revision avoids prefix collisions.
    slug = re.sub(r"[^a-z0-9-]+", "-", source.slug.lower()).strip("-") or "repo"
    return f"{slug}-{revision[:40]}"


def _staging_entries(root: Path) -> list[Path] | None:
    """Staging dirs oldest-first, or None when the root is unreadable."""
    try:
        return sorted(
            (p for p in root.iterdir() if p.is_dir() and not p.is_symlink()),
            key=lambda p: p.stat().st_mtime,
        )
    except OSError:
        return None


def _prune_staging(root: Path) -> None:
    """Bound staging growth: drop entries past the count cap or TTL."""
    import time

    entries = _staging_entries(root)
    if entries is None:
        return
    now = time.time()
    for stale in entries:
        try:
            if now - stale.stat().st_mtime > _STAGING_TTL_SECONDS:
                shutil.rmtree(stale, ignore_errors=True)
        except OSError:
            continue
    entries = _staging_entries(root)
    if entries is None:
        return
    for stale in entries[: max(0, len(entries) - _MAX_STAGED_ENTRIES)]:
        shutil.rmtree(stale, ignore_errors=True)


def write_staged_plan(plan: RepoPlan, repo_dir: Path) -> Path:
    """Persist the clone under a stable staging key with its full plan.

    Writes land in a temp sibling first and rename into place, so a crash
    never leaves a half-copied staging dir that reads as "stage again".
    """
    root = staging_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = _staging_key(plan.source, plan.revision)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,160}", key):
        raise RepoInstallError("invalid staging key")
    tmp_destination = root / f"{key}.staging-{os.getpid()}"
    if tmp_destination.exists():
        shutil.rmtree(tmp_destination, ignore_errors=True)
    try:
        # Links are preserved as links: scan-time confinement already
        # refused escapes, so refused bytes are never materialized here.
        shutil.copytree(repo_dir, tmp_destination, symlinks=True)
    except OSError as exc:
        shutil.rmtree(tmp_destination, ignore_errors=True)
        raise RepoInstallError("could not stage repository files") from exc
    destination = root / key
    detail = {
        "source": plan.source.url,
        "revision": plan.revision,
        "digest": plan.digest,
        "skills": [
            {
                "name": skill.name,
                "description": skill.description,
                "root": skill.root,
                "files": skill.files,
                "skipped_tests": skill.skipped_tests,
            }
            for skill in plan.skills
        ],
        "invalid_skills": plan.invalid_skills,
        "agents": plan.agents,
        "commands": plan.commands,
        "plugins": plan.plugins,
        "mcp_drafts": plan.mcp_drafts,
        "unreadable_inventory": plan.unreadable_inventory,
        "never_run": plan.never_run,
        "skipped_oversize": plan.skipped_oversize,
    }
    plan_text = json.dumps(detail, indent=2, sort_keys=True) + "\n"
    try:
        (tmp_destination / "plan.json.tmp").write_text(plan_text, encoding="utf-8")
        _fsync_file(tmp_destination / "plan.json.tmp")
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        os.replace(tmp_destination, destination)
        (destination / "plan.json.tmp").replace(destination / "plan.json")
        _fsync_dir(destination, best_effort=True)
    except OSError as exc:
        shutil.rmtree(tmp_destination, ignore_errors=True)
        raise RepoInstallError("could not stage repository files") from exc
    _prune_staging(root)
    return destination


def load_staged_plan(digest: str) -> tuple[dict[str, Any], Path]:
    """Find staged detail by digest; digests are re-verified on activate."""
    if not re.fullmatch(r"[0-9a-f]{64}", digest or ""):
        raise RepoInstallError("activation digest must be the staged plan digest")
    root = staging_root()
    if not root.is_dir():
        raise RepoInstallError("no staged repository found; stage a repo link first")
    corrupt: list[str] = []
    for staged in sorted(root.iterdir()):
        if not staged.is_dir() or staged.is_symlink():
            continue
        plan_file = staged / "plan.json"
        if not plan_file.is_file():
            # Only this writer creates entries here, so a plan-less
            # directory is an interrupted write, not a valid empty state.
            corrupt.append(staged.name)
            continue
        try:
            if plan_file.stat().st_size > _PLAN_JSON_MAX_BYTES:
                raise ValueError("plan.json exceeds size limit")
            detail = json.loads(plan_file.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            corrupt.append(staged.name)
            continue
        if isinstance(detail, dict) and detail.get("digest") == digest:
            return detail, staged
    if corrupt:
        raise RepoInstallError(
            f"staged entries unreadable ({', '.join(sorted(corrupt)[:3])}); "
            "stage the repo link again"
        )
    raise RepoInstallError("no staged plan matches that digest; stage the repo link again")


def _manifest_path(skills_dir: Path, name: str) -> Path:
    return skills_dir / name / ".install-manifest.json"


def activate_plan(plan_detail: dict[str, Any], staged: Path, skills_dir: Path) -> list[str]:
    """Re-verify recorded digests from the staged tree, then install.

    Only recorded files are ever installed; anything added to staging
    after review is ignored, and anything removed or modified refuses.
    """
    expected = plan_detail.get("digest", "")
    if not isinstance(plan_detail, dict) or not re.fullmatch(r"[0-9a-f]{64}", expected or ""):
        raise RepoInstallError("staged plan is malformed; stage again")
    try:
        tracked = set(_tracked_files(staged))
    except RepoInstallError as exc:
        raise RepoInstallError("staged repository changed since review; stage again") from exc
    skills: list[dict[str, Any]] = []
    for skill in plan_detail.get("skills", []):
        if not isinstance(skill, dict):
            raise RepoInstallError("staged plan is malformed; stage again")
        files = {}
        for relative in skill.get("files", {}):
            if not isinstance(relative, str) or relative not in tracked:
                raise RepoInstallError("staged repository changed since review; stage again")
            try:
                data = _read_bounded(staged / relative)
            except RepoInstallError as exc:
                raise RepoInstallError(
                    "staged repository changed since review; stage again"
                ) from exc
            files[relative] = hashlib.sha256(data).hexdigest()
        skills.append(
            {"name": skill.get("name", ""), "root": skill.get("root", ""), "files": files}
        )
    recomputed = hashlib.sha256(
        _canonical_plan_bytes(
            plan_detail.get("source", ""), plan_detail.get("revision", ""), skills
        )
    ).hexdigest()
    if recomputed != expected:
        raise RepoInstallError("staged repository changed since review; stage again")
    # Phase 1 builds every skill into temp dirs: nothing live is touched
    # until all skills verify. Phase 2 renames into place with rollback.
    built: list[tuple[str, Path | None]] = []
    try:
        for skill in skills:
            built.append(_build_skill(staged, plan_detail, skill, skills_dir))
        installed: list[str] = []
        committed: list[str] = []
        try:
            for name, tmpdir in built:
                if tmpdir is None:
                    installed.append(name)
                    continue
                try:
                    os.replace(tmpdir, skills_dir / name.lower())
                except OSError as exc:
                    raise RepoInstallError(f"could not install skill '{name}'") from exc
                installed.append(name)
                committed.append(name)
        except RepoInstallError as exc:
            for done in committed:
                target = skills_dir / done.lower()
                if (target / ".install-manifest.json").is_file():
                    shutil.rmtree(target, ignore_errors=True)
            raise RepoInstallError(
                f"{exc} (installed before failure and rolled back: "
                + (", ".join(committed) or "none")
                + ")"
            ) from exc
        return installed
    finally:
        for _, tmpdir in built:
            if tmpdir is not None:
                shutil.rmtree(tmpdir, ignore_errors=True)


def _verify_installed(
    skills_dir: Path, safe: str, manifest: dict[str, Any], SkillParser: Any
) -> list[str]:
    """Re-hash installed files against the manifest; return diverged paths."""
    diverged: list[str] = []
    for relative, digest in (manifest.get("files") or {}).items():
        path = skills_dir / relative
        try:
            if not path.is_file():
                diverged.append(f"{relative} (missing)")
                continue
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                diverged.append(relative)
                continue
            if Path(relative).name == "SKILL.md":
                SkillParser.parse(path)
        except (OSError, ValueError, UnicodeDecodeError):
            diverged.append(f"{relative} (unreadable or invalid)")
    return diverged


def _build_skill(
    staged: Path, plan_detail: dict[str, Any], skill: dict[str, Any], skills_dir: Path
) -> tuple[str, Path | None]:
    """Verify and stage one skill into a temp dir; return (name, tmpdir|None).

    A None tmpdir means the live tree already holds this exact install.
    Raises RepoInstallError before touching the live tree on any problem.
    """
    from machina.skills.registry import SkillParser

    name = skill.get("name", "")
    root = skill.get("root", "")
    if not isinstance(name, str) or not isinstance(root, str):
        raise RepoInstallError("staged plan is malformed; stage again")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", name):
        raise RepoInstallError(f"invalid skill name: {name}")
    safe = name.lower()
    if (skills_dir / safe).exists():
        manifest_file = _manifest_path(skills_dir, safe)
        if manifest_file.is_file():
            try:
                manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                manifest = {}
            if (
                manifest.get("source") == plan_detail.get("source")
                and manifest.get("revision") == plan_detail.get("revision")
                and manifest.get("digest") == plan_detail.get("digest")
            ):
                diverged = _verify_installed(skills_dir, safe, manifest, SkillParser)
                if not diverged:
                    return name, None
                raise RepoInstallError(
                    f"installed skill '{name}' diverged from its manifest "
                    f"({', '.join(diverged[:5])}); remove it first"
                )
        raise RepoInstallError(
            f"skill '{name}' already exists from another source; remove it first"
        )
    # Validate every SKILL.md through the same scans as learned skills.
    # A tampered SKILL.md must refuse cleanly, never escape as a traceback.
    for relative in skill["files"]:
        if Path(relative).name == "SKILL.md":
            try:
                SkillParser.parse(staged / relative)
            except (ValueError, OSError, UnicodeDecodeError) as exc:
                raise RepoInstallError(
                    f"staged skill manifest failed validation: {relative}"
                ) from exc
    prefix = (root + "/") if root else ""
    # Keep incomplete builds out of the registry's direct-child scan, even
    # if the process dies before the ordinary cleanup path runs.
    tmpdir = skills_dir / ".install-staging" / f"{safe}.install-{os.getpid()}-{_rand_suffix()}"
    try:
        for relative, digest in sorted(skill["files"].items()):
            if prefix and not relative.startswith(prefix):
                raise RepoInstallError("staged plan is malformed; stage again")
            source = _confined(staged, relative)
            if not source.is_file():
                raise RepoInstallError(f"staged file vanished: {relative}")
            data = _read_bounded(source)
            if hashlib.sha256(data).hexdigest() != digest:
                raise RepoInstallError("staged repository changed since review; stage again")
            inner = relative[len(prefix) :] if prefix else relative
            destination = _confined(tmpdir, inner)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            _fsync_file(destination)
        manifest = {
            "source": plan_detail.get("source", ""),
            "revision": plan_detail.get("revision", ""),
            "digest": plan_detail.get("digest", ""),
            "files": {
                f"{safe}/{relative[len(prefix) :] if prefix else relative}": digest
                for relative, digest in sorted(skill["files"].items())
            },
        }
        manifest_path = _confined(tmpdir, ".install-manifest.json")
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        _fsync_file(manifest_path)
        _fsync_dir(tmpdir)
    except RepoInstallError:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RepoInstallError(f"could not stage skill '{name}'") from exc
    return name, tmpdir


def _fsync_file(path: Path) -> None:
    with open(path, "rb") as handle:
        os.fsync(handle.fileno())


def _fsync_dir(path: Path, *, best_effort: bool = False) -> None:
    # Staging keeps best-effort durability on platforms without directory
    # fsync. Skill activation must refuse when its directory sync fails.
    try:
        dir_fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        if best_effort:
            return
        raise
    try:
        os.fsync(dir_fd)
    except OSError:
        if not best_effort:
            raise
    finally:
        os.close(dir_fd)


def _rand_suffix() -> str:
    import secrets as _secrets

    return _secrets.token_hex(4)
