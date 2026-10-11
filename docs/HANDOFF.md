# AgentHarness succession handoff

**Latest bug-fix audit:** Read [Post-OpenCode audit](BUGFIX_AUDIT.md) for current fixes and verification. The older measurements and remaining-failure lists below are historical snapshots, superseded by that audit.

**Verified current state:** Backend **1,927 passed / 5 platform-or-optional skips / zero failures**; full TUI check **127 passed / zero skips**, including real gateway restart tests; Ruff, formatting, diff check, and wheel packaging pass. Existing databases receive additive runtime migrations automatically. All audit fixes remain uncommitted; the five skips and service limitations are detailed in the audit.

Written: **2026-10-10**. Repository snapshot: **`167cfbb` — `skill overhaul and self learning implemented`**, plus the uncommitted changes listed below.

**Debloat update:** The user later explicitly requested removing irrelevant skills, superseding the original all-302 requirement below. The bundle now contains **42 curated skills and 88 files** at the same upstream pin. Read [OpenCode continuation](OPENCODE_CONTINUATION.md) first for current changes, measurements, verification, and remaining tasks. Historical full-bundle counts and test results below describe the earlier snapshot. The UI interaction and MCP plans remain unchanged.

**Update 2026-10-10: interactive `/persona`, first TUI flow implemented.** Bare `/persona` now opens an Add/Reset picker instead of printing a report. Add collects text in an overlay multiline editor (prefilled, 2000-character count, example, Save/Cancel) and persists exactly one `config.set {key: persona_description, persist: true}`; Reset asks No/Yes defaulting to No and clears only `persona_description`, leaving growth/profile/RL intact. `/persona <description>` and `/persona --clear` remain as expert shortcuts. Changed `ui/src/features/types.ts`, `ui/src/components.ts` (new `TextForm`), `ui/src/app.ts` (`askText` overlay), `ui/src/features/settings.ts`, `ui/src/features/index.ts`, `ui/test/features.test.ts`, `ui/test/gateway.e2e.test.ts`. ECC bundle untouched (42 skills, 88 files, manifest `01b3ca9e…ed36`). Verified: `npm run typecheck` passed, `npm test` 105 passed / 4 skipped, `npm run lint` passed, `git diff --check` clean, `tests/test_persona_description.py` 4 passed. Section 11 below remains the full spec; only this first flow is implemented.

**Update 2026-10-10: UI consistency overhaul.** Standardized all read-only/empty/kept/progress notices to the `plain` kind (sessions, search, recall, goal, delete-kept, compress, provider, effort, config single-key, agents/skills delete-kept, delegating); unknown `/mode` and `/keys` options and bad `/approvals` ids now throw `error` instead of printing `warning`; `/growth forget` miss is `warning`; `/approvals` lists one table with short ids plus a completer; `/agents` completions cover save/history; `/skills` and `/memory` descriptions match real subcommands; bare-picker commands say `(menu when empty)` (sessions, resume, models, default-model, theme, keys, persona); `Notice` renders `✓`/`!`/`✗` text markers so state survives monochrome skins; approval resolutions report `success`/`error`; nested `askText` explains instead of silently cancelling; `TextForm` starts focused. Changed 13 `ui/src` files plus `ui/test/features.test.ts` (5 new tests). ECC bundle untouched (42 skills, 88 files). Verified: `npm run typecheck` passed, `npm test` 110 passed / 4 skipped, `npm run lint` passed, `git diff --check` clean.

**Update 2026-10-10: transcript block margins.** Each transcript block (`UserMessage`, `AssistantMessage`, `ToolCard`, `Notice` in `ui/src/components.ts`) now renders a one-row top margin, so filled response boxes never touch corner-to-corner; the first block's margin also airs out the session header. Added a `ui/test/ui.test.ts` regression test. Verified: `npm run typecheck` passed, `npm test` 111 passed / 4 skipped, `npm run lint` passed, `git diff --check` clean.

**Update 2026-10-10: chrome gap.** `createLayout` now uses a one-row `VStack` gap, so the last response box no longer touches the composer and the input block stands off the footer (the exact flush-corner case in the screenshot). Updated the two `ui/test/layout.test.ts` row assertions and added gap-row checks. Verified: `npm run typecheck` passed, `npm test` 111 passed / 4 skipped, `npm run lint` passed, `git diff --check` clean.

**Update 2026-10-10: skill-paging guard, per-model turn budget, stream usage fix.** (1) The model paged all 42 skills on `hello!` because the Automatic-skills header invited `skill_list` search with no task-gating; the header now says to answer greetings directly and to page only while results stay relevant, and the `skill_list` description says never to enumerate the catalog (`ah/skills/runtime.py`, `ah/tools/skills.py`, tests in `tests/test_ecc_skills.py`). (2) New `ah/core/context_windows.py` maps model ids to documented windows (longest-match; unknown keeps legacy behavior exactly); both agent loops derive the turn budget as window/4 floored at 8000, explicit `context_budget` still wins, absolute 50k ceiling unchanged (`ah/core/agent.py`, `tests/test_context_windows.py` — e.g. deepseek-chat now 16k, gemini capped at 50k). (3) `DirectProvider.stream_complete` never requested or parsed usage, so direct-vendor streamed turns reported 0 tokens: openai/deepseek specs now send `stream_options.include_usage` with chunk parsing, other vendors get an estimate instead of zero (`ah/core/provider.py`, tests in `tests/test_providers_direct.py`). ECC bundle untouched (42/88). Verified: ruff check/format clean, `git diff --check` clean; focused suites pass — `test_context_windows` 7, `test_providers_direct`/`test_ecc_skills`/`test_skill_workflows`/`test_skill_runtime`/`test_basic`/`test_gateway`/`test_provider_usage`/`test_orchestrator_usage` green. Pre-existing failures unchanged (4 DB-backed `test_usage`, 1 provider-effort, both reproduce on clean HEAD).

**Update 2026-10-10: pipeline optimization pass.** (1) `SYSTEM_PROMPT` now carries general-agent conduct (no tools without a task, never enumerate catalogs/directories unprompted) — fixed cost, no per-turn growth. (2) `automatic_skill_context` returns `""` when nothing matched, removing ~116 tokens of boilerplate from every skill-less turn (greetings now inject zero skill context). (3) Both agent loops condense stale tool rounds to head+tail previews before each LLM call (`_condense_history`; latest round stays full, positionally addressed skill pages exempt so offsets keep resolving, full records stay in DB). Measured on a synthetic 5-round turn: stale tool text 5000→4168 chars (~17% off re-sent history; the larger savings remain behavioral — fewer wasted rounds). New `tests/test_history_condensation.py` (6 tests incl. a two-round loop test proving earliest condenses, latest stays full). Changed `ah/core/agent.py`, `ah/skills/runtime.py`; prior audit note stands: per-result 1k/9k append caps already existed, parallel tool calls already existed. ECC bundle untouched (42/88). Verified: ruff check/format clean, `git diff --check` clean; 131 passed across history/skill/basic/gateway/context/personality/provider suites (1 failure = the known pre-existing provider-effort test).

