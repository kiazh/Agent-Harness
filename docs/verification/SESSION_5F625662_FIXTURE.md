# Session 5f625662 replay fixture (Stream D)

Incident report: `docs/verification/OPENCODE_SESSION_5F625662_ISSUES.md`.
Source export: `session-5f625662.md` (12,176 lines / 723,696 bytes).

## What was extracted (bounded: each file < 2KB, total < 10KB)

`tests/fixtures/session_5f625662/`:

| File | Source | Content |
|---|---|---|
| `hello.json` | export L13-28 | hello turn (0 tools), persona Q/A, workflow-question turn |
| `review_request.json` | export L4409-4413 | review request; controlling read-only clause verbatim, run-on remainder marked `[...]` |
| `report_sequence.json` | export L5719-25, L9725, L9753-63, L11658-60 | report request, interrupt/`can continue`/resume, `just write the md file`; 25 post-ask calls, 0 `write_file` |
| `child_ledger.json` | issues doc L88-97 | 7-row child ledger: 2 provider-errors recorded `complete`, 5 timeouts, 324,073 tokens, no useful review |
| `tool_counts.json` | issues doc L61-79 | 228-call histogram + 2,119,552-token UI total |
| `manifest.json` | — | machine-readable index of the above |

## What was redacted / excluded

- Secret scan of the FULL 723KB export (`sk-*`, `Bearer`, `api-key=`,
  `AKIA`, `xox[bap]-`, `ghp_`, `DEEPSEEK_API_KEY=`, `password=`): **zero hits**.
- Nevertheless excluded: terminal outputs dumping env/config contents;
  long assistant prose is paraphrased, not copied; only short verbatim
  user-instruction excerpts are kept.
- `tests/test_replay_harness.py::test_fixture_contains_zero_secrets`
  re-greps the fixture for these patterns on every run.

## Replay harness usage

```bash
/Users/shadow/machina/.venv/bin/python -m pytest tests/test_replay_harness.py -v
```

`tests/test_replay_harness.py` holds the harness: it loads the fixtures,
drives a deterministic `MockProvider` (no model calls, no DB) through the
report → interrupt → `continue` → just-write sequence, enforces
`machina/core/progress.py` stagnation detection (K=3), and reports the five
metrics: provider-call count, input/output tokens, recall calls, delegate
launches, artifact-written. The fixed behavior stops recalling at K=3
(incident: 25 recalls, no file) and forces a synthesize/write attempt with
a truthful stop reason.
