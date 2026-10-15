"""RED-first regression tests for the 2026-10-15 final-review FIX LANE.

Each test pins one confirmed finding fixed by this lane; the evidence doc is
``docs/verification/proofs/final_review_2026-10-15/fix_misc_small.md``:

* security_review S2 — the MCP mode ceiling clamps an omitted ``mode``
  (fail-closed) instead of letting the child inherit the operator mode.
* line_infra_docs F1 — ``e2e_gate.py`` treats a blank ``--db-url`` as absent
  in both the non-test-database guard and the export path.
* security_review S5 / adversarial F3 — the skill-rewrite temp file is
  unpredictable and created ``O_EXCL`` (a planted symlink is never followed).
* code_review P2-2 — ``security.providers._store_lock`` delegates to
  ``security.store_fence.store_lock`` (one bounded-flock implementation).
* follow-up F14 — the bounded runner starts supervision/output draining before
  delivering stdin, so a large input cannot block indefinitely.
* line_rag_tools_skills P3 — the in-memory skill map has one canonical
  (casefolded) key, so an uppercase local skill cannot shadow/duplicate a
  differently-cased builtin.
"""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

SESSION_ID = "123e4567-e89b-42d3-a456-426614174000"
_OK_OUTPUT = f"New session: {SESSION_ID}\n\nHELLO-TOKEN\n\nSession ID: {SESSION_ID}\n"


# ---------------------------------------------------------------------------
# S2 — MCP mode ceiling clamps an omitted mode
# ---------------------------------------------------------------------------


def _capture_runner(capture: dict):
    from machina.mcp_server import ProcessResult

    def run(cmd, *, timeout, env_extra=None, cancel_event=None):
        capture["cmd"] = cmd
        capture["env"] = env_extra
        return ProcessResult(0, _OK_OUTPUT, "", False, False)

    return run


async def test_mcp_none_mode_clamped_to_tightened_ceiling(monkeypatch):
    """full operator + MCP_MAX_MODE=ask: an omitted mode must spawn --mode ask."""
    from machina import mcp_server

    monkeypatch.setenv("MACHINA_EXECUTION_MODE", "full")
    monkeypatch.setenv("MCP_MAX_MODE", "ask")
    capture: dict = {}
    monkeypatch.setattr(mcp_server, "_run_process", _capture_runner(capture))
    payload = json.loads(await mcp_server.machina_chat("hi", mode=None))
    assert payload["status"] == "completed"
    cmd = capture["cmd"]
    assert "--mode" in cmd
    assert cmd[cmd.index("--mode") + 1] == "ask"
    assert capture["env"]["MACHINA_EXECUTION_MODE"] == "ask"
    assert capture["env"]["MACHINA_MCP_MODE_CEILING"] == "ask"


async def test_mcp_none_mode_without_tightening_inherits_operator_default(monkeypatch):
    """Ceiling == operator mode: no flag is added (requested behavior unchanged)."""
    from machina import mcp_server

    monkeypatch.setenv("MACHINA_EXECUTION_MODE", "ask")
    monkeypatch.delenv("MCP_MAX_MODE", raising=False)
    capture: dict = {}
    monkeypatch.setattr(mcp_server, "_run_process", _capture_runner(capture))
    payload = json.loads(await mcp_server.machina_chat("hi", mode=None))
    assert payload["status"] == "completed"
    assert "--mode" not in capture["cmd"]


# ---------------------------------------------------------------------------
# F1 — a blank --db-url is not consent for a non-test database
# ---------------------------------------------------------------------------


def test_e2e_gate_blank_db_url_is_not_consent(tmp_path, monkeypatch, capsys):
    import tools.e2e_gate as gate

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/prod")
    monkeypatch.delenv("MACHINA_DATABASE_URL", raising=False)
    code = gate.main(["--db-url=", "--venv", str(tmp_path / "nope"), "--journeys", "j1"])
    err = capsys.readouterr().err
    assert code == 2
    assert "refusing to run against non-test database" in err


def test_e2e_gate_blank_db_url_with_test_db_still_proceeds(tmp_path, monkeypatch, capsys):
    import tools.e2e_gate as gate

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/machina_test")
    monkeypatch.delenv("MACHINA_DATABASE_URL", raising=False)
    code = gate.main(["--db-url=", "--venv", str(tmp_path / "nope"), "--journeys", "j1"])
    err = capsys.readouterr().err
    assert code == 2
    assert "refusing to run against non-test database" not in err
    assert "venv python not found" in err


