"""Tests for the unified offline benchmark (machina/research/benchmark.py).

The benchmark is the reporting surface for the incident-remediation program:
these tests pin that its sections run headlessly, that the LoCoMo section is
honest (real metrics with a dataset, explicit SKIP without one), and that the
comparisons block keeps carrying the incident constants + cross-tool protocol.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from machina.research import benchmark, identity_eval, workflow_benchmark
from machina.research.locomo import (
    Conversation,
    DialogueTurn,
    Question,
    evaluate_session_recall,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

_MINI_LOCOMO = [
    {
        "conversation": {
            "session_1": [
                {"dia_id": "D1:1", "speaker": "Ada", "text": "Ada moved to Toronto in March."},
                {"dia_id": "D1:2", "speaker": "Bob", "text": "How is the new apartment?"},
            ],
        },
        "qa": [
            {
                "question": "Where did Ada move to?",
                "answer": "Toronto",
                "evidence": ["D1:1"],
                "category": 1,
            },
        ],
    }
]


def test_core_sections_run_and_write_json(tmp_path):
    out = tmp_path / "bench.json"
    rc = benchmark.main(
        [
            "--section",
            "environment,replay,retrieval,prefix,architecture",
            "--json",
            str(out),
        ]
    )
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))

    assert data["environment"]["version"]
    # Replay: the fixed loop writes its artifact and needs far fewer recalls
    # than the 25 in the incident.
    assert data["replay"]["artifact_written"] is True
    assert data["replay"]["recall_calls"] < benchmark._INCIDENT["post_ask_recalls"]
    # Retrieval: faithfulness gate met (baseline before the program was 0.0).
    assert data["retrieval"]["memory"]["faithfulness"] == 1.0
    # Prefix: shares a real resend share.
    assert 0 < data["prefix"]["identical_prefix_resent_pct"] < 100
    # Architecture: census inside its ratchet.
    arch = data["architecture"]
    assert arch["deferred_imports"] <= arch["ratchet_cap"]
    assert "comparisons" in data


def test_locomo_section_scores_a_fixture(tmp_path):
    fixture = tmp_path / "locomo-mini.json"
    fixture.write_text(json.dumps(_MINI_LOCOMO), encoding="utf-8")
    out = tmp_path / "bench.json"

    rc = benchmark.main(["--section", "locomo", "--locomo-data", str(fixture), "--json", str(out)])
    assert rc == 0

    row = json.loads(out.read_text(encoding="utf-8"))["locomo"]
    assert row["status"] == "OK"
    assert row["conversations"] == 1
    assert row["questions"] == 1
    # The evidence turn shares "Ada" and "to" with the question, so the
    # lexical baseline must rank it first (the other turn shares nothing).
    k5 = row["baseline_tfidf"]["k=5"]
    assert k5["evidence_recall"] == 1.0
    assert k5["hit_rate"] == 1.0
    assert k5["mrr"] == 1.0


def test_locomo_section_skips_honestly_without_dataset(tmp_path, monkeypatch):
    monkeypatch.delenv("MACHINA_LOCOMO_DATA", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    out = tmp_path / "bench.json"

    rc = benchmark.main(
        ["--section", "locomo", "--locomo-data", str(tmp_path / "absent.json"), "--json", str(out)]
    )
    assert rc == 0

    row = json.loads(out.read_text(encoding="utf-8"))["locomo"]
    assert row["status"] == "SKIPPED"
    assert "locomo10.json" in row["how"]


def test_locomo_path_prefers_explicit_argument(tmp_path, monkeypatch):
    explicit = tmp_path / "locomo10.json"
    explicit.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("MACHINA_LOCOMO_DATA", "/nonexistent/env-path.json")

    assert benchmark._locomo_path(str(explicit)) == explicit


def test_comparisons_keep_incident_constants_and_protocol():
    comparisons = benchmark._comparisons({})

    assert "2,119,552" in comparisons["incident_vs_now"]["incident"]
    protocol = comparisons["cross_tool_vs_opencode"]
    assert protocol["status"].startswith("PROTOCOL")
    assert "how_to_fill" in protocol


# ---------------------------------------------------------------------------
# R4-092 (a) — benchmark subprocess timeouts + non-zero exit on errors
# ---------------------------------------------------------------------------


def test_section_failure_exits_nonzero(tmp_path, monkeypatch):
    """A broken section must fail the run, not print a green "done.".

    RED before R4-092a: ``main()`` always returned 0.
    """

    def explode():
        raise RuntimeError("section exploded")

    monkeypatch.setattr(benchmark, "_environment", explode)
    out = tmp_path / "bench.json"

    rc = benchmark.main(["--section", "environment", "--json", str(out)])

    assert rc == 1
    payload = json.loads(out.read_text(encoding="utf-8"))["environment"]
    assert "RuntimeError: section exploded" in payload["error"]


def test_suite_failure_exits_nonzero(tmp_path, monkeypatch):
    """A failing --with-suite row must fail the benchmark run too."""
    monkeypatch.setattr(
        benchmark,
        "_suite",
        lambda: {"summary": "1 failed, 2 passed", "wall_seconds": 0.1, "exit_code": 1},
    )

    rc = benchmark.main(
        ["--section", "environment", "--with-suite", "--json", str(tmp_path / "bench.json")]
    )

    assert rc == 1


def test_subprocess_sections_have_timeouts(monkeypatch):
    """The suite/UI subprocesses must be bounded (they could hang forever).

    RED before R4-092a: neither ``subprocess.run`` call passed a timeout.
    """
    calls: list[dict] = []

    def fake_run(command, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert benchmark._suite()["exit_code"] == 0
    assert benchmark._ui()["exit_code"] == 0
    assert len(calls) == 2
    for kwargs in calls:
        assert kwargs.get("timeout", 0) > 0


def test_suite_timeout_is_reported_as_an_error(monkeypatch, tmp_path):
    """A timed-out subprocess section is an error, never a silent empty row."""

    def timeout_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", timeout_run)
    payload = benchmark._suite()

    assert "timed out" in payload["error"]
    assert payload["exit_code"] != 0

    monkeypatch.setattr(benchmark, "_suite", lambda: payload)
    rc = benchmark.main(
        ["--section", "environment", "--with-suite", "--json", str(tmp_path / "bench.json")]
    )
    assert rc == 1


# ---------------------------------------------------------------------------
# R4-092 (b,c) — workflow_benchmark measurement + cleanup hardening
# ---------------------------------------------------------------------------


def test_workflow_benchmark_has_no_side_effects_inside_asserts():
    """RED before R4-092c: ``assert await ...`` is stripped by ``python -O``,
    silently skipping the measured calls (fabricated timings)."""
    tree = ast.parse(Path(workflow_benchmark.__file__).read_text(encoding="utf-8"))
    offenders: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            offenders.extend(
                child.lineno for child in ast.walk(node.test) if isinstance(child, ast.Await)
            )
    assert not offenders, f"awaits inside assert are skipped under -O: lines {offenders}"


_OPTIMIZED_PROBE = """
import asyncio
from machina.research import workflow_benchmark as wb

