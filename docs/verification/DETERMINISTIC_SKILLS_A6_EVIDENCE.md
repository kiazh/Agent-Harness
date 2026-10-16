# A6 — Track A Integration and Release Evidence

Spec: `~/Desktop/Machina_Revised_Deterministic_Skills_and_Rolling_Context_Plan.md`
(§15 performance contracts, §17 A6, §18 deterministic cases, §19 verification
hierarchy, §20 evidence template, §21 release checklist).

Date: 2026-10-16. Capture window `16:41–17:05` local. Every number below was
produced by executing the commands in §2 against the checkouts below; nothing
is copied from earlier reports. Two agents were writing in the same working
tree during capture (B2: `machina/core/request_gate.py`, `machina/core/agent.py`,
`machina/core/config.py`, `machina/tools/artifacts.py`; plus an uncommitted
follow-up in `ui/src/features/skills.ts` / `ui/test/features.test.ts` that makes
a typed `/skills list` open the interactive directory on overlay hosts — A6
adapted the one App test it invalidated), so the dirty-state fingerprints moved
while A6 ran — each capture states what it inspected.

---

## 1. Exact checkout state (capture time)

| Item | Value |
|---|---|
| Machina repo | `/Users/shadow/machina` |
| `git rev-parse HEAD` | `0f7eb2c70dc24083d201b966f12079d8395f9087` (`feat(context): wire both loops through the shared request gate (B2, shadow default)`) |
| Track A commits inspected | `9cfff3c` A3 (`planned_steps`, sections, stage-aware disclosure), `0908f68` A5 backend (gateway `skills.search/preview_route/section`), `1086f57` A4 (`revisions`, `events`, generation-guarded caches), `002cc19` A5-UI (interactive `/skills` directory) |
| A0 baseline commit (frozen gates origin) | `b7bd6b6` |
| Other commits present | `e37f3dc` B0 artifacts, `0f7eb2c` B2 loop wiring |
| `git status --porcelain \| wc -l` at final capture | 7 modified (my 3 UI files: `ui/src/app.ts`, `ui/src/components/SkillsPicker.ts`, `ui/test/skills-picker.test.ts`; 2 uncommitted follow-up files: `ui/src/features/skills.ts`, `ui/test/features.test.ts`; 2 concurrent B2 files: `machina/core/request_gate.py`, `machina/tools/artifacts.py`) |
| `git diff \| shasum -a 256` | `41930f12f7eef6debf981ef27c3c3963571053970bce077de03812df8d1e9ff3` (drifting while B2 writes) |
| DevTools repo | `/Users/shadow/Machina-DevTools`, HEAD `b109027` (`tests: B2 loop-wiring suite + artifact tool surface + patch-target update`) |
| A6 test file | `tests/test_a6_release_evidence.py` (new, untracked at capture) |
| UI test files (A6 additions) | `ui/test/skills-picker.test.ts` (+293 net lines incl. the concurrent U07 adaptation), `ui/src/components/SkillsPicker.ts` (+195), `ui/src/app.ts` (+14) |
| Python for all runs | `/Users/shadow/machina/.venv/bin/python` (3.14.5, editable install of the checkpoint) |

No commit, add, or stash was made by A6.

---

## 2. Commands and results (verbatim counts)

