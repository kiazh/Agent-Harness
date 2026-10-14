# OpenCode handoff: Machina session failures and excessive token consumption

Date: 2026-10-13

## Required outcome

Fix the runtime failures demonstrated by this real session, especially failure to
finish a simple requested deliverable, loss of task intent after compaction,
repeated irrelevant retrieval, broken delegation and excessive token consumption.
These are connected orchestration problems. Do not treat them as a persona-only
problem, solve them solely by enlarging timeouts, or declare success because
isolated unit tests pass.

The user wants proportional behavior: greetings and self-contained explanations
get direct answers; substantial work gets relevant skills, appropriate specialist
work, evidence and a completed deliverable. Review requests stay read-only. The
user previously removed the arbitrary max-iterations feature; preserve unlimited
productive work while adding progress-aware recovery and observable spend controls.

## Evidence and scope

- Transcript: `docs/verification/fixtures/session-5f625662.md`, 12,176 lines / 723,696 bytes.
- Session: `5f625662-8480-4688-9580-b919d5f3bbcb`; agent `harness`.
- Export timestamps: created 12:28:17, last activity 12:44:46, roughly 16m29s.
- Screenshot supplied by the user shows `deepseek-chat / main / full / ready / 2,119,552 tokens`, repeated recall calls, stopping, and successful export.
- **The user independently checked the DeepSeek API and confirms the excessive usage is real. Treat this as actual provider consumption, not merely a suspicious UI counter.** Reconcile the ledger/UI against provider records to explain it, rather than dismissing the incident as accounting inflation.
- Code examined at HEAD `abfcb99ab3920307177b3527138aa02cc4c2d1da`.
- Existing working changes in `FEATURE_LEDGER.md`, `machina/core/intent.py` and
  `tests/test_turn_intent.py` belong to the earlier session. Preserve them.
- This handoff changes documentation only. No production database queries, paid
  model calls, runtime fixes or new benchmark runs were performed for this report.

Evidence labels below: **Observed** means visible in the supplied transcript or
screenshot; **Confirmed code defect** means the current implementation supports
the failure directly; **Hypothesis** means OpenCode must reproduce and validate it.
Line references refer to the supplied export; code anchors refer to the reviewed
HEAD and may move during fixes.

## Incident summary

1. `hello` was answered correctly with no tools.
2. The assistant described the default persona inaccurately/incompletely.
3. A workflow question became a self-selected code mutation task, followed by a
   user correction that explanations should be lightweight.
4. The user requested an ECC review of token optimization, RAG and AI architecture.
   The assistant spent many calls orienting, then confused skill names with agent
   names and attempted nonexistent delegate targets.
5. Two real children returned provider-error text but were reported as having
   responded and recorded as `complete`.
6. Five subsequent children timed out, including one-file and one-grep tasks.
7. The user switched the deliverable to a thorough Markdown report of delegation
   problems. The assistant gathered extensive evidence but never wrote it.
8. After interruptions/compaction, `continue` led to more rediscovery. Even the
   explicit instruction “can you just write the md file?” caused 25 additional
   tools, chiefly recall, without producing the file.
9. The user had to stop the agent. Total displayed usage was **2,119,552 tokens**,
   independently confirmed as excessive actual usage through DeepSeek by the user.

## Quantified behavior

A complete scan of explicit tool markers in the export found **228 calls**:

| Tool | Calls |
|---|---:|
| terminal | 154 |
| read_file | 22 |
| session_recall | 21 |
| delegate | 9 |
| list_agents | 6 |
| learn_personality | 5 |
| list_files | 4 |
| session_recall_window | 3 |
| recall | 2 |
| harness_status | 1 |
| search_files | 1 |

There are **zero `write_file` calls**. Shell commands did mutate intent code,
tests and the feature ledger earlier, so zero write-file calls does not mean zero
filesystem changes. The decisive failure is the absent requested Markdown report.

After the final explicit request to write the file (export line 11,658), the agent
made **25 more calls**: 15 session recalls, 6 terminal calls, 3 recall windows and
1 general recall. It still did not deliver the file.

Nine delegate calls consist of two nonexistent agent names and seven actual
children. The transcript's database output at lines 9,601–9,609 records:

