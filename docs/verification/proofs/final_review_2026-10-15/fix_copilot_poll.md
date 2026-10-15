# Final review — security fix lane: F3 (`device_flow_poll` unbounded server `slow_down` sleep)

Date: 2026-10-15 · Lane: FIX (sole writer of `machina/security/copilot_auth.py`
and one new evidence test file). No commits. Read-only elsewhere.

Source finding: `docs/verification/proofs/final_review_2026-10-15/line_security_permissions.md`
(F3 · P3, `copilot_auth.py:431-432` loop/`sleep` and `:463-468` `slow_down` branch).

## Files changed

| File | Change |
| --- | --- |
| `machina/security/copilot_auth.py` | F3: add `DEVICE_POLL_MAX_WAIT = 60.0`; clamp each per-poll wait to the remaining device-flow deadline **and** the cap before `sleep(wait)` |
| `tests/test_copilot_poll_deadline.py` | NEW evidence suite (RED-first): stubbed `slow_down` endpoint + fake monotonic clock, fast deadline |

Nothing else in the module changed; pure `status()` / CLI output is untouched.

---

## F3 · P3 — server-supplied `slow_down` `interval` was an unbounded sleep

**Root cause.** The poll budget was checked only at the loop head
(`while time.monotonic() < deadline`). On `error == "slow_down"` with a positive
numeric `interval`, `wait` was replaced **outright** by the server value with no
clip to the remaining deadline and no cap, so `sleep(wait)` could block far past
the documented `DEFAULT_DEVICE_TIMEOUT` (300 s) contract. The initial and
non-numeric `slow_down` paths kept `+DEVICE_POLL_SAFETY_MARGIN` / `+= 5.0`, so
the omission was specific to the server-supplied value.

**Fix (smallest safe change).** Before every `sleep`, clamp the current `wait`
to `(remaining deadline, DEVICE_POLL_MAX_WAIT)`; if the deadline is exhausted,
stop and let the existing typed timeout raise:

```python
DEVICE_POLL_MAX_WAIT = 60.0
...
while time.monotonic() < deadline:
    wait = min(wait, DEVICE_POLL_MAX_WAIT, max(0.0, deadline - time.monotonic()))
    if wait <= 0:
        break
    sleep(wait)
```

**Semantics preserved.**
- The success path is unchanged: a `slow_down` `interval` smaller than the
  remaining budget (and the 60 s cap) is still honoured exactly — the existing
  `test_device_flow_poll_success_and_pending` pin
  (`sleeps[-1] == 9.0` with `timeout=30`) still passes.
- The `authorization_pending`, no-numeric-`slow_down`, `expired_token`,
  `access_denied`, other-error, `validate_token`-failure and success branches are
  byte-for-byte unchanged; only the pre-`sleep` wait value is bounded.
- Deadline expiry still raises the pre-existing typed error
  `CopilotAuthError(code="copilot_device_timeout")`; the message and `status`
  output are untouched.

**RED (pre-fix):** `logs/fix_copilot_poll_RED.txt` — both new tests failed:
`sum(sleeps) == 4.0 > 2.0` budget, and the huge `1000000000.0` interval was
handed straight to `sleep`.
**GREEN (post-fix):** `logs/fix_copilot_poll_GREEN.txt` — 2 passed.

Direct virtual-time trace after the fix (`interval=0.01`, `timeout=2.0`,
server interval `1e9`): `sleeps == [2.0]`, total `2.0 ≤ 2.0`, typed timeout
raised. With `timeout=300.0`: every wait `≤ 60.0`, total `≤ 300.0`.

---

## Verification

- **New F3 suite:** `logs/fix_copilot_poll_GREEN.txt` — `tests/test_copilot_poll_deadline.py` **2 passed**.
- **Focused suites** (`logs/fix_copilot_poll_focused.txt`): **243 passed** —
  `test_copilot_auth`, `test_copilot_provider`, `test_oauth_cli_auth`,
  `test_r4_049b_fdclose` (copilot `publish_device_token` fd path),
  all `test_*_adversarial.py` (`sse_stream`, `chatgpt_auth`,
  `embedding_failopen`, `redaction`, `env_file`, `head_to_head_grader`),
  `test_r5_011_013_provider_transport`, `test_subscription_removals`.
- **Lint/format** (`logs/fix_copilot_poll_ruff.txt`): `ruff check` — All checks
  passed; `ruff format --check` — 2 files already formatted.

## Logs

- `logs/fix_copilot_poll_RED.txt`
- `logs/fix_copilot_poll_GREEN.txt`
- `logs/fix_copilot_poll_focused.txt`
- `logs/fix_copilot_poll_ruff.txt`