**Update 2026-10-10: growth indicator + profiles that own their learning.** Growth verified live on the test DB (teach persists across reloads; episode→complete→feedback moves policy weights, counters advance). New `growth.status` protocol event (consolidation_done/review_done) emitted by the gateway via per-turn agent listeners; the TUI renders a dim "Growth updated…" notice exempt from turn tracking, current session only. Profiles: `/profile` bare opens View/New/Switch (slug ids, New via editor, current marked in `/profiles`), App holds the active profile (default = OS user, ensured at startup), and non-default profiles scope new chats to a synthesized `harness-<slug>` agent definition — so RL episodes, growth notes, and memories (all agent-id-keyed) follow the profile with no schema change; unknown agents still fail closed and profile scopes stay out of delegation lists. Default model/persona stay global installation settings by design. Verified: full backend suite **1935 passed, 0 failed** (test DB connected), UI 132/0, protocol conformance green, ruff/format/diff clean.

**Update 2026-10-10: README rewritten.** Root `README.md` replaced: 37 commands (was 32), 21 skins with the correct list (was 24 with names that don't exist), current test snapshot (1844/9 backend, 124/5 UI, dated), per-model turn budget, gated skill routing, subagent fan-out, `/persona`+`/growth` rows, `AGENT_HARNESS_EXECUTION_MODE`/`CONTEXT_BUDGET` config rows, full-mode troubleshooting notes. Install/architecture/API/research sections preserved.

**Update 2026-10-10: theme switch keeps the chat.** `/theme` called `host.banner()`, which clears the whole transcript to redraw the header. Skin changes now call a new `host.restyle()` path (`Transcript.restyle()` + `requestRender`) that keeps every block; `Notice`/`UserMessage` store raw text and re-resolve theme colors via `refreshSkin()` (other widgets already render live from lazy theme functions). Tests: `/theme` asserts zero clears, re-skin keeps text with idempotent styling, restyle drops nothing. Verified: typecheck passed, 124 UI tests pass / 5 skipped, lint clean.

**Update 2026-10-10: full checkup.** Backend **1844 passed, 9 failed, 5 skipped**; UI typecheck/test/lint green (111/0); wheel builds with `context_windows` included, no pycache. The checkup caught and fixed two real problems: (a) `_turn_model` crashed on non-string model attributes (`TypeError`, ~35 chaos failures — hardened with `isinstance` coercion); (b) `TestConsistentTokenBudget` source-pinned the old inline derivation (updated to the shared helper). It also surfaced test hermeticity debt from the live `full` default: 8 tests assumed the repo `ask` default (audit allowlist/grant-failure/session-leak, Bug13 cd ×2, comprehensive terminal/writefile, session-mode workspace/global) — pinned per-test via env/mode isolation, plus conftest now snapshots in-memory config and resets session modes per test. Notably, `terminal("cd")` executing under `full` is *designed* FULL-HOST behavior (allowlist intentionally bypassed), not a hole. Remaining 9 failures each verified pre-existing on clean HEAD: 4 DB-backed `test_usage`, 1 load-concurrency, 1 provider-effort, 1 memory-scorer float, 1 research fixture, 1 schema table.

**Update 2026-10-10: subagent auto-delegation.** Finding: ECC ships no deployable subagent roster (zero `agents/*.yaml` in the 42-skill bundle — only docs/rules text), so there is nothing to "implement" there; the roster is the 3 native types (`harness` general, `researcher` read-only, `coder` edits/tests) plus user-saved customs, via the existing `delegate`/`list_agents` tools (parallel fan-out through the concurrent executor, depth-capped, authority-inheriting). The gap was guidance: nothing told the model to fan out (the only mention was buried in skill context). `SYSTEM_PROMPT` now teaches task-based delegation — one `delegate` call per independent subtask in a single block (max three), type picked via `list_agents`, standalone task each, no approval evasion — and the `delegate` description advertises parallel fan-out. New tests in `tests/test_delegate_scope.py` incl. a loop test proving 3 parallel delegates all run and synthesize. Verified: delegate suite 5 passed, ruff/format/diff clean, `test_comprehensive` shows only its 5 pre-existing HEAD failures.

**Update 2026-10-10: default execution mode → full (user-requested).** `execution_mode` in `~/.agent-harness/config.yaml` changed `ask`→`full` via the validated `config.set(..., persist=True)` path (one key, no other file/config change; no env override present). Effect: subsequently created sessions inherit full and auto-grant at creation, so ordinary host operations proceed without approval cards. Verified floor intact in code and at runtime: `credential_access`/`privilege_escalation`/`security_settings`/`destructive_system` still require exact digest-bound grants in every mode (`ah/permissions/policy.py` CONFLICTING_CAPABILITIES + AH-AUDIT-007; live `decide()` check returns `pending` for a credential read in full mode), and global-full never elevates existing sessions (LP-07). Fresh-session effective mode resolves `full`; permission/gateway/precedence suites 102 passed. Revert with `/mode revoke`, `config.set execution_mode ask --persist`, or deleting the key.

**Update 2026-10-10: general agent, specialist workflows gated.** The agent latched onto `ecc-architecture-decision-records` from the single shared word "why" in "why dont you finihs?", then followed its "inspect existing work first" instruction around the home directory and dead-ended instead of acting as a generalist. `plan_workflow` now requires strong evidence (`_strong_match`: named skill, whole-word trigger, shared name word, or 2+ shared content words) before attaching domain/specialist skills; vague queries yield no specialists. Explicit coding/research tasks still get the generic pipeline (search-first/tdd/verify/deep-research) even with no domain skill, so uncovered languages keep engineering discipline without fake specialization. The injected header/render framing changed from imperative ("Apply… Follow this sequence… Inspect existing work first") to conditional references with "without an actionable task, just answer / do not go looking for work" (`ah/skills/workflow.py`, `ah/skills/runtime.py`, 4 new tests in `tests/test_skill_workflows.py`). Verified: skill suites 49 passed, `test_basic`/`test_gateway`/`test_personality_growth` 48 passed, ruff check/format clean, `git diff --check` clean.

This document is intended to let the next maintainer continue without the chat history. It records what exists, what was verified, what remains uncertain, and the next implementation plan. It is a snapshot, not a claim that every feature or external service is production verified. Recheck Git status and the referenced code before acting.

## 1. Read this first

The next requested work is to **improve the existing terminal UI and add MCP server support**, end to end. The user wants an agent that works naturally out of the box: skills activate automatically, personality develops with use, and routine work does not require constant permission prompts.

The UI already exists. It is TypeScript using `@earendil-works/pi-tui`, backed by a Python JSON-RPC gateway. This is not a request to replace it with a web application. MCP runtime support does not yet exist. ECC contains MCP configuration examples, but vendoring those files did not install or connect any servers.

There is useful uncommitted work. Preserve and review it before starting the next feature. Do not reset the checkout, overwrite unrelated changes, or assume HEAD contains everything described here.

Start with these documents:

- [README](../README.md): installation, commands, architecture, and configuration.
- [Permission policy review](PERMISSION_POLICY_REVIEW.md): the approval problem and policy changes.
- [Personality growth](PERSONALITY_GROWTH.md): persona configuration, persisted learning, and RL boundaries.
- [ECC skills](ECC_SKILLS.md): bundle provenance, automatic routing, workflow composition, and packaging.
- [UI/gateway protocol](../communications/ui-gateway-protocol.md): transport and turn semantics.
- [Production completion ledger](plans/PRODUCTION_COMPLETION_PROGRESS.md): broader release gates. This handoff does not supersede them.

## 2. The product intent and decisions already made

The original complaint was that almost every tool caused an approval prompt. The desired policy is risk based: ordinary authorized project work should proceed; dangerous effects should be reviewed. Do not fix future integration issues by returning to approval on every invocation.

The user then requested Hermes-like personality growth connected to the README's RL feature. The implemented approach combines durable, bounded personality notes with a learned memory-retrieval policy. It does **not** train the underlying language model weights. Do not describe it as full model fine-tuning.

The user also requested a configurable persona description, all ECC skills as default capabilities, automatic task-based activation, token efficiency, and cooperation between skills. Those requests are part of the expected baseline, not optional plugins the user must remember to enable.

The practical interpretation of “out of the box” is:

- Bundled skills and automatic routing work without a separate installation or opt-in flag.
- Available native capabilities are used through the existing harness.
- Missing accounts, credentials, binaries, or servers are reported clearly when actually needed.
- No fabricated credentials, silent package execution, automatic installation of every upstream integration, or assertion that an unavailable external service works.
- Skills, learned notes, and remote tool output do not override the user, core identity, or permission policy.

The user strongly dislikes unnecessary confirmation. Proceed with authorized reversible work. When a dangerous external effect requires approval, prepare a concrete reviewable action first and explain the actual boundary. Do not invent approval gates for routine inspection, local implementation, or planning.

## 3. Exact state being handed over

### Committed foundation

Recent history at the time of writing:

| Commit | Description |
| --- | --- |
| `167cfbb` | Skill overhaul and self learning implemented |
| `d9ec620` | Policy changes |
| `4f615c6` | Model/provider switching: re-pin live session and persist default across restarts |
| `5acb45d` | Earlier baseline |
| `b8084b0` | Earlier baseline |

The permission, personality, persona, and ECC bundle foundation is in the committed checkout. The latest workflow refinements below remain uncommitted. No commit or push was requested for this handoff.

### Uncommitted implementation

| File | What to preserve |
| --- | --- |
| `ah/core/agent.py` | Context-aware workflow injection and bounded skill output handling |
| `ah/skills/runtime.py` | Routing, technology/specialty filtering, targeted selection refinements |
| `ah/tools/base.py` | Skill-resource cache exclusion needed for integrity checks |
| `ah/tools/skills.py` | Ranked discovery, targeted instruction reads, resource paging |
| `docs/ECC_SKILLS.md` | Documentation of the cohesive default behavior |
| `ah/skills/workflow.py` — new | Native workflow composition and short-follow-up stack inheritance |
| `tests/test_skill_workflows.py` — new | Workflow selection and actual tool-pipeline regression coverage |

Run `git diff` and read the two new files before integrating. Some implementation plans live under gitignored `docs/superpowers/plans/`; do not rely on those surviving a clean checkout. This document preserves the important intent and next plan.

### Explicitly not implemented

- No MCP client/server manager in `ah/` or `ui/`.
- No native `ah/tools/mcp.py` bridge or MCP configuration/runtime lifecycle.
- No MCP server management UI.
- No new UI redesign from this handoff request.
- No automatic underlying-model weight training.
- No blanket installation or execution of ECC hooks, scripts, agents, services, or upstream MCP manifests.

## 4. Architecture map: where to work

| Area | Primary files | Responsibility |
| --- | --- | --- |
| Shared runtime | `ah/core/runtime.py` | One service graph, startup, capability reporting, refresh, lifecycle |
| Agent loop | `ah/core/agent.py` | Context preparation, normal/streaming execution, tools, instrumentation |
| Configuration | `ah/core/config.py` | Defaults, validation, persona and feature configuration |
| Permission enforcement | `ah/permissions/policy.py`, `commands.py`, `broker.py`, `tools.py`, `store.py` | Risk classification, tool effects, exact proposals, grants, enforcement |
| Turn ownership | `ah/core/turns.py`, `execution_context.py`, `session_mode.py`, `approval_resume.py` | Session/turn context, cancellation, ownership, resume |
| Tool registration | `ah/tools/base.py`, `ah/tools/__init__.py` | Tool metadata, global registration, execution/cache behavior |
| Personality/memory | `ah/memory/growth.py`, `ah/memory/policy.py`, `ah/tools/memory.py` | Durable notes, retrieval policy, agent memory tools |
| Database | `ah/db/connection.py`, `ah/db/growth_schema.sql` | Schema initialization and additive growth migration |
| Skill bundle | `ah/skills/bundle.py`, `ah/skills/registry.py` | Pinned default bundle, provenance, registry lifecycle |
| Skill routing | `ah/skills/runtime.py`, `ah/skills/workflow.py` | Metadata ranking, bounded context, coherent task workflow |
| Skill tools | `ah/tools/skills.py` | Discovery, paged instruction/resource access |
| Gateway | `ah/gateway/server.py`, `ah/gateway/features/` | JSON-RPC, session-owned feature methods, turn events |
| HTTP API | `ah/api/app.py` | Authenticated HTTP/SSE using the shared runtime |
| Secret resolution | `ah/security/secrets.py`, `ah/gateway/features/secrets.py` | Existing secret resolution and UI key handling |
| Protocol source | `ah/protocol/events.json` | Canonical event contract |
| TUI entry/coordinator | `ui/src/main.ts`, `ui/src/app.ts` | Startup, application state, transcript, editor, overlays |
| TUI layout/rendering | `ui/src/components.ts`, `transcript.ts`, `theme.ts` | Composer/footer, pickers, transcript/tool cards, theme roles |
| TUI features | `ui/src/features/` | Sessions, models, settings, skills, growth, keys, agents, jobs, etc. |
| TUI transport | `ui/src/gateway.ts`, `protocol.ts`, `events.ts` | Python subprocess, JSON-RPC requests, typed contracts/events |

A new capability belongs in this architecture. Do not create an independent service graph for MCP or a second agent loop for the redesigned UI. CLI, TUI, HTTP, jobs, and delegated work must agree about available tools and permissions.

Runtime capability states include `available`, `enabled`, `active`, `degraded`, `unavailable`, and `research-only`, with reasons and next actions. Extend this reporting for MCP instead of making server failure an unexplained chat failure.

## 5. Permission policy: preserve this carefully

All actual tool execution belongs behind the broker. Tool metadata must describe effects accurately; the broker determines whether the requested action may run or needs an exact approval.

Routine project reads, searches, appropriate local writes, and supported test commands generally proceed under the applicable mode and workspace boundaries. Destructive commands, credential access, outside-boundary operations, network/install effects, and unknown risky mutations remain subject to review. The exact current rules are in code; this paragraph is not a replacement classifier.

Native `skill_list`, `skill_read`, and `skill_resource` map to a side-effect-free `skill.read` action. Memory/personality tools have their own permitted memory actions. That does **not** mean an arbitrary new extension tool can safely be labeled `tool.*` and executed without review.

Important invariants:

1. Parent restrictions intersect a child agent's tool allowlist. Skills and MCP discovery must not widen it.
2. An approval decision is not execution. Persisted proposals bind the exact action, session, turn, and relevant content.
3. Changing arguments after approval requires the correct fresh decision, not reuse of a vaguely similar grant.
4. Cancellation, ownership renewal, approval waiting, and resumption must keep their existing semantics.
5. Agent principals cannot approve their own pending actions.
6. An upstream skill asking for confirmation does not automatically impose that gate on already authorized routine work.
7. The development agent's filesystem sandbox prompts are separate from application approval behavior. Do not weaken the app because a local test runner needed sandbox escalation.

For MCP, the hardest issue is effect classification. Remote tool annotations or friendly descriptions are hints, not proof of safety. Unknown mutating tools need conservative treatment. A configured, well-understood read-only operation can be classified appropriately; a remote tool's claim that it is read-only cannot grant itself authority.

## 6. Personality, persona, and RL: what actually works

Growth is enabled by default. Notes are persisted per agent in PostgreSQL and injected through shared context preparation, so they survive sessions, restarts, and model/provider changes. The core agent role remains stable; learned style, preferences, and verified lessons are bounded additions.

The agent can call `learn_personality` during ordinary work. There is no extra reflection-model request after every turn. Notes are capped at 500 characters each and 2,400 characters total including keys. Keys are validated, and secret-looking content is redacted.

`persona_description` is a validated global installation setting capped at 2,000 characters. It defines desired style/character, independently of whether growth is enabled. Environment overrides can take precedence over saved configuration. It is not a permission override.

Existing user surfaces include:

```text
/persona                         inspect persona
/persona <description>           set desired persona
/persona --clear                 clear configured persona
/growth                          inspect learned growth
/growth teach style verbosity …  teach a preference
/growth feedback good|bad […]    provide episode feedback
/growth forget style:verbosity   remove a note
```

The backend exposes session-owned growth methods and `ah growth` CLI commands for inspection, teaching, feedback, forgetting, and policy import/export. See the personality document for exact syntax and config names.

The live RL feature is a contextual bandit over **retrieve / noop**. It masks the broader six-action research format down to supported live actions. Cold-start behavior is biased toward retrieval, approximately 92%. Human feedback in `[-1, 1]` updates policy weights with delayed importance correction clipped to 1.2; weights must remain finite and bounded to ±20.

There is no automatic positive reward simply because an answer finished. Feedback is ownership checked and one-time. Cancelled or unfinished turns are not eligible. Terminal error/budget/iteration endings may be eligible for feedback without implying the task succeeded. The latest 500 episodes per agent retain hashed features/action/probability rather than raw user query text in the RL record.

The existing offline `GroupRelativeTrainer` is a research path requiring scored counterfactual rollouts. It is not an automatic online trainer of the LLM. A policy JSON can be trained/imported through the documented research/growth surfaces.

Growth migration failure degrades the feature without making chat unavailable. Disabling memory/growth suppresses automatic learning and note injection and rejects disabled teaching/feedback/install actions while inspection/export/removal remain available. Preserve explicit recall behavior.

Ownership today follows the local-account, agent-scoped model. Do not claim multi-tenant per-user isolation. Learned procedural skill proposals still use the separate opt-in review flow; this is distinct from the mandatory bundled ECC defaults.

## 7. ECC skills: provenance, efficiency, and cohesive execution

Upstream: [affaan-m/ECC](https://github.com/affaan-m/ECC).

Pinned commit: **`4eb71d92a39cab44ad40ac9d8a6a5ccb4029d6c2`**.

The repository vendors the complete tracked upstream snapshot under `skills/ecc/`: **302 canonical skills, 598 files inside skill directories, and 4,293 upstream files total**. The bundle is approximately 66 MB uncompressed. A previously built wheel was approximately 38.31 MB; size is historical, not a release guarantee.

Retain `skills/ecc/LICENSE`, the per-file notices, and [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md). The root license is MIT, copyright 2026 Affaan Mustafa. Updating the bundle requires checking the new snapshot and its notices rather than assuming the current counts/license inventory remain true.

`tools/vendor_ecc.py` imports a clean local upstream clone at an exact revision. It does not run upstream scripts. `ah/skills/bundle.py` checks the revision and pinned manifest hash:

```text
4fa532a994618223cedda3d8ef965b776902944ae968c7754c2eb2100e8f0fe0
```

Instruction and resource reads are lazy and integrity checked. Resource paths must be manifest-listed and confined to the bundle root; absolute paths, traversal, and symlink escapes are rejected. Do not bypass these checks with a result cache. `skill_resource` is intentionally uncacheable at the general tool-result layer.

Default `SkillRegistry()` includes ECC regardless of the configured local skills directory. Explicit custom registries are useful isolated test seams, not an end-user switch to accidentally remove the default bundle. Built-ins use `ecc-` names and cannot be disabled, deleted, edited, or shadowed. Legitimate derived local `ecc-…-learned` skills remain usable. Local files cannot claim built-in provenance.

The ordinary local-skill validation/scanning remains strict. The pinned upstream loader accommodates legitimate security documentation without broadly trusting arbitrary imports. Do not “solve” scanner false positives by weakening all local validation.

### Routing and context budget

The router ranks cached metadata with BM25-style scoring, aliases, whole-word triggers, technology gates, and specialty filters. It makes **no extra classifier LLM call**. The full catalog and bodies are much too large to inject indiscriminately: historical measurements were about 21,114 catalog tokens and 645,083 enabled-body tokens.

Initial skill context is bounded to at most 1,200 tokens and one fifth of the context budget, with existing system instructions, the query, and overhead accounted for. Tiny contexts may leave no skill space. Preserve that behavior rather than overflow the model budget.

The latest workflow implementation composes up to six complementary skills and initially discloses up to two anchor excerpts. The remaining stages are referenced for targeted reads. Typical coding workflows connect domain guidance, inspect/plan, relevant security checks, testing, specialization, and verification; research workflows compose relevant domain and research guidance. Greetings and unrelated prose do not receive a coding pipeline automatically.

Short follow-ups of at most 16 words can inherit stack information from the newest relevant user context, then the goal, then other recent context. An explicit technology in the new request wins. Never union all historical stacks: an old Django task must not contaminate a new FastAPI or Rust task.

Regression examples already addressed:

- Plain Python must not select Django simply because Django descriptions mention Python.
- Explicit FastAPI must not inherit Rust from previous context.
- Generic “API” must not activate X/Twitter skills.
- Installer, animation/motion, Celery, and other specialized skills need relevant topics.
- Explicit local whole-word triggers must not be buried behind generic defaults.
- Canonical built-in collisions are rejected without deleting derived learned skills.

### Native progressive disclosure

`skill_list(query, offset)` ranks nonempty queries and returns four entries per page. `skill_read(name, offset=0, limit=800, query="")` supports targeted passage search and real character offsets, with an 8,000-character maximum. `skill_resource(name, path="", offset=0, limit=4000)` handles manifest-root or skill-relative paths and bounded resource pages.

Skill pages are preserved through the agent output handling up to the bounded page allowance, rather than chopped at the ordinary 1,000-character preview. Otherwise the model could receive a continuation offset for content it never saw. Preserve this distinction.

Upstream slash commands, hooks, and agent prompts are references. Workflows use actual native harness tools, check availability, and share one task plan and evidence. No invocation of an upstream agent role should bypass the harness's real agent configuration or permission restrictions.

## 8. UI and gateway contract: don't break turn state

The UI has a flexible transcript above a bottom-docked composer/footer. `ui/src/app.ts` coordinates overlays, editor completion, feature dispatch, per-session tokens, in-flight state, and the gateway. The current in-flight timeout is 300 seconds, aligned with the backend turn timeout. Do not unlock the composer early while the server still owns an active turn.

`ui/src/gateway.ts` owns a transport-neutral JSON-RPC channel with request IDs, pending waiters, request timeouts, event dispatch, and close cleanup. The normal TUI gateway spawns `python -m ah.gateway` over stdio. Server stderr is not protocol JSON.

`ah/protocol/events.json` is the canonical version-2 event source. `ui/src/events.ts` is generated; do not hand-edit it independently. Regeneration:

```bash
.venv/bin/python -c "from pathlib import Path; from ah.protocol import typescript_source; Path('ui/src/events.ts').write_text(typescript_source(), encoding='utf-8')"
```

`tests/test_protocol_conformance.py` verifies exact generated-source conformity. Update protocol documentation and both consumers when the contract changes.

Version-2 events include protocol version, type, session ID, and turn ID. The HTTP SSE path shares the event semantics. Unsupported protocol versions are rejected; older compatibility is intentional. Tool arguments/results/file diffs are transport data, not safe default log or metric fields.

State meanings that the UI must preserve:

| Event/state | Meaning |
| --- | --- |
| `message.start`, `turn.started` | Accepted or started; not terminal |
| `message.delta` | Streamed, redacted text |
| `message.stopping` | Cancellation requested; ownership may still be held |
| `tool.start` | Tool invocation began |
| `tool.complete` | Tool result available; inspect `isError` |
| `needs_approval` | Exact action awaits a decision |
| `approval.resolved` | Decision recorded, not proof of execution |
| `approval.resumed` | Resume acquired a fresh claim |
| `turn.ownership_lost` | Cancellation/ownership transition, not ordinary success |
| `turn.cleanup_pending` | Retirement/claim cleanup still pending |
| `error` | Sanitized error; not by itself a terminal retirement event |
| `message.complete` | Terminal after retirement/ownership release |

Likewise, HTTP stream `[DONE]` means the stream closed, not that the task succeeded. Approval waiting renews ownership and excludes human wait from compute time. HTTP approval resumption validates exact arguments and claims ownership through the broker pipeline. Preserve these properties while improving visuals.

## 9. Verification record and honest limits

These are prior execution results, not tests rerun for this documentation change. Timing matters: the latest focused run includes final routing refinements; the full backend snapshot predates the last small routing/filter additions.

| Check | Recorded result | Scope/qualification |
| --- | --- | --- |
| Focused workflow/ECC/runtime/cache/permission suite | **122 passed** | Latest focused result after final routing refinements |
| Expanded gateway/phase-6 coverage | **142 passed** | Earlier snapshot, before the newest context regression test |
| Full backend suite | **1,824 passed, 3 failed, 5 skipped** | Earlier full snapshot, about 162 seconds; known failures below |
| Backend Ruff check | Passed | `ah` and the workflow test file |
| Formatting/diff checks | Passed | Changed implementation files and whitespace |
| UI feature tests | **47 passed** | Earlier feature verification |
| UI typecheck/lint | Passed | Earlier UI verification |
| Full UI suite | **97 passed, 1 failed, 4 skipped** | Earlier snapshot; selected-row fill failure below |
| Wheel outside checkout | Passed | Default offline bundle, all 302 skills, all 4,293 source hashes, custom local directory, bounded workflow |
| Docker | Not built in this work | Dockerfiles include skills, but an image build was not verified |

Known backend failures reproduced before the relevant feature work:

1. `tests/test_memory.py::TestImportanceScorer::test_score_deterministic`: tiny timestamp-sensitive floating-point difference.
2. `tests/test_research_phase2.py::test_labeled_identity_sequence_evaluation_cleans_up`: missing gitignored `research/identity_scenarios_example.json` fixture.
3. `tests/test_schema_conformance.py::test_static_python_sql_compiles_against_schema`: missing research table, reported from `workflow_benchmark.py:69` as `UndefinedTableError`.

Known UI failure: `ui/test/ui.test.ts`, **“selected rows paint an accent background pill”**, asserting that the selected row carries a background fill. Reproduce it and determine whether the theme implementation or expectation needs correction. Do not hide it by deleting the assertion without understanding the intended interaction.

The workflow end-to-end regression uses a deterministic provider stub with real native skill reads, actual file writes, and an actual Python pytest subprocess. It covers ordinary and streaming agent loops without approval cards. This demonstrates the harness pipeline; it does not prove every external model will follow every instruction or every ECC script/service will work.

There was no live-provider behavior matrix and no complete external-integration certification. Do not say “everything is green” or “every skill has been executed.”

Useful verification commands:

```bash
.venv/bin/python -m pytest tests/test_skill_workflows.py tests/test_ecc_skills.py tests/test_skill_runtime.py tests/test_skill_registry_cache.py tests/test_permission_risk_policy.py -q --tb=short
.venv/bin/python -m pytest tests/test_protocol_conformance.py -q
.venv/bin/python -m pytest -q --tb=short
git diff --check
```

From `ui/`:

```bash
npm run typecheck
npm test
npm run lint
```

Database-backed tests need the configured test environment and PostgreSQL. Do not run independent schema-mutating suites concurrently: DDL can deadlock. Do not edit source while a full suite is inspecting source locations. Some tests write under the user's harness directory; a development sandbox can require permission for that separately from app policy.

## 10. Setup, packaging, and secrets

The actual prerequisites are Python 3.11+, Node 22.19+, and PostgreSQL 16+ with pgvector. Follow the README rather than inventing a second bootstrap route. Typical development setup:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
npm install --ignore-scripts --prefix ui
# Configure .env using .env.example or ah setup; never commit credentials.
.venv/bin/ah doctor
.venv/bin/ah init
.venv/bin/ah
```

For this existing checkout, inspect the current environment first rather than recreating it. A stale `.venv/bin/pip` shebang previously pointed to the old checkout path. Prefer `.venv/bin/python -m pip`.

The wheel was built with `.venv/bin/python -m hatchling build -t wheel`; the historical artifact is `dist/agentharness-0.2.0-py3-none-any.whl`. `pyproject.toml` force-includes skills for wheel/sdist. Dockerfiles copy the skills into the image. Preserve offline bundle availability when changing packaging.

Pytest collects `tests/`, not vendored upstream test trees. Ruff excludes `skills/ecc`. Git ignore rules explicitly preserve the vendored snapshot, while generated `skills/telemetry.json` is ignored. Do not apply the harness formatter or test runner to upstream source indiscriminately.

The workspace `.env` contains real credentials. Do not paste it into documentation, logs, fixtures, screenshots, issue reports, or model prompts. Use variable names and redacted examples. Existing key handling keeps secrets out of `config.yaml` and command history. MCP credentials should use the existing secret resolution design instead of plaintext config additions.

## 11. Next plan: improve the existing terminal UI

### Design brief — interactive commands first

**Status: planned, not implemented.** This brief applies the [Impeccable skill](https://github.com/pbakaus/impeccable)'s Operate principles: task completion, consistent controls, restrained semantic color, progressive disclosure, clear feedback, and complete interaction states. The installed skill's `SKILL.md`, `reference/shape.md`, and `reference/operate.md` were read for this documentation. Its context launcher could not run because its engine was not installed and its cache location was outside the writable workspace. Existing source was inspected directly instead. No root `PRODUCT.md` or `DESIGN.md` was found; the vendored ECC design file is not design authority for this UI.

**Audience and job:** someone configuring or operating an agent from a terminal who should not have to memorize subcommands, flags, or configuration keys. Success means typing a familiar command, making a visible choice, entering any required value, and seeing an accurate result.

**Direction:** preserve the existing terminal identity, theme tokens, transcript, composer, and footer. Change command interaction from printed instructions/status to actionable controls. Bare management commands open a picker, form, or detail view with actions. Current values appear as supporting context inside the control rather than being the entire response. Read-only reports can remain readable reports; use interaction wherever the user has a meaningful next action.

**Primary acceptance example:** `/persona` followed by Enter opens **Add** and **Reset** choices immediately. Add opens a text editor; submitting saves the description as the installation's default persona. Reset asks a yes/no question and clears only the configured persona when confirmed. The user never has to discover `/config persona_description … --save` to complete this task.

These are UI questions, not broker permission requests or LLM conversation turns. Do not send a configuration wizard to the model, charge model tokens for its steps, or add approval cards to ordinary settings changes. Explicit argument forms remain available as fast paths for experienced users.

### Persona flow — concrete interaction contract

The current implementation in `ui/src/features/settings.ts` prints `Persona: …` for bare `/persona`. Replace that behavior in the next implementation with the following sequence:

```text
/persona → Enter

Persona
Choose what to do with your default persona.

› Add       Write or update your persona description
  Reset     Use the agent's default persona

↑↓ Choose   Enter Continue   Esc Cancel
```

Open the action control without first posting the current description to the transcript. Fetch current config as necessary; show a compact loading state inside the control and enable actions when the state is known. If a persona exists, a short wrapped preview may appear below the heading. Add remains the initial action label; its description makes clear that it updates an existing value.

**Add path:**

```text
Add persona
Describe how you want your agent to communicate.
Saved as your default across sessions.

[Focused multiline editor; current description prefilled if present]

Example: Be concise, warm, and direct. Explain tradeoffs clearly.

0 / 2000 characters
[Save]  [Cancel]
```

Use an actual text-input component, not a printed request followed by the normal agent chat composer. Give it a visible label, an example outside the input, a character count, and explicit Save/Cancel actions. Prefilling makes replacement deliberate: saving replaces the single `persona_description` value; “Add” does not append repeated descriptions or create multiple persona records. Keep the editor draft separate from persisted and effective config.

On Save, validate a nonblank description against the backend's existing 2,000-character limit and whitespace semantics. Call the existing `config.set` with `key: "persona_description"`, the entered value, and **`persist: true`**. Use the established configuration path; do not write a separate persona file. Apply the returned config through the normal `host.onConfig` path. Verify live application and restart persistence before claiming both. Do not truncate over-limit input silently.

Show “Saving…” and prevent duplicate submissions. Only after successful persistence show **“Default persona saved.”** and return focus to the previous composer position. A failed save keeps the draft and shows an inline actionable error with Retry and Cancel. If the backend can change live config before a persistence failure, handle and report that distinction; never label an unsaved value as saved.

If an environment override controls the effective persona, explain it in the form: the saved default may not become effective until the override is removed. Read back effective state as needed. Distinguish “default saved” from “active now”; do not promise the latter merely because a write succeeded.

**Reset path:**

```text
Reset your default persona?
This removes your custom description. Learned preferences remain.

› No, keep it
  Yes, reset

↑↓ Choose   Enter Confirm   Esc Cancel
```

Default focus is No. Yes persists an empty `persona_description` through the same `config.set` path with `persist: true`; success reads **“Default persona reset.”** Reset never clears personality notes, RL episodes, policies, or profile preferences. If no custom description exists, show “Already using the agent default” inside the control and offer Add or Back instead of asking a meaningless confirmation. Environment overrides still need the same effective-state explanation.

**Cancel/back behavior:** Escape, Cancel, or No makes no config write. From the editor, Back may return to the action picker with the draft preserved for that invocation. Closing the whole flow discards the unsaved draft. Never submit the persona draft as a chat message. Reset is the only destructive choice here that needs a yes/no confirmation; Add → Save does not need another redundant confirmation.

**Expert shortcuts:** preserve `/persona <description>` and `/persona --clear` as explicit direct actions, with the same backend validation and persistence semantics. Update help/autocomplete to describe bare `/persona` as “Manage your default persona.” Do not require the wizard for scripts or callers that already provide the desired value.

### Apply the pattern throughout management commands

Audit every command and document its primary task before implementation. Avoid replacing every report with a generic yes/no question: named choices work better for multiple actions, yes/no for a clear binary decision, and a text editor for free-form input.

| Bare command/surface | Planned interaction | Persistence/action semantics |
| --- | --- | --- |
| `/persona` | Add / Reset → editor or yes/no reset | Saved installation default; learned growth retained |
| `/provider`, `/effort` | Current selection plus supported-choice picker | Preserve/document existing session versus default scope; offer an explicit saved-default action where supported |
| `/config` | Searchable settings list → appropriate text/enum/toggle editor | Show scope before Save; secret settings route to key management |
| `/profile` | Preference list → Add / Edit / Remove when backend supports it | Use existing profile ownership; implement a missing mutation contract before exposing its action |
| `/growth` | Notes/feedback view → Teach / Forget / Give feedback | Clearly separate learned notes from configured persona; bounded owned mutations |
| `/skills` | Searchable skill list → details/resources/proposals/actions | Built-in ECC remains mandatory; never show unsupported disable/delete controls |
| `/keys` | Provider/key status → Set / Clear | Mask input, never echo values or put them in history; confirm destructive clearing |
| `/sessions`, `/agents`, `/jobs` | Searchable list → contextual supported actions | Confirm deletion; honor active-turn ownership and actual backend capabilities |
| `/models`, `/default-model`, `/theme` | Preserve and improve existing pickers | Make session choice versus persisted default explicit |
| Future MCP management | Server list → Add / Edit / Connect / Disconnect / Remove | Typed validated forms, secret references, health states, existing risk boundaries |
| `/status`, `/usage`, `/context` | Structured report with relevant navigation/actions | Viewing remains cheap and read-only; no fake choices |

This table is a target interaction audit, not evidence that every proposed operation already has a backend endpoint. Inspect each current handler; never render a dead button. Keep explicit command arguments and CLI/API paths functional.

### Shared interaction infrastructure — planned implementation

`FeatureHost` currently exposes `pick()` and an existing yes/no-style `confirm()` helper but no reusable text-entry method. Extend the host contract and `App` once with shared choice, text-form, validation, and confirmation components; do not invent a separate prompt loop for each command. Exact method names can be chosen during implementation.

- Use typed results that distinguish submitted values, cancelled interaction, and request failure.
- Give each flow an owned lifecycle and prevent overlapping prompts. Restore focus on completion/cancel and release it if the gateway closes.
- Keep the normal chat draft separate. Server streaming can continue behind a settings control without stealing focus; preserve existing restrictions on session/turn operations.
- Use existing theme roles for selected, disabled, loading, error, and success. Include a marker/text cue as well as color.
- Reuse the existing terminal overlay/layout system; do not add web DOM/dialog assumptions. Adapt a compact anchored panel or expanded form to available terminal space.
- Choices use arrows and Enter. Forms provide Tab/Shift+Tab between editor/actions, multiline Enter, and a documented save shortcut such as Ctrl+S; Escape cancels. Test actual terminal-library key handling before finalizing these bindings.
- Never rely on hover or mouse input. Wrap descriptions and errors at narrow widths, scroll long forms, and keep actions reachable on short terminals.
- Show a brief success notice rather than pasting the full submitted persona into the transcript. Configuration form values do not become LLM turns or session chat history.
- Carry validation errors beside their field. Disconnected/timeout states retain nonsecret drafts and offer a retry; duplicate Save or stale responses must not overwrite newer edits.

### Planned acceptance tests for the interactive redesign

1. Bare `/persona` opens Add/Reset controls and does not merely print a persona report.
2. Add focuses the editor; Save issues exactly one persistent `config.set`, updates normal config state, and survives a real gateway restart.
3. Editing an existing persona replaces it; it does not append duplicate text. Valid multiline/Unicode values match backend validation.
4. Blank/over-limit input is rejected with a recoverable inline message; backend validation remains authoritative.
5. Cancel, Escape, Back, and No never persist or submit an agent turn; the previous chat draft and focus survive.
6. Reset defaults to No; confirmed Yes clears only `persona_description`, leaving growth/profile/RL data intact.
7. Saving failure, timeout, disconnect, and repeated Save preserve a usable state and never show false success.
8. Environment override behavior is accurately explained, including restart/effective-value checks.
9. Keyboard-only use works at narrow/short sizes and across themes, including the selected-row background regression.
10. Other management commands share these interaction primitives and expose only real supported actions. Expert argument shortcuts remain compatible.

Implement meaningful state/component tests and a real persistence integration test. Inspect representative terminal states in one batched pass, fix the identified defects together, and confirm with one more pass, following Impeccable's bounded verification guidance. Do not build the UI in this documentation phase.

### Phase UI-0 — establish the baseline

1. Preserve/review the uncommitted workflow changes and run focused verification.
2. Reproduce the selected-row background failure and record the intended theme behavior.
3. Launch the TUI with a safe development environment. Capture representative states: empty session, streaming answer, tool execution, failure, approval, cancellation, model picker, session picker, persona/growth, and narrow terminal.
4. Read the existing UI feature contracts and tests before moving components. During actual interface work, apply the available interface-design skill, using this product's terminal constraints.
5. Use the interactive design brief above as the behavioral spec; resolve library-specific key bindings and layouts against actual terminal behavior. The implementation should improve usability, not merely change colors.

### Phase UI-1 — layout and interaction foundations

Preserve the transcript/composer/footer architecture while improving spacing, hierarchy, theme consistency, and focus behavior. Priorities:

- Reliable selected rows with clear keyboard focus across all supported skins.
- Predictable resizing at narrow widths and short terminal heights; no hidden composer or overlapping overlays.
- Clear empty/loading/error states and concise actionable copy.
- Shared action pickers, editable forms, and scoped persistence feedback; implement `/persona` as the first complete interactive management flow.
- Discoverable slash commands and shortcuts without permanent screen clutter.
- Useful picker descriptions and filtering for sessions, models, skills, and eventually servers.
- Accessible contrast and non-color state cues; terminal width accounting must handle Unicode and ANSI sequences.
- Stable scrolling: following a stream should not fight intentional user scrollback.

Keep reusable layout/theme primitives in the established modules. Avoid scattering hardcoded color escapes and duplicated selection renderers across features.

### Phase UI-2 — make agent state understandable

Improve the transcript presentation of tools, approvals, and completion:

- Distinguish running, succeeded, failed, truncated, and cancelled tool results.
- Keep large output compact with a deliberate expansion/read-more behavior appropriate to the TUI.
- Make the dangerous effect and exact approval target legible without dumping credentials or huge arguments.
- Show approved, resumed, and executed as different states.
- Keep stopping/cleanup visible until the backend truly retires the turn.
- Surface degraded capabilities with a reason and useful next action.
- Make persona configuration and learned growth understandable: requested persona, persisted notes, and feedback affect different things.

Do not change backend completion/ownership semantics just to simplify visual state. Fix the presentation to match the contract.

### Phase UI-3 — verify a cohesive product

Use meaningful component/state tests, protocol tests, and gateway integration coverage. Manually exercise keyboard-only operation, theme changes, resize, long tool output, errors, cancellation, and approval resumption. Verify model/provider switching and session isolation still work. Report the full UI result and remaining failures explicitly.

Deliverable: an improved existing TUI with documented interactions and evidence, not a disconnected mockup.

## 12. Next plan: native MCP support

Everything in this section is a **proposed implementation**, not an existing capability. Validate the current protocol and SDK before choosing APIs. Start with the [official MCP documentation](https://modelcontextprotocol.io/) and [official Python SDK](https://github.com/modelcontextprotocol/python-sdk); check the versions, transport behavior, licensing, and lifecycle APIs actually used. These are reading targets, not a claim that this handoff has verified current SDK details.

### Phase MCP-0 — design against existing boundaries

Define the smallest complete path: configure one local fixture server, connect, discover tools, execute through the native broker from a normal task, stream results, handle failure, and shut down cleanly. Then extend to supported remote transport and authentication.

Proposed responsibilities, with final module names chosen during implementation:

| Proposed unit | Responsibility |
| --- | --- |
| `ah/mcp/config.py` | Validated server definitions, secret references, migration/serialization |
| `ah/mcp/client.py` | SDK adapter, transport setup, bounded calls, cancellation |
| `ah/mcp/manager.py` | Runtime-owned connection lifecycle, status, reconnect, shutdown |
| `ah/mcp/catalog.py` | Namespaced discovery, schema validation, ranking/cache invalidation |
| `ah/tools/mcp.py` | Native registry bridge and effect metadata; broker remains authoritative |
| Gateway MCP feature | Authenticated configuration/status/discovery methods |
| UI MCP feature | Server setup, health, tool discovery, actionable errors |

Combine units if simpler; don't create needless layers. The manager must be owned by `RuntimeServices`, shared across entry points, with explicit lifecycle and concurrency rules.

### Phase MCP-1 — configuration and secure lifecycle

Support explicitly configured servers without requiring a separate global enable flag. Decide which transports ship first after SDK research; local stdio and an appropriate supported HTTP transport are candidate targets.

Server definitions need a stable name, transport, launch command/arguments or endpoint, working directory where applicable, credential references, timeouts, and connection policy. Validate them centrally. Return redacted summaries, never resolved credentials.

Saving a server is not permission for every possible server tool effect. Conversely, connecting to an already authorized configured server should not ask again on every prompt. Model configuration authorization, process/network setup, and per-tool action authorization separately.

Do not automatically execute repository `.mcp.json` files or vendored ECC manifests. In particular, an example containing package-runner downloads is not an instruction to fetch and run it at startup. Installation/launch effects must follow the existing risk policy and explicit configuration intent.

Use minimal child environments with declared secret references. Do not pass every parent credential to every server. Workspace roots, working directories, remote endpoints, authentication scope, and redirect handling need explicit boundaries. Support intentionally configured local development endpoints without turning that into unrestricted network access.

Connections should be lazy or otherwise bounded so unused servers do not inflate startup latency. Handshake/discovery failures should mark the server degraded while native chat remains available. Bound startup, call, cancellation, and shutdown time. Keep protocol stdout separate from sanitized stderr diagnostics.

Do not automatically retry a possibly completed mutation after a disconnect. Reconnect and read-only retry rules need to be explicit. Cancellation should reach the server where supported and terminate stuck owned subprocesses through bounded cleanup when necessary.

### Phase MCP-2 — tools through the actual harness

Expose discovered tools through a native adapter with stable collision-safe server/tool names and a reversible mapping to original names. Preserve valid argument schemas and fail clearly on unsupported definitions. Do not silently rewrite schemas in ways that change tool meaning.

Every call must receive the same session/turn/principal context and broker enforcement as native tools. Preserve inherited allowlists for delegated agents. Unknown external tools must not become automatically safe because they entered through an extension adapter.

Define trusted effect mappings or explicit server/tool policies for understood operations, conservative handling for unknown mutations, and exact approval previews for dangerous actions. Remote safety annotations may inform discovery but cannot grant authority. Credential-bearing arguments must be redacted in UI/logging without losing the exact internal approval binding.

Tool discovery must be token efficient. Do not inject every server's full catalog into every prompt. Reuse the skills approach: compact indexed metadata, relevant discovery, bounded schemas/context, lazy detail reads, and no mandatory classifier model call. Ensure a natural task can discover a relevant tool; a purely hidden lazy catalog is not enough.

Cache definitions with invalidation on reconnect, server update, and catalog changes. Do not cache dangerous execution outcomes as reusable authorization. Preserve existing uncacheable integrity-sensitive native tools.

MCP resources and prompts should remain lower-authority data with bounded reads and explicit relevance. They must not overwrite system instructions, permissions, or persona. Define server-initiated features such as sampling, elicitation, and roots deliberately; do not enable recursive model calls, unbounded cost, secret access, or filesystem expansion by default.

### Phase MCP-3 — gateway and UI surfaces

Add typed authenticated feature methods for server listing/configuration, connection status, connect/disconnect, and bounded tool discovery. Names such as `mcp.list`, `mcp.add`, `mcp.update`, `mcp.remove`, and `mcp.tools` are proposals, not a current contract.

Keep administrative configuration ownership separate from ordinary task execution. Validate the relevant session/principal instead of exposing sensitive methods through an accidentally public gateway handler. Update Python handlers, TypeScript contracts, protocol documentation, and tests together. Add events only when an existing contract cannot express the state correctly.

The TUI should make these states visible: saved, connecting, ready, degraded, disconnected, and unavailable credentials. A server test should perform handshake/catalog discovery or a known harmless health check, not arbitrarily execute a mutating tool. Show actionable diagnostics with secret redaction.

Configuration should be possible without manually editing internal files, and persisted servers should behave consistently after restart. Clearly show prerequisites when a configured command is missing. The user should not have to tell the model which skill or server tool to use for every task.

### Phase MCP-4 — real end-to-end verification

Use a fixture MCP server started as an actual subprocess and, for supported remote transport, a local HTTP fixture. Mock-only tests cannot establish lifecycle and wire behavior. Cover:

1. Clean startup, handshake, discovery, schema conversion, normal calls, and streaming-agent usage.
2. Read-only tasks without repeated approvals; dangerous actions requiring an exact decision.
3. Changed arguments, denied/revoked grants, and attempted child-agent restriction bypass.
4. Missing binary, malformed catalog, tool error, timeout, disconnect, reconnect, and server crash.
5. Cancellation and shutdown without leaked processes/tasks or premature turn completion.
6. Secret redaction in config RPC, transcript, stderr, audit, and exceptions.
7. Naming collisions, server updates, catalog invalidation, and bounded context/token overhead.
8. Session isolation, HTTP/gateway authentication, delegated tasks, and scheduled-job compatibility.
9. Wheel/install behavior outside the checkout and operation without unnecessary network downloads.

Acceptance scenario: configure a fixture server once, restart the harness, ask a natural task, discover and use an appropriate read-only tool without a new permission card on every invocation, then request a dangerous action and see its exact approval. Denial prevents execution. Server failure leaves native chat operational. UI and HTTP report the same lifecycle semantics.

## 13. Suggested execution order

1. Inspect the working tree and finish/review the existing cohesive workflow changes.
2. Reproduce and triage the known baseline test failures; keep unrelated remediation explicit.
3. Establish the UI state/design baseline and fix selection/layout foundations.
4. Research and implement the minimal MCP runtime path behind the existing service graph and broker.
5. Add durable server management and the TUI feature once backend contracts are concrete.
6. Improve transcript/approval/capability presentation using real integration states.
7. Run focused regressions, then full relevant suites, packaging checks, and manual terminal scenarios.
8. Update this handoff, feature docs, protocol contract, and release ledger with actual results.

Work in reviewable increments. No merge, push, deployment, or publication is implied by this document.

## 14. Failure traps for the next maintainer

- Confusing vendored ECC source with active external integrations.
- Treating upstream `AGENTS.md` files as root harness instructions. They belong to the bundle.
- Breaking mandatory skill defaults while adding a custom local skills directory.
- Sending hundreds of skill bodies or MCP schemas on every turn.
- Adding a classifier LLM call that consumes latency/tokens just to route capabilities.
- Letting stale conversation stacks dictate a new explicit technology.
- Truncating skill pages while advancing their offsets past unseen text.
- Caching resource reads in a way that skips integrity verification.
- Treating model completion or approval recording as successful tool execution.
- Widening delegated-agent restrictions during discovery.
- Using remote tool descriptions as a security boundary.
- Passing all environment credentials into every server subprocess.
- Starting all servers or package downloads just because an example manifest exists.
- Making one broken server prevent the harness from starting.
- Reporting old test snapshots as verification of newer code.
- Claiming personality context/bandit updates are model-weight RL.
- Writing a second runtime for a new entry point and drifting from shared behavior.

## 15. Final successor checklist

Before calling the next phase complete, be able to answer all of these with evidence:

- What changed in the existing UI, and how does it behave at narrow sizes and during streaming/cancellation?
- Which MCP transports and SDK version are actually supported, and why?
- Where is server configuration persisted, who may change it, and how are credentials referenced?
- How does a normal task discover a tool without a giant context dump?
- Which tool effects run automatically, which require approval, and where is that enforced?
- Can a child agent, malicious server description, prompt, or resource widen authority? It must not.
- Do CLI, TUI, HTTP, jobs, and delegation share the same implementation?
- What happens when the server fails, the user cancels, or the process shuts down?
- Have actual wire/process tests, protocol checks, packaging checks, and manual UI scenarios passed?
- Which failures or untested external dependencies remain?
- Are bundle provenance, licenses, docs, and release gates still accurate?

Leave the next maintainer exact commands, failing test names, and honest limits. The most valuable inheritance is a system whose behavior can be explained and verified.
