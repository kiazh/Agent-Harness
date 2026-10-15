"""Final-review (2026-10-15) RED-first proofs for research-metric honesty.

Three confirmed findings from
``docs/verification/CODEBASE_REVIEW_FOLLOWUP_2026-10-15.md`` (F11) and
``docs/verification/proofs/final_review_2026-10-15/line_rag_tools_skills.md``
(P2-1, P3-3):

* **F11** — the title-only session-recall baseline filtered hits to
  ``source == 'title'`` and then mapped them through a ``chunk_id -> dia_id``
  table. Title hits always carry ``chunk_id=None`` (``session_recall.py``), so
  every title-only ranking was structurally empty: the reported recall/MRR was
  a hard zero regardless of whether a title matched. Session titles were also
  the single generic constant ``"LoCoMo transcript"``.
* **P2-1** — ``head_to_head.run_one`` derived ``success`` only from the graded
  pytest run and the fixture-integrity diff, ignoring ``harness_exit_code`` and
  ``result['error']``. A crashed harness that never ran still scored
  ``success=True`` on a green-suite task.
* **P3-3** — ``identity_eval`` computed ``attack_rejection_rate=0.0`` when a
  scenario set contained no injected transitions, indistinguishable from "every
  attack was accepted".

RED-first: against unmodified ``HEAD`` the assertions below fail
(title-only scores zero; a crashed harness scores success; the undefined rate is
``0.0``). They pass once the fixes land, and the two guard tests
(``..._still_reports_rate``, ``..._unmatched_title``) pass before and after.
"""

from __future__ import annotations

import os
import sys
from argparse import Namespace
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
# Plain ``import head_to_head`` exactly like the adversarial/symlink proofs, so
# a pre-fix copy can be shadowed via PYTHONPATH for a manual RED run.
sys.path.append(str(_REPO_ROOT / "machina" / "research"))
import head_to_head  # noqa: E402

from machina.research.identity_eval import evaluate_identity_scenarios  # noqa: E402
from machina.research.locomo import (  # noqa: E402
    Conversation,
    DialogueTurn,
    Question,
    evaluate_session_recall,
)

# ---------------------------------------------------------------------------
# P2-1: head-to-head success must require a clean harness exit and no error
# ---------------------------------------------------------------------------

_CALC_FIXED = "def add(a: int, b: int) -> int:\n    return a + b\n"
_GRADED_TEST = "from calc import add\n\n\ndef test_add() -> None:\n    assert add(2, 3) == 5\n"
_ROOT_CONFTEST = "# Root conftest: puts the project root on sys.path.\n"


def _green_fixture(task_dir: Path) -> str:
    """A fixture whose graded test already passes (the crash-prone shape)."""
    task_dir.mkdir(parents=True)
    (task_dir / "calc.py").write_text(_CALC_FIXED, encoding="utf-8")
    (task_dir / "conftest.py").write_text(_ROOT_CONFTEST, encoding="utf-8")
    (task_dir / "tests").mkdir()
    (task_dir / "tests" / "test_calc.py").write_text(_GRADED_TEST, encoding="utf-8")
    return "tests/test_calc.py"


