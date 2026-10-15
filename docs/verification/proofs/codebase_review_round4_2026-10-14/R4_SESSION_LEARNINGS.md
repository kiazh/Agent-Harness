# R4 session learnings — 2026-10-14 (extract before compaction)

Source: the round-4 full-codebase review → staged fix campaign (waves A–C, agents, verifiers).

## Patterns Discovered

**Pattern: Adversarial harness as the acceptance gate (strict-xfail flip)**
- Context: hardening work where the bug must be proven before it is fixed (races, leaks, policy
  bypasses, grading integrity).
- Implementation: build the harness first, assert the *correct* behavior, mark the
  bug-documenting cases `@pytest.mark.xfail(strict=True, reason="R4-xxx: …")`. CI stays green,
  the RED evidence is committed, and "done" = the markers flip to hard passes (strict XPASS is a
  failure, so leaving markers in after a fix is itself caught).
- Example: `tests/test_auth_store_adversarial.py` (3 xfails → 7 hard passes),
  `tests/test_redaction_adversarial.py` (37 xfails), `tests/test_sse_stream_adversarial.py`.

**Pattern: Driver-owned contextvars for per-step async generators**
- Context: an async generator whose each `__anext__` runs in a NEW asyncio Task (`_owned_step`).
- Implementation: contextvars that must survive across steps are set by the DRIVER (the code
  that creates the step tasks) — tasks copy the creator's context at creation. Never set them
  inside the generator: later steps won't see it, and the exit runs in a foreign context.
- Corollary: teardown must be tolerant — `try: var.reset(token) except ValueError: pass` with a
  why-comment (async generators can be closed from a different task below; the original context
  is tearing down). See `session_mode.end_turn_scope`, `streaming.py` resume guard.
- Reference: `TurnEngine.stream` docstring documents this contract; follow it for span/scope.

**Pattern: Credential-store fencing (flock + generation + fingerprint + fenced logout)**
- Context: rotating tokens in files that multiple processes can touch.
- Implementation: per-store flock across read→network→publish; peer adoption under the lock;
  durable generation counter bumped on logout; fingerprint gate at publish; fenced logout =
  bump durably FIRST (mkstemp+fsync+replace), then unlink, lock-free (a logout must not block
  behind a network-held lock); a loser of any race re-checks and removes only its own write.
- Example: `machina/security/store_fence.py` used by all credential stores.

**Pattern: The mock-boundary rule**
- Context: any fix proof.
- Implementation: mocks may replace external providers/HTTP/DNS/keyring only — never the seam
  being fixed and never internal modules on the fixed path. The strongest RED tests fail against
  unfixed code for the exact named reason (run them in a scratch worktree when possible).
- Why: the review's largest finding class was tests mocking seams the run path never calls.

**Pattern: Sequential stage gates + per-stage ledger**
- Context: long multi-agent campaigns.
- Implementation: one full backend gate, THEN the UI gate alone (never in parallel — the UI
  clipboard test bounds an external process at the product's 3 s timeout and parallel load
  flakes it); persist per-stage ledgers (files changed, behavior changes called out, deferred
  items) under `docs/verification/proofs/…`. Keep a refutation ledger: candidate findings
  disproven by adversarial verifiers become "do not fix" entries.

