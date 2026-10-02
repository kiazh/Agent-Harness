# Hermes Agent Memory & Procedural Memory System — Research

> **Status:** Research document for AgentHarness improvement.
> **Source:** Hermes Agent v2026.x (Nous Research), official docs, GitHub source, and community analysis.
> **Date:** 2026-10-01

---

## 1. Architecture Overview

Hermes Agent implements a **four-layer memory architecture** that separates concerns by persistence scope and retrieval mechanism:

```
┌─────────────────────────────────────────────────────────┐
│ Layer 4: Procedural Memory                              │
│   Skills — reusable task knowledge, auto-created        │
│   Location: $HERMES_HOME/skills/                    │
│   Trigger: Background review after complex tasks        │
├─────────────────────────────────────────────────────────┤
│ Layer 3: Long-Term Semantic Memory                      │
│   Session Search — FTS5 full-text search + LLM summary  │
│   Location: ~/.hermes/state.db (SQLite WAL)             │
│   Trigger: On-demand via session_search tool            │
├─────────────────────────────────────────────────────────┤
│ Layer 2: Persistent Declarative Memory                  │
│   MEMORY.md + USER.md — structured facts & profiles     │
│   Location: ~/.hermes/memories/                          │
│   Trigger: Background review every N turns              │
├─────────────────────────────────────────────────────────┤
│ Layer 1: External Memory Plugins                        │
│   Honcho, mem0, Supermemory, Hindsight, etc.            │
│   Location: Cloud or self-hosted                        │
│   Trigger: Provider-specific (auto or tool-called)      │
└─────────────────────────────────────────────────────────┘
```

The key design insight: **each layer has a different cost/latency/fidelity tradeoff**, and the agent uses them in combination. Layer 2 is always-on and free; Layer 3 is free and on-demand; Layer 1 is optional and adds depth; Layer 4 captures reusable procedures.

---

## 2. Agent-Curated Memory with Periodic Nudges

### 2.1 The Two-File System

Hermes uses two bounded markdown files at `~/.hermes/memories/`:

| File | Purpose | Char Limit | ~Tokens | Typical Entries |
|---|---|---|---|---|
| **MEMORY.md** | Agent's personal notes — environment facts, project conventions, tool quirks, things learned | 2,200 chars | ~800 | 8–15 entries |
| **USER.md** | User profile — preferences, communication style, expectations, workflow habits | 1,375 chars | ~500 | 5–10 entries |

**Key design decisions:**

- **Bounded, not auto-compacting.** When a write would exceed the limit, the `memory` tool returns an error with current entries and usage. The agent must consolidate or remove entries itself in the same turn. This forces prioritization.
- **Character limits, not token limits.** Char counts are model-independent and deterministic.
- **Entry delimiter: `§` (section sign).** Entries can be multiline.
- **Frozen snapshot pattern.** Both files are loaded into the system prompt once at session start. Mid-session writes update disk immediately but do NOT change the active system prompt — this preserves the LLM's prefix cache for the entire session. The snapshot refreshes on next session start.

### 2.2 The Memory Tool

A single `memory` tool with three actions:

| Action | Behavior |
|---|---|
| `add` | Append a new entry. Rejects exact duplicates. Returns error if over limit. |
| `replace` | Overwrite the **whole matched entry** with new content. `old_text` only locates the entry via substring match — it is not cut out and replaced. New content must be the complete new entry. |
| `remove` | Remove an entry by substring match. |

**No read action** — memory content is automatically injected into the system prompt at session start. The agent sees its memories as part of its conversation context.

**Substring matching:** `replace` and `remove` use short unique substring matching. If the substring matches multiple entries, an error is returned asking for a more specific match.

**Security:** Memory content is scanned at write time with the "strict" (broadest) threat-pattern set. This is write-time filtering, not read-time — a deliberate tradeoff that is cheaper per turn and cache-friendly.

**External drift detection:** The system detects if files were modified outside the tool (round-trip mismatch or single entry exceeding the whole-file limit). On detection, it writes a timestamped `.bak` snapshot and refuses the mutation.

### 2.3 The Nudge Mechanism — Evolution

The nudge mechanism has evolved through three phases:

#### Phase 1: Inline Nudges (Original)

Nudges were appended directly to user messages:

```python
# In run_conversation(), every 10 user turns:
user_message += "\n\n[System: You've had several exchanges. Consider: has the user shared preferences, corrected you, or revealed something about their workflow worth remembering for future sessions?]"
```

