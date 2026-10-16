# A0 — Baseline, Definitions, and Integration Map

Spec: `~/Desktop/Machina_Revised_Deterministic_Skills_and_Rolling_Context_Plan.md`
(1195 lines; §3.4 labels, §8.2 corpus, §16 migration plan, §20 evidence template).
Date: 2026-10-16. Captured from the working tree at **2026-10-16T18:24Z**.

Evidence discipline: every anchor below was read from the current checkout, not from
memory. No test suite was executed for this document; the only commands run were
read-only probes (import + classify + rank/plan) recorded verbatim below. Line
numbers refer to the **dirty working tree** at the capture time and can drift while
other agents edit hot files (see §6) — re-resolve with the fingerprint before migrating.

---

## 1. Actual checkout

| Item | Value |
|---|---|
| Repo | `/Users/shadow/machina` |
| `git rev-parse HEAD` | `b7bd6b611e5323cf0d1db0ea3e1a9f6ae1acbb2a` (unchanged across captures) |
| `git diff \| shasum -a 256` — first capture | `6e4c38d202a36667e74e59975953d3fd3cf8a7355344c7c319d73e18d12dfc50` |
| `git diff \| shasum -a 256` — 18:24:18Z capture | `2bf3a0bf99c9545c98a7b1b4d6e99e4bd6acda8ab2e9f561985bf31ab813c790` |
| `git diff --cached \| shasum -a 256` | `e3b0c442…b855` (empty; nothing staged) |
| Dirty entries (`git status --porcelain \| wc -l`) | 103 = 80 `M` (tracked) + 23 `??` (untracked) |

The diff fingerprints prove the tree was being edited **during** A0 capture
(file count moved 102 → 103). A re-check at `2026-10-16T18:26:18Z` showed 106
dirty entries and `git diff` sha `854ce4d6…a2d34` — still moving, plus A0's own
handoff file (one new untracked doc). Use the fingerprint you observe at
migration start; the anchors below belong to the 18:24:18Z capture.
The HEAD commit matches the spec §2 GitHub revision, so the snapshot and the
repository agree at commit level; the working tree does not.

**Implementation starts from a dirty tree containing the owner's in-flight
token-efficiency / nowplaying wave.** Evidence (untracked unless noted):
`docs/TOKEN_EFFICIENCY.md`, `docs/verification/TOKEN_EFFICIENCY_2026-10-16.md`,
`machina/core/{artifacts,nowplaying,pricing,projection,reducers,tool_views}.py`,
`machina/core/provider/{prompt_cache,usage_map}.py`,
`machina/gateway/features/nowplaying.py`, `machina/tools/artifacts.py`,
`ui/src/{components/NowPlayingBar.ts,features/music.ts,nowplaying/}`; modified
`machina/core/{agent,prompt,config,assembler,compression,usage}.py`,
`machina/skills/learning.py`, `machina/gateway/features/__init__.py` (hot files, §6).

Test harness: `/Users/shadow/Machina-DevTools` is a separate repo (HEAD
`a8b45429…`, 5 modified test files) whose `tests/`, `machina/`, `ui/`, `skills/`
are symlinks into `/Users/shadow/machina`. Tests live there, not in the main repo
(there is no `/Users/shadow/machina/tests`).

---

## 2. Catalogue snapshot and current command results

Read-only probe on the primary checkout with the repo venv
(`.venv/bin/python`), registry loaded once:

```text
total_loaded 76   enabled 76
by_source {'builtin-ecc': 42, 'local': 34}
```

34 local skill ids are installed under `skills/` (`skills/*/SKILL.md`; learned
dir `~/.machina/skills` is empty: `registry.py:208-223`). Runtime-effective
counts depend on config/telemetry and were not exercised through a provider.

### Intent behavior (`machina/core/intent.py`)