| Child | Recorded status | Recorded tokens | Duration | Result |
|---|---|---:|---:|---|
| rag-pipeline-reviewer | complete | 14,098 | 5.0s | LLM provider error |
| performance-optimizer | complete | 19,666 | 10.7s | LLM provider error |
| rag-pipeline-reviewer | error | 39,504 | 60.0s | delegation timed out |
| performance-optimizer | error | 70,349 | 60.0s | delegation timed out |
| architect | error | 34,412 | 60.0s | delegation timed out |
| rag-pipeline-reviewer | error | 70,131 | 60.0s | delegation timed out |
| architect | error | 75,913 | 60.0s | delegation timed out |
| **Total** | | **324,073** | | **No useful review returned** |

These are recorded child charges, not independently verified billed tokens for
those seven rows. Do not subtract them from the UI total until parent/descendant
accounting is reconciled. The API confirmation establishes excessive real overall
usage, not the precise attribution of every individual ledger entry.

## P0: Complete the requested artifact instead of endlessly rediscovering it

**Observed.** The user requested a thorough report at line 5,713, resumed at 9,755,
and explicitly demanded the file at 11,658. No report followed any of these asks.
The final recall window even returned that latest user instruction, yet the agent
continued recalling unrelated material (12,167 onward).

This is the central product failure: an agent can consume millions of tokens and
produce no requested deliverable despite already having sufficient evidence.

Fix requirements:

- Preserve the current objective, expected artifact/path, user constraints, known
  findings, unresolved questions and immediate next action across compaction,
  interruptions and resume. This task state must not depend on semantic recall.
- Prioritize the latest explicit user instruction over recovered historical text.
- Once enough evidence exists, write a concrete report first, then improve it if
  necessary. Do not delay a useful artifact for exhaustive rediscovery.
- Add progress-aware detection of repeated reads/searches with no new evidence or
  artifact progress. Recover by synthesizing/writing; do not impose an arbitrary
  fixed iteration limit on productive work.
- Verify artifact existence/content and return its path before marking a task done.

Acceptance: replay a report request with known findings, interrupt and compact it,
then send `continue` and `just write the md file`. The artifact is written promptly,
constraints survive, and repeated recall is unnecessary.

## P0: Real token consumption is unacceptable for the work delivered

**Observed and user-confirmed.** UI: **2,119,552 tokens**; DeepSeek usage independently
checked by the user. The session produced no requested architecture/report artifact.

Large repeated prompt inputs can legitimately accumulate millions of input tokens:
context gets resent on model calls even if tool outputs look short in the UI.
The transcript proves substantial repetition, but does not contain enough per-call
usage to assign the two-million total precisely. Investigate rather than inventing
a per-call breakdown or dollar estimate.

Fix requirements:

- Produce a per-call accounting report: parent/child session, provider/model,
  request identity, input/output tokens, cached-input usage where available,
  duration, request outcome, retries, purpose and budget/reservation settlement.
- Split main-loop, child, compaction and other secondary model calls. Count each
  provider call once in the overall total; separately expose inclusive parent
  charges used for enforcement.
- Separate actual provider usage, estimated usage, outstanding reservations and
  conservative charges for failed/unknown requests in the UI and diagnostics.
- Profile prompt composition: core/system instructions, automatic skills, tool
  schemas, current task state, recent conversation, summaries, retrieved text and
  repeated tool results. Identify what is resent and why.
- Bound irrelevant retrieval and duplicate catalogs before they enter prompts.
  Cache deterministic discovery and avoid repeatedly paying for the same context.
- Apply explicit session/task/child token and cost budgets to all model paths,
  including compaction and retries. Reconcile existing budget behavior first.
- Show spend and an actionable stop reason when spending stops. No silent loops,
  no dropped accounting and no claims that a budget refusal means task success.
- Tune limits from observed productive workloads and the user's constraints;
  avoid a universal arbitrary token ceiling or resurrecting max-iterations.

Code entry points: `machina/core/usage.py` (reserve/finish/summary),
`machina/core/agent.py`, `machina/core/compression.py`, `machina/core/orchestrator.py`,
provider usage parsing, and the gateway/UI usage display.

Acceptance: reconcile provider totals with ledger/UI for this session; explain
any differences; replay the artifact task and compare total actual input/output
usage, calls, completion and latency. Reduced spend without a delivered artifact
is not a pass. Do not make extra paid benchmark calls without a bounded purpose.

## P0: Delegation reports failed child runs as successful completions

**Confirmed code defect + observed live manifestation.** Export lines 4,397–4,404
show `[agent responded]` followed by `LLM provider error. Check server logs.`
The database output records both as `complete`.

