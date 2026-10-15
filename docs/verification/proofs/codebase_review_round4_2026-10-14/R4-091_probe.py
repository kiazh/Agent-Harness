"""R4-091 adversarial supplement probe — skills/soulspec cluster.

Boundary and hostile cases beyond the pinned tests, one block per deliverable:
(hub type-hardening, delete/reinstall + refusal, pre-stat caps, Windows fsync,
URL ports, workflow classifier). Exit 0 = every case holds.

Run: uv run python docs/verification/proofs/codebase_review_round4_2026-10-14/R4-091_probe.py
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

from machina.skills import repo_install
from machina.skills.registry import (
    SkillHub,
    SkillHubError,
    SkillParser,
    SkillRegistry,
)
from machina.skills.repo_install import RepoInstallError, parse_repo_url, remove_installed_skill
from machina.skills.workflow import plan_workflow
from machina.soulspec.schema import SoulSpec

ROOT = Path(tempfile.mkdtemp(prefix="r4-091-probe-"))

SKILL_MD = """---
name: alpha
description: Alpha skill
triggers: [alpha]
---
Steps for alpha.
"""

failures: list[str] = []


def check(label):
    def run(fn):
        try:
            fn()
        except Exception as exc:  # probe: any failure is a failure
            failures.append(label)
            print(f"FAIL {label}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {label}")

    return run


# ── (a) hub JSON type-hardening ──────────────────────────────────────────────


def hub_case():
    hub_dir = ROOT / "hub"
    registry = SkillRegistry(ROOT / "hub-skills")
    hub = SkillHub(registry, hub_dir)
    (hub_dir / "good.json").write_text(
        json.dumps(
            {
                "name": "good",
                "description": "Fine",
                "triggers": [],
                "content": "Steps",
                "published_at": "2026-10-14T00:00:00+00:00",
                "usage_count": 1,
                "view_count": 2,
            }
        ),
        encoding="utf-8",
    )
    (hub_dir / "falsy-tags.json").write_text(
        json.dumps({"name": "falsy", "description": "x", "tags": 0}), encoding="utf-8"
    )
    (hub_dir / "bad-published.json").write_text(
        json.dumps({"name": "badpub", "description": "x", "published_at": 5}),
        encoding="utf-8",
    )
    (hub_dir / "not-object.json").write_text('"just a string"', encoding="utf-8")
    assert [e["name"] for e in hub.list_hub()] == ["good"], hub.list_hub()
    assert [e["name"] for e in hub.get_hub_telemetry()] == ["good"]
    # Consume-time operations raise the typed error, never Key/TypeError.
    (hub_dir / "nameless.json").write_text(
        json.dumps({"content": "Steps", "triggers": [1]}), encoding="utf-8"
    )
    for name in ("nameless", "falsy-tags"):
        try:
            hub.install(name)
        except SkillHubError:
            pass
        else:
            raise AssertionError(f"install({name}) did not raise SkillHubError")
    try:
        hub.install("../escape")
    except SkillHubError:
        pass
    else:
        raise AssertionError("traversal name accepted")


check("(a) hub: listing/telemetry survive corrupt entries; install raises SkillHubError")(hub_case)


# ── (b) delete removes the manifest-owned tree; refusals are typed ───────────


def _installed_dir(name: str, files: dict[str, str], manifest_files: dict[str, str]) -> Path:
    skill_dir = ROOT / "delete-skills" / name
    for relative, content in files.items():
        path = skill_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (skill_dir / ".install-manifest.json").write_text(
        json.dumps({"files": manifest_files}), encoding="utf-8"
    )
    return skill_dir


def delete_case():
    payload = SKILL_MD
    digest = hashlib.sha256(payload.encode()).hexdigest()
    skill_dir = _installed_dir(
        "alpha",
        {
            "SKILL.md": payload,
            "scripts/run.py": "print('a')\n",
            "extra-planted.txt": "keep?\n",
        },
        {"alpha/SKILL.md": digest, "alpha/scripts/run.py": digest},
    )
    assert remove_installed_skill(skill_dir) is True
    assert not skill_dir.exists(), "owned tree not fully removed"

    traversal = _installed_dir(
        "beta",
        {"SKILL.md": payload},
        {"beta/SKILL.md": digest, "beta/../escape.txt": digest},
    )
    try:
        remove_installed_skill(traversal)
    except RepoInstallError:
        assert (traversal / "SKILL.md").is_file(), "refusal must not delete"
    else:
        raise AssertionError("traversal manifest accepted")

    foreign = _installed_dir("gamma", {"SKILL.md": payload}, {"delta/SKILL.md": digest})
    try:
        remove_installed_skill(foreign)
    except RepoInstallError:
        assert (foreign / "SKILL.md").is_file()
    else:
        raise AssertionError("foreign-prefix manifest accepted")

    plain = ROOT / "delete-skills" / "plain" / "SKILL.md"
    plain.parent.mkdir(parents=True)
    plain.write_text(payload)
    assert remove_installed_skill(plain.parent) is False, "plain dir must not claim ownership"


check("(b) delete: owned tree removed; traversal/foreign manifests refuse; plain dir untouched")(
    delete_case
)


# ── (c1) skill parse 1 MB cap is a pre-stat guard ────────────────────────────


def parse_cap_case():
    line = "x y z\n"
    at_cap = line * ((SkillParser.MAX_CONTENT_SIZE - 4) // len(line)) + "abcd"
    assert len(at_cap) == SkillParser.MAX_CONTENT_SIZE
    ok_dir = ROOT / "cap-skill"
    ok_dir.mkdir()
    (ok_dir / "SKILL.md").write_text(at_cap)
    assert SkillParser.parse(ok_dir / "SKILL.md").content
    over_dir = ROOT / "over-skill"
    over_dir.mkdir()
    (over_dir / "SKILL.md").write_text(at_cap + "e")
    try:
        SkillParser.parse(over_dir / "SKILL.md")
    except ValueError as exc:
        assert "maximum size" in str(exc)
    else:
        raise AssertionError("over-cap skill parsed")

    # A symlink to a huge target must be rejected without being read.
    target = ROOT / "huge-target.md"
    with target.open("wb") as handle:
        handle.truncate(SkillParser.MAX_CONTENT_SIZE * 8)
    link_dir = ROOT / "link-skill"
    link_dir.mkdir()
    (link_dir / "SKILL.md").symlink_to(target)
    reads = []
    real_read_text = Path.read_text

    def spy(path, *args, **kwargs):
        reads.append(path)
        return real_read_text(path, *args, **kwargs)

    Path.read_text = spy
    try:
        SkillParser.parse(link_dir / "SKILL.md")
    except ValueError:
        pass
    else:
        raise AssertionError("symlinked huge skill parsed")
    finally:
        Path.read_text = real_read_text
    assert reads == [], f"huge target was read: {reads}"


check("(c1) parse: boundary accepted; cap+1 rejected; symlinked 8x cap never read")(parse_cap_case)


# ── (c2) soulspec 256 KB pre-stat + 64-reference count cap ───────────────────


def _write_package(root: Path, extra: int) -> None:
    spec = SoulSpec(
        name="probe-persona",
        persona=SoulSpec.Persona(name="Probe", description="Probe", system_prompt="You are."),
    )
    spec.write_package(root, author="Probe")
    manifest = json.loads((root / "soul.json").read_text(encoding="utf-8"))
    for index in range(extra):
        name = f"doc{index:02d}.md"
        (root / name).write_text("ok", encoding="utf-8")
        manifest["files"][f"doc{index:02d}"] = name
    (root / "soul.json").write_text(json.dumps(manifest), encoding="utf-8")


def soulspec_case():
    at_cap_dir = ROOT / "soulspec-at-cap"
    _write_package(at_cap_dir, 63)  # soul + 63 = 64 references
    assert SoulSpec.from_package(at_cap_dir).name == "probe-persona"
    over_dir = ROOT / "soulspec-over"
    _write_package(over_dir, 64)  # 65 references
    try:
        SoulSpec.from_package(over_dir)
    except ValueError as exc:
        assert "too many" in str(exc)
    else:
        raise AssertionError("65-reference package imported")

    links_dir = ROOT / "soulspec-links"
    _write_package(links_dir, 0)
    huge = links_dir / "HUGE.md"
    with huge.open("wb") as handle:
        handle.truncate(256 * 1024 + 1)
    (links_dir / "LINKED.md").symlink_to(huge)
    manifest = json.loads((links_dir / "soul.json").read_text(encoding="utf-8"))
    manifest["files"]["linked"] = "LINKED.md"
    (links_dir / "soul.json").write_text(json.dumps(manifest), encoding="utf-8")
    reads = []
    real_read_bytes = Path.read_bytes

    def spy(path):
        reads.append(path)
        return real_read_bytes(path)

    Path.read_bytes = spy
    try:
        SoulSpec.from_package(links_dir)
    except ValueError as exc:
        assert "size" in str(exc)
    else:
        raise AssertionError("symlinked oversized package file imported")
    finally:
        Path.read_bytes = real_read_bytes
    assert huge not in reads, "oversized package file was read"


check("(c2) soulspec: 64 refs ok / 65 rejected; oversized symlink never read")(soulspec_case)


# ── (d) Windows fsync degradation is platform-conditional ────────────────────


def windows_fsync_case():
    real_open = os.open
    real_supported = repo_install._DIR_FSYNC_SUPPORTED

    def failing_open(path, flags, *args, **kwargs):
        raise PermissionError("directories cannot be opened as file descriptors")

    try:
        repo_install._DIR_FSYNC_SUPPORTED = False
        repo_install.os.open = failing_open
        repo_install._fsync_dir(ROOT / "delete-skills")  # must return silently
        repo_install._DIR_FSYNC_SUPPORTED = True
        try:
            repo_install._fsync_dir(ROOT / "delete-skills")
        except PermissionError:
            pass
        else:
            raise AssertionError("POSIX strict fsync contract lost")
    finally:
        repo_install.os.open = real_open
        repo_install._DIR_FSYNC_SUPPORTED = real_supported


check("(d) fsync: best-effort on Windows branch; still strict on POSIX branch")(windows_fsync_case)


# ── (e) repo URL ports raise the typed error ─────────────────────────────────


def url_case():
    for url in (
        "https://github.com:99999/o/r",
        "https://github.com:abc/o/r",
        "https://github.com:8443/o/r",
    ):
        try:
            parse_repo_url(url)
        except RepoInstallError:
            pass
        else:
            raise AssertionError(f"bad port accepted: {url}")
    good = parse_repo_url("https://github.com/o/r")
    assert good.owner == "o" and good.repo == "r"


check("(e) url: :99999/:abc/:8443 → RepoInstallError; plain link accepted")(url_case)


# ── (f) classifier gate keeps real tech tasks activating ─────────────────────


def workflow_case():
    tmp_skills = ROOT / "workflow-skills"
    for query in ("go ahead", "go on", "go", "rust", "swift", "and then go ahead", "rust vs go"):
        plan = plan_workflow(query, skills=SkillRegistry(tmp_skills))
        assert plan.kind != "coding", f"{query!r} -> {plan.kind}"
    for query in ("write a Go server", "build a Rust CLI", "implement a Swift app"):
        plan = plan_workflow(query, skills=SkillRegistry(tmp_skills))
        assert plan.kind == "coding", f"{query!r} -> {plan.kind}"
    history = [{"chunk_type": "user_message", "payload": {"content": "Build a Go server"}}]
    assert (
        plan_workflow("continue", skills=SkillRegistry(tmp_skills), recent=history).kind == "coding"
    )


check("(f) workflow: bare go/rust/swift gated; real tech tasks and inherited stack activate")(
    workflow_case
)


# ── (b2) registry.delete_skill honors the same contract end-to-end ───────────


def registry_delete_case():
    skills_dir = ROOT / "registry-skills"
    payload = SKILL_MD.replace("name: alpha", "name: alpha-reg")
    digest = hashlib.sha256(payload.encode()).hexdigest()
    skill_dir = skills_dir / "alpha-reg"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(payload)
    (skill_dir / "scripts" / "run.py").write_text("print('a')\n")
    (skill_dir / ".install-manifest.json").write_text(
        json.dumps({"files": {"alpha-reg/SKILL.md": digest, "alpha-reg/scripts/run.py": digest}}),
        encoding="utf-8",
    )
    registry = SkillRegistry(skills_dir=skills_dir)
    registry.load_all()
    assert registry.get("alpha-reg") is not None
    assert registry.delete_skill("alpha-reg") is True
    assert not (skills_dir / "alpha-reg").exists()
    # A plain learned skill still deletes through the ordinary path.
    registry.create_skill("beta-reg", "Beta", "Body")
    assert registry.delete_skill("beta-reg") is True
    assert not (skills_dir / "beta-reg").exists()


check("(b2) registry.delete_skill: manifest tree removed; plain skill unchanged path")(
    registry_delete_case
)


if failures:
    print(f"\n{len(failures)} case(s) failed: {', '.join(failures)}")
    sys.exit(1)
print("\nall probe cases passed")