**Pattern: Vendor-compliance before shipping subscription-OAuth interop**
- Context: reusing a vendor subscription (OAuth tokens/device flows) from a third-party tool.
- Implementation: check PRIMARY sources (vendor legal docs/product terms) and classify:
  **supported** (Kimi — "obtain an API Key to integrate into third-party tools", first-party
  guides), **not prohibited but un-sanctioned** (Copilot — third-party CLI/ACP supported; raw
  token reuse grey), **prohibited** (Anthropic: subscription OAuth in third-party tools "not
  permitted"; Google: third-party OAuth reuse is a ToS violation with suspensions). Prefer
  API-key integrations; delete prohibited flows atomically (code + CLI + UI + tests + docs).

**Pattern: File-disjoint agent lanes with collision serialization**
- Context: many parallel fix agents on one dirty tree.
- Implementation: partition tasks so each agent is the sole writer of its files; serialize any
  shared file (or fold both tasks into one writer); verify lane discipline with `git status`
  against expected file sets; the final tree diff should map 1:1 to lanes.

**Pattern: Commit-per-cluster with task-ID messages**
- Context: tracking/bisecting a large dirty tree.
- Implementation: commit in dependency-safe order in file-clusters; messages `R4-N + R4-M: …`
  with a bullet body (what, why, evidence, gates) matching the repo's existing style. Shared
  files go in the FIRST commit whose tree they keep consistent (e.g., a sink call must land
  after the API it calls, or in the same commit).

## Best Practices Applied

1. **RED-first with worktree replay** — run the new test against unfixed code (scratch worktree
   or PYTHONPATH binding), capture the failure, then fix; keep the before/after in the proofs dir.
2. **Fail-closed teardown discipline** — teardown paths must never abort cleanup (claim release,
   provider close); tolerate cross-context resets, guard optional resets, keep `finally`s total.
3. **Behavior-change callouts** — every intended behavior change (resolver fail-closed, timeout
   caps, paused rendering, lock-free logout, hermetic grading) is written into the stage ledger;
   nothing changes silently.
4. **Bounded locks with typed errors** — replace unbounded `flock` waits with a bounded
   non-blocking poll loop + typed timeout error; close the fd on every failure path.
5. **Verification by direct inspection, not agent claims** — `wc -l` the ratchet, grep the
   dangling refs, read the diffstat, re-run the failing test; an agent's "done" is a claim.
6. **Load-flake policy: harden the fixture or the orchestration, never widen the product bound
   or add retries** — the clipboard 3 s bound stayed; the fixture became a `#!/bin/sh`+`/bin/cat`
   wrapper (ms boot); the gates were serialized by rule.

## Mistakes to Avoid

1. **Announcing work that wasn't launched** — "running the final gate now" was said before the
   gate was actually started (caught by the user). Prevent: launch the tool first; then speak.
2. **`cmd | tail` masking exit codes** — a commit gate chained `pytest … | tail && git commit`
   and committed a failing state (1695f7c went in with one load-triggered failure). Prevent:
   `set -o pipefail` (or capture `$?`) in every gating shell command.
3. **Scheduling drift** — R4-016 (a P1) was missed by every wave despite being in the plan;
   only found when the user asked "the 50". Prevent: reconcile the task index against launched
   waves before closing any stage; maintain the "not yet scheduled" list explicitly.
4. **Parallel full-suite gates** — self-inflicted load flake on a wall-clock-bounded UI test.
   Prevent: the written rule — backend gate, then UI gate, sequentially.
5. **Trusting subagent completion states** — three failure modes seen: a wedged run, a junk
   one-word report, and a premature turn-end mid-suite ("Suite at 97%"). Prevent: verify the
   repo state directly afterwards; treat reports as claims; resume or replace as needed.
6. **Over-narrow completion criteria** — "task done" is only real when the harness flips, the
   gate is green, and the ledger records it; markers left unremoved after a fix fail strict
   xfail and catch over-claimed completion.

## Reusable Snippets

```python
# Strict-xfail acceptance gate (flip = delete the marker when the fix lands)
@pytest.mark.xfail(strict=True, reason="R4-013: concurrent refresh may double-submit")
async def test_concurrent_refresh_submits_one_post(): ...

# Tolerant cross-context teardown (async generators closed from another task)
try:
    _turn_scope.reset(token)
except ValueError:  # token's context already torn down (generator close across tasks)
    pass
```

```bash
# Gating commit (pipefail + focused run + repo-style message)
set -o pipefail
git add <paths> && \
.venv/bin/ruff check <paths> && \
MACHINA_TEST_DATABASE_URL='postgresql://postgres:postgres@localhost:5432/machina_test' \
  .venv/bin/python -m pytest <focused tests> -q -p no:cacheprovider 2>&1 | tail -2 && \
git commit -m "R4-xxx: title" -m "what/why" -m "gates: …"
```

## Suggested Skill Updates

- `skills/testing-qa/SKILL.md`: add the adversarial-harness acceptance-gate pattern, the
  strict-xfail flip discipline, the mock-boundary rule, and the sequential-gate/orchestration
  flake policy.
- `skills/context-runtime/SKILL.md`: add the driver-owned contextvar contract for per-step async
  generators + tolerant teardown.
- `skills/security-sandboxing/SKILL.md`: add credential-store fencing (flock+generation+fingerprint,
  lock-free fenced logout) and bounded lock acquisition with typed errors.
- `skills/docs-communication/SKILL.md`: add the per-stage ledger + behavior-change callout +
  refutation-ledger practice for review campaigns.

## Instincts (continuous-learning-v2)

```json
[
  {"trigger":"async generator whose steps run in fresh asyncio tasks","action":"set contextvars in the driver before creating step tasks; make teardown tolerate foreign-context resets","confidence":0.9,"source":"session-extraction","timestamp":"2026-10-14T21:40:00-04:00"},
  {"trigger":"hardening a race/leak/policy bug","action":"write the adversarial harness first with strict-xfail cases; done = markers flip and the gate is green","confidence":0.9,"source":"session-extraction","timestamp":"2026-10-14T21:40:00-04:00"},
  {"trigger":"shell command gates a commit or push","action":"set -o pipefail first; never let a tail/head mask a failing exit code","confidence":0.95,"source":"session-extraction","timestamp":"2026-10-14T21:40:00-04:00"},
  {"trigger":"running backend and UI test gates","action":"run them sequentially, never in parallel (load flakes the wall-clock-bounded UI clipboard test)","confidence":0.9,"source":"session-extraction","timestamp":"2026-10-14T21:40:00-04:00"},
  {"trigger":"adding third-party interop with a vendor subscription","action":"check vendor primary-source terms first; classify supported / grey / prohibited; prefer API-key integrations; remove prohibited flows atomically","confidence":0.85,"source":"session-extraction","timestamp":"2026-10-14T21:40:00-04:00"},
  {"trigger":"mocking in a fix-proof test","action":"mock only external boundaries; never the seam being fixed; run the proof against unfixed code to prove RED","confidence":0.9,"source":"session-extraction","timestamp":"2026-10-14T21:40:00-04:00"},
  {"trigger":"closing a campaign stage","action":"reconcile the task index against launched waves; record behavior changes and deferred items in the stage ledger before moving on","confidence":0.85,"source":"session-extraction","timestamp":"2026-10-14T21:40:00-04:00"}
]
```
