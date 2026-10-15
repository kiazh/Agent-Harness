# R4-012 — Anthropic/Claude tool-result coalescing (proof)

- Task: `docs/verification/FIX_PLAN_ROUND4_TASKS.md` §R4-012 (P1; adversarial /
  network-boundary capture per the proof matrix).
- Finding: `CODEBASE_REVIEW_ROUND4_2026-10-14.md` P1 #3 (V1-3 confirmed):
  `machina/core/provider/anthropic.py:96-108` emitted one `{"role":"user"}` message per
  `role:"tool"` message, so a ≥2-tool batch (one tool message per result —
  `prompt.append_tool_messages`, `agent.py:1320`) produced consecutive `user` roles,
  violating the Messages API alternation contract. `ClaudeProvider` inherits the
  converter (`claude.py:60-64`) and was affected identically.
- Fix revision: working tree on top of `cdc5232` (no commit). RED replay used
  `cdc5232` in a throwaway worktree.

## Root cause (one sentence)

The converter treated every `role:"tool"` history entry as its own user turn instead of
coalescing a contiguous run of tool results into the single user message the Messages
API requires behind an assistant `tool_use` batch.

## Fix (minimal, behavior-justified)

`machina/core/provider/anthropic.py` `_to_anthropic_messages` only: build the
`tool_result` block (now preserving a truthy `is_error` from the source message) and
append it to the previous emitted message when that message is an existing
tool-result user message; otherwise open a new user message. Adjacent runs therefore
collapse to one user message with the ordered blocks; the 1-tool shape is byte-identical
to before. Net zero line change (577 lines before and after) so the god-file ratchet
(`tests/test_architecture_layers.py`, per-file 577 / package 5501) is preserved.

```diff
             if role == "tool":
-                out.append(
-                    {
-                        "role": "user",
-                        "content": [
-                            {
-                                "type": "tool_result",
-                                "tool_use_id": msg.get("tool_call_id", ""),
-                                "content": str(msg.get("content", "")),
-                            }
-                        ],
-                    }
-                )
+                result: dict[str, Any] = {
+                    "type": "tool_result",
+                    "tool_use_id": msg.get("tool_call_id", ""),
+                    "content": str(msg.get("content", "")),
+                    **({"is_error": True} if msg.get("is_error") else {}),
+                }
+                prev = out[-1]["content"] if out else None
+                if isinstance(prev, list) and prev and prev[0].get("type") == "tool_result":
+                    prev.append(result)
+                else:
+                    out.append({"role": "user", "content": [result]})
                 continue
```

## Proof (adversarial, network-boundary capture)

`tests/test_anthropic_tool_results.py` builds conversations exactly as the agent does
(`prompt.append_tool_messages`, one `role:"tool"` message per batch result), then runs
the **real** `AnthropicProvider.complete()` / `ClaudeProvider.complete()` with only the
httpx transport replaced by `httpx.MockTransport` (AnthropicProvider: client swap, same
seam as `test_fm2_provider.py`; ClaudeProvider: constructor `transport=`); the captured
outgoing POST body is asserted. `_to_anthropic_messages` is never patched — every case
drives it through the production request path.

Cases: (a) 1 tool result; (b) 2; (c) 3 mixed ok/error; (d) interleaved assistant text
between two batches; plus (e) ClaudeProvider 2-tool batch (subclass inheritance); plus
(f) adversary: a tool run immediately after a plain user-text message must not merge
into the text message while still coalescing into one user message.

Assertions per capture: roles alternate (no adjacent equal roles, first is `user`);
each assistant `tool_use` batch is followed by exactly one `user` message whose blocks
are `tool_result`s in the same call-id order; `content` text and `is_error` preserved.

### RED on unfixed code (worktree at `cdc5232`)

```
$ TMP=/private/var/folders/sk/y8w9dwmd4lnc2yvygqqxd4xw0000gn/T/opencode
$ git worktree add --detach "$TMP/machina-r4-012-red" cdc5232
$ cp tests/test_anthropic_tool_results.py "$TMP/machina-r4-012-red/tests/"
$ cd "$TMP/machina-r4-012-red"
$ PYTHONPATH="$TMP/machina-r4-012-red" /Users/shadow/machina/.venv/bin/python \
    -m pytest tests/test_anthropic_tool_results.py -q -p no:cacheprovider
```

Result (saved: `R4-012_red_pytest.txt`): `5 failed, 1 passed` — the single-tool case
passes (unchanged behavior), every batched case fails for exactly the named reason:

```
E       AssertionError: consecutive: ['user', 'assistant', 'user', 'user']
FAILED tests/test_anthropic_tool_results.py::test_two_tool_batch_coalesces_into_one_user_message
FAILED tests/test_anthropic_tool_results.py::test_three_tool_batch_preserves_order_and_error_flags
FAILED tests/test_anthropic_tool_results.py::test_interleaved_batches_alternate_roles_and_text
FAILED tests/test_anthropic_tool_results.py::test_adjacent_tool_run_coalesces_without_corrupting_user_text
FAILED tests/test_anthropic_tool_results.py::test_claude_provider_inherits_coalescing
```

After the fix (main tree, same command): `6 passed` (`R4-012_green_pytest.txt`).

### Before/after outgoing payloads (sanitized)

Captured with `R4-012_capture_probe.py` (same real path, output
`R4-012_red_payloads.txt` / `R4-012_green_payloads.txt`; synthetic ids/content only).
Role sequences:

| case | before (unfixed) | after (fixed) |
|---|---|---|
| (a) 1 tool | `user, assistant, user` | `user, assistant, user` |
| (b) 2 tools | `user, assistant, user, user` | `user, assistant, user` |
| (c) 3 mixed | `user, assistant, user, user, user` | `user, assistant, user` |
| (d) interleaved | `user, assistant, user, user, assistant, user` | `user, assistant, user, assistant, user` |

Case (b) before — invalid: one user message per result:

```json
{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_a", "content": "ok-a"}]},
{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_b", "content": "ok-b"}]}
```

Case (b) after — one user message, ordered blocks:

```json
{
  "role": "user",
  "content": [
    {"type": "tool_result", "tool_use_id": "toolu_a", "content": "ok-a"},
    {"type": "tool_result", "tool_use_id": "toolu_b", "content": "ok-b"}
  ]
}
```

Case (c) after — order and the error flag preserved on the middle result:

```json
{
  "role": "user",
  "content": [
    {"type": "tool_result", "tool_use_id": "toolu_a", "content": "ok-a"},
    {"type": "tool_result", "tool_use_id": "toolu_b", "content": "Error: boom-b", "is_error": true},
    {"type": "tool_result", "tool_use_id": "toolu_c", "content": "ok-c"}
  ]
}
```

Note: `is_error` is passed through from the source `role:"tool"` message when present;
the current harness writer does not set it, so ordinary turns keep the exact previous
wire shape (no `is_error` key). The ClaudeProvider capture shows the identical shape.

## Checks run

```
.venv/bin/python -m pytest tests/test_anthropic_tool_results.py tests/test_fm2_provider.py tests/test_providers_direct.py -q -p no:cacheprovider
    78 passed
.venv/bin/python -m pytest tests/test_architecture_layers.py -q -p no:cacheprovider
    27 passed
.venv/bin/python -m pytest tests/test_claude_provider.py tests/test_image_attachments.py -q -p no:cacheprovider
    16 passed  (supplementary; converter consumers)
.venv/bin/python -m ruff format --check <touched files>   # clean
.venv/bin/python -m ruff check <touched files>            # All checks passed
wc -l machina/core/provider/anthropic.py                  # 577 (unchanged; ratchet intact)
```

## Files

- `machina/core/provider/anthropic.py` — the fix (converter only; no public API change).
- `tests/test_anthropic_tool_results.py` — new adversarial network-boundary proof.
- `docs/verification/proofs/codebase_review_round4_2026-10-14/R4-012_capture_probe.py`,
  `R4-012_red_payloads.txt`, `R4-012_green_payloads.txt`,
  `R4-012_red_pytest.txt`, `R4-012_green_pytest.txt`, this file — evidence.

## Residual risks

- `prev[0].get("type")` assumes list-content message blocks are dicts. `anthropic_content`
  (images.py) already calls `block.get("type")` on every user list-content element on the
  same path, so this assumes nothing the existing converter did not already require; a
  non-dict block still fails loudly, never silently.
- `is_error` pass-through is only exercised by the proof fixtures because no current
  harness writer sets the flag; it changes nothing on today's turns and keeps the block
  faithful if a producer starts sending it.
- The API-alternation contract is enforced/asserted for harness-shaped histories; an
  externally supplied malformed history (e.g. `tool` directly after a `user` text) still
  yields two consecutive user messages by design — the adversarial case proves the user
  text is never mutated or corrupted and the run still coalesces.