```text
'make me a website'                           -> classify='ambiguous'      conversational=False
'build a landing page'                        -> classify='implement'      conversational=False
'what is a website?'                          -> classify='explain'        conversational=True
'explain how someone would build a website'   -> classify='explain'        conversational=True
'review this website, do not edit'            -> classify='review-readonly' conversational=False
'build a portfolio website'                   -> classify='implement'      conversational=False
'compare my stock portfolio'                  -> classify='ambiguous'      conversational=False
'add tests'                                   -> classify='implement'      conversational=False
'switch the implementation to Vue'            -> classify='ambiguous'      conversational=False
'go ahead' / 'go' / 'rust' / 'swift'          -> classify='ambiguous'      conversational=False
```

Red observation confirmed: `"make"` is absent from `_ACTION_VERB`
(`intent.py:75-81`), so `"make me a website"` falls through to `ambiguous`.

### Workflow planning (`machina/skills/workflow.py`) and ranking (`runtime.py`)

```text
'make me a website'                 kind=task    skills=[]                       stages=[]
'build a landing page'              kind=task    skills=[]                       stages=[]
'build a portfolio website'         kind=task    skills=[]                       stages=[]
'compare my stock portfolio'        kind=task    skills=[]                       stages=[]
'add tests'                         kind=coding  skills=[testing-qa, ecc-search-first, ecc-tdd-workflow, ecc-verification-loop]
                                                 stages=[(inspect and plan,ecc-search-first),(domain,testing-qa),(implement and test,ecc-tdd-workflow),(verify,ecc-verification-loop)]
'switch the implementation to Vue'  kind=coding  skills=[ecc-search-first, ecc-tdd-workflow, ecc-verification-loop]   stages=[(inspect and plan,·),(implement and test,·),(verify,·)]
'go ahead' / 'go' / 'rust' / 'swift' / 'review this website'   kind=task  skills=[]  stages=[]
rank_skills('make me a website') -> []      rank_skills('build a landing page') -> []
rank_skills('website')          -> []      rank_skills('frontend') -> ['ecc-coding-standards','ecc-contract-first']
```

Two load-bearing observations:

1. Every web case in §8.2 is **red today**: no family, no candidate, no plan.
2. `selected` insertion order ≠ rendered `stages` order: for `"add tests"` the
   first two disclosed skills (`workflow.skills[:2]`, `runtime.py:164`) are
   `testing-qa, ecc-search-first` while the rendered stage order starts with
   `inspect and plan` — exactly the §10.1 incoherence.

---

## 3. Integration map (exact consumers for migration)

All anchors `file:line` from the 18:24Z working tree.

### 3.1 Skills registry / parser — `machina/skills/registry.py`

| Anchor | Behavior |
|---|---|
| `registry.py:276` `class SkillParser`; `:363` `parse()` | front-matter via regex `^---\n(.*?)\n---\n(.*)` at `:378`; `yaml.safe_load` at `:396` |
| `registry.py:404-407` | only `name`/`description`/`triggers` are consumed as metadata; unknown/extra YAML keys are silently ignored (no routing metadata, no `metadata` population — matches spec §7.2 warning) |
| `registry.py:330-343` validation | name slug, description ≤1000, triggers ≤32 × ≤100 chars; free-form fields unvalidated |
| `registry.py:653` `load_all()` | stat-signature cache: early return `:665-667`; `_signature()` `:506-531` uses `(dir, mtime_ns, size)` per `SKILL.md` + telemetry stat; `_invalidate()` `:502` |
| `registry.py:684-688` | malformed skill skipped with warning, never fatal |
| `registry.py:689-694` | provenance precedence: a local file cannot shadow `builtin-ecc`; local claiming ecc type is forced to `local` |
| `registry.py:698-744` | `get`/`list_skills`/`get_skill_content`/`record_use`/`record_view` |

### 3.2 Search / ranking

