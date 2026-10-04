# Native PostgreSQL vs LangGraph Approval Trial

**Goal:** Compare restartable, agent-scoped approval of one delegated action
using the same PostgreSQL test database and effect receipt.

**Acceptance:** A separate process proposes and pauses a workflow; a new
process approves or denies it; a second resume cannot repeat the effect;
cancellation prevents the effect; budget and ownership checks are exercised;
latency and schema overhead are recorded. The LangGraph candidate uses its
PostgreSQL checkpointer with synchronous persistence and remains an optional
dependency during the trial. The native candidate uses a small table and the
existing asyncpg pool.

**Boundary:** This trial will use a database effect so idempotency can be
atomic with its receipt. It cannot establish exactly-once execution for
arbitrary external tools. Production selection requires gateway event and
real delegated-agent integration after the comparison.

**Progress (2026-10-04):** Native and LangGraph implementations, PostgreSQL
restart tests (including a separate Python process), denial/cancellation,
agent scope, and repeat-resume tests pass. A five-run local latency sample is
recorded in `research/results/workflow-trial-2026-10-04.json`. Gateway event,
usage-budget, audit, and real delegated-agent parity are still open, so this
does not select a production engine.
