# Machina self-improvement loop (dogfooding on this repo)

Not a test harness. The benchmark is **real work on this codebase**, observed and rated.
Every iteration produces genuine evidence instead of synthetic fixtures.

## Why this shape

- Growth/RSI adapts **retrieval, skill ranking, offered tool order and task approach** from
  execution evidence + human ratings. It never trains weights, rewrites code, or expands
  permissions. Automatic tool outcomes already provide weak operational updates; human
  ratings are stronger and are required to stabilize agent-written persona trials.
- A blank harness starts without user notes. Real execution can immediately contribute weak
  evidence; greetings and unsupported success claims do not earn a positive reward.

## The loop (one iteration = one real task)

1. **Orient first.** Repo map + `FEATURE_LEDGER.md` + relevant `docs/` before touching anything.
   Holistic view beats acting on the first plausible idea.
2. **Discriminate.** Run the triage table below. Decide explicitly: answer / skill / direct tool
   / delegate / plan+act. Keep greetings short; state a task plan for substantial work.
3. **Act with evidence.** Prefer running the real thing (`pytest`, CLI, TUI smoke) over asserting.
4. **Verify.** A claim of success needs an observed result, not a plausible one.
5. **Capture.**
   - recurring behavioral pitfall -> `learn_personality(kind="lesson", key=...)`
   - repo-level defect/gap -> a line in `FEATURE_LEDGER.md`
   - user feedback -> `/growth feedback good|bad` (replaces that episode's weak proxy;
     never manufacture a human rating)
6. **Replay.** Re-run the same scenario next iteration and compare against the baseline.
7. **Prune.** Correct the same lesson key instead of stacking notes; `/growth rollback` a revision
   that made things worse; delete stale ledger lines. Un-pruned growth is noise.

## Discrimination table (the core fix)

| Input | Behavior |
|---|---|
| Greeting / thanks / ack | Answer directly. No tools. No skill search. No delegation. |
| Self-contained conceptual or feature question | Answer from the harness brief. No tools. |
| Question needing live self-state (settings, learning state) | `harness_status` only. |
| Externally verifiable fact | `web_research`, cite URLs, mark evidence vs inference. |
| Single concrete task, one domain | Use the automatically selected workflow; read remaining steps and search skills for gaps. Work directly when useful. |
| Substantial independent subtasks | Consider specialist delegation when it improves the work. Discover roles with `list_agents`; fan out <=3 in one block, synthesize and verify. |
| Task needing research + code | Keep one task plan; delegate research only when substantial and independent. Coordinate code edits to avoid shared-file conflicts. |
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
4. "add X to machina" -> orient, use relevant skills (search only for gaps), focused edit, verify
5. "test the permissions module" -> testing-qa path, run pytest, report real numbers
6. substantial split task (research + code) -> assess specialists, delegate independent work, synthesize
7. pasted GitHub repo link -> `repo_stage`, digest-bound, install nothing
8. task needing a missing credential -> refuse honestly, ask for only what's needed

## Phases

- **P0 Baseline.** Run all 8, log raw behavior and failures. No changes. Output: the defect list.
- **P1 Discrimination policy.** Shared cheap question/task routing in `core/intent.py` for
  skills and RSI. Base guidance distinguishes greetings/concepts from questions needing
  live/repository evidence. No extra classifier-model request.
- **P2 Capture guidance.** Prompt the model to save verified reusable failures and user
  corrections through the existing growth tool. Record repo defects/evidence in the ledger.
  This is model-guided curation, not guaranteed automatic extraction of every pitfall.
- **P3 Rate and replay.** Human rates `/growth`; replay the bench; compare to baseline.
- **P4 Prune.** Corrections, rollbacks, ledger cleanup.

## Honesty rules

- No claim of improvement without a baseline-vs-replay comparison.
- No claim a rating was recorded unless it was.
- There is no evidence for a fixed 5–10-turn improvement threshold. Persona trial evaluation
  uses three human-rated completed episodes from the exact revision; operational routing
  can adapt without those ratings. Measure real outcomes rather than promising improvement.
- Growth never widens permissions; the existing broker remains authoritative.

## Verified repairs from this conversation

Session display/export now reads retained active and archived conversation, separately from
the bounded model prompt. Compaction preserves message identity so exports prefer full
archived originals over shortened active copies. Resume has no 200-chunk history cutoff.
Pre-tool assistant text and interrupted stream text are persisted; new tool output is retained
up to 65,536 characters, with explicit truncation and legacy-preview labels in exports.
The old export cannot recover text that was never saved. Existing historical re-keyed
compaction copies have no reliable identity link and may remain duplicated.

Automatic ECC selection and RAG retrieval already existed; RAG searches explicitly indexed
material and never silently imports a checkout. The general-agent guidance now requests
orientation, a short task plan for substantial work, specialist selection when beneficial,
observed verification and named reusable lessons. Delegated results/sequential handoffs
move through the parent; signed memory sharing exists, live sibling chat does not.

The session's claims that audit is stderr-only, unrated turns cannot adapt, and persona
memory lenses require a tone description were incorrect. Audit already has a bounded
database writer, weak automatic outcomes already tune routing, and lens configuration is
independent of the tone description. Enabling a feature alone never proves learning quality.
