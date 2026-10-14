# WIRING: progress monitor hook point (follow-up, NOT yet wired)

Delivered: tested `machina/core/progress.py` only. Per Stream D scope,
`machina/core/agent.py` was deliberately NOT modified.

## Exact hook point

`ReActAgent.run`, `machina/core/agent.py:1788-1804`:

```python
batch = tool_calls_made[batch_start:]  # line 1788
...
iteration += 1  # line 1804
```

Insert between them (pseudo):

```python
for entry in batch:
    history = record(history,
                     entry.get("tool", ""),
                     entry.get("target", ""),   # file path / query
                     entry.get("artifact_bytes", 0))
if is_stagnant(history):
    # forced synthesize/write attempt + truthful stop_reason(history);
    # NEVER an iteration/token cap: productive loops must be unaffected.
```

Mirror hook: `ReActAgent.run_stream` loop starting at
`machina/core/agent.py:1847` (same batch boundary after its
`_execute_tool_calls` call).

## State notes for the follow-up

- `history` must live in durable task state (survive compaction/interrupt/
  resume), NOT in semantic recall — see incident P0 in
  `docs/verification/OPENCODE_SESSION_5F625662_ISSUES.md` lines 104-129.
- `artifact_bytes`: bytes newly written toward the requested deliverable
  this batch (0 for recall/read-only evidence gathering).
- `target` for coverage: file path for read/write/search tools; normalized
  query string for recall tools.
- K stays the named tunable `STAGNATION_ROUNDS_K` in `progress.py`.