calls = []

class FakeDatabase:
    def __init__(self, dsn=None):
        pass
    async def connect(self):
        pass
    async def execute(self, sql, *args):
        pass
    async def close(self):
        pass

class FakeNative:
    def __init__(self, database):
        pass
    async def setup(self):
        pass
    async def start(self, *args):
        calls.append("native-start")
        return "pending"
    async def resume(self, *args):
        calls.append("native-resume")
        return "complete"

class FakeGraph:
    def __init__(self, database, dsn):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *exc):
        pass
    async def start(self, *args):
        calls.append("graph-start")
        return "pending"
    async def resume(self, *args):
        calls.append("graph-resume")
        return "complete"
    async def checkpoint_count(self, run_ids):
        return 0
    async def delete_thread(self, run_id):
        pass

wb.Database = FakeDatabase
wb.NativeApprovalTrial = FakeNative
wb.GraphApprovalTrial = FakeGraph
wb.version = lambda name: "test"
asyncio.run(wb.compare("postgresql://unused", 1))
print(f"starts={sum(1 for c in calls if c.endswith('start'))}")
print(f"resumes={sum(1 for c in calls if c.endswith('resume'))}")
"""


def test_compare_measures_steps_under_python_optimized_mode(tmp_path):
    """RED before R4-092c: under ``-O`` the measured awaits never ran."""
    probe = tmp_path / "optimized_probe.py"
    probe.write_text(_OPTIMIZED_PROBE, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH", "")]))

    out = subprocess.run(
        [sys.executable, "-O", str(probe)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )

    assert out.returncode == 0, out.stderr
    assert "starts=2" in out.stdout
    assert "resumes=2" in out.stdout


def _install_workflow_fakes(
    monkeypatch,
    calls: list[tuple[str, str, object]],
    databases: list,
    *,
    start_error: Exception | None = None,
    fail_deletes: tuple[str, ...] = (),
) -> None:
    """Install a fake Database + trial backends to drive ``compare`` offline."""

    class FakeDatabase:
        def __init__(self, dsn=None):
            self.dsn = dsn
            self.statements: list[str] = []
            self.closed = False
            databases.append(self)

        async def connect(self):
            return None

        async def execute(self, sql, *args):
            self.statements.append(sql)
            if fail_deletes and sql.strip().startswith(fail_deletes):
                raise RuntimeError(f"delete failed: {sql.strip()[:40]}")

        async def close(self):
            self.closed = True

    class FakeNative:
        def __init__(self, database):
            self.database = database

        async def setup(self):
            return None

        async def start(self, run_id, session_id, agent_id, action):
            calls.append(("native", "start", run_id))
            if start_error is not None:
                raise start_error
            return "pending"

        async def resume(self, run_id, agent_id, decision):
            calls.append(("native", "resume", run_id))
            return "complete"

    class FakeGraph:
        def __init__(self, database, dsn):
            self.database = database
            self.dsn = dsn

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return None

        async def start(self, run_id, session_id, agent_id, action):
            calls.append(("graph", "start", run_id))
            return "pending"

        async def resume(self, run_id, agent_id, decision):
            calls.append(("graph", "resume", run_id))
            return "complete"

        async def checkpoint_count(self, run_ids):
            return 3

        async def delete_thread(self, run_id):
            calls.append(("graph", "delete_thread", run_id))

    monkeypatch.setattr(workflow_benchmark, "Database", FakeDatabase)
    monkeypatch.setattr(workflow_benchmark, "NativeApprovalTrial", FakeNative)
    monkeypatch.setattr(workflow_benchmark, "GraphApprovalTrial", FakeGraph)
    monkeypatch.setattr(workflow_benchmark, "version", lambda name: "test")


async def test_compare_runs_the_measured_steps_each_iteration(monkeypatch):
    calls: list[tuple[str, str, object]] = []
    databases: list = []
    _install_workflow_fakes(monkeypatch, calls, databases)

    result = await workflow_benchmark.compare("postgresql://unused", iterations=2)

    phases = [(backend, phase) for backend, phase, _ in calls]
    assert phases.count(("native", "start")) == 2
    assert phases.count(("native", "resume")) == 2
    assert phases.count(("graph", "start")) == 2
    assert phases.count(("graph", "resume")) == 2
    assert result["graphCheckpointRows"] == 3
    assert any(s.startswith("DELETE FROM sessions") for s in databases[-1].statements)
    assert databases[-1].closed is True


async def test_compare_cleanup_attempts_every_step_when_a_delete_fails(monkeypatch):
    """RED before R4-092b: the first failing delete aborted the remaining
    cleanup steps (runs/session deletes and the pool close were skipped)."""
    calls: list[tuple[str, str, object]] = []
    databases: list = []
    _install_workflow_fakes(
        monkeypatch,
        calls,
        databases,
        fail_deletes=("DELETE FROM workflow_trial_effects",),
    )

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await workflow_benchmark.compare("postgresql://unused", iterations=1)

    database = databases[-1]
    assert any(s.startswith("DELETE FROM workflow_trial_effects") for s in database.statements)
    assert any(s.startswith("DELETE FROM workflow_trial_runs") for s in database.statements)
    assert any(s.startswith("DELETE FROM sessions") for s in database.statements)
    assert database.closed is True


async def test_compare_cleanup_failure_does_not_mask_the_trial_error(monkeypatch):
    """RED before R4-092b: the cleanup RuntimeError masked the trial error."""
    calls: list[tuple[str, str, object]] = []
    databases: list = []
    _install_workflow_fakes(
        monkeypatch,
        calls,
        databases,
        start_error=LookupError("trial exploded"),
        fail_deletes=("DELETE FROM workflow_trial_effects",),
    )

    with pytest.raises(LookupError, match="trial exploded") as excinfo:
        await workflow_benchmark.compare("postgresql://unused", iterations=1)

    database = databases[-1]
    assert database.closed is True
    assert any(s.startswith("DELETE FROM sessions") for s in database.statements)
    assert "cleanup failed" in " ".join(getattr(excinfo.value, "__notes__", []))


# ---------------------------------------------------------------------------
# R4-092 (b) — locomo session-recall cleanup isolation
# ---------------------------------------------------------------------------


def _fake_recall_modules(monkeypatch, session_manager):
    import machina.core.context as context_module
    import machina.core.session as session_module
    import machina.core.session_recall as recall_module

    class FakeContextManager:
        async def archive_chunk(self, *args, **kwargs):
            return None

    class FakeSessionRecall:
        async def discover(self, agent_id, question, limit=5):
            return []

    monkeypatch.setattr(session_module, "session_manager", session_manager)
    monkeypatch.setattr(context_module, "context_manager", FakeContextManager())
    monkeypatch.setattr(recall_module, "SessionRecall", FakeSessionRecall)


def _recall_dataset() -> list[Conversation]:
    return [
        Conversation(
            turns=(DialogueTurn("D1:1", "Ada", "hello"), DialogueTurn("D1:2", "Ben", "hi")),
            questions=(Question("Who greeted?", ("D1:1",)),),
        )
    ]


async def test_session_recall_cleanup_isolates_delete_failures(monkeypatch):
    """RED before R4-092b: one failed delete skipped the remaining sessions."""
    deleted: list[uuid.UUID] = []

    class FakeSessionManager:
        async def create(self, title, agent_id=None):
            return SimpleNamespace(id=uuid.uuid4())

        async def delete(self, session_id):
            deleted.append(session_id)
            if len(deleted) == 1:
                raise RuntimeError("session delete exploded")

    _fake_recall_modules(monkeypatch, FakeSessionManager())

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await evaluate_session_recall(_recall_dataset(), k=1, turns_per_session=1)

    assert len(deleted) == 2


async def test_session_recall_cleanup_failure_does_not_mask_the_original_error(monkeypatch):
    """A cleanup failure must never replace the error that caused it."""
    deleted: list[uuid.UUID] = []

    class FakeSessionManager:
        def __init__(self):
            self.created = 0

        async def create(self, title, agent_id=None):
            self.created += 1
            if self.created == 2:
                raise LookupError("second session exploded")
            return SimpleNamespace(id=uuid.uuid4())

        async def delete(self, session_id):
            deleted.append(session_id)
            raise RuntimeError("session delete exploded")

    _fake_recall_modules(monkeypatch, FakeSessionManager())

    with pytest.raises(LookupError, match="second session exploded") as excinfo:
        await evaluate_session_recall(_recall_dataset(), k=1, turns_per_session=1)

    assert len(deleted) == 1
    assert "cleanup failed" in " ".join(getattr(excinfo.value, "__notes__", []))


# ---------------------------------------------------------------------------
# R4-092 (b) — identity-eval cleanup isolation
# ---------------------------------------------------------------------------


def _identity_scenarios() -> list[dict]:
    return [
        {
            "baseline": {},
            "steps": [{"proposed": {}, "injected": False, "content": "hello"}],
        }
    ]


async def test_identity_cleanup_attempts_every_delete(monkeypatch):
    """RED before R4-092b: the first failing delete skipped the rest."""
    statements: list[str] = []

    class FakeGate:
        async def save_beliefs(self, belief):
            return None

        async def observe_transition(self, belief, causal_memory_ids=None, threshold=0.5):
            return SimpleNamespace(quarantined_count=0, accepted=True)

    class FakeDatabase:
        async def execute(self, sql, *args):
            statements.append(sql)
            if len(statements) == 1:
                raise RuntimeError("cleanup exploded")

        async def fetchval(self, sql, *args):
            return 1

    monkeypatch.setattr(identity_eval, "IdentityGate", FakeGate)
    monkeypatch.setattr(identity_eval, "db", FakeDatabase())

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await identity_eval.evaluate_identity_scenarios(_identity_scenarios())

    assert len(statements) == 6


async def test_identity_cleanup_failure_does_not_mask_the_original_error(monkeypatch):
    """A cleanup failure must never replace the error that caused it."""
    statements: list[str] = []

    class ExplodingGate:
        async def save_beliefs(self, belief):
            raise LookupError("gate exploded")

    class FakeDatabase:
        async def execute(self, sql, *args):
            statements.append(sql)
            raise RuntimeError("cleanup exploded")

        async def fetchval(self, sql, *args):
            return 1

    monkeypatch.setattr(identity_eval, "IdentityGate", ExplodingGate)
    monkeypatch.setattr(identity_eval, "db", FakeDatabase())

    with pytest.raises(LookupError, match="gate exploded") as excinfo:
        await identity_eval.evaluate_identity_scenarios(_identity_scenarios())

    assert len(statements) == 6
    assert "cleanup failed" in " ".join(getattr(excinfo.value, "__notes__", []))