| # | Command | Result |
|---|---|---|
| C1 | `run-tests.sh tests/test_a6_release_evidence.py -q` (`MACHINA_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/machina_test`, `PYTHON=.venv/bin/python`) | **23 passed, 5 xfailed** (28 tests; ≈3.7–4.3 s across runs) |
| C2 | same file without `MACHINA_TEST_DATABASE_URL` | **22 passed, 2 skipped (DB gateway tests), 4 xfailed** |
| C3 | A6 file + `test_task_descriptor` + `test_skills_search` + `test_skills_planned_steps` + `test_skills_sections` + `test_skills_revisions` + `test_skills_events` + `test_skills_metadata` + `test_skills_gateway_search` + `test_gateway` + `test_ecc_skills` + `test_features_split`, `-q` (with DSN) | **277 passed, 5 xfailed** in 10.32 s |
| C4 | earlier eight-suite subset (same minus gateway/ecc/features/gateway-search) | 192 passed in 2.19 s |
| C5 | `ui: node --test test/skills-picker.test.ts` | **23 tests, 23 pass, 0 fail** (19 pre-existing + 4 new) |
| C6 | `ui: npm run check` (typecheck + full UI suite + eslint) | at capture: **999 tests, 993 pass, 6 skip, 0 fail**; typecheck and lint clean. Report-time re-check: the shared UI gate is red on another writer's in-flight help-palette wiring (`ui/src/components/HelpPalette.ts`, `ui/test/help-palette.test.ts`, and `HelpPaletteHost` wiring in `app.ts`/`features/types.ts` — regions outside the skills picker); A6's own UI files stay green in isolation (C5 re-run 23/23; eslint on `SkillsPicker.ts`/`app.ts`/`skills-picker.test.ts` clean). |
| C7 | `python -c "import machina.skills.planned_steps, machina.skills.revisions, machina.skills.events, machina.skills.search"` from the installed venv | ok (resolves to `/Users/shadow/machina/machina/skills/…`) |
| C8 | `.venv/bin/python -m pip wheel --no-deps --no-build-isolation . -w /private/tmp/opencode/a6wheel` | `machina-0.2.0-py3-none-any.whl`, 1 689 537 B, sha256 `9199bb9f…e052cf`, 474 entries; all A6-relevant modules present (§9) |
| C9 | ledger with `-s` (see §8) | recorded below |

Transient observation: one combined C3-style run (before the final capture)
failed `test_ecc_skills.py::test_default_skills_reach_first_provider_call[False/True]`
while B2 was saving `machina/core/request_gate.py`; the same selector passes
alone (`2 passed`) and the final C3 run passes (277 passed). Recorded as a
concurrent-edit race, not a reproducible failure.

Workspace churn during the session (not A6): `ui/test/zz-debug-skills.test.ts`
appeared and was removed again by another writer; a help-palette change
(`ui/src/components/HelpPalette.ts`, `ui/test/help-palette.test.ts`, and
`HelpPaletteHost` wiring in `app.ts`/`features/types.ts`) was mid-landing at
report time and is the sole cause of the report-time typecheck failure noted
at C6. Neither is A6's.

---

## 3. In-overlay section reader (A5-UI deferred follow-up)

Details mode in the real `/skills` overlay now has a section-read action
(`s` / `ctrl+s`) that calls the bounded `skills.section` contract with the
**shown** candidate `contentRevision` and renders:

* the versioned passage (`section: <heading> (N of M chars · declared|parsed
  anchor · content <rev8>)`) plus its text, and
* the continuation hint (`[section continues at offset N — press s]`) when the
  server returns a continuation; pressing `s` again pages the same section at
  that offset with the same revision;
* a stale response renders an explicit message
  (`section stale: <id> was requested at content <shown>, but the skill changed
  to <current> — nothing from the edited text was served`) — no passage is
  invented; the retry uses the revision the server reported as current;
* a transport failure renders `(section read failed: …)`.

No new authority: the action only issues `skills.section` (read-only). The App
test proves zero non-section RPCs after the read and zero `prompt.submit`
frames for the whole overlay session.

Tests (all through the real App or the real component; `ui/test/skills-picker.test.ts`):

* happy path (App + component): request params are exactly
  `{skillId, contentRevision, section, budget: 800}`; passage + continuation
  render; the second press continues at the server offset with the same revision.
* stale (App + component): honest stale state, empty passage, retry binds to
  the reported current revision.
* error (App + component): honest failure state, no fabricated text.

**Finding F1 (integration defect, recorded, not fixed in A6).** `skills.search`
returns the A4 *content identity*
(`machina.skills.search.content_revision(skill)` = SHA-256 over version +
content + metadata digest) as `contentRevision`, while the A5
`skills.section` handler binds the request against
`machina.skills.sections.content_fingerprint(content)` (content bytes only).
Observed live revision values for `testing-qa`:
`search=cae54bccf0a32883`, `sections=59235bb3a4c6d61d`. A client that passes
the revision it was shown therefore always observes `stale` on the first read.
The in-overlay reader degrades honestly (shows the stale state, then re-reads
against the current revision), and the live join test is pinned as xfail with
this exact reason (`test_a6_live_gateway_search_revision_joins_section_read`).
Fixing it means either normalizing the two revision identities in the A5
handler or exposing the section fingerprint in search candidates — outside
A6's allowed files. This is also why checkbox 7 in §11 is partial.

