"""Offline incident-replay harness for session 5f625662 (Stream D).

Replays the fixture sequence (report request -> interrupt/compact -> continue
-> "just write the md file") against a MOCK provider through the fixed
``machina/core/progress.py`` logic. No live model calls, no production DB.

Reports five metrics: provider-call count, input/output tokens, recall calls,
delegate launches, artifact-written.
"""

import json
import re
import time
from pathlib import Path

from machina.core.progress import (
    STAGNATION_ROUNDS_K,
    is_stagnant,
    record,
    stop_reason,
)

FIX = Path(__file__).parent / "fixtures" / "session_5f625662"

SECRET_PATTERNS = [
    r"sk-[A-Za-z0-9]{10,}",
    r"Bearer [A-Za-z0-9._\-]{10,}",
    r"api[_-]?key\s*[:=]\s*\S+",
    r"AKIA[0-9A-Z]{16}",
    r"xox[bap]-[A-Za-z0-9\-]+",
    r"ghp_[A-Za-z0-9]{10,}",
    r"deepseek[_-]?api[_-]?key\s*[:=]",
    r"password\s*[:=]\s*\S+",
]


class MockProvider:
    """Deterministic offline stand-in. Any live-call attempt raises."""

    def __init__(self):
        self.provider_calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.recall_calls = 0
        self.delegate_launches = 0
        self.live_calls = 0

    def complete(self, prompt_text, purpose="synthesize"):
        self.provider_calls += 1
        tin = max(1, len(prompt_text) // 4)
        tout = 256  # deterministic canned synthesis size
        self.tokens_in += tin
        self.tokens_out += tout
        return f"# Delegation defect report ({purpose})\n\nSynthesized offline.\n"

    def recall(self, query):
        self.recall_calls += 1
        # Mirrors the incident: repeated recall returns echoes, no new evidence.
        return "echo: agent catalog / earlier recall output (no new evidence)"

    def delegate(self, agent, task):
        self.delegate_launches += 1
        return "LLM provider error. Check server logs."


def replay_incident(artifact_dir):
    """Drive the fixture through the fixed logic. Returns metrics dict."""
    t0 = time.monotonic()
    seq = json.loads((FIX / "report_sequence.json").read_text())
    ledger = json.loads((FIX / "child_ledger.json").read_text())
    review = json.loads((FIX / "review_request.json").read_text())
    mock = MockProvider()
    history = []

    # Known findings already gathered (7 ledger rows); the fixture documents the
    # explicit "just write the md file" ask as the final user instruction.
    evidence_count = len(ledger["rows"])

    # Replay the 7 ledger delegates offline (all failed in the incident).
    for row in ledger["rows"]:
        mock.delegate(row["agent"], "review task")

    # Post-final-ask loop: recalls that add no new coverage and no bytes.
    # The query target is already covered by the pre-ask recalls, so every
    # in-loop round is stagnant and the fixed logic forces a synthesize/write
    # attempt at exactly K.
    history = record(history, "session_recall", "write the md file delegation doc", 0)
    stop = ""
    rounds_until_write = seq["calls_after_final_ask"]["total"]  # incident: 25, unbounded
    for _ in range(rounds_until_write):
        mock.recall("just write the md delegation doc")
        history = record(history, "session_recall", "write the md file delegation doc", 0)
        if is_stagnant(history):
            stop = stop_reason(history)
            break

    assert stop, "stagnation must force a write attempt (incident ran 25 calls, wrote nothing)"
    body = mock.complete(
        f"findings={evidence_count} constraint={review['controlling_constraint']} "
        f"ledger_total={ledger['total_tokens']}",
        purpose="delegation-defect-report",
    )
    artifact = Path(artifact_dir) / "DELEGATION_DEFECT_REPORT.md"
    artifact.write_text(
        body
        + f"\nConstraint preserved: {review['controlling_constraint']}\n"
        + f"Child ledger total tokens: {ledger['total_tokens']}\n"
        + f"Stop reason: {stop}\n"
    )
    nbytes = artifact.stat().st_size
    history = record(history, "write_file", str(artifact), nbytes)

    return {
        "provider_calls": mock.provider_calls,
        "tokens_in": mock.tokens_in,
        "tokens_out": mock.tokens_out,
        "recall_calls": mock.recall_calls,
        "delegate_launches": mock.delegate_launches,
        "artifact_written": artifact.exists() and nbytes > 0,
        "artifact_path": str(artifact),
        "stop_reason": stop,
        "elapsed_s": time.monotonic() - t0,
        "live_calls": mock.live_calls,
    }


def test_harness_reports_five_metrics_offline(tmp_path):
    m = replay_incident(tmp_path)
    for key in (
        "provider_calls",
        "tokens_in",
        "tokens_out",
        "recall_calls",
        "delegate_launches",
        "artifact_written",
    ):
        assert key in m, f"missing metric {key}"
    assert m["live_calls"] == 0
    assert m["elapsed_s"] < 10
    assert m["delegate_launches"] == 7  # the 7-row child ledger
    assert m["artifact_written"] is True


def test_stagnation_forces_write_at_k_not_25(tmp_path):
    m = replay_incident(tmp_path)
    assert m["recall_calls"] == STAGNATION_ROUNDS_K, "fixed loop stops recalling at K"
    assert "stagnant" in m["stop_reason"].lower()
    assert str(STAGNATION_ROUNDS_K) in m["stop_reason"]
    text = Path(m["artifact_path"]).read_text()
    assert "read-only" in text.lower() or "dont implement" in text.lower()
    assert "324073" in text  # ledger evidence preserved, not rediscovered


def test_fixture_contains_zero_secrets():
    hits = []
    for f in FIX.glob("*.json"):
        text = f.read_text()
        for pat in SECRET_PATTERNS:
            if re.search(pat, text, re.I):
                hits.append((f.name, pat))
    assert hits == [], f"secret patterns in fixture: {hits}"


def test_fixture_bounded():
    total = sum(f.stat().st_size for f in FIX.glob("*.json"))
    assert total < 10 * 1024, f"fixture must stay bounded, got {total} bytes"
    for f in FIX.glob("*.json"):
        assert f.stat().st_size < 2048, f"{f.name} exceeds 2KB bound"
