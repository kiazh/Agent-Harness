# M3 — H12 (Copilot SSE error frames) + H19 (RAG reranker lifecycle at shutdown)

- Lane: M3 — holistic review 2026-10-15, findings H12 and H19 (both P2).
- Snapshot: HEAD `3ef6d5b` plus concurrent lanes' uncommitted edits in the shared tree.
- Changed files (owned): `machina/core/provider/copilot.py`, `machina/core/runtime.py`,
  `machina/tools/rag.py`, `tests/test_copilot_provider.py`, `tests/test_deep_runtime_shutdown.py`.
- `machina/core/provider/transport.py` untouched (owned by another lane); the H12 fix reuses its
  existing `_stream_error_message` helper.
- `CopilotProvider.DEFAULT_MODEL` left at `"gpt-6.1-sol"` (untouched, per lane note).
- No commit/push performed.

## H12 — Copilot SSE error frames can end as successful empty or partial responses

**Verdict: REPRODUCED.**

Repro (RED): the existing Copilot SSE harness (`tests/test_copilot_provider.py`,
`httpx.MockTransport`, `data: {...}` frames + `data: [DONE]`) with a synthetic stream of
`{"error": {"message": "upstream overloaded"}}` then `[DONE]`:

- before the fix the generator completed with a `done` event and **empty** content — no error;
- with `["partial ", error]` the error frame vanished into the payload and the call completed as
  a successful partial response;
- the usage reservation was finished as a successful completion with estimated usage over the
  partial text (no typed error ever reached the caller).

Log: `logs/H12_red_before_fix.txt` — 3 failed / 1 passed (the passing one is the benign-frame
control); the core failure is `DID NOT RAISE ProviderError`.

Fix (`machina/core/provider/copilot.py`):

- import `_stream_error_message` from the transport module (the same helper the direct/openrouter
  transports raise with; `transport.py` itself is not modified);
- in the SSE loop, immediately after the JSON-dict check and **before** usage/choices processing
  (`copilot.py:305`): `if chunk.get("error"): raise ProviderError(_stream_error_message(chunk, resolved_model))`.
- the pre-existing `except Exception` accounting path runs for the raised error: error metrics,
  `llm_stream_error` audit, and `usage_store.finish(reservation_id, failed=True)`; partial output
  already yielded as `text` events is never re-counted as a completed response (`finish(failed=True)`
  keeps the conservative `reserved_tokens` accounting).

Tests (added to the existing harness, RED-first):

- `test_error_frame_fails_stream_instead_of_empty_success`
- `test_error_frame_after_partial_output_fails_without_done_event`
- `test_error_frame_finishes_reservation_failed_without_success_usage`
- `test_benign_frames_without_choices_or_error_stay_tolerated` (control: `{"error": null}` and
  metadata-only frames still complete; guards against over-eager error detection)

Behavior callout: an HTTP 200 stream carrying an explicit error frame now raises typed
`ProviderError` (`provider stream error for <model>: <detail>`); `CopilotProvider.complete()`
raises instead of returning an empty `LLMResponse`. Callers already treat typed `ProviderError`
as a failed turn.

## H19 — active RAG reranker clients are not closed at runtime shutdown

**Verdict: REPRODUCED.**

Repro (RED): `tests/test_deep_runtime_shutdown.py` with a fake active pipeline
(`_embedder`/`_reranker` close-tracking mocks) installed as `machina.tools.rag._rag_pipeline`:

- before the fix `runtime.shutdown()` awaited `embedder.close()` once but `reranker.close()`
  **zero** times (`AssertionError: Expected mock to have been awaited once. Awaited 0 times.`);
- the released generation also stayed cached (`_rag_pipeline is not None`), so a restart would
  hand back clients that had just been closed.

Log: `logs/H19_red_before_fix.txt` — 2 failed / 7 deselected.

Fix:

- `machina/tools/rag.py:231` — new single lifecycle helper `close_active_rag_pipeline()`:
  under `_rag_pipeline_lock` it swaps out the active generation and resets `_rag_pipeline` and
  `_rag_pipeline_identity`, then releases the generation through the existing
  `_close_pipeline_quietly` component loop (embedder **and** reranker; each close isolated and
  logged-only on failure).
- `machina/core/runtime.py:270` — the shutdown RAG stage now calls `close_active_rag_pipeline()`
  (wrapped in `_bounded_cleanup`, label `rag pipeline close`) instead of reaching into
  `_rag_pipeline._embedder` inline. The shared embedder close is unchanged and runs first.

Tests (added to the runtime shutdown suite, RED-first):

- `test_shutdown_closes_every_component_of_the_active_rag_pipeline` — through
  `RuntimeServices.shutdown()`: embedder and reranker each closed once; shared generation reset.
- `test_close_active_rag_pipeline_is_one_lifecycle_seam` — per-component isolation (embedder
  close raises, reranker still closes) and a second call is a no-op.

Behavior callouts:

- runtime shutdown now resets the shared RAG generation state; a post-shutdown/restart request
  builds a fresh pipeline instead of reusing the released (closed) one;
- consumer lease bookkeeping (`_pipeline_users` / `_retired_pipelines`) is deliberately untouched:
  retired generations keep their documented close-on-last-release contract (the finding scopes the
  gap to the **active** generation);
- close failures remain logged-only; the shutdown exception surface is unchanged.

## Verification

- RED (before fix): `logs/H12_red_before_fix.txt`, `logs/H19_red_before_fix.txt`.
- GREEN: `logs/H12_green_after_fix.txt` (15 passed — full file), `logs/H19_green_after_fix.txt`
  (9 passed — full file).
- Focused suites (`logs/focused_suites.txt`): `test_copilot_provider.py`,
  `test_copilot_poll_deadline.py`, `test_model_catalog_copilot_2026_10_15.py`,
  `test_deep_runtime_shutdown.py`, `test_final_review_2026_10_15_providers.py`,
  `test_reranker_local.py`, `test_rag_consumer_leases.py`,
  `test_final_review_fix_rag_identity_chunker.py`, `test_bug_fixes_rag_tools.py` — **117 passed**.
- Provider/SSE adjacency (`logs/provider_sse_reranker_adjacent.txt`): `test_fm2_provider.py`
  (includes the W5 mid-stream error-frame tests), `test_r5_011_013_provider_transport.py`
  (includes the Copilot spaceless-`data:` SSE test), `test_embedding_failopen_adversarial.py` —
  **69 passed**.
- `ruff check` and `ruff format --check` clean on all five changed files.

## Cross-lane report — shared ratchet file (not touched by this lane)

`tests/test_architecture_layers.py` is outside this lane's ownership and is currently red on the
shared tree; the wave-landing reconciliation must absorb:

- `GOD_FILE_CEILINGS`: `("core/provider/copilot.py", 431)` → **443** (+12, this lane's H12 delta,
  measured post-`ruff format`).
- `_PROVIDER_PACKAGE_TOTAL_CEILING` (5003): +12 from this lane **and** +2 net already present from
  `core/provider/transport.py` (another lane's uncommitted edit).
- `DEFERRED_IMPORT_CAP` (717): this lane's files add **zero** deferred imports (`core/runtime.py`
  25 → 25; the import swap is 1:1); the observed +1 comes from `machina/services.py` (another lane).
- `core/runtime.py` and `tools/rag.py` have no god-file ceilings; `tools/rag.py` 410 → 431 lines.

Log: `logs/architecture_ratchet_status.txt` (3 failed / 23 passed at capture time).