---

## 4. Held-out ranking/routing cases (§18)

Seventeen phrases not present in the spec's S01–S16 table, run through the real
pipeline (`build_task_descriptor` → `search_skills` / `plan_workflow` →
`automatic_skill_context`) with socket/subprocess patched to fail. "Observed"
is the actual output at the captured commit. `ctx` = characters of automatic
disclosure. Tests: `tests/test_a6_release_evidence.py::test_a6_held_out_pipeline`
(4 misses are non-strict xfails carrying their observed behavior).

| ID | Phrase | Frozen gate | Observed | Verdict |
|---|---|---|---|---|
| H01 | spin up a marketing site | web-build family, unresolved stack, mutating | class=ambiguous, families=(), scope=unknown, ctx=0 | **MISS** — "marketing site" outside the narrow web-noun list |
| H02 | create a simple homepage for my band | web-build family, unresolved stack | class=implement, families=(task,), scope=mutating, ctx=0 | **MISS** — "homepage" outside the noun list |
| H03 | what even is a website | explain, no web-build, read-only, no context | class=explain, read-only, ctx=0 | pass |
| H04 | don't build anything, just explain how sites work | not a build authorization; no mutating scope | class=implement, families=(task,), scope=mutating, ctx=0 | **MISS** — "build" is not in the negated-action verb set |
| H05 | walk me through how a band site gets made | no accidental website creation | class=ambiguous, no web-build, ctx=0 | pass |
| H06 | is my stock portfolio diversified | explain, not web-build | class=explain, read-only, no web-build | pass |
| H07 | review my landing page copy, don't change anything | review-readonly + read-only + no-edit constraints | class=review-readonly, constraints=(read-only,no-edit), scope=read-only, ctx=0 | pass |
| H08 | if someone says "build me a website", which skill family handles it? | quoted request is not permission | class=ambiguous, no web-build, scope=unknown; ctx names `skills-system` (2929 chars) | pass (no web-build claim) |
| H09 | add unit tests (active Python state) | continuation + inherited Python + testing capability + coding plan | class=implement, families=(coding,continuation), inherited/effective languages=(python,), caps=(testing,), kind=coding, skills=[ecc-tdd-workflow, ecc-search-first, testing-qa, ecc-verification-loop], ctx=5409 | pass |
| H10 | add unit tests (no state) | coding but **not** continuation, stack unresolved | families=(coding,), no continuation, unresolved=(stack,), ctx=5409 | pass |
| H11 | switch the frontend to Svelte (inherited React) | explicit frameworks=(svelte,), effective svelte | explicit={}, inherited/effective frameworks=(react,) | **MISS** — `svelte` absent from `TECHNOLOGY_MARKERS` |
| H12 | go | ambiguous, never a language workflow | class=ambiguous, no coding, no stack, ctx=0 | pass |
| H13 | rust | same | same | pass |
| H14 | swift | same | same | pass |
| H15 | deploy my portfolio site to vercel | deployment capability, no database/payments inference | class=implement, caps=(deployment,), ctx=2698 (deployment-devops) | pass |
| H16 | craft a landing page for the launch | web-build, no backend capabilities | families=(web-build,), scope=mutating, unresolved stack, no db/payments/deploy caps | pass |
| H17 | set up a portfolio page for my photography | web-build, no backend capabilities | families=(web-build,), scope=mutating, unresolved stack, no backend caps | pass |

**Held-out totals: 13 pass / 4 miss.** The four misses are
recorded in `RECORDED_MISSES` and as xfails; no threshold, phrase list, or
vocabulary was tuned to move any verdict. (H01/H02 are coverage gaps of the
web-noun list; H04 is a negation gap; H11 is a vocabulary gap. Each fix is a
one-line-class change that would flip its xfail to XPASS.)

Explicitly NOT asserted as failures, with reasons: "walk me through…" resolves
`ambiguous` rather than `explain` (conservative, no build authorization — the
gate is "no accidental build"); H08's `skills-system` disclosure is a router
question about skill families, not a website build.

---

## 5. No-provider-call audit (§15, §19)

Three independent proofs over the routing path
(`task_descriptor`, `intent`, `runtime`, `search`, `workflow`,
`planned_steps`, `sections`, `metadata`, `revisions`, `registry`,
`gateway/features/skills` — 7 783 source lines):

