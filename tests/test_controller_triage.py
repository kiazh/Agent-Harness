"""Stream B: deterministic TurnController foundation — matrix, scorer, cache.

Pure functions only: no network/DB/provider calls by construction.
RED-first per tdd-guide; hypotheses tagged [DEBUG-B] if behavior surprises.
"""

import re
import socket

import pytest

# --- B1: ~40-query labeled matrix for classify_task ---------------------------

MATRIX = [
    # conversational (greetings/smalltalk — matches existing conversational())
    ("hi", "conversational"),
    ("hello there!", "conversational"),
    ("thanks!", "conversational"),
    ("hey", "conversational"),
    ("ok", "conversational"),
    ("bye", "conversational"),
    # explain (conceptual questions, no follow-on action)
    ("How does your Python testing work?", "explain"),
    ("What is memory growth?", "explain"),
    ("Why is the sky blue?", "explain"),
    ("Can you explain your research workflow?", "explain"),
    ("What is the default persona?", "explain"),
    ("How does the gateway work?", "explain"),
    # learn-directory (repo-layout / file-location questions)
    ("Where is the persona stored?", "learn-directory"),
    ("What files are in this repo?", "learn-directory"),
    ("Show me the project structure", "learn-directory"),
    ("List all Python files", "learn-directory"),
    ("Where is the session store defined?", "learn-directory"),
    ("What is the layout of this repository?", "learn-directory"),
    # review-readonly (explicit no-change review asks + bare review verbs)
    ("dont implement changes just review", "review-readonly"),
    ("Just review this code, no changes", "review-readonly"),
    ("Review only, do not edit anything", "review-readonly"),
    ("Take a look at auth.py and give feedback without changing it", "review-readonly"),
    ("Can you review the Python code without modifying it?", "review-readonly"),
    ("Do a Python code review", "review-readonly"),
    # debug (error-signal asks)
    ("Fix this traceback in one file", "debug"),
    ("Why does pytest fail with AssertionError in test_x?", "debug"),
    ("There's a stack trace when I run server.py, help fix it", "debug"),
    ("Debug the failing test in tests/test_auth.py", "debug"),
    ("This crashes on startup with ImportError, what's wrong?", "debug"),
    ("Fix the bug causing the 500 error", "debug"),
    # implement (explicit build/mutate asks)
    ("Implement user login in auth.py", "implement"),
    ("Build the export feature", "implement"),
    ("Add retry logic to gateway/server.py", "implement"),
    ("Refactor the session store", "implement"),
    ("Write a new test for compaction", "implement"),
    ("Create the migration script", "implement"),
    # ambiguous (bare follow-ons — never guessed, caller resolves)
    ("continue", "ambiguous"),
    ("just write the file", "ambiguous"),
    ("do it", "ambiguous"),
    ("go ahead", "ambiguous"),
    ("proceed", "ambiguous"),
    ("and then?", "ambiguous"),
]


@pytest.mark.parametrize("query,expected", MATRIX)
def test_classify_task_matrix(query, expected):
    from machina.core.intent import classify_task

    assert classify_task(query) == expected


def test_matrix_covers_all_classes():
    labels = {expected for _, expected in MATRIX}
    assert labels == {
        "conversational",
        "explain",
        "learn-directory",
        "review-readonly",
        "debug",
        "implement",
        "ambiguous",
    }
    assert len(MATRIX) >= 40


def test_classify_task_reuses_conversational_contract():
    from machina.core.intent import classify_task, conversational

    assert classify_task("hi") == "conversational"
    assert conversational("hi") is True
    assert conversational("Fix this traceback in one file") is False
    # ambiguous follow-ons carry action verbs but must never guess implement
    assert conversational("just write the file") is False
    assert classify_task("just write the file") == "ambiguous"


def test_triage_resolves_ambiguous_against_task_state():
    from machina.controller.triage import resolve_task

    assert resolve_task("continue", task_state=None) == "implement"
    assert resolve_task("continue", task_state=None, default=None) == "ambiguous"
    assert resolve_task("continue", task_state={"active_class": "debug"}) == "debug"
    assert resolve_task("Fix this traceback in one file") == "debug"


def test_classifier_is_pure_no_network(monkeypatch):
    from machina.core.intent import classify_task

    def _boom(*a, **k):
        raise AssertionError("[DEBUG-B01] classifier attempted network I/O")

    monkeypatch.setattr(socket, "socket", _boom)
    for query, _ in MATRIX:
        classify_task(query)


# --- B2: static complexity scorer ----------------------------------------------

REQUIRED_SCORER_CASES = [
    ("Fix this traceback in one file", {}, 0, "single"),
    ("hi", {}, 0, "direct"),
]


@pytest.mark.parametrize("query,repo_stats,error_count,expected", REQUIRED_SCORER_CASES)
def test_score_complexity_required_outcomes(query, repo_stats, error_count, expected):
    from machina.controller.complexity import score_complexity

    assert score_complexity(query, repo_stats, error_count)["level"] == expected