`AgentResponse.outcome` defaults to `completed` (`core/models.py:138`). Provider
failure responses explicitly set `outcome="failed"` (`core/agent.py:1713,1951`).
Yet `core/orchestrator.py:329` only checks approval pauses, choosing `complete`
otherwise; it does not consume the typed failure outcome. The wrapper at
`tools/agents.py:115–123` then renders the false success label.

Fix: map typed child outcomes to persisted delegation status and tool outcome;
propagate safe failure details and usage. Keep waiting-approval semantics intact.
Do not detect failure by searching response prose. Ensure parent strategy/growth
attribution cannot reward a failed child because the wrapper says “responded”.

Acceptance: child returns failed AgentResponse without raising; wrapper, DB,
parent handling and diagnostics all report failure, while retained child usage is
charged exactly once. Cover provider failure and budget failure in both loop modes.

## P0/P1: Delegation timeout settings contradict the actual tool path

**Confirmed code defect.** `Orchestrator.DELEGATION_TIMEOUT_SECONDS` is 120 seconds;
agent timeout metadata also says 120. However `tools/agents.py:90` sets an ambient
deadline to `loop.time() + 60`, inherited by the orchestrator's `wait_for`.

The session's successful isolated probe at lines 9,402–9,408 prints:

```text
orchestrator.DELEGATION_TIMEOUT_SECONDS = 120.0
agent._TOOL_TIMEOUTS['delegate']        = 120.0
effective child budget (s)              = 60.0
```

All five timed-out rows in the supplied export lasted approximately 60 seconds.

Fix: one authoritative delegation policy/deadline, clipped to inherited deadlines;
retain owner-aware cancellation, bounded cleanup, context-variable restoration,
authority intersection and complete usage receipts. Test through the actual
registered delegate tool, not only a direct orchestrator call.

**Important:** correcting 60 vs 120 does not establish the root cause of slow or
broken children. One-grep children used roughly 70k–76k recorded tokens before
failing. Simply doubling timeout can increase waste without fixing completion.

## P1: Provider errors are not sufficiently diagnosable

**Observed.** Two children failed immediately with generic provider text. No safe
request-correlated explanation was presented to the parent. Five later failures
had a different mechanism: timeouts. They must not be conflated.

Ranked hypotheses to test, not established diagnoses:

1. Child context/tool execution plus repeated LLM calls consumed the short deadline.
2. Provider request/response incompatibility or transient errors caused early failures.
3. Child cancellation/retry behavior or prompt growth amplified failed work.

Inspect existing sanitized logs for this session before calling the provider again.
Expose correlation ID, provider/model, error class, safe HTTP status when known,
retry count, effective deadline and charged/actual usage. Preserve secret redaction.
Do not claim a 60-second timeout explains the earlier 5s/10.7s provider errors.

## P1: Repeated recall retrieves catalogs and its own previous searches

**Observed + confirmed retrieval mechanism.** Queries such as `just write the md`,
`deliverable: markdown file with test evidence` and `write the md file delegation
doc` repeatedly return agent catalogs, old tool output and earlier recall output.
The screenshot shows this feedback loop directly.

`core/session_recall.py::discover` builds an OR tsquery, ranks all owned live and
archived transcript chunks with `ts_rank`, and only uses recency as a tie-breaker.
There is no current-session/task or message-role filter in the tool surface.
Deduplication is by chunk identity, so multiple distinct copies of a catalog or
recall result can dominate the first page. The wrapper emits short leading
previews; `session_recall_window` caps message representations at 300 characters.

Fix requirements:

- Resolve active instructions from current task state/recent user messages first.
- Add appropriate session, role/type and recency filters and better query matching.
- Suppress or downrank repeated tool catalogs and retrieval-result echoes for
  instruction recovery; retain explicit access for debugging evidence searches.
- Diversify/deduplicate semantically redundant results and show relevance/source.
- Retrieve enough of the relevant user instruction rather than just an arbitrary
  first 160/300 characters; keep the total output bounded.
- Stop repeating near-identical queries when they produce no useful new evidence.

Acceptance: with repeated catalog/recall chunks in the archive, instruction-recovery
queries find the actual user's report request and never need 15 repeated searches.
Maintain agent ownership isolation; this is not permission to search other users.

## P1: Compaction loses the useful task state and distorts evidence