| Anchor | Behavior |
|---|---|
| `machina/skills/runtime.py:62` `class SkillIndex` | enabled-only index (`:64`); IDF map `:75-78` |
| `runtime.py:81-123` `rank()` | whole-word triggers `:85-88`; technology guard `:89-96`; IDF gate `:101`; trigger boost `+25` `:115-116`; learned weights clamped ×[0.8,1.2] `:117`; deterministic tie-break `(-explicit,-score,name)` `:122` |
| `runtime.py:126-138` `rank_skills()` | `WeakKeyDictionary` cache `:40`, inline signature `(name, description, triggers, enabled)` `:131-133`; `skills.load_all()` first `:129` |
| Call sites | `workflow.py:128`; `tools/skills.py:55` (`skill_list`); `ports.py:60-73` (facade); `controller/preselect.py:44` (per-turn cache) |
| `machina/skills/ports.py:31` `SkillSource` Protocol | consumer seam ("Stream B" wave, committed at HEAD); signature-locked by `tests/test_architecture_pairs.py` in DevTools |
| `controller/preselect.py:27` `SkillPreselectCache` | exported `controller/__init__.py:13`; **no production caller** found (only export + `controller/README.md`) — decide keep/migrate |

### 3.3 Workflow planning — `machina/skills/workflow.py`

`SkillWorkflow(kind, skills, stages)` dataclass `:25-29`; `render()` `:31-47`.
`plan_workflow` `:72`: `conversational` early-out `:84`; inherited-stack carry-over
from `recent`/`goal` for ≤16-word follow-ups `:91-127`; ambiguous-tech guard
`{"go","rust","swift"}` `:22`, `:135-141`; coding/research/task kind `:142-164`;
`selected` + `stages` built by `add()` `:168-172` (cap 6 skills, dedupe by name);
technology anchors `:182-209`; explicit match `:210-221`; stage adds `:222-249`;
**`stages.sort` by independent `order` map** `:250-260` (the §10.1 incoherence).

### 3.4 Automatic disclosure — `machina/skills/runtime.py:141-234`

`automatic_skill_context(query, *, skills, budget=1200, can_read, can_search,
can_resources, recent, goal, skill_weights, activated)`; budget hard clamp
`min(1200, …)` `:156`, `<100 → ""` `:157-158`; plans then takes
`workflow.skills[:2]` `:164`; header/render `:165-196`; whole-context budget
guard `:197-198`; empty when no matches `:199-203`; per-skill excerpt with
character-offset continuation locator `:205-227`; `record_use` at disclosure
`:231`; `activated` out-param `:232-233`.

**Only production entry point:** `core/prompt.py:300` imports it, `:311` calls it
inside `build_skill_section` (`prompt.py:283-322`), which `build_system_prompt`
concatenates at `prompt.py:401-410`. `build_system_prompt` is called only from
`_prepare_context` (`core/agent.py:536`, call at `:717-729`), which both loops use
(`agent.py:1812` run, `:2029` stream). Continuation interface:
`tools/skills.py:35` `skill_list`, `:103` `skill_read`, `:137` `skill_resource`;
effects declared `:167-172`.

### 3.5 Intent layer — `machina/core/intent.py`

`TASK_CLASSES` `:83-91`; `conversational()` `:94-99`; `classify_task()` `:102-123`.
Consumers: `workflow.py:9,135`; `controller/triage.py:9,23` (`resolve_task`,
advisory `agent._last_triage` via `controller/wiring.py:124-126`);
`controller/complexity.py:15,77`; `memory/rsi.py:15` (`conversational`);
`gateway/server.py:783` (trace label only).

### 3.6 Prompt assembly

`build_system_prompt` `prompt.py:371-417` (persona `:396`, capability `:399`,
`system_base + skill_text` `:400-410`, output policy `:411-413`, triage hint
`:414-416`); `build_skill_section` `:283-322` computes `available` against
`system_base` and passes `budget=min(1200, session.context_budget // 5, available)`
`:313`. Assembler slot: `core/assembler.py:97` param, `:156 add("system", …)`.
`assemble_turn_prompt` `prompt.py:420-438`. Disclosure output therefore enters
the prompt as the skills suffix of the system message.

### 3.7 In-turn condensation

