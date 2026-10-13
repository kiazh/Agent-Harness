"""Synthetic-history tests for machina/core/progress.py (Stream D).

Looping-recall histories must trigger stagnant at exactly K; productive
histories must never trigger, however long they run.
"""

from machina.core.progress import (
    STAGNATION_ROUNDS_K,
    is_stagnant,
    record,
    should_write_first,
    stagnant_trailing_count,
    stop_reason,
)


def looping_recall_history(n, target="session_recall:write the md file"):
    h = []
    for _ in range(n):
        h = record(h, "session_recall", target, 0)
    return h


def stagnated_history(target="session_recall:write the md file"):
    """Looping history driven until is_stagnant first fires."""
    h = []
    while not is_stagnant(h):
        h = record(h, "session_recall", target, 0)
    return h


def test_looping_recall_triggers_at_exactly_k():
    assert STAGNATION_ROUNDS_K == 3
    # Not before K consecutive stagnant rounds ...
    h = looping_recall_history(STAGNATION_ROUNDS_K)  # 1 productive + 2 stagnant
    assert not is_stagnant(h)
    # ... and fires the moment the trailing stagnant count reaches exactly K.
    h = stagnated_history()
    assert is_stagnant(h)
    assert stagnant_trailing_count(h) == STAGNATION_ROUNDS_K


def test_looping_recall_first_round_not_stagnant_then_stagnates():
    # Round 1 covers a new target (productive); rounds 2..K+1 repeat it.
    h = record([], "session_recall", "q1", 0)
    assert not is_stagnant(h)
    for _ in range(STAGNATION_ROUNDS_K):
        h = record(h, "session_recall", "q1", 0)
    assert is_stagnant(h)


def test_productive_new_coverage_never_triggers():
    h = []
    for i in range(50):
        h = record(h, "read_file", f"machina/core/mod{i}.py", 0)
    assert not is_stagnant(h)


def test_productive_artifact_bytes_never_trigger():
    h = []
    for i in range(50):
        # same artifact target re-written with real bytes = progress
        h = record(h, "write_file", "docs/REPORT.md", 512)
    assert not is_stagnant(h)


def test_mixed_productive_tail_resets_stagnation():
    h = looping_recall_history(10)
    assert is_stagnant(h)
    h = record(h, "read_file", "machina/core/orchestrator.py", 0)  # new coverage
    assert not is_stagnant(h)


def test_record_does_not_mutate_input():
    h = []
    h2 = record(h, "read_file", "a.py", 0)
    assert h == [] and len(h2) == 1


def test_stop_reason_truthful():
    stag = stagnated_history()
    reason = stop_reason(stag)
    assert "stagnant" in reason.lower()
    assert str(STAGNATION_ROUNDS_K) in reason
    assert "write" in reason.lower()
    productive = record([], "read_file", "new.py", 0)
    assert "continuing" in stop_reason(productive)
    assert "continuing" in stop_reason([])
    partial = looping_recall_history(2)  # 1 productive + 1 stagnant, below K
    assert "continuing" in stop_reason(partial)


def test_should_write_first():
    assert should_write_first(0, True) is True  # explicit ask always wins
    assert should_write_first(0, False) is False
    assert should_write_first(2, False) is False
    assert should_write_first(3, False) is True
    assert should_write_first(25, False) is True  # incident had ample evidence
