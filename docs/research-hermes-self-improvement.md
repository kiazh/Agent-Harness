# Hermes Agent Self-Improvement & Learning Loop Architecture

> Research document analyzing Hermes Agent's self-improvement mechanisms for AgentHarness adoption.
> Sources: official docs, source code (GitHub main), third-party audits, community analysis.

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [The Closed Learning Loop](#the-closed-learning-loop)
3. [Skill Creation from Experience](#skill-creation-from-experience)
4. [Skill Improvement During Use](#skill-improvement-during-use)
5. [Self-Nudging: Persisting Knowledge](#self-nudging-persisting-knowledge)
6. [Skill Curation Process](#skill-curation-process)
7. [Four-Layer Memory System](#four-layer-memory-system)
8. [Progressive Disclosure](#progressive-disclosure)
9. [Context Compression & Caching](#context-compression--caching)
10. [Cron Scheduling](#cron-scheduling)
11. [User Modeling (Honcho)](#user-modeling-honcho)
12. [Session Search (FTS5)](#session-search-fts5)
13. [Skill Authoring Standards](#skill-authoring-standards)
14. [Safety Mechanisms & Guardrails](#safety-mechanisms--guardrails)
15. [What Hermes Explicitly Does NOT Do](#what-hermes-explicitly-does-not-do)
16. [Adoption Roadmap for AgentHarness](#adoption-roadmap-for-agentharness)
17. [Key Files & References](#key-files--references)

---

## Executive Summary

Hermes Agent (Nous Research) implements the most complete production-grade self-improvement loop available in any open-source agent framework. The core insight: **the agent writes its own context** — it distills completed workflows into reusable Markdown files (skills), stores durable facts in bounded memory files, and reviews every session in the background to extract lessons.

The system is NOT a recursive self-modifier (it never edits its own source code). Instead, it builds **durable behavioral artifacts** — skills, memory entries, session history — that influence future sessions. This makes improvement safe, inspectable, and reversible.

**Key mechanisms:**
- Background review agent inspects every finished turn and writes/patches skills
- Skills are plain Markdown files in `~/.hermes/skills/` (agentskills.io standard)
- Four-layer memory: prompt memory, session search, skills, user model
- Progressive disclosure keeps token costs flat regardless of skill count
- Curator consolidates and archives stale skills on a 7-day cycle
- Cron system for scheduled autonomous work
- Honcho integration for dialectic user modeling

---

## The Closed Learning Loop

Hermes describes its self-improvement as a four-phase cycle:

```
┌─────────────────────────────────────────────────────────┐
│                   CLOSED LEARNING LOOP                   │
│                                                          │
│  1. EXECUTE                                              │
│     Agent performs task (5+ tool calls, error recovery,  │
│     user correction, novel workflow)                     │
│         ↓                                                │
│  2. EVALUATE                                             │
│     Background review agent inspects the turn:           │
│     "What worked? What failed? What's reusable?"         │
│         ↓                                                │
│  3. EXTRACT                                              │
│     If signal threshold met → write/patch skill          │
│     via skill_manage tool                               │
│         ↓                                                │
│  4. RETRIEVE                                             │
│     Next session: progressive disclosure loads skill     │
│     index; agent loads full SKILL.md on demand           │
│         ↓                                                │
│     Back to 1. EXECUTE (with skill loaded)               │
└─────────────────────────────────────────────────────────┘
```

**Trigger thresholds for skill creation:**
- Task required 5+ tool calls
- Error occurred and was successfully recovered
- User corrected the agent's approach, style, or workflow
- Non-trivial technique/workaround emerged
- A loaded skill was found to be wrong or outdated

**What does NOT trigger skill creation:**
- Environment-dependent failures (missing binaries, fresh-install errors)
- Negative claims about tools ("browser tools don't work")
- Session-specific transient errors that resolved
- One-off task narratives ("summarize today's market")

---

## Skill Creation from Experience

### 1. Background Review Agent (Primary Mechanism)

**Source:** `agent/background_review.py`

After every turn, `AIAgent.run_conversation` calls `spawn_background_review` which:
1. Launches a **daemon thread** (non-blocking)
2. Copies the current session's message snapshot
3. Starts a **forked AIAgent** (separate agent instance)
4. Sends the review prompt to the fork
5. The fork writes skills/memory directly to disk

**Review prompt (skill review):**
```
Review the conversation above and update the skill library. Be ACTIVE —
most sessions produce at least one skill update, even if small. A pass
that does nothing is a missed learning opportunity, not a neutral outcome.

Target shape of the library: CLASS-LEVEL skills, each with a rich
SKILL.md and a `references/` directory for session-specific detail.
Not a long flat list of narrow one-session-only-skill entries.

Signals to look for (any one of these warrants action):
• User corrected your style, tone, format, legality, or verbosity.
  Frustration signals like 'stop doing X', 'this is too verbose',
  'don't format like this' are FIRST-CLASS skill signals.
• User corrected your workflow, approach, or sequence of steps.
• Non-trivial technique, fix, workaround, debugging path, or
  tool-usage pattern emerged that a future session would benefit from.
• A skill that got loaded or consulted this session turned out
  to be wrong, missing a step, or outdated. Patch it NOW.

Preference order — prefer the earliest action that fits:
1. UPDATE A CURRENTly-LOADED SKILL (patch it first)
2. UPDATE AN EXISTING UMBRELLA (via skills_list + skill_view)
3. ADD A SUPPORT FILE under an existing umbrella
4. CREATE A NEW CLASS-LEVEL UMBRELLA SKILL when nothing exists
```

**Memory review prompt:**
```
Review the conversation above and consider saving to memory if appropriate.
Focus on:
1. Has the user revealed things about themselves — their persona, desires,
   preferences, or personal details worth remembering?
2. Has the user expressed expectations about how you should behave?
If something stands out, save it using the memory tool.
If nothing is worth saving, just say 'Nothing to save.' and stop.
```

### 2. The `/learn` Command

**Source:** `tools/skill_manager_tool.py`, docs

`/learn` is the explicit skill creation path — it turns any source material into a reusable skill:

```bash
# From a local code directory
/learn the REST client in ~/projects/acme-sdk, focus on auth + pagination

# From an online doc page
/learn https://docs.example.com/api/quickstart

# From the workflow just executed in conversation
/learn how I just deployed the staging server

# From pasted notes / described procedure
/learn filing an expense: open the portal, New > Expense, attach receipt, submit

# From a book or large docs corpus → knowledge-base skill
/learn ~/books/designing-data-intensive-applications.pdf
```

For large sources, `/learn` creates an **expansive knowledge-base skill**: a lean `SKILL.md` with core mental models + an index, with one distilled file per chapter/topic under `references/`. Reference files cost nothing until needed.

Re-running `/learn` with new material on the same topic **folds it into the existing skill** rather than creating a duplicate.

### 3. The `skill_manage` Tool

**Source:** `tools/skill_manager_tool.py`

The agent's primary tool for skill CRUD:

| Action | Description |
|--------|-------------|
| `create` | Create new skill with SKILL.md and directory structure |
| `edit` | Rewrite entire SKILL.md content |
| `patch` | Targeted find-and-replace inside SKILL.md or support file |
| `write_file` | Add/overwrite support file (references/, templates/, scripts/) |
| `remove_file` | Delete a support file |
| `delete` | Delete an entire skill |

**Advisory linter** runs on `create`, `patch`, and `write_file`:
- `incident-log-shape`: body dense in PR/issue numbers (warns)
- `references-sprawl`: more than 60 reference files (warns)
- `oversized-body`: SKILL.md body past ~24k chars (warns)

Linter findings are returned in the tool result but **never block a write**.

### 4. Skill File Structure

```
~/.hermes/skills/
└── my-skill/
    ├── SKILL.md                    # Main skill document (YAML frontmatter + markdown)
    ├── references/
    │   ├── api-docs.md             # Session-specific detail, knowledge banks
    │   └── examples.md
    ├── templates/
    │   └── config.yaml             # Starter files to copy and modify
    └── scripts/
        └── setup.sh                # Re-runnable actions
```

**SKILL.md format:**
```yaml
---
name: my-skill
description: One-line trigger description (≤60 chars)
triggers:
  - keyword1
  - keyword2
version: 1.0.0
---

# Skill Title

## Instructions
Step-by-step procedure...

## Pitfalls
- What goes wrong and how to avoid it

## Verification
How to confirm the task was done correctly
```

---

## Skill Improvement During Use

### Mid-Session Skill Patching

When the agent loads a skill and discovers a better approach, or hits an error the skill didn't predict, it can **update the skill mid-task**:

1. Agent loads skill via `skill_view(name)`
2. Discovers the skill is outdated or wrong
3. Calls `skill_manage(action="patch", ...)` with targeted fix
4. Prefers `patch` over `edit` — smaller, safer, less likely to break working skill

**Priority order for updates:**
1. Patch the currently-loaded skill first
2. Then an existing umbrella skill in the right category
3. Then add a support file under an existing umbrella
4. Only then create a new skill

### Read-Before-Write Guard

**Source:** `tools/skill_manager_tool.py` (v2026.7.1+)

A safety guard prevents accidental overwrites: the review fork must call `skill_view(name)` before `skill_manage(action="patch")` on an existing skill. This ensures the fork has actually seen the current content before modifying it.

The guard tracks reads **per-path** (not per-skill): `skill_view(name, file_path="references/api-docs.md")` is required before overwriting that specific support file.

### User-Preference Embedding

When the user corrects style/format/workflow, the update belongs in the **SKILL.md body**, not just in memory:
- **Memory** captures "who the user is and what the current situation is"
- **Skills** capture "how to do this class of task for this user"

---

## Self-Nudging: Persisting Knowledge

### SKILLS_GUIDANCE in System Prompt

**Source:** `agent/prompt_builder.py`

The system prompt is assembled in three layers: **stable**, **context**, and **volatile**. The stable layer carries a hard rule:

```
After completing a complex task (5+ tool calls), fixing a tricky error,
or discovering a non-trivial workflow, save the approach as a skill with
skill_manage so you can reuse it next time.
```

This is the primary "nudge" — the agent is instructed to persist knowledge as part of its core operating instructions.

### Activity Bias in Review Prompts

The background review prompt is deliberately **biased toward action**:

> "Be ACTIVE — most sessions produce at least one skill update, even if small. A pass that does nothing is a missed learning opportunity, not a neutral outcome."

This counteracts the natural tendency to do nothing. The review agent is told that inaction is a failure mode, not a neutral outcome.

### Memory Nudges

The memory review prompt asks two specific questions:
1. Did the user reveal things about themselves?
2. Did the user express expectations about how the agent should behave?

If yes → save to memory. If no → "Nothing to save." (explicit no-op signal).

### Write Approval Gate (Optional)

```yaml
skills:
  write_approval: false     # false = write freely (default) | true = require approval
```

When `true`, every `skill_manage` write is **staged** under `~/.hermes/pending/skills/` and reviewed via:
- `/skills pending` — list staged writes
- `/skills diff` — full unified diff
- `/skills apply` — approve
- `/skills reject` — deny

This is the human-in-the-loop safety rail for the self-improvement loop.

---

## Skill Curation Process

### The Curator

**Source:** `agent/curator.py`, docs

The curator is a **background maintenance pass** for agent-created skills. It runs on an inactivity check (not cron) — triggered when:
1. Enough time has passed since last run (`interval_hours`, default **7 days**)
2. The agent has been idle long enough (2+ hours)

**What it does:**
- Tracks each skill's **view count**, **use count**, and **patch count**
- Moves unused skills through lifecycle states: `active → stale → archived`
- Periodically spawns a short auxiliary-model review proposing consolidations
- **Never auto-deletes** — worst case is archival to `~/.hermes/skills/.archive/`

**Lifecycle states:**

| State | Condition | Behavior |
|-------|-----------|----------|
| `active` | Normal use | Available in skills_list |
| `stale` | 30 days without use | Still available, flagged |
| `archived` | 90 days without use | Moved to `.archive/`, recoverable |

**Scope:**
- By default, manages only **agent-created** skills
- `curator.prune_builtins: true` can also archive unused bundled skills (opt-in)
- Hub-installed skills are always off-limits
- Pinned skills (`hermes curator pin`) can be improved but not archived

**Adoption:**
```bash
hermes curator list-unmanaged          # Find unmanaged skills
hermes curator adopt <name>            # Hand over to curator
hermes curator adopt --all-unmanaged   # Hand over all
```

### Consolidation

When the review agent notices two existing skills that overlap, it notes it in its reply. The curator handles consolidation at scale — merging overlapping skills into umbrella skills.

### Protected Skills

The background review fork **cannot edit**:
- Bundled skills (shipped with Hermes)
- Hub-installed skills (via `hermes skills install`)
- Skills in external directories
- Pinned skills (blocks deletion/archive, not content updates)
- User-owned skills (hand-written, installed by URL, or asked to create)

---

## Four-Layer Memory System

Hermes splits memory into four distinct layers, each with its own trigger and job:

| Layer | Storage | What it holds | When loaded |
|-------|---------|---------------|-------------|
| **1. Prompt Memory** | `MEMORY.md` (2,200 chars) + `USER.md` (1,375 chars) | Stable facts, preferences, environment | Every session (frozen snapshot) |
| **2. Session Search** | SQLite + FTS5 | Full conversation history, searchable | On demand via `session_search` |
| **3. Skills** | `~/.hermes/skills/` (Markdown) | Procedures, workflows, pitfalls | On demand via `skill_view` |
| **4. User Model** | Honcho (external) | Dialectic user representation | Auto-injected context |

### Layer 1: Prompt Memory

**Files:** `~/.hermes/memories/MEMORY.md` and `USER.md`

**Production hardening:**
- **File locking** via `fcntl`/`msvcrt` — prevents race conditions between concurrent agent processes
- **Frozen-snapshot pattern** — memory block captured once at session start; mid-session writes hit disk but don't mutate current prompt (preserves prefix cache)
- **Injection scanner** — scans all writes for prompt-injection patterns, exfiltration indicators, SSH-backdoor signatures
- **Character limits** — MEMORY.md capped at 2,200 chars (~800 tokens), USER.md at 1,375 chars (~500 tokens)
- **No auto-compact** — when full, the tool returns an error; the agent must consolidate/remove entries itself

**External providers (one at a time):**
- Honcho, Mem0, Hindsight, Supermemory, Holographic, RetainDB, ByteRover, OpenViking
- Built-in memory is always first, always active
- Exactly one external provider can run alongside it
- Provider context wrapped in `<memory-context>` fence tags + system note clarifying it's background context, not user input

### Layer 2: Session Search

**Storage:** `~/.hermes/state.db` (SQLite with FTS5 full-text search)

**Schema:**
- Session ID, source platform, user ID
- Session title (unique, human-readable)
- Model name and configuration
- System prompt snapshot
- Full message history (role, content, tool calls, tool results)
- Token counts (input/output)
- Timestamps (started_at, ended_at)
- Parent session ID (for compression-triggered session splitting)

**Capabilities:**
- Resume conversations by session name or ID
- Cross-session full-text search
- LLM-summarized recall of past sessions
- Session splitting on compression (new continuation session with numbered title)

### Layer 3: Skills

See [Skill Creation](#skill-creation-from-experience) and [Skill Curation](#skill-curation-process) above.

### Layer 4: User Model (Honcho)

See [User Modeling](#user-modeling-honcho) below.

---

## Progressive Disclosure

The key engineering decision that makes large skill libraries viable:

**Level 0 — Index (always loaded):**
- Skill names + one-line descriptions only
- ~3k tokens total for entire library
- Compact lookup table in system prompt

**Level 1 — Full Skill (loaded on demand):**
- Agent calls `skill_view(name)` when it judges a skill relevant
- Full SKILL.md content loaded into context
- Stays in context for rest of session

**Level 2 — Deep References (rare):**
- `references/` subdirectory loaded only when needed
- Example scripts, config templates, API docs
- Loaded via `skill_view(name, file_path="references/api-docs.md")`

**Result:** Token cost scales with **relevance**, not library size. An agent with 500 skills pays roughly the same base cost as one with 50.

---

## Context Compression & Caching

### Dual Compression System

**Source:** `agent/context_compressor.py`, `gateway/run_turn.py`

Hermes has two independent compression layers:

```
Incoming message
      ↓
┌──────────────────────────┐
│ Gateway Session Hygiene  │  Fires at 85% of context
│ (pre-agent, rough est.)  │  Safety net for large sessions
└─────────────┬────────────┘
              ↓
┌──────────────────────────┐
│ Agent ContextCompressor  │  Fires at 50% of context (default)
│ (in-loop, real tokens)   │  Normal context management
└──────────────────────────┘
```

**Gateway Session Hygiene (85% threshold):**
- Located in `gateway/run_turn.py`
- Safety net for sessions that grew between turns (e.g., overnight Telegram accumulation)
- Uses rough character-based token estimate
- Fires only when `len(history) >= 4`

**Agent ContextCompressor (50% threshold, configurable):**
- Located in `agent/context_compressor.py`
- Primary compression with accurate API-reported token counts
- Configurable via `compression.threshold` (default 0.50)
- Per-model threshold overrides via `compression.model_thresholds`

**Configuration:**
```yaml
compression:
  enabled: true
  threshold: 0.50            # Fraction of context window
  target_ratio: 0.20         # How much of threshold to keep as tail
  in_place: true             # Compact on same session id, no rotation
  model_thresholds:
    "glm-5.2": 0.40          # Per-model overrides (substring match)
```

**Compression process:**
1. **Pre-pass:** Old tool results (>200 chars) outside protected tail replaced with `[Old tool output cleared to save context space]`
2. **Determine boundaries:** Identify what to keep (recent messages, system prompt, goal)
3. **Summarize:** Use auxiliary model (cheap/fast) to compress older context
4. **Reassemble:** Combine summary + protected tail + recent messages

### Prompt Caching

**Source:** `agent/prompt_caching.py`

- Anthropic prompt caching for prefix-cache hits
- Frozen-snapshot memory pattern preserves cache (memory writes don't invalidate prefix)
- Auxiliary models for compression, vision, web extract, approval scoring, skill search

### Auxiliary Models

Hermes uses a two-tier model system:
- **Main model** — what the agent thinks with
- **Auxiliary models** — smaller side-jobs (compression, vision, web extract, approval, skill search, title generation, curator)

This allows expensive reasoning models to be used for main tasks while cheap flash models handle summarization at 1/50th the cost.

---

## Cron Scheduling

**Source:** `tools/cronjob.py`, `~/.hermes/cron/jobs.json`

### Architecture

- Gateway daemon ticks scheduler every **60 seconds**
- Jobs stored in `~/.hermes/cron/jobs.json` (plain JSON, survives updates)
- Each due job runs in a **fresh AIAgent session**
- File lock at `~/.hermes/cron/.tick.lock` prevents overlapping ticks

### Job Capabilities

- One-shot or recurring schedules (natural language or cron expressions)
- Attach zero, one, or multiple skills to a job
- Deliver results to origin chat, local files, or platform targets
- **No-agent mode**: script runs on schedule, stdout delivered verbatim (zero LLM)
- Per-job model pin, reasoning effort pin
- `context_from`: consume output of other jobs as input
- Workdir support (loads AGENTS.md, CLAUDE.md from that directory)

### Lifecycle

```
create → pause/resume → edit → run (trigger) → remove
```

### Safety

- Cron-run sessions **cannot recursively create cron jobs** (configurable via `cron.allow_agent_scheduling`)
- Model/provider drift guard: unpinned jobs fail closed if global model changes
- Pre-flight validation: checks API key, skills, delivery targets before spending tokens
- Failure streak tracking with review nudges
- Prompt-injection scanning on job prompts

### Execution Ledger

- `~/.hermes/cron/executions.db` tracks every attempt
- States: `claimed → running → completed/failed/unknown`
- Unknown attempts (process died) are audit records, never auto-rerun

---

## User Modeling (Honcho)

**Source:** `plugins/memory/honcho/`, docs

Honcho provides **AI-native cross-session user modeling** with dialectic reasoning.

### Architecture

```
┌─────────────────────────────────────────┐
│              Honcho Cloud               │
│                                         │
│  ┌─────────────┐    ┌─────────────┐    │
│  │  User Peer  │◄──►│  AI Peer    │    │
│  │  (peerName) │    │  (aiPeer)   │    │
│  └─────────────┘    └─────────────┘    │
│         │                  │            │
│         └──────┬───────────┘            │
│                │                        │
│         ┌──────┴──────┐                 │
│         │  Workspace  │                 │
│         │  (shared)   │                 │
│         └─────────────┘                 │
└─────────────────────────────────────────┘
```

### Two-Layer Context Injection

1. **Base context** (refreshed on `contextCadence`):
   - Session summary
   - User representation (preferences, facts, patterns)
   - User peer card
   - AI self-representation and identity card

2. **Dialectic supplement** (refreshed on `dialecticCadence`):
   - LLM-synthesized reasoning about user's current state and needs
   - Multi-pass depth (1–3 passes)
   - Cold/warm prompt selection (cold = general facts, warm = session-scoped)

### Three Orthogonal Knobs

| Knob | Config Key | Default | Range | Description |
|------|-----------|---------|-------|-------------|
| Cadence | `contextCadence` / `dialecticCadence` | 1 / 2 | 1–5 | How often to refresh |
| Depth | `dialecticDepth` | 1 | 1–3 | Reasoning rounds per query |
| Level | `dialecticReasoningLevel` | `low` | minimal–max | Intensity of each round |

### Tools

| Tool | Description |
|------|-------------|
| `honcho_profile` | Fast peer card retrieval (no LLM) |
| `honcho_search` | Semantic search over memory |
| `honcho_context` | Dialectic Q&A powered by LLM |
| `honcho_reasoning` | LLM-synthesized reasoning |
| `honcho_conclude` | Create/delete durable conclusions |

### Multi-Profile Support

Each Hermes profile gets its own AI peer while sharing the same workspace (user context). A `coder` profile stays code-oriented while a `writer` profile stays editorial — both against the same user.

---

## Session Search (FTS5)

**Storage:** `~/.hermes/state.db` (SQLite with FTS5)

Every conversation is automatically saved and indexed. The agent can search past sessions via the `session_search` tool with three modes:

| Mode | Description |
|------|-------------|
| `DISCOVERY` | Broad search across all sessions |
| `SCROLL` | Paginated browsing |
| `BROWSE` | Navigate within a specific session |

**Capabilities:**
- Full-text search across all past conversations
- LLM-summarized recall of search results
- Session resume by name or ID
- Cross-session pattern recognition

---

## Skill Authoring Standards

### SKILL.md Format

```yaml
---
name: skill-name
description: ≤60-char trigger description
triggers:
  - keyword1
  - keyword2
version: 1.0.0
---

# Skill Title

## Instructions
Numbered steps with exact commands and tool calls.

## Pitfalls
- Generalizable rule + one clause of why (the mechanism)
- Attached to the step it affects
- Stated once, not as incident narration

## Verification
How to confirm the task was done correctly.
```

### Authoring Rules

1. **Lessons, not logs**: A pitfall is a generalizable rule + why, not a story
2. **No incident narration**: No PR numbers, dates, quoted chat
3. **Class-level names**: Skill name must be a class of task, not a specific session artifact
4. **Always-on rules in SKILL.md**: `references/` holds topic-specific detail
5. **Support file types:**
   - `references/*.md` — session-specific detail, knowledge banks
   - `templates/*` — starter files to copy and modify
   - `scripts/*` — re-runnable actions (verification, probes)
6. **One-line pointer**: SKILL.md should reference each support file
7. **Don't restate loaded context**: Skills don't duplicate AGENTS.md or tool schemas

### Linter Rules

| Rule | Trigger | Action |
|------|---------|--------|
| `incident-log-shape` | Body dense in PR/issue numbers | Warn |
| `references-sprawl` | >60 reference files | Warn |
| `oversized-body` | SKILL.md body >~24k chars | Warn |

---

## Safety Mechanisms & Guardrails

### 1. Write Approval Gate
```yaml
skills:
  write_approval: true   # Stage all skill writes for human review
```

### 2. Read-Before-Write Guard
Review fork must `skill_view(name)` before `skill_manage(action="patch")` on existing skills.

### 3. Protected Skills
Bundled, hub-installed, pinned, and user-owned skills cannot be edited by the background review.

### 4. Injection Scanner
All memory writes and context files scanned for:
- Prompt-injection patterns
- Exfiltration indicators
- SSH-backdoor signatures
- Hidden Unicode tricks

### 5. Memory Character Limits
- MEMORY.md: 2,200 chars (~800 tokens)
- USER.md: 1,375 chars (~500 tokens)
- No auto-compact — agent must consolidate when full

### 6. One External Memory Provider
Built-in memory always active; exactly one external provider at a time.

### 7. Curator Never Auto-Delete
Worst case is archival to `.archive/`, always recoverable.

### 8. Cron Safety
- No recursive cron creation
- Model/provider drift guard
- Pre-flight validation before spending tokens
- Prompt-injection scanning on job prompts

---

## What Hermes Explicitly Does NOT Do

An honest audit of the codebase reveals:

- **No autonomous source-code modification** — no tool writes to the agent's own Python files
- **No automatic prompt rewriting** — system prompts assembled deterministically
- **No self-grading loop** — trajectories persisted for external analysis, not consumed by the running agent
- **No agent-authored git commits** — all commits are human-authored
- **No recursive self-improvement** — the agent doesn't edit its own substrate in a tight feedback loop

The "self-improving" label means: **the agent writes durable behavioral artifacts (skills, memory) that influence future sessions** — not that it modifies its own code or weights.

---

## Adoption Roadmap for AgentHarness

### Current AgentHarness State

| System | AgentHarness | Gap |
|--------|-------------|-----|
| Skills | `ah/skills/registry.py` - file-based, trigger matching, curator, `ah learn` | No background review, no agent-driven patching |
| Memory | `ah/memory/` - PostgreSQL + pgvector, consolidation, approval gate, redaction, identity gate | No frozen snapshot, no char limits |
| Context | `ah/core/context.py` - msgpack chunks, token budget, reversible archive | No dual compression, no auxiliary models |
| Sessions | `ah/core/session.py` - PostgreSQL, TTLCache | No FTS5 search, no session splitting |
| User Model | `UserProfileStore` preferences/topics | No Honcho or dialectic reasoning |
| Cron | Provided: `ah/core/scheduler.py` + `cron.py` (heartbeat/interval/cron) | No per-job model pin, no script-only jobs |
| Self-Improvement | Skill curator maintenance pass | No background review fork, no skill nudge |

### Phase 1: Background Review Agent (High Impact, Medium Effort)

**What to build:**
- `ah/core/background_review.py` — daemon thread that forks the agent after each turn
- Review prompt constants (skill review + memory review)
- Trigger detection (5+ tool calls, error recovery, user correction)
- Write to existing `ah/skills/` and `ah/memory/` stores

**Key design decisions:**
- Use asyncio tasks instead of threads (AgentHarness is async)
- Review agent should use a cheaper auxiliary model
- Respect write approval gate if configured

### Phase 2: Skill Self-Improvement (High Impact, Low Effort)

**What to build:**
- `skill_manage` equivalent tool (create, patch, edit, write_file, remove_file)
- Read-before-write guard (must `skill_view` before `patch`)
- Mid-session skill patching
- Skill linter (incident-log-shape, references-sprawl, oversized-body)

### Phase 3: SKILLS_GUIDANCE Nudge (High Impact, Low Effort)

**What to build:**
- Add to system prompt assembly in `ah/core/assembler.py`:
  ```
  After completing a complex task (5+ tool calls), fixing a tricky error,
  or discovering a non-trivial workflow, save the approach as a skill.
  ```
- Activity bias: "A pass that does nothing is a missed learning opportunity"

### Phase 4: Skill Curation (Medium Impact, Medium Effort)

**What to build:**
- `ah/skills/curator.py` — background maintenance pass
- Track view/use/patch counts per skill
- Lifecycle: active → stale (30 days) → archived (90 days)
- Consolidation pass for overlapping skills
- Never auto-delete

### Phase 5: Progressive Disclosure (Medium Impact, Low Effort)

**What to build:**
- Level 0: Skill index (name + description) in system prompt
- Level 1: Full SKILL.md loaded on demand
- Level 2: Support files loaded on demand
- Already partially implemented in `ah/skills/registry.py`

### Phase 6: Memory Production Hardening (Medium Impact, Medium Effort)

**What to build:**
- Frozen-snapshot pattern (capture at session start, don't mutate)
- Injection scanner on memory writes
- Character limits (MEMORY.md: 2,200, USER.md: 1,375)
- File locking for concurrent writes

### Phase 7: Context Compression (Medium Impact, High Effort)

**What to build:**
- Dual compression system (gateway hygiene + in-loop)
- Auxiliary model for summarization
- Configurable thresholds
- Per-model threshold overrides

### Phase 8: Cron Scheduling (Medium Impact, High Effort)

**What to build:**
- `ah/tools/cron.py` — cronjob tool
- Job storage in JSON
- Scheduler tick in gateway
- No-agent mode for script-only jobs
- Per-job model pin

### Phase 9: User Model (Low Impact, High Effort)

**What to build:**
- Honcho integration or equivalent
- Dialectic reasoning layer
- Two-layer context injection
- Multi-profile support

### Phase 10: Session Search (Low Impact, Medium Effort)

**What to build:**
- FTS5 full-text search over session history
- Session resume by name
- LLM-summarized recall

---

## Key Files & References

### Hermes Agent Source Files

| File | Purpose |
|------|---------|
| `agent/background_review.py` | Background review agent, spawn logic, review prompts |
| `agent/prompt_builder.py` | System prompt assembly, SKILLS_GUIDANCE injection |
| `agent/context_compressor.py` | Dual compression system |
| `agent/memory_manager.py` | "Built-in + at most one external" policy |
| `tools/skill_manager_tool.py` | skill_manage tool, read-before-write guard, linter |
| `tools/memory_tool.py` | MEMORY.md/USER.md read/write with file locking |
| `tools/cronjob.py` | Cron job management |
| `tools/delegate_tool.py` | Sub-agent spawning with depth limits |
| `tools/registry.py` | Self-registering tool registry with AST discovery |
| `plugins/memory/honcho/` | Honcho user modeling integration |
| `gateway/run.py` | Gateway runner, session hygiene |
| `hermes_cli/main.py` | CLI entry, update protocol |

### Documentation

- [Skills System](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
- [Curator](https://hermes-agent.nousresearch.com/docs/user-guide/features/curator)
- [Persistent Memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)
- [Memory Providers](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory-providers)
- [Context Compression](https://hermes-agent.nousresearch.com/docs/developer-guide/context-compression-and-caching)
- [Cron](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron)
- [Honcho](https://hermes-agent.nousresearch.com/docs/user-guide/features/honcho)
- [Sessions](https://hermes-agent.nousresearch.com/docs/user-guide/sessions)

### Third-Party Analysis

- [Inside Hermes Agent: How Self-Improving Skills Work](https://fp8.co/articles/Inside-Hermes-Agent-Self-Improving-Skill-Memory) — fp8.co
- [What 'Self-Improving AI Agent' Actually Means in Production](https://saulius.io/blog/hermes-agent-self-improving-ai-architecture) — saulius.io
- [The Anatomy of a Self-Improving AI Agent](https://dev.to/nilambuilds/the-anatomy-of-a-self-improving-ai-agent-how-hermes-agents-closed-learning-loop-actually-works-4jk7) — DEV Community
- [Self-Improving AI Agents: What Works, What Doesn't](https://pickaxe.co/post/self-improving-ai-agents) — pickaxe.co
- [8 Self-Evolving Skills Hermes Agent Writes on Its Own](https://ssojet.com/blog/hermes-agent-self-evolving-skills) — SSOJet

---

*Research completed: 2026-10-01*
*Sources: Hermes Agent official docs, GitHub source (main branch), third-party audits*