**Problem:** In long sessions, 33–75% of user messages contained system instructions that weren't part of what the user actually said. These backward-looking directives competed with the user's forward-looking task at every transition point.

#### Phase 2: API-Call-Time Injection

Nudges were injected at API-call time only, never persisted to history. This fixed the pollution but still competed for the model's attention within the same inference call.

#### Phase 3: Background Review Fork (Current)

The nudge was replaced entirely by a **background review agent** that runs AFTER the main agent finishes responding:

1. After the agent finishes responding, check if nudge trigger conditions are met (every N user turns for memory, after N+ tool iterations for skills).
2. If triggered, snapshot the current conversation history (read-only copy).
3. Spawn a background thread with a focused prompt: "Review this conversation. Save any user preferences or corrections to memory. If a reusable workflow was completed, save or update a skill."
4. The background call uses the **same main model** (not auxiliary — skills/memory are high-value, high-precision tasks) with only `memory` and `skill_manage` tools enabled.
5. Writes directly to the memory/skill stores. No output to the user. No messages added to the main conversation.
6. Main agent never sees a nudge. User's next message is processed with full attention on their request.

**Key properties:**
- **Zero attention conflict.** The main agent focuses entirely on the user's task.
- **Zero latency impact.** Runs in a background thread after the response is delivered.
- **Same token cost.** Processes the same context, just on a separate track.
- **Cleaner history.** No system instructions polluting user messages.
- **Better quality.** A dedicated prompt for "review and save" produces better memory/skill decisions than a hint appended to an unrelated user message.

### 2.4 Trigger Conditions

| Trigger | Default | Unit | What It Counts |
|---|---|---|---|
| Memory review | Every 10 user turns | `run_conversation()` calls | Top-level user prompts |
| Skill review | After 10+ tool iterations | Tool-calling iterations | Agent loop iterations |