`core/prompt.py:202-232` `condense_history`; constants `TOOL_RESULT_STALE_CAP`
`:71`, `SKILL_PAGE_RE` `:76`, `_REDUCED_VIEW_TAIL_RE` `:84`, `stale_tool_preview`
`:93`. Agent wrapper `agent.py:756-764` (import `:48`, `__all__` `:87`); call
sites `agent.py:1841` (run) and `:2058` (stream). Skill pages and reducer views
are exempt from condensation (`prompt.py:228-230`) — old offsets keep resolving.

### 3.8 Capacity guard (final fallback)

Run loop: `agent.py:1841-1866` (condense → tool defs → `_request_context_tokens` →
compare `effective_budget` → warn + `audit_log("agent_request_context_exceeded")`
`:1852` → flush → `AgentResponse(outcome="failed")` `:1860-1866`).
Stream loop: `agent.py:2058-2089` (same, `done` event `:2079-2088`).
Budget/tokens helpers: `prompt.py:130-142` (`effective_turn_budget`),
`prompt.py:145-167` (`request_context_tokens`).

### 3.9 Post-turn compaction — ownership

`core/turn_engine.py:699-716` `schedule_auto_compact()` (fire-and-forget
`asyncio.create_task(services.maybe_auto_compact)`, runtime-tracked).
`services.py:410-518` `maybe_auto_compact`: config gates `:419-423`; **MUTATION
claim** `begin_mutation` `:432-438` (busy → defer, never queue); threshold on
`_estimate_next_request` (`services.py:325`) vs `budget*threshold` `:445-451`;
no-progress guard `:452-458`; `compress_session(..., mutation_token=token)`
`:461`; ownership re-check `:464-474`; `last_auto_compact_tokens` merge `:478-487`;
optional eviction `:488-501`; `end_mutation` in `finally` `:512-517`.
Callers (after ownership release): `gateway/server.py:1158-1167` and
`api/routes/streaming.py:483-485`. Defaults: `config.py:259-263,308-309`
(dict) and `:600-604,625-626` (dataclass) — `auto_compaction_enabled=true`,
`compression_enabled=true`, `threshold=0.8`, `target_ratio=0.5`,
`preserve_recent=3`, `llm_summarize=true`, `eviction_max_tokens=0`.

### 3.10 Gateway / UI / CLI

Gateway handlers `gateway/features/skills.py:22-139` (`skills.list/show/learn/
delete/curator`, `learning.list/approve/reject/revert`), registered
`gateway/features/__init__.py:124-128` imports + `:212-216` method map.
`skills.list` keeps `gw.require_db()` `skills.py:24`; builtin-ecc delete guard
`:68-69`. TUI consumer `ui/src/features/skills.ts:9-138` (`skills.list` `:29`,
`skills.show` `:43`; learning sub-commands `:58-107`), registered
`ui/src/features/index.ts:10,73`. CLI consumer `cli/__init__.py:929`
`skills_list` typer command. **No `skills.search`, `preview_route`, `skills.section`,
`TaskDescriptor`, `DisclosurePlan`, `RequestProjection` or `project_request`
exist anywhere in `machina/` or `ui/src/` (grep-verified empty).**

### 3.11 Adjacent seams discovered (not in the spec appendix)

- `machina/controller/{triage,complexity,preselect,wiring}.py` — committed
  advisory hooks; `wiring.py:121-147` `record_triage`/`log_complexity`.
- `machina/skills/ports.py` — consumer seam over registry/runtime.
- `machina/core/projection.py` (untracked, owner wave) is **evidence projection**
  (dedupe/budget of retrieved chunks) — unrelated to Track B "request
  projection". Keep names distinct to avoid collision.
- `machina/core/reducers.py` (untracked) references `condense_history` `:35`.

---

## 4. Current behavior inventory (spec §3.4 labels)

| Label (spec §3.4) | Status | Current evidence |
|---|---|---|
| Historical tool-result condensation | **present** | `prompt.py:202-232` + both loop call sites `agent.py:1841,2058` |
| Session compaction at safe boundaries | **present** | `turn_engine.py:699-716`, `services.py:410-518`, callers after release |
| Model-capacity preflight | **present** | `agent.py:1845-1866`, `:2062-2089`; final fallback only |
| Durable, budgeted, recoverable in-turn working-set projection | **NOT established** | no `project_request`/artifact recovery path exists; overflow still returns `outcome="failed"` (`agent.py:1861,2082`) |