def test_score_complexity_multifile_review_needs_subagents():
    from machina.controller.complexity import score_complexity

    out = score_complexity(
        "Review all files across the entire repo, dont implement changes just review",
        {"files": 1200, "loc": 200000},
        0,
    )
    assert out["level"] == "subagents"
    assert out["reasons"]


def test_score_complexity_thresholds_are_named_tunable_constants():
    from machina.controller import complexity

    assert isinstance(complexity.SCORE_DIRECT_MAX, int)
    assert isinstance(complexity.SCORE_SUBAGENTS_MIN, int)
    assert complexity.SCORE_DIRECT_MAX < complexity.SCORE_SUBAGENTS_MIN
    low = complexity.score_complexity("hi")
    assert low["level"] == "direct"


def test_score_complexity_deterministic_and_reasoned():
    from machina.controller.complexity import score_complexity

    first = score_complexity("Fix this traceback in one file", {"files": 10}, 0)
    second = score_complexity("Fix this traceback in one file", {"files": 10}, 0)
    assert first == second
    assert set(first) >= {"level", "reasons"}
    assert first["level"] in {"direct", "single", "subagents"}
    assert first["reasons"]
    escalated = score_complexity("Fix this traceback in one file", {"files": 10}, 5)
    assert escalated["level"] in {"single", "subagents"}


def test_scorer_is_pure_no_network(monkeypatch):
    from machina.controller.complexity import score_complexity

    def _boom(*a, **k):
        raise AssertionError("[DEBUG-B02] scorer attempted network I/O")

    monkeypatch.setattr(socket, "socket", _boom)
    score_complexity("Build the export feature", {"files": 5, "loc": 500}, 2)


# --- B3: skill preselect cache --------------------------------------------------

def test_preselect_identical_queries_rank_once(tmp_path):
    from machina.controller.preselect import SkillPreselectCache
    from machina.skills.registry import SkillRegistry

    cache = SkillPreselectCache(SkillRegistry(tmp_path))
    first = cache.rank("How does your Python testing work?")
    second = cache.rank("How does your Python testing work?")
    assert [s.name for s in first] == [s.name for s in second]
    assert cache.rank_calls == 1


def test_preselect_normalizes_query_before_hashing(tmp_path):
    from machina.controller.preselect import SkillPreselectCache
    from machina.skills.registry import SkillRegistry

    cache = SkillPreselectCache(SkillRegistry(tmp_path))
    cache.rank("  How does your python TESTING work? ")
    cache.rank("how does your python testing work?")
    assert cache.rank_calls == 1


def test_preselect_invalidate_forces_rerank(tmp_path):
    from machina.controller.preselect import SkillPreselectCache
    from machina.skills.registry import SkillRegistry

    cache = SkillPreselectCache(SkillRegistry(tmp_path))
    cache.rank("How does your Python testing work?")
    cache.invalidate("How does your Python testing work?")
    cache.rank("How does your Python testing work?")
    assert cache.rank_calls == 2
    cache.invalidate()
    cache.rank("How does your Python testing work?")
    assert cache.rank_calls == 3


def test_preselect_reuses_rank_skills_without_reimplementing(tmp_path, monkeypatch):
    import machina.controller.preselect as preselect_mod
    from machina.controller.preselect import SkillPreselectCache
    from machina.skills.registry import SkillRegistry

    calls = []

    real_rank = preselect_mod.rank_skills

    def _counting(skills, query, **kwargs):
        calls.append(query)
        return real_rank(skills, query, **kwargs)

    monkeypatch.setattr(preselect_mod, "rank_skills", _counting)
    cache = SkillPreselectCache(SkillRegistry(tmp_path))
    cache.rank("How does your Python testing work?")
    cache.rank("How does your Python testing work?")
    assert len(calls) == 1


# --- zero provider calls by construction ---------------------------------------

BANNED_IMPORTS = re.compile(
    r"^\s*(?:import|from)\s+([\w.]+)", re.M
)
BANNED_MODULES = (
    "provider",
    "llm",
    "openai",
    "anthropic",
    "litellm",
    "ollama",
    "cohere",
    "gemini",
    "mistral",
)


def test_controller_source_imports_nothing_provider_related():
    import pathlib

    package = pathlib.Path(__file__).resolve().parent.parent / "machina" / "controller"
    assert package.is_dir()
    for path in sorted(package.glob("*.py")):
        text = path.read_text()
        for match in BANNED_IMPORTS.finditer(text):
            dotted = match.group(1).lower()
            parts = dotted.split(".")
            assert not any(p in BANNED_MODULES for p in parts), (
                f"{path.name} imports banned module: {match.group(1)}"
            )
        assert "__import__" not in text, f"{path.name} uses dynamic import"


def test_importing_controller_pulls_no_provider_modules():
    import sys

    before = set(sys.modules)
    import machina.controller  # noqa: F401

    new = [name for name in set(sys.modules) - before if "machina" in name]
    for name in new:
        lowered = name.lower()
        assert not any(b in lowered.split(".") for b in BANNED_MODULES), (
            f"controller import pulled banned module: {name}"
        )
