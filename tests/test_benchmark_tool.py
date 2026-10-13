"""Tests for the unified offline benchmark (machina/research/benchmark.py).

The benchmark is the reporting surface for the incident-remediation program:
these tests pin that its sections run headlessly, that the LoCoMo section is
honest (real metrics with a dataset, explicit SKIP without one), and that the
comparisons block keeps carrying the incident constants + cross-tool protocol.
"""

from __future__ import annotations

import json
from pathlib import Path

from machina.research import benchmark

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