1. **AST scan** (`test_a6_routing_sources_import_no_provider_embedding_or_network_module`):
   zero imports matching `machina.core.provider`, `machina.core.agent`,
   `machina.rag`, `openai`, `anthropic`, `cohere`, `mistralai`, `litellm`,
   `ollama`, embedding stacks (`sentence_transformers`, `fastembed`,
   `model2vec`, `onnxruntime`, `torch`), HTTP/network clients
   (`httpx`, `requests`, `aiohttp`, `urllib.request`), `socket`, `subprocess`;
   no `__import__`, `os.system`, `os.popen` text either.
2. **Fresh-subprocess imports**
   (`test_a6_fresh_import_of_the_routing_path_pulls_no_provider_or_embedding_modules`):
   a child interpreter imports the ten core modules → none of the banned
   roots appear (including `machina.core.images`, `machina.connectors`);
   the deterministic path then runs end-to-end in that child. For the gateway
   handler the child snapshots `sys.modules` after importing the pre-existing
   `machina.gateway.server` wiring, imports `machina.gateway.features.skills`,
   and asserts the import *delta* adds no provider/embedding/httpx module.
   (Honest transitive note: importing any `machina.gateway.*` module pulls
   `machina.core.provider` through `machina/gateway/__init__.py` → `server.py`;
   `httpx` arrives via `machina.core.config` → redaction → `security/secrets`;
   both are pre-existing platform imports, and neither is called.)
3. **Zero calls under patched socket/subprocess**:
   `test_a6_full_routing_pipeline_makes_zero_network_or_subprocess_calls` runs
   the seven-case corpus (including cold + warm search, workflow, disclosure)
   with `socket.socket/create_connection/getaddrinfo`, `subprocess.run/Popen`,
   `os.system/popen` replacing to `AssertionError`.
   `test_a6_preview_and_search_handlers_never_touch_a_provider` additionally
   installs a provider-module tripwire and calls the real
   `skills.search` (browse), `skills.section`, and `skills.preview_route`
   handlers: zero provider touches, every handler keeps its `require_db` guard.
   The live gateway run (§7) recorded zero `prompt.submit`/turn frames.

Tokenizer note: `get_token_count` uses tiktoken with a len//4 fallback; the
encoding comes from the local tiktoken cache (`/tmp/data-gym-cache` observed)
and the no-network run passes, so no download occurs on this path.

---

## 6. Human/automatic parity (§17 A6 acceptance)

Real catalog at capture: **76 skills, 76 enabled**, catalog revision
`acfd90318662d609` (all six parity queries). Automatic =
`runtime.search_skills(include_excluded=False)`; browse =
`include_excluded=True` (the `skills.search` UI shape); a third check runs the
real gateway handler and compares it field-for-field with the browse scope.

| Query | Retr. | Classification | Identical candidates + revision (auto vs browse) |
|---|---|---|---|
| make me a website | 0 | no_lexical_evidence | yes (both empty) |
| add tests | 11 | ok | yes |
| testing | 11 | ok | yes |
| deploy | 2 | ok | yes |
| website | 0 | no_lexical_evidence | yes (both empty) |
| go ahead | 3 | ok | yes |

Documented difference (fixture with one disabled skill; "deploy the service"):

| Scope | Candidates | Catalog revision |
|---|---|---|
| automatic | `team-deploy` 31.7405 applicable | `45d1d64cfb3d291c` (enabled-only scope) |
| browse | `team-deploy` 31.4716 applicable, `old-deploy` 31.4671 **excluded** ("disabled: …") | `5ecd47fbf0ef46bf` (full scope) |

The only candidate-set difference is the disabled entry itself; ordering and
applicability of enabled candidates are identical; scores shift because the
IDF statistics are catalog-dependent by construction (spec §9.2), measured at
Δ≈0.27 points and bounded <2.0 in the test; revisions match each scope's
documented membership. Tests:
`test_a6_automatic_and_browse_search_agree_on_the_real_catalog`,
`test_a6_disabled_skill_is_the_only_documented_scope_difference`,
`test_a6_gateway_browse_matches_the_runtime_browse_scope`.

---

## 7. Live-gateway protocol integration (§19.4)

Real in-process `Gateway` (`handle_line` JSON-RPC strings) against the
disposable test database `postgresql://…/machina_test` and the real 76-skill
catalog. Observed:

