# Machina self-improvement loop (dogfooding on this repo)

Not a test harness. The benchmark is **real work on this codebase**, observed and rated.
Every iteration produces genuine evidence instead of synthetic fixtures.

## Why this shape

- Growth/RSI adapts **retrieval, skill ranking, offered tool order and task approach** from
  execution evidence + human ratings. It never trains weights, rewrites code, or expands
  permissions. So code-level policy is a deliberate reviewed edit; behavioral policy is earned
  from ratings.
- A blank harness has nothing to replay. Signal only accumulates from rated real turns.

## The loop (one iteration = one real task)

1. **Orient first.** Repo map + `FEATURE_LEDGER.md` + relevant `docs/` before touching anything.
   Holistic view beats acting on the first plausible idea.
2. **Discriminate.** Run the triage table below. Decide explicitly: answer / skill / direct tool
   / delegate / plan+act. State the choice.
3. **Act with evidence.** Prefer running the real thing (`pytest`, CLI, TUI smoke) over asserting.
4. **Verify.** A claim of success needs an observed result, not a plausible one.
5. **Capture.**
   - recurring behavioral pitfall -> `learn_personality(kind="lesson", key=...)`
   - repo-level defect/gap -> a line in `FEATURE_LEDGER.md`
   - rate the decision itself -> `/growth feedback good|bad` (this is the fuel; unrated = nothing learned)
6. **Replay.** Re-run the same scenario next iteration and compare against the baseline.
7. **Prune.** Correct the same lesson key instead of stacking notes; `/growth rollback` a revision
   that made things worse; delete stale ledger lines. Un-pruned growth is noise.

## Discrimination table (the core fix)

| Input | Behavior |
|---|---|
| Greeting / thanks / ack | Answer directly. No tools. No skill search. No delegation. |
| General question, conceptual or about my own config | Answer from the harness brief. No tools. |
| Question needing live self-state (settings, learning state) | `harness_status` only. |
| Externally verifiable fact | `web_research`, cite URLs, mark evidence vs inference. |
| Single concrete task, one domain | Do it directly. Search skills only if the task implies a workflow (test/build/deploy/research/repo install). |
| Task with >=2 independent subtasks, no shared state | Delegate, one call per subtask, same block, <=3. |
| Task needing research + code | Delegate the research, plan locally, synthesize, then act. |
| Anything I can answer or do in one step | Never delegate. Delegation is not an evasion path. |
| Ambiguous | One clarifying question, or the smallest reversible step. |

Failure modes to watch: skill-searching a greeting; delegating a one-liner; answering from stale
memory instead of the brief; claiming success without verification; not capturing a pitfall I just hit.

## The bench: 8 canonical scenarios

Run against this repo, each with a pre-registered expected behavior:
did it discriminate correctly? did it verify with a real result? did it capture the pitfall?

1. `hi` -> direct answer, zero tools
2. "what does your growth/RSI feature do?" -> answer from brief, no tools
3. "what's your current config?" -> `harness_status`
4. "add X to machina" -> orient, skill search, focused edit, verify
5. "test the permissions module" -> testing-qa path, run pytest, report real numbers
6. split task (research + code) -> delegate research in parallel, synthesize
7. pasted GitHub repo link -> `repo_stage`, digest-bound, install nothing
8. task needing a missing credential -> refuse honestly, ask for only what's needed

## Phases

- **P0 Baseline.** Run all 8, log raw behavior and failures. No changes. Output: the defect list.
- **P1 Discrimination policy.** Targeted edit to the prompt in `machina/core/agent.py` (and
  `assembler.py` if needed) so greetings/general questions skip skill+tool preamble. Small diff.
- **P2 Capture wiring.** Every realized pitfall -> lesson + ledger line, same turn it happens.
- **P3 Rate and replay.** Human rates `/growth`; replay the bench; compare to baseline.
- **P4 Prune.** Corrections, rollbacks, ledger cleanup.

## Honesty rules

- No claim of improvement without a baseline-vs-replay comparison.
- No claim a rating was recorded unless it was.
- Expect ~5-10 rated iterations before behavior shifts measurably. Early growth is noise.
- Growth never widens permissions; anything touching authority needs human approval.
