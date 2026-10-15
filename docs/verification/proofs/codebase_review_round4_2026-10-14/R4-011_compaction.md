# R4-011 — Compaction drops delegated result text

- Round: 4 · Date: 2026-10-14 · DB: `postgresql://postgres:postgres@localhost:5432/machina_test` (real `machina_test`).
- Fix: `machina/core/compression.py` `_chunk_to_text` result branch
  `payload.get("result", "")` → `payload.get("result", payload.get("content", ""))`.
- Shared by both `compress()` and `acompress()` (single `_llm_summarize`/`_chunk_to_text` path); no other refactor performed.

## Writer / renderer verification (grep)

Writers of `chunk_type="result"` (production):

- `machina/core/orchestrator.py:417-423` → `payload={"agent": agent_name, "content": f"[{agent_name}] {final_content}"}`
- `machina/core/scheduler.py:973-979` → `payload={"content": output, "job_id": ...}`

Both store text under `"content"`. Renderers:

- `machina/core/assembler.py:178-184` already does `payload.get("result", payload.get("content", ""))` (intended fallback).
- `machina/services.py:102` is inside the `tool_call` branch, not `result`.
- `machina/core/prompt.py:161-190` (native-block replay) copies provider-raw content blocks
  (`LLMResponse.raw[PROVIDER_CONTENT_KEY]`); it does not read context-chunk payloads — unaffected.

## RED — unfixed code

Direct renderer probe (orchestrator-shaped chunk):

```text
>>> repr(ContextCompressor()._chunk_to_text(chunk))  # payload={"agent": "researcher", "content": "[researcher] R4-011 delegated child output: 7af3 checks passed"}
'[Result] ok: '
```

E2E proof test (`tests/test_compression.py::test_e2e_compaction_keeps_delegated_result_text`)
drives real DB + real `services.compress_session_with_status`, faking only `machina.core.provider.get_provider`
(provider/network boundary) with a prompt-echoing fake:

```text
$ MACHINA_TEST_DATABASE_URL=... pytest tests/test_compression.py::test_e2e_compaction_keeps_delegated_result_text -q -p no:cacheprovider
>           assert any(_CHILD_MARKER in p for p in fake.prompts)
E           assert False
E            +  where False = any(<generator object ...>)
tests/test_compression.py:502: AssertionError
FAILED ... - 1 failed in 0.51s
```

Prompt actually sent at the provider boundary in the RED run (child output absent):

```text
Summarize the following conversation context concisely. Preserve key facts, decisions, and tool results. Keep it under 200 words.

[Result] ok: 
[User] please delegate
```

Persisted `compression_summary` payload likewise ended with `[Result] ok: ` (marker absent).

## GREEN — fixed code

Direct renderer probe (same chunk; legacy `"result"` key still wins):

```text
'[Result] ok: [researcher] R4-011 delegated child output: 7af3 checks passed'
'[Result] ok: legacy success'   # payload {"status": "ok", "result": "legacy success"}
```

E2E proof test:

```text
$ MACHINA_TEST_DATABASE_URL=... pytest tests/test_compression.py::test_e2e_compaction_keeps_delegated_result_text -q -p no:cacheprovider
1 passed in 0.43s
```

Provider-boundary prompt and persisted summary after the fix:

```text
[Result] ok: [researcher] R4-011 delegated child output: 7af3 checks passed
marker in prompt: True
marker in persisted summary: True
```

## Proof type

Full E2E (no narrowing): real `machina_test` DB, real `context_manager.add_chunk` /
`get_all_chunks`, real `services.compress_session_with_status`, real usage accounting.
The only fake is `machina.core.provider.get_provider` returning a prompt-echoing provider —
the network boundary. `_chunk_to_text` and `_llm_summarize` are never patched.

## Checks

```text
$ MACHINA_TEST_DATABASE_URL=... pytest tests/test_compression.py tests/test_history_condensation.py -q -p no:cacheprovider
27 passed in 0.56s   (incl. test_e2e_compaction_keeps_delegated_result_text PASSED)
$ pytest tests/test_architecture_layers.py -q -p no:cacheprovider
27 passed in 1.07s
$ ruff format --check machina/core/compression.py tests/test_compression.py
2 files already formatted
$ ruff check machina/core/compression.py tests/test_compression.py
All checks passed!
```