**Observed.** The summary at line 9,723 reports “5 attempts, 4 timeouts” and associates
14k–19k usage with timeouts. The actual record has seven real children: two provider
errors and five timeouts, the latter with roughly 34k–76k tokens each. Its text ends
mid-description. The preceding summary at 4,385 also ends mid-description.

`core/compression.py:396–415` requests a generic summary under 200 words with 500
output tokens. That mechanism does not require explicit deliverable, constraints,
next action or unresolved hypotheses. A weak summary can preserve noisy history
while losing what the agent should do now.

Fix: keep a structured task checkpoint independently of free-form summary;
validate required fields and prioritize the latest ask, review-only constraint,
report path, proved findings and next action. Preserve quantitative evidence or
references to it without rewriting it inaccurately. Detect incomplete summaries.
Do not recover task state solely by repeatedly searching those same archives.

Acceptance: compaction/resume preserves the report request and correct counts,
then continues writing instead of restarting investigation.

## P1: Discovery is noisy, truncated and confused with skill invocation

**Observed.** The assistant had a real agent roster at line 89, later attempted
`ecc-codebase-onboarding` and `ecc-architecture-decision-records` as delegate names
(4,101–4,107), then requested more rosters because its view was truncated. These
are skill-like names, not registered agent targets.

Six explicit list calls occurred. The catalog opening `- a11y-architect:` appears
86 times in the export, including copies inside recall results; this does not
mean there were 86 original list calls.

`tools/agents.py:134–141` returns the entire roster. `core/agent.py:1377–1383` gives
ordinary tool responses only 1,000 characters, making a full roster impractical
for model consumption and contributing to repeated lookup.

Fix: provide bounded query/filter/pagination for agent selection, validate names
against registered targets, and distinguish skill references from executable
agents/tools/commands in runtime guidance. Use short capability discovery results
with explicit continuation rather than dumping the entire catalog.

The export has no explicit skill-list/read calls. **Do not infer that no skill was
used:** `skills/runtime.py::automatic_skill_context` automatically selects bounded
references before model calls. Instrument which skills were selected, loaded and
actually used, so “I ran ECC skills” becomes an auditable claim. A skill helps an
existing task; it must not invent extra work or imply uninstalled capabilities.

Acceptance: appropriate existing agents are selected without nonexistent names
or repeated full-roster calls, and relevant skill use is visible in traces.

## P1: Effort and scope do not match the user's request

**Observed, with ambiguity acknowledged.** “can we test the task workflow what do
you do for large task and codebases” could reasonably invite a demonstration, but
it did not identify a defect to change. The assistant picked its own router fix.
The user subsequently clarified that explanations should not trigger full work.

Later the user explicitly says “dont implement changes just review” (line 4,409).
That is the controlling scope for the architecture review and must survive resume.
No evidence establishes that subsequent review turns made new code edits; do not
misreport earlier edits as violations of that later instruction.

Fix: distinguish explanation, demonstration, read-only review and implementation;
keep authorization separate from the cheap routing heuristic. Prefer a direct
explanation when asked to describe workflow. Relevant tools are appropriate when
asked to inspect a live setting or repository. “General question” must not become
a blanket exemption from verification for dynamic or repository-specific claims.

Also correct the assistant's claim that the router prevents the action entirely.
`core/intent.py` says explicitly that it guides skills/strategy, not authorization
or semantic classification. The regex bug can affect guidance; this transcript
alone does not prove that a requested mutation can never execute.

Acceptance: greeting -> direct answer; workflow explanation -> proportional prose;
explicit repository review -> evidence with no edits; explicit refactor -> planned,
verified implementation. Mixed requests remain eligible for appropriate work.

## P2: Persona self-description confuses identity, style and retrieval settings

**Observed.** At line 24 the agent treats `harness` as the persona and infers
`persona_set=false` means no authored persona. The live snapshot at line 1,206
instead shows an available SoulSpec persona, no explicit voice/traits, three active
learned notes and no human style override.

Fix: explain agent identifier/role, authored SoulSpec identity, `/persona` human
style override and agent-owned learned guidance separately but as one effective
persona. `trust` is an emotion-weighted recall setting, not a felt emotion or a
complete personality. Do not infer absence of identity from absence of an override.
Use current trusted context directly; inspect live state only when needed.

## P2: Growth exists but does not yet prevent these repeated failures