**Important nuance:** The memory nudge counts user turns, not tool iterations. This was identified as a potential issue (#94226) — in modern agentic usage, a single user prompt can drive tens of model/tool iterations, so an entire working session of durable corrections can sit inside what the counter sees as 1–2 turns. The skill side already uses tool iterations.

**Suppression behavior:** When the model calls the `memory` tool itself (proactive write), the turn counter resets to 0. Proactive memory writes *suppress* the scheduled review rather than complement it.

### 2.5 Background Review Implementation Details

The background review fork:
- Shares the parent's `session_id` for prompt-cache warmth
- Has `_persist_disabled = True` — never writes to the session DB (prevents the review from injecting messages into the user's real session)
- Has `_memory_nudge_interval = 0` and `_skill_nudge_interval = 0` — no recursive reviews
- Uses a thread-scoped tool whitelist: only `memory` and `skills` tools are allowed
- Runs with `max_iterations=16` (bounded)
- Captures successful tool actions and surfaces a compact summary: `"💾 Self-improvement review: ..."`
- If routed to a different model, replays a digest instead of the full snapshot (cache is cold anyway)

### 2.6 Configuration

```yaml
# In ~/.hermes/config.yaml
memory:
  memory_enabled: true
  user_profile_enabled: true
  memory_char_limit: 2200    # ~800 tokens
  user_char_limit: 1375      # ~500 tokens
  write_approval: false      # false = write freely | true = require approval
  nudge_interval: 10         # turns between consolidation nudges
  flush_min_turns: 6         # minimum turns before a flush is allowed (legacy, now removed)
  provider: builtin          # builtin | honcho | mem0 | supermemory | retaindb | hindsight | byterover | holographic | openviking
```

Setting both `memory_enabled` and `user_profile_enabled` to `false` turns off the built-in stores completely — the memory tool is dropped from the schema and its guidance block is dropped from the system prompt.

---

## 3. FTS5 Session Search with LLM Summarization

### 3.1 Storage Layer

All sessions are stored in a SQLite database at `~/.hermes/state.db` using WAL mode:

```sql
-- Core tables
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  source TEXT,           -- 'cli', 'telegram', 'discord', etc.
  user_id TEXT,
  model TEXT,
  title TEXT UNIQUE,
  system_prompt TEXT,
  started_at REAL,
  ended_at REAL,
  input_tokens INTEGER,
  output_tokens INTEGER,
  parent_session_id TEXT  -- for compression-triggered splits
);

CREATE TABLE messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT,
  role TEXT,             -- 'user', 'assistant', 'tool'
  content TEXT,
  tool_calls TEXT,       -- JSON
  tool_name TEXT,
  token_count INTEGER,
  timestamp REAL
);

-- FTS5 virtual tables (auto-synced via triggers)
CREATE VIRTUAL TABLE messages_fts USING fts5(
  content, tool_calls, tool_name,
  content=messages, content_rowid=id
);

CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(
  content, tool_calls, tool_name,
  content=messages, content_rowid=id,
  tokenize='trigram'
);
```

**Key design decisions:**
- **WAL mode** for concurrent readers + one writer (critical for multi-platform gateway scenarios)
- **Three FTS5 tables:** base (unicode61 tokenizer), trigram (CJK/substring search), and cjk (bigram tokenizer for CJK)
- **Triggers** keep FTS tables in sync on INSERT/UPDATE/DELETE
- **Session lineage** via `parent_session_id` chains (compression-triggered splits)
- **Source tagging** for platform filtering

### 3.2 The session_search Tool

The `session_search` tool provides three calling shapes inferred from arguments:

#### Shape 1: Discovery (pass `query`)

```
session_search(query="auth refactor", limit=3)
```

1. Runs FTS5 full-text search with BM25 ranking
2. Dedupes hits by session lineage
3. Returns top N sessions, each with:
   - `session_id`, `title`, `when`, `source`
   - `snippet`: FTS5-highlighted match excerpt
   - `bookend_start`: first 3 user+assistant messages (the goal/kickoff)
   - `messages`: ±5 messages around the FTS5 match, with anchor flagged
   - `bookend_end`: last 3 user+assistant messages (the resolution/decisions)
   - `match_message_id`, `messages_before`, `messages_after`

Bookends + window together let the agent reconstruct **goal → match → resolution** without paying for the whole transcript.

#### Shape 2: Scroll (pass `session_id` + `around_message_id`)

```
session_search(session_id="...", around_message_id=12345, window=10)
```

Returns a window of ±`window` messages centered on the anchor. No FTS5, no bookends. To scroll forward/backward, re-anchor on the last/first message id of the returned window.

#### Shape 3: Browse (no args)

```
session_search()
```

Returns recent sessions chronologically: titles, previews, timestamps.

### 3.3 LLM Summarization (Historical — Now Removed)

**Important:** The LLM summarization path was **removed** in PR #27590. The old implementation:

1. FTS5 finds matching messages, groups by session
2. Loads each session's full conversation
3. `_truncate_around_matches()` extracts ~100K chars centered on match positions
4. Formats as readable transcript (including tool call names)
5. Sends to auxiliary LLM (Gemini Flash) for focused summary
6. Returns per-session summaries with metadata

**Why it was removed:**
- Cost: ~$0.30/call
- Latency: ~30s for three sessions
- Quality: LLM could confabulate when the right session wasn't in the hit list
- The new approach returns byte-for-byte content from SQLite — 2-5x more bytes, but they're real DB content

**New approach:** Zero LLM cost, ~20ms discovery time, byte-for-byte content fidelity. The agent reads actual messages from the DB, not LLM-laundered prose.

### 3.4 FTS5 Query Syntax

| Syntax | Example | Meaning |
|---|---|---|
| Keywords | `docker deployment` | Both terms (implicit AND) |
| Quoted phrase | `"exact phrase"` | Exact phrase match |
| Boolean OR | `docker OR kubernetes` | Either term |
| Boolean NOT | `python NOT java` | Exclude term |
| Prefix | `deploy*` | Prefix match |

The `_sanitize_fts5_query()` method handles edge cases:
- Strips unmatched quotes and special characters
- Wraps hyphenated terms in quotes (`chat-send` → `"chat-send"`)
- Removes dangling boolean operators (`hello AND` → `hello`)

### 3.5 Search Quality and Routing

The search system uses multiple strategies with automatic fallback:

1. **FTS5 with unicode61 tokenizer** — default for Latin scripts
2. **Trigram tokenizer** — for CJK and substring search
3. **CJK bigram tokenizer** — for CJK phrases
4. **LIKE fallback** — for short CJK queries (< 3 chars) when FTS is unavailable

**Cron demotion:** Cron/automation sessions are stable-sorted below interactive sessions in discover ranking, so they can't drown the user's own conversations out of top-N results. Scan limit raised from 50 to 300.

**Slow search logging:** Searches exceeding 1000ms are logged with the routing path taken.

### 3.6 Session Management CLI

```bash
hermes sessions list           # Browse past sessions
hermes sessions export --format md --session-id <id>
hermes sessions delete <id>
hermes sessions prune
hermes sessions stats
hermes sessions rename
hermes sessions browse
```

---

## 4. Honcho Dialectic User Modeling

### 4.1 What Honcho Is

Honcho is an AI-native memory backend by Plastic Labs that adds **dialectic reasoning** and deep user modeling on top of Hermes's built-in memory. Instead of simple key-value storage, Honcho maintains a running model of who the user is — their preferences, communication style, goals, and patterns — by reasoning about conversations after they happen.

### 4.2 Dialectic Reasoning Process

After each conversation turn (gated by `dialecticCadence`), Honcho analyzes the exchange and derives insights about the user's preferences, habits, and goals. The dialectic supports **multi-pass depth (1–3 passes)** with automatic cold/warm prompt selection:

**Cold start (no base context yet):**
> "Who is this person? What are their preferences, goals, and working style?"

**Warm session (base context exists):**
> "Given what's been discussed in this session so far, what context about this user is most relevant?"

### 4.3 Multi-Pass Depth

When `dialecticDepth > 1`, each dialectic invocation runs multiple `.chat()` passes:

| Pass | Purpose |
|---|---|
| Pass 0 | Cold or warm prompt (initial assessment) |
| Pass 1 | Self-audit — identifies gaps in initial assessment, synthesizes evidence from recent sessions |
| Pass 2 | Reconciliation — checks for contradictions between prior passes, produces final synthesis |

Each pass uses a proportional reasoning level (lighter early passes, base level for the main pass). Passes bail out early if the prior pass returned strong signal (long, structured output), so depth 3 doesn't always mean 3 LLM calls.

### 4.4 Two-Layer Context Injection

Every turn (in hybrid or context mode), Honcho assembles two layers of context injected into the system prompt:

| Layer | Content | Refresh Cadence | Purpose |
|---|---|---|---|
| **Base context** | Session summary, user representation, user peer card, AI self-representation, AI identity card | `contextCadence` (default: 1) | "Who is this user" |
| **Dialectic supplement** | LLM-synthesized reasoning about the user's current state and needs | `dialecticCadence` (default: 2) | "What matters right now" |

Both layers are concatenated and truncated to the `contextTokens` budget.

### 4.5 Three Orthogonal Config Knobs

| Knob | Controls | Default |
|---|---|---|
| `contextCadence` | Turns between `context()` API calls (base layer refresh) | 1 |
| `dialecticCadence` | Turns between `peer.chat()` LLM calls (dialectic layer refresh) | 2 (recommended 1–5) |
| `dialecticDepth` | Number of `.chat()` passes per dialectic invocation | 1 |

These are orthogonal — you can have frequent context refreshes with infrequent dialectic, or deep multi-pass dialectic at low frequency.

Example: `contextCadence: 1, dialecticCadence: 5, dialecticDepth: 2` refreshes base context every turn, runs dialectic every 5 turns, and each dialectic run makes 2 passes.

### 4.6 Session-Start Prewarm

On session init, Honcho fires a dialectic call in the background at the full configured `dialecticDepth` and hands the result directly to turn 1's context assembly. A single-pass prewarm on a cold peer often returns thin output — multi-pass depth runs the audit/reconcile cycle before the user ever speaks. If prewarm hasn't landed by turn 1, turn 1 falls back to a synchronous call with a bounded timeout.

### 4.7 Query-Adaptive Reasoning Level

The auto-injected dialectic scales `dialecticReasoningLevel` by query length:
- +1 level at ≥120 chars
- +2 at ≥400 chars
- Clamped at `reasoningLevelCap` (default: "high")

Available levels: `minimal`, `low`, `medium`, `high`, `max`.

### 4.8 Multi-Agent Profiles (Peer Isolation)

When multiple Hermes instances talk to the same user (e.g., a coding assistant and a personal assistant), Honcho maintains separate "peer" profiles. Each peer sees only its own observations and conclusions, preventing cross-contamination of context.

### 4.9 Observation Modes

Honcho models a conversation as peers exchanging messages. Each peer has two observation toggles:

| Toggle | Effect |
|---|---|
| `observeMe` | Honcho builds a representation of this peer from its own messages |
| `observeOthers` | This peer observes the other peer's messages (feeds cross-peer reasoning) |

Two peers × two toggles = four flags. `observationMode` is a shorthand preset:

| Preset | User flags | AI flags | Semantics |
|---|---|---|---|
| `directional` (default) | me: on, others: on | me: on, others: on | Full mutual observation. Enables cross-peer dialectic. |
| `unified` | me: on, others: off | me: off, others: on | Shared-pool semantics. AI observes user's messages only. |

### 4.10 Session Strategy

Controls how Honcho sessions map to your work:

| Strategy | Behavior |
|---|---|
| `per-session` | Each hermes run gets a fresh session. Clean starts, memory via tools. Recommended for new users. |
| `per-directory` | One Honcho session per working directory. Context accumulates across runs. |
| `per-repo` | One session per git repository. |
| `global` | Single session across all directories. |

### 4.11 Recall Mode

Controls how memory flows into conversations:

| Mode | Behavior |
|---|---|
| `hybrid` | Context auto-injected into system prompt AND tools available (model decides when to query) |
| `context` | Auto-injection only, tools hidden |
| `tools` | Tools only, no auto-injection. Agent must explicitly call `honcho_reasoning`, `honcho_search`, etc. |

### 4.12 Honcho Tools

When Honcho is active as the memory provider, five tools become available:

| Tool | Purpose |
|---|---|
| `honcho_profile` | Read or update peer card — pass `card` (list of facts) to update, omit to read |
| `honcho_search` | Semantic search over context — raw excerpts, no LLM synthesis |
| `honcho_context` | Full session context — summary, representation, card, recent messages |
| `honcho_reasoning` | Synthesized answer from Honcho's LLM — pass `reasoning_level` (minimal/low/medium/high/max) to control depth |
| `honcho_conclude` | Create or delete conclusions — pass `conclusion` to create, `delete_id` to remove (PII only) |

### 4.13 Configuration

```yaml
# In ~/.hermes/config.yaml
memory:
  provider: honcho

# In ~/.hermes/.env
HONCHO_API_KEY=***
```

Setup: `hermes memory setup` → select "honcho" from the provider list.

---

## 5. Procedural Memory: The Skills System

### 5.1 Overview

Skills are Hermes's **procedural memory** — reusable task knowledge that the agent creates, improves, and loads on demand. They are the fourth layer of the memory architecture.

### 5.2 Skill Structure

Each skill is a directory containing a `SKILL.md` file with YAML frontmatter:

```yaml
---
name: my-skill
description: "Use when <trigger>. <one-line behavior>."
version: 1.0.0
author: Hermes Agent
license: MIT
---

# Skill Content

Procedural knowledge, scripts, references...
```

### 5.3 Skill Creation and Improvement

- **Auto-creation:** After complex tasks, the background review agent can create new skills.
- **Self-improvement:** Skills are patched during use — if a skill was wrong or incomplete, the agent updates it.
- **Security scan:** New skills are scanned for security issues before being saved.
- **Write approval:** `skills.write_approval` can gate skill writes.

### 5.4 Skill Loading

Skills are loaded **on demand** — the system prompt includes skill descriptions (triggers), and the full content is loaded only when relevant. This avoids consuming context window tokens for skills that aren't needed.

### 5.5 Learning Journey (`/journey`)

The `/journey` command provides visibility into the agent's learning:

| Command | What it does |
|---|---|
| `hermes journey list` | List all learned skills and memory chunks |
| `hermes journey delete <node> [-y]` | Delete a node. Skills are **archived** (restorable), memory chunks are removed. |
| `hermes journey edit <node>` | Open the node's content in `$EDITOR` |

---

## 6. Comparison with AgentHarness

| Dimension | Hermes Agent | AgentHarness (Current) |
|---|---|---|
| **Persistent Memory** | MEMORY.md + USER.md (bounded, curated) | `ah/memory/` stub — not implemented |
| **Cross-Session Search** | FTS5 over SQLite — full-text search of past sessions | `ContextManager` with pgvector similarity search |
| **External Plugins** | 8+ backends (Honcho, mem0, etc.) — one active at a time | None |
| **User Modeling** | Honcho dialectic reasoning | None |
| **Auto-Persistence** | Nudges → background review | None |
| **Skill Auto-Generation** | Auto-create + security scan | `ah/skills/registry.py` — manual only |
| **Skill Self-Improvement** | Agent-driven patching | None |
| **Cache Optimization** | Frozen snapshot pattern | None |
| **Session Storage** | SQLite WAL + FTS5 | `ah/db/schema.sql` — 3 tables, no FTS |

---

## 7. Key Takeaways for AgentHarness

### 7.1 Memory System

1. **Bounded files with forced consolidation** — the char limit + error-on-overflow pattern forces the agent to prioritize. This is better than auto-compaction because the agent makes consolidation decisions with full context.
2. **Frozen snapshot pattern** — loading memory once at session start and never changing it mid-session preserves prompt caching. This is a critical performance optimization.
3. **Background review fork** — the evolution from inline nudges → API-call-time injection → background fork is a masterclass in avoiding attention conflict. The key insight: never mix backward-looking system work with forward-looking user tasks in the same inference call.
4. **Write-time security scanning** — filtering memory content at write time (not read time) is cheaper and cache-friendly.
5. **External drift detection** — detecting when files were modified outside the tool prevents silent corruption.

### 7.2 Session Search

1. **FTS5 with multiple tokenizers** — base (unicode61), trigram (CJK/substring), and cjk (bigram) tables with automatic fallback.
2. **Bookend + window pattern** — returning first/last messages + ±5 around the match lets the agent reconstruct goal → match → resolution without loading full transcripts.
3. **Zero-LLM-cost approach** — the removal of LLM summarization in favor of byte-for-byte DB content is a significant improvement. The old approach cost ~$0.30/call and could confabulate.
4. **Cron demotion** — stable-sorting interactive sessions above cron sessions prevents automation from drowning user conversations.
5. **Source filtering** — the ability to filter by source platform (cli, telegram, discord) and role (user, assistant, tool) is essential for multi-platform agents.

### 7.3 Honcho Dialectic

1. **Multi-pass dialectic** — the cold/warm prompt selection + self-audit + reconciliation passes produce deeper user models than single-pass analysis.
2. **Orthogonal config knobs** — `contextCadence`, `dialecticCadence`, and `dialecticDepth` are independent, allowing fine-grained cost/quality tradeoffs.
3. **Peer isolation** — separate profiles per agent prevent cross-contamination in multi-agent setups.
4. **Query-adaptive reasoning** — scaling reasoning level by query length is a clever heuristic for balancing cost and depth.
5. **Session-start prewarm** — firing the dialectic in the background at session init means the user gets a more informed first response.

### 7.4 Procedural Memory

1. **Skills as procedural memory** — the separation of declarative (MEMORY.md/USER.md) from procedural (skills) memory is clean and well-motivated.
2. **On-demand loading** — skill descriptions in the system prompt, full content loaded only when relevant, is the right approach for context efficiency.
3. **Self-improvement loop** — the background review agent can create AND improve skills, closing the learning loop.
4. **Learning journey** — providing visibility into what the agent has learned (`/journey`) is essential for trust and debugging.

---

## 8. Implementation Priorities for AgentHarness

Based on this research, the highest-impact improvements for AgentHarness would be:

1. **Implement the bounded memory file system** (MEMORY.md + USER.md equivalent) with the frozen snapshot pattern and forced consolidation.
2. **Add FTS5 session search** to the existing SQLite schema, with the bookend + window pattern for efficient context reconstruction.
3. **Implement the background review fork** for memory and skill curation, replacing any inline nudge approach.
4. **Add write-time security scanning** for memory content.
5. **Implement external drift detection** for memory files.
6. **Add skill self-improvement** to the existing `ah/skills/registry.py`.
7. **Consider a Honcho-like dialectic user modeling** plugin for deep user understanding.

---

## 9. References

- Hermes Agent official docs: https://hermes-agent.nousresearch.com/docs/
- Hermes Agent GitHub: https://github.com/NousResearch/hermes-agent
- Memory system deep dive: https://agentwikis.com/wiki/hermes/wiki/concepts/memory-system.md
- Session storage internals: https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/session-storage.md
- Honcho memory docs: https://hermes-agent.nousresearch.com/docs/user-guide/features/honcho
- Memory tool source: https://github.com/NousResearch/hermes-agent/blob/main/tools/memory_tool.py
- Session search tool source: https://github.com/NousResearch/hermes-agent/blob/main/tools/session_search_tool.py
- Background review source: https://github.com/NousResearch/hermes-agent/blob/main/agent/background_review.py
- FTS5 search source: https://github.com/NousResearch/hermes-agent/blob/main/hermes_state_search.py
- PR #27590 (session_search rewrite): https://github.com/NousResearch/hermes-agent/pull/27590
- PR #2235 (background review): https://github.com/NousResearch/hermes-agent/commit/45058b4
- PR #15696 (flush removal): https://github.com/NousResearch/hermes-agent/pull/15696
- Issue #94226 (memory cadence): https://github.com/NousResearch/hermes-agent/issues/94226
- Issue #2227 (nudge pollution): https://github.com/NousResearch/hermes-agent/issues/2227
