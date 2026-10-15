# Fix batch 3 (a): gateway / observability / memory — final review 2026-10-15

Lane: FIX LANE (final review batch 3). Sole writer of `machina/api/routes/streaming.py`,
`machina/api/routes/shared.py`, `machina/protocol/__init__.py` (SSE v2 `tool.complete`
`isError` pass-through only), `machina/observability/traces.py` (`render_diagnostics`
only), `machina/memory/redaction.py` (`StreamingSecretRedactor.flush` only) + affected
tests + one new evidence test file. No commits. Working tree left for the orchestrator.

Source: `docs/verification/proofs/final_review_2026-10-15/line_gateway_obs_cli.md`
(F3/P10, F4/P6) and `line_memory_connectors.md` (F5/N4).

## Files changed

| File | Change |
|---|---|
| `machina/api/routes/shared.py` | `_serialize_event` gains `tool_is_error: bool | None = None`; the `tool_result` branch sets `data["isError"]` when provided (was: never set). |
| `machina/api/routes/streaming.py` | `tool_complete` branch forwards `payload["isError"]` on v2 requests (`req.protocolVersion == 2`); v1 passes `None`. |
| `machina/observability/traces.py` | `render_diagnostics` appends `(+N dropped)` to the `llm:` line when `ledger_dropped` is non-zero. |
| `machina/memory/redaction.py` | `StreamingSecretRedactor.flush()` emits the withheld `_pem_prefix` (via `redact_secrets`) instead of discarding it; the oversized body tail is still dropped. |
| `tests/test_final_review_2026_10_15_batch3_gateway_obs_memory.py` | NEW evidence test file (RED-first). |
| `tests/test_fm6_traces.py` | Affected test: assert the rendered diagnostics carry the ledger marker. |
| `tests/test_protocol_conformance.py` | Affected test: v2 `tool.complete` carries the real `isError`; v1 shape unchanged. |

`machina/protocol/__init__.py` was reviewed and **not changed**: `canonical_http_event`
already reads `isError=data.get("isError", False)` (`protocol/__init__.py:140`). The gap
was purely upstream (the flag never reached `data`); the documented v2 pass-through is now
actually exercised.

## Per-finding status

### P10 (F3) — HTTP SSE v2 `tool.complete.isError` always false — FIXED

- Root cause: `streaming.py` dropped `payload["isError"]`; `shared.py` never set it; the
  engine payload carries it (`core/turn_engine.py:361`, `result.startswith("Error:")`).
- Fix: forward the real flag through `_serialize_event(..., tool_is_error=…)` on the v2
  path only. The stdio gateway (`gateway/server.py:1027`) already forwarded it; the HTTP
  wire now matches `communications/ui-gateway-protocol.md:40`.
- No wire-shape drift beyond the flag: v1 (`protocolVersion=1`, the default) keeps the
  earlier `tool_result` shape (`type/tool/result/outcome/id`, no `isError`); v2 gains only
  the real boolean value. The denied case stays `isError=false` (unchanged; pinned by
  `test_r4_022_denied_outcome.py`).
- Tests: `test_http_v2_tool_complete_forwards_iserror_flag`,
  `test_http_v1_tool_complete_keeps_legacy_shape`,
  `test_sse_route_v2_tool_complete_carries_real_iserror`,
  `test_sse_route_v1_tool_complete_omits_iserror` (new); `test_v2_tool_complete_carries_real_iserror_flag`
  (protocol conformance).

### P6 (F4) — `render_diagnostics` omits the `ledger_dropped` marker — FIXED

- `turn_report` returns `ledger_dropped` (`traces.py:818`) and the ring drops the oldest
  entry when over `MAX_LEDGER_ENTRIES_PER_TURN` (500); every other capped ring printed a
  `(+N dropped)` marker. The spend summary now prints `(+N dropped)` on the `llm:` line
  (only when non-zero, mirroring the tools line).
- Tests: `test_render_diagnostics_marks_dropped_ledger_entries`,
  `test_render_diagnostics_omits_marker_below_cap` (new); `test_fm6_traces.py` extended.

### N4 (F5) — `flush()` discards the withheld prefix of an oversized open PEM — FIXED

- Root cause: in the `_pem_open` branch, `flush()` cleared `_pem_prefix` and returned `""`,
  silently losing text that preceded the oversized unterminated block.
- Fix: emit `redact_secrets(self._pem_prefix).text` (the same whole-buffer redaction the
  normal flush path uses). The oversized block's buffered body tail is still dropped, so
  the no-leak invariant is preserved; complete secrets inside the prefix are still removed.
  The prefix is withheld body-*external* text (held only because a partial rule reached the
  block), not PEM body material.
- Tests: `test_flush_emits_withheld_prefix_for_oversized_open_pem`,
  `test_flush_prefix_is_redacted_when_it_contains_a_secret`,
  `test_flush_emits_prefix_after_real_overflow` (new). Existing
  `test_handoff_regressions.py::test_streaming_redactor_oversized_open_pem_never_emits_body`
  and the adversarial corpus (`tests/test_redaction_adversarial.py`) still pass — no body
  byte is emitted and benign text is untouched.

## Verification

Environment: `.venv/bin/python` (Python 3.14.5), repository root.

| Log | Command | Result |
|---|---|---|
| `logs/fix_batch3_RED.log` | new evidence file with the four source files stashed | `5 failed, 4 passed` (RED) |
| `logs/fix_batch3_GREEN.log` | new evidence file, fixes applied | `9 passed` |
| `logs/fix_batch3_focused.log` | new + redaction adversarial + fm5 + fm6/traces + r4_063_064 + protocol conformance + wiring parity + r4_022 + sse adversarial + tool-timeout contract | `257 passed` |
| `logs/fix_batch3_ruff.log` | `ruff check` on all changed/owned files | `All checks passed!` |

The RED run isolates the fix: `5 failed` on the four findings' assertions; the four
remaining tests are shape/negative guards that hold both before and after (v1 omits
`isError`; below-cap diagnostics omit the marker).

`fix_batch3_a.diff` holds the exact source/test hunks.

### Unrelated pre-existing failure (not this lane)

`tests/test_handoff_regressions.py::test_keyset_pagination_counts_and_concurrent_insert`
fails with `TypeError: … _page() got an unexpected keyword argument 'before_seq'`
(`machina/core/context.py:727`). `core/context.py` is owned/modified by another lane in this
batch; the failure is independent of the gateway/observability/memory files changed here and
is reported only for completeness.

## Residual / notes

- `_pem_prefix` is `buf[:start]` after the frontier cut, so it is non-empty only when a
  configured credential (or another partial rule) lowers the frontier below the block start;
  the natural trigger is covered by `test_flush_emits_prefix_after_real_overflow`.
- Returning the prefix on flush changes no final-output content for in-cap streams and never
  emits PEM body bytes.