Selected ≠ disclosed ≠ followed ≠ verified applies throughout: `record_use` at
disclosure (`runtime.py:231`) is activity telemetry, not compliance.

---

## 5. Deterministic request corpus (A0 deliverable 2)

`/Users/shadow/Machina-DevTools/tests/data/deterministic_skills/requests.json`
— 15 cases derived from spec §8.2 / §18 (S02–S11). Each case:
`{id, kind, input, context?, required:{task_family|null, stack_resolution,
constraints_preserved[], must_not[]}}`; schema semantics in the adjacent
`README.md`. JSON validated with `python3 -c json.load` (see §8 below).

---

## 6. Hot files (uncommitted owner edits — do not edit, expect drift)

`machina/core/agent.py`, `machina/core/prompt.py`, `machina/core/config.py`,
`machina/core/assembler.py`, `machina/core/compression.py`, `machina/core/usage.py`,
`machina/skills/learning.py`, `machina/gateway/features/__init__.py`.
All eight were modified at capture time (line numbers above may drift first in
these files; `agent.py` capacity anchors already shifted ~+18 lines vs the spec
snapshot's 1822–1844/2037–2064).

---

## 7. Test assets — present vs unavailable

Present and relevant (in DevTools; **not executed by A0**):
`tests/test_skill_registry_cache.py`, `test_skill_runtime.py`,
`test_skill_workflows.py`, `test_turn_intent.py`, `test_ecc_skills.py`,
`test_fm6_skill_read.py`, `test_fm6_workflow_stages.py`,
`test_history_condensation.py`, `test_compression.py`,
`test_compaction_checkpoint.py`, `test_holistic_h04_compaction_prompt.py`,
`test_architecture_pairs.py`, `test_r5_019_skill_atomicity.py`;
`ui/test/skills-revert.test.ts` (covers only `learning.revert`).

**Absent / unavailable explicitly:**
- No test anywhere mentions `"website"`, `TaskDescriptor`, `DisclosurePlan`,
  `RequestProjection`, `skills.search` or `preview_route` (grep-verified).
- No suite run exists for this checkout at this fingerprint; historical logs in
  `docs/verification/` are not current evidence (spec §17 A0, §19).
- Dynamic/provider, DB, UI-App and rolling-working-set (§18 C01–C16) tests are
  unexecuted; none may be reported as passing from this document.
- DevTools itself is dirty (5 modified test files) — test expectations there are
  not a frozen baseline either.

---

## 8. Consumers to migrate (spec §16)

| # | Consumer | Current API (anchor) | New API (spec) | Compatibility requirement |
|---|---|---|---|---|
| 1 | Registry parse | `SkillParser.parse` `registry.py:363`; `load_all` `:653` | validated v2 metadata + v1 fallback (§7.2, A1) | stat-signature cache semantics `:665-667,:506`; skip-bad-skill `:684-688`; ECC precedence `:689-694`; case-insensitive ids |
| 2 | Ranking core | `SkillIndex` `runtime.py:62`; `rank_skills` `:126` | `SearchResult` with applicability/reasons (§9.1, A2) | exact-id/trigger boosts `:85-88,:115`; deterministic tie `:122`; enabled filter `:64`; no provider/embedding call (§15) |
| 3 | Ranking callers | `workflow.py:128`; `tools/skills.py:55`; `ports.py:60`; `controller/preselect.py:44` | same via `ports`/descriptor query | `ports.rank_skills` signature lock (`ports.py:65`); `skill_list` 4-per-page paging (`tools/skills.py:56`) |
| 4 | Workflow planning | `plan_workflow` `workflow.py:72-261` | one planned-step representation, stable step IDs (§10.1, A3) | `SkillWorkflow.render` labels/order `:31-47`; stage order map `:251-259` |
| 5 | Automatic disclosure | `automatic_skill_context` `runtime.py:141-234` | `DisclosurePlan` stage-aware selection (§10.3, A3) | ≤1200 budget `:156`; ≤2 skills `:164`; char-offset continuations `:223-227`; `record_use` disclosure event `:231`; empty on no evidence `:199-203` |
| 6 | Intent | `classify_task`/`conversational` `intent.py:94,102` | `TaskDescriptor` on top; no third classifier (§8.1, A2) | `TASK_CLASSES` values `:83-91`; `resolve_task` semantics `controller/triage.py:14-32`; RSI use `memory/rsi.py:15` |
| 7 | Prompt assembly | `build_skill_section/build_system_prompt` `prompt.py:283,371`; call `agent.py:717` | descriptor/disclosure-driven sections | compose order + triage hint `prompt.py:396-416`; system slot `assembler.py:156` |
| 8 | In-turn condensation | `condense_history` `prompt.py:202`; `agent.py:756-764`; loops `:1841,:2058` | Track B request projection (B1/B2, §14) | skill-page/reduced-view exemptions `:228-230`; latest-batch preservation; keep current fallback until B2 proven (§5.9) |
| 9 | Capacity guard | `agent.py:1845-1866,2062-2089` | structured safe refusal from shared projector (§14.5) | audit event + `outcome="failed"`; never truncate pins (§14.3); no side-effect replay |
| 10 | Post-turn compaction | `turn_engine.py:699`; `services.py:410` | unchanged; coexist with projection (§14.7, B3) | MUTATION claim/defer/re-check `services.py:427-438,:464-474`; callers only after release `server.py:1167`, `streaming.py:485` |
| 11 | Gateway skills | `features/skills.py:22-139`; map `features/__init__.py:212-216` | additive `skills.search/preview_route/section` (§12.2, A5) | `skills.list` shape + `require_db` `:24`; builtin-ecc delete guard `:68-69` |
| 12 | TUI `/skills` | `ui/src/features/skills.ts:9-138`; reg `features/index.ts:73` | searchable interactive picker + preview (§12.3, A5) | sub-command names preserved; selection by stable skill ID; unsupported destructive controls stay absent |
| 13 | Native skill tools | `tools/skills.py:35,103,137` | version-bound section reads additive (§10.4, A3) | character-offset reads keep working; `skill.read` effects `:167-172` |
| 14 | Telemetry | `registry.py:736,744`; calls `runtime.py:231`, `tools/skills.py:121` | observed event names (§13, A4) | legacy counters preserved; disclosure is not success |
| 15 | Controller seam | `controller/{triage,complexity,preselect,wiring}.py` | share `TaskDescriptor` (A2) | advisory-only, never-raising hooks `wiring.py:121-147`; preselect cache currently uncalled — keep or retire with evidence |
| 16 | CLI | `cli/__init__.py:929` | unchanged listing | table output preserved until shared contract works |

---

## 9. Proposed quality/performance gates (freeze before A6 evaluation)

- Corpus: all 15 `requests.json` cases produce the `required` decisions; no
  regression on the existing DevTools skills/intent tests.
- Determinism: selection/preview make **zero provider or embedding calls**; same
  catalog + policy revision → identical ordering (ties stable).
- Disclosure: budget bound holds; a cut instruction returns a locator, never a
  "loaded" claim (§10.3).
- Track B: no completion claim until B2 proves both loops with captured
  payloads; numbers (latency/reduction) measured per §15, not promised.

---

## 10. Unexecuted checks (environment blockers)

- No pytest/vitest run in either repo (per A0 scope; spec §17/§19 discipline).
- No DB-backed, provider-backed, gateway/HTTP or UI-App execution.
- No watcher/cache-invalidation, section-anchor, or artifact-recovery behavior
  exercised — the corresponding contracts do not exist yet.

## 11. Rollback path

A0 changes no production code. Rollback = delete this document and the DevTools
corpus directory; the tree returns to the owner's dirty state fingerprints above.