**Observed.** The agent saved the effort-matching lesson after three overlength
rejections (lines 1,136–1,149). Live status confirms that lesson was available.
Nevertheless the later investigation became an unproductive loop.

Fix: respect known note limits before submitting; capture concise, verified
lessons about missing deliverables, skill/agent confusion and repeated recall.
Use stable keys and preserve human revision/trial semantics. Keep notes fallible,
not a substitute for task state or a grant of authority. Verify whether lessons
actually reach prompts after compaction; do not claim self-improvement merely
because persistence works or the agent writes a success story.

Acceptance: note capture fits bounds in one call; later task replay measures
whether the observed failure is avoided. No model-weight-training claim.

## P2: Investigation commands produce avoidable noise and misleading evidence

**Observed.** Repeated large file reads, broad grep/ls, searching `src/` in a
`machina/` package, and rediscovering previously read ranges dominate the session.
One pytest run targeted nonexistent `tests/test_skill_workflow.py` (line 996),
then a corrected run passed 84 tests. Several probe scripts failed because DB was
not connected, connection lifecycle spanned separate loops, or active scope did
not match. These probe setup errors are not established production DB defects.

Fix: read targeted ranges once, cache an evidence map, discover file names before
running tests, and preserve command exit status instead of masking it behind
`tail` pipelines. Use isolated test fixtures for deadline/status reproductions.
Keep diagnostics focused on a falsifiable question. Record exact verified scope:
84 affected tests were run, not the whole suite.

The final report says “Nothing else touched” while also saying it appended the
feature ledger; later git output confirms that ledger changed. Include every
modified file in summaries. Avoid claiming parallel execution just from adjacent
exported tool entries; those entries do not prove concurrency timing.

## P2: Export fidelity needs checks, but this file does not prove truncation

The user previously reported exports truncating after resume. This supplied export
contains archive/live material, summaries and the final interrupted turn. It ends
with `[interrupted]`; that can accurately reflect stopping the agent. Do not claim
its ending proves lost messages without comparing persisted transcript data.

Fix verification: compare exported message/tool IDs and chronological order
against persisted live+archive chunks before/after compaction, interruption and
resume. Require exactly-once coverage, full relevant user instructions and truthful
truncation markers. Distinguish UI previews, model-visible truncation, stored
payload truncation and export omissions. Add a sidecar/diagnostic usage summary so
future incident exports can substantiate token attribution without leaking secrets.

## Implementation order and required verification

1. Preserve this incident as a replay fixture with sanitized evidence; preserve
   current working changes. Write an implementation plan tied to the above issues.
2. Fix typed delegation failure mapping and the deadline discrepancy with failing
   regressions through the real tool boundary. Keep authority/cancellation/usage
   protections, especially `tests/test_failed_delegation_usage.py`.
3. Reconcile DeepSeek actual usage, ledger and UI; identify expensive call paths
   and context composition. Do not optimize only the display counter.
4. Fix current task-state survival and artifact completion before adding more
   exploration, autonomous reflection or more delegates.
5. Improve recall/discovery/progress handling and bounded relevant context.
6. Verify persona explanation, skill attribution and growth lessons against the
   actual shared runtime; update affected docs and honest status summaries.
7. Run focused regressions, then existing relevant backend checks. Replay the real
   failure with a bounded, declared cost if live model validation is necessary.

Minimum test matrix:

| Scenario | Required result |
|---|---|
| Greeting/self-contained workflow description | Proportional direct response |
| Read-only architecture review | Relevant evidence, no code edits, final report |
| Report request + compaction + interrupt/resume | Task/constraints survive; file delivered |
| Failed child AgentResponse | Failed wrapper/DB outcome; correct retained usage |
| Delegate tool deadline/inherited shorter deadline | Consistent effective deadline and reset |
| Child timeout/cancellation/descendant usage | Cleanup drains; authority retained; no lost/double charge |
| Archive full of catalogs/recall echoes | Actual user instruction retrieved first |
| Nonexistent skill-like delegate name | Validated discovery; no paid child launch |
| High spend with no task progress | Bounded recovery/stop reason and truthful usage |
| Export after compaction/resume | Exactly-once chronological transcript coverage |

Completion evidence must include produced artifact paths, exact test commands and
results, before/after actual usage and completion for the replay, and remaining
uncertainties. A larger timeout, a saved growth note, a green unit suite or an
assistant's own assertion of improvement alone is insufficient.