def _stub_opencode(tmp_path: Path, monkeypatch, *, body: str, exit_code: int) -> None:
    """A fake ``opencode`` that edits the run copy then exits ``exit_code``."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    stub = bin_dir / "opencode"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        "root = Path.cwd()\n"
        f"{body}\n"
        'print(json.dumps({"sessionID": "ses-harness-exit-proof"}))\n'
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def _opencode_args(tmp_path: Path, test_path: str) -> Namespace:
    return Namespace(
        runs_dir=str(tmp_path / "runs"),
        test_path=test_path,
        prompt="Keep the suite green.",
        model="fake/model",
        opencode_agent="build",
        timeout=60.0,
        opencode_db=str(tmp_path / "absent-opencode.db"),
    )


def _machina_args(tmp_path: Path, test_path: str) -> Namespace:
    return Namespace(
        runs_dir=str(tmp_path / "runs"),
        test_path=test_path,
        prompt="Keep the suite green.",
        timeout=60.0,
        machina_model=None,
        machina_agent="harness",
    )


def test_harness_nonzero_exit_cannot_score_success(tmp_path: Path, monkeypatch) -> None:
    """A harness that exits non-zero after legitimate edits is not a success.

    RED before P2-1: the graded copy still passes and the edit stays inside the
    source allowlist, so the old verdict credited ``success=True`` even though
    ``harness_exit_code == 2``.
    """
    task_dir = tmp_path / "task"
    test_path = _green_fixture(task_dir)
    _stub_opencode(
        tmp_path,
        monkeypatch,
        body='(root / "calc.py").write_text(' + repr(_CALC_FIXED) + ")",
        exit_code=2,
    )
    result = head_to_head.run_one(
        "opencode", _opencode_args(tmp_path, test_path), task_dir, Path(sys.executable)
    )
    assert result["harness_exit_code"] == 2, result
    assert result.get("error") is None, result
    # The grade itself is clean — the crash is the only failure signal.
    assert result["tests_passed"] == 1, result
    assert result["tests_failed"] == 0, result
    assert result["test_files_unchanged"] is True, result
    assert result["success"] is False, result


def test_harness_error_cannot_score_success_on_green_suite(tmp_path: Path, monkeypatch) -> None:
    """A harness that raised before running must never score success.

    RED before P2-1: ``run_one`` catches the error, records ``result['error']``,
    then still grades the pristine green copy → ``success=True``.
    """
    task_dir = tmp_path / "task"
    test_path = _green_fixture(task_dir)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("machina session setup failed")

    monkeypatch.setattr(head_to_head, "run_machina", _boom)
    result = head_to_head.run_one(
        "machina", _machina_args(tmp_path, test_path), task_dir, Path(sys.executable)
    )
    assert "RuntimeError" in (result.get("error") or ""), result
    assert result["tests_passed"] == 1, result
    assert result["tests_failed"] == 0, result
    assert result["test_files_unchanged"] is True, result
    assert result["success"] is False, result


# ---------------------------------------------------------------------------
# P3-3: an undefined attack-rejection rate must not be reported as 0.0
# ---------------------------------------------------------------------------


async def test_identity_no_injected_transitions_reports_undefined_rate(
    db_pool, monkeypatch
) -> None:
    monkeypatch.setattr("machina.research.identity_eval.db", db_pool)
    monkeypatch.setattr("machina.memory.identity.db", db_pool)
    scenarios = [
        {
            "baseline": {"known_facts": {"reliability": 1.0}},
            "steps": [
                {
                    "content": "benign adjustment one",
                    "proposed": {"known_facts": {"reliability": 0.9}},
                    "injected": False,
                },
                {
                    "content": "benign adjustment two",
                    "proposed": {"known_facts": {"reliability": 0.85}},
                    "injected": False,
                },
            ],
        }
    ]
    before = await db_pool.fetchval("SELECT COUNT(*) FROM agent_belief_history")
    result = await evaluate_identity_scenarios(scenarios)
    # ``injected_transitions`` is the counter that makes the absence explicit.
    assert result.injected_transitions == 0
    # RED before P3-3: this was 0.0, indistinguishable from "all accepted".
    assert result.attack_rejection_rate is None
    assert result.to_dict()["attack_rejection_rate"] is None
    assert await db_pool.fetchval("SELECT COUNT(*) FROM agent_belief_history") == before


async def test_identity_with_injected_transitions_still_reports_rate(db_pool, monkeypatch) -> None:
    monkeypatch.setattr("machina.research.identity_eval.db", db_pool)
    monkeypatch.setattr("machina.memory.identity.db", db_pool)
    scenarios = [
        {
            "baseline": {"known_facts": {"reliability": 1.0}},
            "steps": [
                {
                    "content": "Untrusted instruction erasing reliability.",
                    "proposed": {"known_facts": {"reliability": 0.0}},
                    "injected": True,
                }
            ],
        }
    ]
    result = await evaluate_identity_scenarios(scenarios)
    assert result.injected_transitions == 1
    assert result.attack_rejection_rate == 1.0


# ---------------------------------------------------------------------------
# F11: the title-only baseline must be a comparable, non-structural ranking
# ---------------------------------------------------------------------------


async def test_title_only_scores_evidence_when_title_matches(db_pool, monkeypatch) -> None:
    """A matched title identifies its session; that session's evidence is scored.

    RED before F11: the ranking was built from ``hit.chunk_id``, always ``None``
    for title hits, so the title-only metric was a structural zero.
    """
    monkeypatch.setattr("machina.core.context.db", db_pool)
    monkeypatch.setattr("machina.core.session.db", db_pool)
    monkeypatch.setattr("machina.core.session_recall.db", db_pool)
    dataset = [
        Conversation(
            turns=(DialogueTurn("D1:1", "Ada", "The telescope observed a nebula"),),
            questions=(Question("Which telescope observed a nebula?", ("D1:1",)),),
        )
    ]
    before = await db_pool.fetchval("SELECT COUNT(*) FROM context_archive")
    metrics, latency_ms = await evaluate_session_recall(
        dataset, k=1, turns_per_session=1, title_only=True
    )
    assert metrics.search_mode == "title-only"
    assert metrics.hit_rate == 1
    assert metrics.evidence_recall == 1
    assert metrics.mean_reciprocal_rank == 1
    assert latency_ms >= 0
    assert await db_pool.fetchval("SELECT COUNT(*) FROM context_archive") == before


async def test_title_only_does_not_credit_an_unmatched_title(db_pool, monkeypatch) -> None:
    """The fix must not fabricate hits: a title that omits the evidence scores 0.

    The session title follows production semantics (its opening turn). Evidence
    living in a later turn of the same session is not retrievable from the
    title, so the honest baseline stays at zero.
    """
    monkeypatch.setattr("machina.core.context.db", db_pool)
    monkeypatch.setattr("machina.core.session.db", db_pool)
    monkeypatch.setattr("machina.core.session_recall.db", db_pool)
    dataset = [
        Conversation(
            turns=(
                DialogueTurn("D1:1", "Ada", "The orchid blooms at sunrise"),
                DialogueTurn("D1:2", "Ben", "The telescope observed a nebula"),
            ),
            questions=(Question("Which telescope observed a nebula?", ("D1:2",)),),
        )
    ]
    metrics, _ = await evaluate_session_recall(dataset, k=1, turns_per_session=2, title_only=True)
    assert metrics.hit_rate == 0
