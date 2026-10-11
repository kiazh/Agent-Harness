# Persona and harness RSI design

The user requests RSI integrated with persona across the harness, implemented
through completion. The working interpretation is recursive improvement of
durable agent behavior from actual usage and feedback. It applies to persona,
memory retrieval, task strategy, relevant skill selection, and available tool
ordering. It does not claim model-weight training or unattended rewriting of
the running application's source code. Those require separate training or
deployment systems and are not substitutes for a working feedback loop.

## Existing foundations and selected approach

Extend `GrowthStore`, the shared `BaseReActAgent` lifecycle, and the existing
skill router. This reaches CLI, gateway/TUI, HTTP, jobs, and delegated agents
through their shared agent implementation. Avoid a second agent runtime.

[Reflexion](https://arxiv.org/abs/2303.11366) motivates durable experience-backed
context; [Self-Refine](https://arxiv.org/abs/2303.17651) motivates iterative
feedback and revision. The implementation uses bounded runtime statistics and
versioned persona notes, not these papers' exact experimental algorithms.
An extra reviewer call on every turn would add cost and self-grading bias.
Instead, collect structured execution evidence and keep user ratings distinct.

## Lifecycle

1. At turn preparation, load the agent's RSI state and select a fixed, short
   strategy for the task domain. Skill and tool weights influence only already
   relevant/available candidates; explicit requests and inherited restrictions
   remain authoritative. No classifier/provider call is added.
2. Bind a durable episode to the current persona revision and session. Store
   domain/strategy, activated skill names, tool outcome metadata, and token counts.
   Never retain raw prompts, tool arguments, tool output, or response text in RSI.
3. At a terminal unpaused turn, derive a weak operational proxy from actual tool
   outcomes or an explicit terminal failure. Plain assistant claims of success
   receive no positive reward. Cancellation/approval pauses do not learn.
4. Recompute bounded routing statistics from completed episodes, with explicit
   user ratings taking precedence over automatic proxies. Memory retrieval uses
   the existing masked contextual bandit; weak proxies receive a smaller update
   than human feedback. Dangerous capabilities are never learned/granted.
5. Version persona changes. Human management changes establish a fresh baseline;
   agent-written notes are trials. Three human-rated subsequent episodes from
   the trial revision determine retention: average below zero rolls back, average
   at least zero stabilizes. Operational proxies alone cannot promote a persona.
   This is a conservative heuristic, not proof of causal improvement.
6. Persist recursive lessons as bounded fixed-language operational guidance:
   observed failures increase emphasis on prerequisites/verification, and successful
   outcomes adjust future strategy and relevant capability ranking. No raw server
   instructions become system authority or executable learned code.

## Storage and limits

Extend the additive growth migration with per-agent RSI profiles, immutable
revision snapshots, and episode metadata referencing existing growth episodes.
Keep at most 50 revisions and the existing 500 episodes per agent. Revisions
increase monotonically; rollback creates a new revision rather than rewriting
history. Preserve the baseline needed by an active trial during pruning.
Routing multipliers are bounded to 0.8–1.2 and never bypass relevance filters.
Checkpoints also retain a bounded list of pending policy-update episode IDs.
Automatic deltas remain in the episode ledger; restoration activates only those
present in the selected snapshot, including correct handling of turns completing
after a restore. Human ratings can replace the directly applied proxy without
duplicating delta arrays into every revision.
Strategy context is bounded to 400 tokens; persona notes retain the existing
2,400-character aggregate bound and 500-character per-note bound.

RSI defaults on when memory/personality growth are enabled. An explicit
`rsi_enabled=false` stops RSI collection/adaptation. Disabling growth preserves
the existing no-learning semantics. Human edits still supersede existing trials
while RSI is off, including explicit confirmation of an unchanged note. A DB/learning failure cannot block normal
chat or widen tools. Owner/session checks and transaction locks make rating,
completion, and rollback idempotent and isolated.

## User surfaces

Extend `growth.get` with RSI status/history and add `growth.rollback` using a
session-bound revision. CLI gains `ah growth rollback`; `/growth` gains an
interactive menu for status, ratings, note management, and rollback. Existing
argument commands remain valid. Show automatic operational observations separately
from human ratings and trials separately from stable revisions.

## Acceptance

Real test-database tests must prove persistence, concurrent single completion
and rating, explicit-rating precedence, revision attribution, trial retention
and rollback, human-change preservation, bounded history, and agent/session
isolation. Real agent-loop tests for streaming and non-streaming prove an
observed tool outcome changes later routing/strategy without extra provider calls
and cancellation/pauses do not train. Gateway and CLI boundaries share the store;
UI tests exercise menus and rollback. Full backend/UI checks remain required.

No commits, pushes, deployments, live-provider calls, or production database
changes are authorized. Prior connector and installer changes must be preserved.