# ---------------------------------------------------------------------------
# S5 / F3 — the skill-rewrite temp file never follows a planted symlink
# ---------------------------------------------------------------------------


def test_write_skill_file_ignores_a_planted_symlink_temp(tmp_path):
    from machina.skills.registry import SkillRegistry

    registry = SkillRegistry(tmp_path)
    registry.create_skill("deploy", "Deploy steps", "Steps to deploy safely")
    skill_file = Path(registry.get("deploy").file_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("ORIGINAL", encoding="utf-8")
    # The pre-fix temp name was predictable: <SKILL.md>.<pid>.tmp.
    planted = skill_file.with_name(f"{skill_file.name}.{os.getpid()}.tmp")
    planted.symlink_to(outside)

    registry.update_skill("deploy", content="NEW BODY")

    assert outside.read_text(encoding="utf-8") == "ORIGINAL"
    assert "NEW BODY" in skill_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# P2-2 — provider store lock delegates to store_fence.store_lock
# ---------------------------------------------------------------------------


def test_provider_store_lock_delegates_to_store_fence(tmp_path, monkeypatch):
    from machina.security import providers as store
    from machina.security import store_fence

    monkeypatch.setenv("MACHINA_PROVIDERS_FILE", str(tmp_path / "providers.json"))
    calls: list[str] = []
    real_lock = store_fence.store_lock

    def spy(target):
        calls.append(str(target))
        return real_lock(target)

    monkeypatch.setattr(store_fence, "store_lock", spy)
    store.add_provider("acme", "http://127.0.0.1:9000/v1")
    assert calls and calls[0].endswith("providers.json")


def test_provider_store_lock_translates_store_fence_timeout(tmp_path, monkeypatch):
    from machina.security import providers as store
    from machina.security import store_fence

    monkeypatch.setenv("MACHINA_PROVIDERS_FILE", str(tmp_path / "providers.json"))

    @contextmanager
    def contended(target):
        raise store_fence.StoreLockTimeout("lock held by a peer")
        yield  # pragma: no cover - unreachable

    monkeypatch.setattr(store_fence, "store_lock", contended)
    with pytest.raises(store.ProviderStoreLockTimeout):
        store.add_provider("acme", "http://127.0.0.1:9000/v1")


# ---------------------------------------------------------------------------
# F14 — bounded runner supervises before delivering stdin
# ---------------------------------------------------------------------------


def test_run_bounded_large_stdin_is_killed_at_the_deadline():
    """A child that never reads stdin must still be bounded by the deadline."""
    from machina.core.child_process import run_bounded

    started = time.monotonic()
    result = run_bounded(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        timeout=1.0,
        stdin_text="x" * 1_000_000,
    )
    elapsed = time.monotonic() - started
    assert result.timed_out is True
    assert elapsed < 3.0


def test_run_bounded_delivers_large_stdin_when_the_child_reads():
    from machina.core.child_process import run_bounded

    payload = "x" * 500_000
    result = run_bounded(
        [sys.executable, "-c", "import sys; sys.stdout.write(str(len(sys.stdin.read())))"],
        timeout=30.0,
        stdin_text=payload,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == str(len(payload))


# ---------------------------------------------------------------------------
# P3 — one canonical (casefolded) key for the in-memory skill map
# ---------------------------------------------------------------------------


def test_registry_lookup_is_case_insensitive(tmp_path):
    from machina.skills.registry import SkillRegistry

    registry = SkillRegistry(tmp_path)
    registry.create_skill("Deploy", "Deploy steps", "Steps to deploy safely")
    assert registry.get("DEPLOY") is not None
    assert registry.get("deploy") is not None
    assert registry.get_skill_content("DePlOy") is not None


def test_uppercase_local_skill_cannot_duplicate_a_builtin(tmp_path):
    from machina.skills.registry import Skill, SkillRegistry

    class FakeBundle:
        def skills(self):
            return [
                Skill(
                    name="Foo",
                    description="builtin",
                    triggers=[],
                    content="",
                    file_path="builtin",
                    source_type="builtin-ecc",
                )
            ]

    registry = SkillRegistry(tmp_path / "skills")
    registry._include_ecc = True
    registry._ecc_bundle = FakeBundle()
    skill_dir = registry.skills_dir / "FOO"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: FOO\ndescription: local\n---\nlocal body", encoding="utf-8"
    )

    registry.load_all(refresh=True)

    assert sorted(skill.name for skill in registry.list_skills()) == ["Foo"]
    assert registry.get("FOO").source_type == "builtin-ecc"
