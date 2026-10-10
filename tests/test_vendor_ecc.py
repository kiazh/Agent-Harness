"""The importer ships relevant skills and dependencies, not a second agent platform."""

import hashlib
import json
import subprocess

import pytest

from tools import vendor_ecc


def source_repo(tmp_path, monkeypatch):
    source = tmp_path / "upstream"
    source.mkdir()
    files = {
        "LICENSE": "MIT fixture notice\n",
        "skills/python-patterns/SKILL.md": (
            "---\nname: python-patterns\ndescription: Python patterns\n---\n"
            "Read [guide](../../docs/guide.md), `scripts/check.py`, and `scripts/run.js`.\n"
        ),
        "skills/python-patterns/reference.txt": "skill local resource\n",
        "skills/visa-doc-translate/SKILL.md": (
            "---\nname: visa-doc-translate\ndescription: Visa translation\n---\nTranslate\n"
        ),
        "docs/guide.md": "Read [nested](details/nested.md).\n",
        "docs/details/nested.md": "Shared guidance\n",
        "scripts/check.py": "from .helper import check\n",
        "scripts/helper.py": "def check(): return True\n",
        "scripts/run.js": "require('./lib');\n",
        "scripts/lib/index.js": "require('./helper');\n",
        "scripts/lib/helper.js": "module.exports = true;\n",
        "tests/unrelated.py": "# upstream test\n",
        "assets/promo.png": "unused promotional image\n",
    }
    for name, content in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=source,
        check=True,
    )
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    monkeypatch.setattr(vendor_ecc, "PIN", revision)
    return source, files


def test_importer_curates_skills_and_preserves_transitive_resources(tmp_path, monkeypatch):
    source, files = source_repo(tmp_path, monkeypatch)
    target = tmp_path / "bundle"
    vendor_ecc.main(source, target)
    manifest = json.loads((target / "manifest.json").read_text())
    assert [s["name"] for s in manifest["skills"]] == ["ecc-python-patterns"]
    assert set(manifest["files"]) == {
        "LICENSE",
        "skills/python-patterns/SKILL.md",
        "skills/python-patterns/reference.txt",
        "docs/guide.md",
        "docs/details/nested.md",
        "scripts/check.py",
        "scripts/helper.py",
        "scripts/run.js",
        "scripts/lib/index.js",
        "scripts/lib/helper.js",
    }
    for name, digest in manifest["files"].items():
        assert (target / name).read_text() == files[name]
        assert hashlib.sha256((target / name).read_bytes()).hexdigest() == digest
    first = (target / "manifest.json").read_bytes()
    vendor_ecc.main(source, target)
    assert (target / "manifest.json").read_bytes() == first


def test_reimport_refuses_to_delete_modified_previous_vendor_file(tmp_path, monkeypatch):
    source, _ = source_repo(tmp_path, monkeypatch)
    target = tmp_path / "bundle"
    vendor_ecc.main(source, target)
    extra = target / "assets/promo.png"
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_text("original")
    manifest = json.loads((target / "manifest.json").read_text())
    manifest["files"]["assets/promo.png"] = hashlib.sha256(b"original").hexdigest()
    (target / "manifest.json").write_text(json.dumps(manifest))
    extra.write_text("local edit")
    with pytest.raises(ValueError, match="modified"):
        vendor_ecc.main(source, target)
    assert extra.read_text() == "local edit"


def test_reimport_prunes_only_manifest_owned_files(tmp_path, monkeypatch):
    source, _ = source_repo(tmp_path, monkeypatch)
    target = tmp_path / "bundle"
    vendor_ecc.main(source, target)
    extra = target / "obsolete.txt"
    extra.write_text("original")
    local = target / "local-note.txt"
    local.write_text("keep my note")
    manifest = json.loads((target / "manifest.json").read_text())
    manifest["files"]["obsolete.txt"] = hashlib.sha256(b"original").hexdigest()
    (target / "manifest.json").write_text(json.dumps(manifest))
    vendor_ecc.main(source, target)
    assert not extra.exists()
    assert local.read_text() == "keep my note"