* `initialize` → version `0.2.0`, protocol 1 (DB connect + schema upgrade).
* `skills.search {query:"testing", filters:{include_excluded:true}, limit:5}`
  → `browse=true`, catalog `acfd90318662d609`, policy `skills-search-v1`,
  `catalog_size=76`, `retrieved=11`, top: `testing-qa` 11.39 / `ecc-tdd-workflow`
  9.2 / `alembic-migrations` 8.19; unknown filter key → `INVALID_PARAMS`.
* `skills.preview_route {request:"make me a website"}` → descriptor
  `implement`/`["web-build"]`/unresolved `["stack"]`, selection kind `task`,
  budget 1200, token basis present, **zero turn/provider frames**.
* `skills.section` (addressed with the section module's own fingerprint, the
  identity the handler compares): exact passage, `stale=false`, and a
  continuation page that tiles the section text byte-for-byte
  (`page1 + page2 == section_text[:…]`).
* Stale revision `deadbeefdeadbeef` → `stale=true`, empty text,
  `section-stale-content` diagnostic. Unknown section → empty text,
  `section-unknown` diagnostic (no silent neighbouring passage).
* Every frame round-trips `json.dumps` (real serialization boundary).

**Not passed / remaining:** the search-revision → section-read join (F1) is
xfail with the observed mismatch (`cae54bccf0a32883` vs `59235bb3a4c6d61d`).
Unexecuted at this level: cross-process stdio transport (the harness writes
frames in-process), concurrent gateway sessions, and any real provider turn
(no provider calls are permitted in these tests).

---

## 8. Performance/cost ledger (§15)

Workstation: local dev Mac (darwin, CPython 3.14.5). Single-process,
`time.perf_counter_ns`, warm registry. p50/p95 are percentile estimates over
the stated n; the descriptor/search/disclosure rows are micro-benchmarks of
pure steps, not wall-clock turn latency. Two runs varied <6% on p50; the
figures below are the final captured run.

| Metric | p50 | p95 | n |
|---|---|---|---|
| `build_task_descriptor` (6-query batch) | 100.7 µs | 116.8 µs | 50 |
| `search_skills` warm (shared per-turn index cache) | 2057.9 µs | 2201.1 µs | 200 |
| `search_skills` cold (index rebuild) | 3406.0 µs | 3503.4 µs | 8 |
| `plan_disclosure` ("add tests", 4 steps) | 134.9 µs | 153.5 µs | 200 |
| section parse cold (miss, 891 B content) | 19.5 µs | 27.5 µs | 24 |
| section index hit (same content revision) | 1.3 µs | 1.7 µs | 2000 |

* A4 pinned a **0.55 µs** section-cache hit. A6 measures the same predicate
  (content fingerprint + `OrderedDict` lookup with LRU touch) at **1.3 µs p50**
  on an 891 B skill; the two are not directly comparable (different fixture
  size/timer), and no threshold was tuned to reconcile them.
* Warm `registry.load_all()`: **0 content parses, 36 `Path.stat` calls** for
  76 catalog skills — a telemetry-free reload re-stats but never re-parses
  (spec §11). No media/attachment module is imported by the routing corpus
  (pinned by §5).

Tokens loaded (per acceptance case; local assembler count, basis
`assembler.get_token_count` — tiktoken with len//4 fallback):

| Case | Context chars | Assembler tokens | plan `included_tokens` (chosen only) | Whole-body est tokens (char/4) | Steps |
|---|---|---|---|---|---|
| make me a website | 0 | 0 | 0 | 0 | 0 |
| build a landing page | 0 | 0 | 0 | 0 | 0 |
| what is a website? | 0 | 0 | 0 | 0 | 0 |
| add tests (active Python state) | 4233 | 932 | 0 | 1845 | 4 |
| compare my stock portfolio | 0 | 0 | 0 | 0 | 0 |
| go ahead | 0 | 0 | 0 | 0 | 0 |

(The ledger test's "add tests" row used the active Python state; re-measuring
the same query without state produced identical values — 4233/932/0/1845/4 —
because no stage fact is present either way.)

**Finding F2 (measurement gap, recorded).** `DisclosurePlan.included_tokens`
sums *chosen sections* only; it reports 0 while the assembled context carries
932 tokens, because no skill currently declares schema-v2 sections and the
disclosure falls back to whole-body excerpts (whole-body estimate 1845
char/4-tokens). Both numbers are reported side by side; the ledger keeps its
stated basis instead of substituting one for the other.

**Provider-side tokens are NOT measurable here.** Input/output/cache-read/
cache-write counts require a provider call; the A6 constraint forbids provider
calls in its tests, so those §15 rows remain unmeasured and no number is
invented. Accepted task success (§15, level 6) is likewise out of scope for
this evidence.

---

## 9. Packaging review

* Wheel target: `[tool.hatch.build.targets.wheel] packages = ["machina", "skills"]`
  (`pyproject.toml`). All Track A modules live under `machina/`:
  A3/A4/A5 added `machina/skills/{planned_steps,revisions,events,search,sections,metadata}.py`,
  `machina/core/task_descriptor.py`, `machina/gateway/features/skills.py`
  (`git show --name-only` for `9cfff3c`, `0908f68`, `1086f57`, `002cc19`).
  No Track A file lives outside `machina/`, `skills/`, `ui/`, or `docs/`.
  The only new files outside those trees are this document and the DevTools
  test file (test harness repo, not shipped).
* Dependency set unchanged: `git log b7bd6b6..HEAD -- pyproject.toml` is empty
  (last touch: `ac7b3bc`, pre-Track-A); `git diff HEAD -- pyproject.toml` empty.
  `pyproject.toml dependencies` still names the same 15 runtime packages.
* Wheel built from the captured tree (C8): `machina-0.2.0-py3-none-any.whl`
  contains `machina/core/intent.py`, `machina/core/task_descriptor.py`, and
  `machina/skills/{events,metadata,planned_steps,revisions,runtime,search,sections,workflow}.py`,
  plus 76 bundled `skills/*/SKILL.md` (474 zip entries total).
* Installed-venv import (C7) succeeds for all four named modules from outside
  the repo directory (`cd /tmp && .venv/bin/python -c …`).

---

## 10. Unexecuted checks and environment blockers

1. **Provider-side token/cost numbers** — not measurable without a provider
   call; forbidden here by the no-provider constraint (§8).
2. **Task-outcome evaluation (level 6)** — no accepted-task run under measured
   cost was performed; A6 claims levels 1–4 of §19 only.
3. **F1 fix verification** — the search↔section revision identity mismatch is
   recorded (xfail), not fixed (outside A6's file ownership); its fix needs
   A5-backend or A4-identity ownership.
4. **Cross-process stdio transport / concurrent gateway sessions** — the live
   test drives the real `handle_line` JSON-RPC boundary in-process; a separate
   OS process and multi-client concurrency were not exercised.
5. **Held-out misses H01/H02/H04/H11** — recorded as xfail, no fix attempted
   (threshold/vocabulary tuning is explicitly out of scope for A6).
6. **Long-run stability of measured latencies** — p50/p95 are single-session
   micro-benchmarks on one workstation, not a CI budget; no budget was frozen
   from them.
7. One transient combined-suite failure during a B2 file save was not
   reproducible (see §2); no claim is based on it.

---

## 11. Track A release checklist (§21) — status at this commit

| # | Item | Status | Evidence |
|---|---|---|---|
| 1 | One shared task descriptor and canonical search/ranking implementation | **partial** | Descriptor exists and is consumed by a real consumer (`skills.preview_route`) and by tests (C1/C3); the automatic selection path (`plan_workflow`) reuses intent vocabulary but still ranks through the legacy `rank_skills`, and `search.execute_search` is a second scoring implementation over the same cached index — both live at this commit. No regressing divergence was observed (parity tables §6), but "one implementation" is not literally true yet. |
| 2 | Metadata parsed/validated and used by real consumers | **partial** | Parser/validator/fallback/cycle tests pass (`test_skills_metadata`, C3). Real-data caveat: **0 of 76 loaded skills carry schema-v2 metadata and no enrichment sidecar exists**, so production consumers never exercise the v2 branches; only fixtures do. |
| 3 | Broad website task and negative/ambiguous cases pass | **partial** | All §8.2 table cases pass descriptor gates (`test_task_descriptor`); held-out gates: 13/17 pass, **4 recorded misses** (H01/H02/H04/H11, §4). Cannot be checked while those xfails exist. |
| 4 | No new LLM/embedding calls in deterministic selection/preview | **checked** | §5: AST scan (11 files), fresh-subprocess import proofs (10 clean modules + gateway delta), patched socket/subprocess runs for descriptor/search/workflow/disclosure, provider tripwire on the search/section/preview handlers, live gateway zero turn frames. |
| 5 | Source precedence, case-insensitive identity, and integrity remain intact | **checked** | `test_skills_search` (source-precedence tie-break, weights never override eligibility), `test_skills_gateway_search` (case-insensitive `skills.show`/section reads, closed filters), `test_skills_metadata` (injection scan, provenance), all in C3. |
| 6 | Stage and disclosure ordering share one authoritative representation | **checked** | A3 `steps` derive selection/display/disclosure; D01 tests in `test_skills_planned_steps` (C3); `automatic_skill_context` renders `workflow.steps` and records each disclosed section event. |
| 7 | Coherent prerequisites and versioned continuation work | **partial** | Prerequisite/budget/tiling and stale-version semantics pass at helper + protocol level (C1, C3; live tiling §7). Caveat: **F1** — the search→section revision join is broken at the real boundary, so the real-UI first read is stale (xfail pinned). |
| 8 | Cache generations and telemetry meanings are accurate | **checked** | `test_skills_revisions`/`test_skills_events` (generation-guarded publication, telemetry-only reload invalidates no content cache — mirrored by the ledger's 0-parse warm load); activation vocabulary is observed events, browse reads are administrative counters, never success counters (`gateway/features/skills._record_browse_activity`). |
| 9 | Real searchable /skills UI uses bounded gateway contracts | **checked** | A5-UI + A6 reader: 23/23 tests through the real App (C5), full UI gate 999/993/0 (C6); bounded `skills.search` filters/cursors, `skills.preview_route` call-free, `skills.section` version-bound; only supported controls rendered. Caveat: F1 affects the section reader's first read (honest stale state). |
| 10 | Current native/component/protocol/task evaluation evidence exists | **partial** | Native/component + protocol evidence exists (C1/C3/C5/C6, live gateway §7). Level-6 task evaluation is absent and is explicitly **not** claimed; provider token rows unmeasured (§8/§10). |

Nothing above is marked checked on the strength of a helper test alone; each
checked row names a real consumer, protocol, or live boundary pass.

---

## 12. Findings register

| ID | Finding | Impact | Status |
|---|---|---|---|
| F1 | `skills.search` content revision (A4 identity) ≠ `skills.section` binding revision (`sections.content_fingerprint`); observed `cae54bccf0a32883` vs `59235bb3a4c6d61d` for `testing-qa` | Any client joining search→section observes `stale` on first read; UI degrades honestly via reported-current-revision retry | Recorded; xfail `test_a6_live_gateway_search_revision_joins_section_read`; needs A4/A5-backend fix |
| F2 | `DisclosurePlan.included_tokens` ignores whole-body fallback units (0 vs 932 assembled tokens for "add tests") | Under-reports tokens loaded whenever no v2 sections exist (current catalog) | Recorded in ledger §8; fix needs A3 `planned_steps` ownership |
| F3 | Held-out misses H01 (marketing site), H02 (homepage), H04 ("don't build…"), H11 (Svelte) | Web-noun/negation/vocabulary coverage gaps; no safe-authorization failure (no accidental build claimed) | Recorded xfails with gates and observations (§4); not tuned |
| F4 | Concurrent-edit transient: `test_ecc_skills` provider-call cases failed once while B2 saved `request_gate.py`; passes alone and in the final combined run | None (unreproducible) | Recorded in §2 for honesty |

## 13. Rollback path and next actions

* The A6 UI reader is additive: deleting the `section` provider binding and the
  details-mode `s` handler restores the A5-UI details screen; no other surface
  depends on it. The disclosure rollback switch (`skills_disclosure_planner`)
  is untouched by A6.
* Next: (a) reconcile F1 revision identities (A4/A5-backend owner) and flip the
  xfail to XPASS; (b) count whole-body units in `included_tokens` (A3 owner);
  (c) route `plan_workflow` ranking through the typed search implementation or
  document the two-path contract explicitly (checklist 1); (d) ship real v2
  metadata or enrichment sidecars so checklist 2 exercises production data.
