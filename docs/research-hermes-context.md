# Hermes Agent: Context Compression, Session Search & User Modeling

> Research notes for AgentHarness improvement. Source: official Hermes Agent docs, source code, and community analysis.

---

## 1. Context Compression (`/compress` / `/compact`)

### Architecture: Dual Compression System

Hermes uses **two independent compression layers** that operate at different thresholds:

```
                     ┌──────────────────────────┐
  Incoming message   │   Gateway Session Hygiene │  Fires at 85% of context
  ─────────────────► │   (pre-agent, rough est.) │  Safety net for large sessions
                     └─────────────┬────────────┘
                                   │
                                   ▼
                     ┌──────────────────────────┐
                     │   Agent ContextCompressor │  Fires at 50% of context (default)
                     │   (in-loop, real tokens)  │  Normal context management
                     └──────────────────────────┘
```

#### Layer 1: Gateway Session Hygiene (85% threshold)
- **Location**: `gateway/run_turn.py`
- **Purpose**: Safety net for sessions that grow between turns (e.g., overnight Telegram/Discord accumulation)
- **Token source**: Prefers API-reported tokens from last turn → usage anchor on session row → rough character-based estimate
- **Fires**: Only when `len(history) >= 4` and compression is enabled
- **Why 85%?**: Intentionally higher than the agent compressor. Setting it at 50% caused premature compression on every turn in long gateway sessions.

#### Layer 2: Agent ContextCompressor (50% threshold, configurable)
- **Location**: `agent/context_compressor.py`
- **Purpose**: Primary compression system, runs inside the agent's tool loop with accurate API-reported token counts
- **Token accounting**: Uses "provider anchors" — the provider's last prompt/completion token counts plus a rough estimate of only the messages appended since that response. The anchor is persisted on the session row so a fresh process (`--resume`) restores it.
- **Fallback**: Without an anchor (first request, rewind/edit-resend), a whole-context rough estimate over threshold **waits one request** for provider evidence before compressing.

### The 4-Phase Compression Algorithm

The `ContextCompressor.compress()` method follows this sequence:

#### Phase 1: Prune Old Tool Results (cheap, no LLM call)
- Old tool results (>200 chars) outside the protected tail are replaced with `[Old tool output cleared to save context space]`
- A tool round the model has not answered yet keeps its text results verbatim and image results intact in the tail
- A tool round exceeding 20% of the input budget can be summarized, with older images retired

#### Phase 2: Determine Boundaries
```
┌─────────────────────────────────────────────────────────────┐
│  Message list                                               │
│                                                             │
│  [0..2]  ← protect_first_n (system + first exchange)        │
│  [3..N]  ← middle turns → SUMMARIZED                        │
│  [N..end] ← tail (by token budget OR protect_last_n)        │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```
- Tail protection is **token-budget based**: walks backward from the end, accumulating tokens until budget is exhausted
- Budget capped at 20% of context window on every model
- `protect_last_n` (default 20) is a minimum only up to a small count floor (8 rows)
- `min_tail_user_messages` (default 1) guarantees at least 1 real user message survives
- Boundaries aligned to avoid splitting tool_call/tool_result groups

#### Phase 3: Generate Structured Summary
- The middle turns are sent to an **auxiliary LLM** (can be a smaller/cheaper model) with a structured template:

```text
## Goal
[What the user is trying to accomplish]

## Constraints & Preferences
[User preferences, coding style, constraints, important decisions]

## Progress
### Done
[Completed work — specific file paths, commands run, results]
### In Progress
[Work currently underway]
### Blocked
[Any blockers or issues encountered]

## Key Decisions
[Important technical decisions and why]

## Relevant Files
[Files read, modified, or created — with brief note on each]

## Next Steps
[What needs to happen next]

## Critical Context
[Specific values, error messages, configuration details]
```

- Summary budget: `content_tokens × 0.20` (min 2,000, max `min(context_length × 0.05, 12,000)`)
- The summary model's context window must be ≥ the main model's, or the summary silently fails

#### Phase 4: Assemble Compressed Messages
1. Head messages (with a note appended to system prompt on first compression)
2. Summary message (role chosen to avoid consecutive same-role violations)
3. Tail messages (unmodified)
- Orphaned tool_call/tool_result pairs cleaned up by `_sanitize_tool_pairs()`

### Iterative Re-compression
On subsequent compressions, the previous summary is passed to the LLM with instructions to **update** it rather than summarize from scratch. This preserves information across multiple compactions — items move from "In Progress" to "Done", new progress is added, obsolete information is removed.

### Tail Modes

| Mode | Behavior |
|------|----------|
| `lean` (default) | Clamped tail of 2.5% × context window (10K floor, 25K cap). Carries continuity via: detailed identifier-preserving session log, mechanically extracted anchor index (PR numbers, SHAs, paths, error strings — regex, never paraphrased), every real user message quoted verbatim (newest-first budget), and a `session_search` recovery pointer. Old tool results demoted to one-line stubs with recovery pointer. |
| `legacy` | Keeps a `target_ratio`-sized verbatim tail (~100K+ tokens on big-window models) |

### In-Place Compaction (default: `in_place: true`)
- Compaction **rewrites the live message list on the same session id**
- Pre-compaction turns are soft-archived under the same id (`active=0, compacted=1`)
- Still searchable via `session_search` and recoverable, never deleted
- No `parent_session_id` chain, no `name #N` renumbering
- One conversation keeps one durable id for its whole life

### Failure Handling
- **Failure cooldown**: Escalating 60s → 300s → 900s, persisted in `state.db`. While armed, ordinary threshold-triggered compaction is deferred.
- **Manual `/compress`** (`force=True`): Clears the cooldown and retries.
- **Repeated stall → deterministic fallback**: After 2 consecutive stalls, commits a static fallback summary through the ordinary pipeline.
- **Provider overload → abort**: Preserves transcript unchanged. After 3 consecutive overload aborts, commits deterministic fallback.
- **Provider-proven overflow**: When provider rejects with context-length error, ignores cooldown for one bounded attempt.

### Configuration

```yaml
compression:
  enabled: true
  threshold: 0.50            # Fraction of context window (default: 0.50)
  threshold_tokens: null     # Optional absolute cap on trigger
  model_thresholds: {}       # Per-model overrides (substring match, longest key wins)
  target_ratio: 0.20         # Tail protection token budget (legacy mode)
  tail_mode: lean            # lean | legacy
  protect_last_n: 20         # Minimum protected tail messages
  min_tail_user_messages: 1  # Real user messages guaranteed in tail
  protect_first_n: 3         # Hardcoded: system prompt + first exchange
  idle_compact_after_seconds: 0  # Opt-in idle compaction
  in_place: true             # Compact on same session id

auxiliary:
  compression:
    model: null              # Override model for summaries
    provider: auto           # Provider for summaries
```

### Per-Model Threshold Overrides
```yaml
compression:
  model_thresholds:
    "glm-5.2": 0.40
    "claude-sonnet": 0.35
    "openai-codex:astra": 0.85   # only on Codex OAuth route
```
- Keys are substring-matched against model name; longest match wins
- Provider-scoped keys (`<provider>:<substring>`) only match on that route
- Small-context floor (0.75 for <512K windows) applies on top (raise-only)

### Prompt Caching Integration
- Anthropic Claude models get automatic prompt caching via `cache_control`
- TTL: `5m` (default) or `1h` for human-paced sources; `auto` resolves per session
- Cache-aware design: system prompt + first 3 messages are cached; rolling 3-message window re-establishes caching within 1-2 turns after compaction
- Model identity is part of the cache key — mid-conversation model changes get zero cache hits

### Context Pressure Warnings
- **Removed** — intermediate pressure warnings caused models to "give up" prematurely on complex tasks
- Compression fires at threshold with no prior warning step

---

## 2. Token Usage Tracking (`/usage`)

### How It Works

1. **Per-API-call accumulation** (`agent/conversation_loop.py`): After every API call, the response's usage object is normalized via `normalize_usage()` from `agent/usage_pricing.py` into a `CanonicalUsage` dataclass:
   - `input_tokens` — raw input (excludes cache)
   - `output_tokens` — generated tokens
   - `cache_read_tokens` — cache hits
   - `cache_write_tokens` — cache writes
   - `reasoning_tokens` — thinking/reasoning tokens

2. **Session counters** on the AIAgent instance:
   ```python
   agent.session_input_tokens += canonical_usage.input_tokens
   agent.session_output_tokens += canonical_usage.output_tokens
   agent.session_cache_read_tokens += canonical_usage.cache_read_tokens
   agent.session_cache_write_tokens += canonical_usage.cache_write_tokens
   agent.session_reasoning_tokens += canonical_usage.reasoning_tokens
   agent.session_total_tokens += total_tokens
   agent.session_api_calls += 1
   agent.session_estimated_cost_usd += cost
   ```

3. **Persisted to SQLite** via `SessionDB.update_token_counts()` (`hermes_state.py`)

### The `/usage` Slash Command
- Reads the live agent's session counters (mid-turn) or cached agent (between turns)
- Calls `fetch_account_usage()` from `agent/account_usage.py` for provider-level rate limits
- Calls `estimate_usage_cost()` from `agent/usage_pricing.py` for cost estimates
- **Shows**: model, input tokens, cache read/write, output tokens, total, API calls, cost, context window %
- Gateway handler: `gateway/run.py` line 13194 (`_handle_usage_command`)
- CLI: `cli.py` → `process_command`

### Programmatic Access
```python
agent = AIAgent(...)
agent.chat("hello")
print(f"In: {agent.session_input_tokens}, Out: {agent.session_output_tokens}")
print(f"Total: {agent.session_total_tokens}, API calls: {agent.session_api_calls}")
print(f"Cost: ${agent.session_estimated_cost_usd:.4f}")
```

### One-Shot Usage Report
```bash
hermes -z "..." --usage-file /path/report.json
```
Writes a machine-readable JSON usage report after the run: `estimated_cost_usd`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`, `total_tokens`, `api_calls`, `model`, `provider`, `session_id`, `service_tier`, `completed`/`failed` flags.

---

## 3. Usage Analytics (`/insights`)

### The `InsightsEngine` (`agent/insights.py`)

Inspired by Claude Code's `/insights` command, adapted for Hermes Agent's multi-platform architecture.

```python
from agent.insights import InsightsEngine
engine = InsightsEngine(db)
report = engine.generate(days=30)
print(engine.format_terminal(report))
```

### What It Analyzes
- **Token consumption** across sessions
- **Cost estimates** (USD) using `estimate_usage_cost()` from `agent/usage_pricing.py`
- **Tool usage patterns** — which tools are used most
- **Activity trends** — usage over time
- **Model/platform breakdowns** — which models and platforms are used
- **Session metrics** — duration, message counts, etc.

### CLI Command
```bash
hermes insights [--days N] [--source platform]
```
- `--days N`: Analyze last N days (default 30)
- `--source platform`: Filter by platform (cli, telegram, discord, etc.)
- Works offline — derived from local session history in SQLite

### In-Session Slash Command
```
/insights [days]
```
Shows usage analytics for the specified period (default 30 days).

### Data Source
All insights come from the SQLite `state.db` — session metadata, token counts, and message history. No external API calls needed.

---

## 4. User Modeling (USER.md)

### Two-File Memory System

| File | Purpose | Char Limit | Who Writes |
|------|---------|------------|------------|
| **MEMORY.md** | Agent's personal notes — environment facts, conventions, things learned | 2,200 chars (~800 tokens) | Agent via `memory` tool |
| **USER.md** | User profile — preferences, communication style, expectations | 1,375 chars (~500 tokens) | Agent via `memory` tool |

Both stored in `~/.hermes/memories/` and injected into the system prompt as a **frozen snapshot** at session start.

### How USER.md Is Built

The agent **automatically** builds the user profile — you don't need to ask. It saves when it learns:

- **User preferences**: "I prefer TypeScript over JavaScript" → save to `user`
- **Communication style**: "Don't use sudo for Docker commands, user is in docker group" → save to `memory`
- **Corrections**: "Project uses tabs, 120-char line width" → save to `memory`
- **Name, role, timezone** → save to `user`
- **Pet peeves and things to avoid** → save to `user`
- **Workflow habits** → save to `user`
- **Technical skill level** → save to `user`

### Memory Tool Actions

- **add** — Add a new memory entry
- **replace** — Replace an existing entry (uses substring matching via `old_text`)
- **remove** — Remove an entry (uses substring matching via `old_text`)
- No `read` action — memory content is automatically injected into the system prompt

### Substring Matching
```python
memory(action="replace", target="memory",
       old_text="dark mode",
       content="User prefers light mode in VS Code, dark mode in terminal")
```
- `old_text` only locates the entry; `replace` overwrites the **whole matched entry**
- If substring matches multiple entries, returns error asking for more specific match

### Frozen Snapshot Pattern
- System prompt injection is captured once at session start and never changes mid-session
- When agent adds/removes memory entries, changes persist to disk immediately but won't appear in system prompt until next session
- **Why**: Preserves LLM's prefix cache for performance
- Tool responses always show live state

### Capacity Management
- When a write would exceed the limit, the tool returns an error with current entries and usage
- Agent consolidates/removes entries in the same turn before retrying
- Best practice: When memory is above 80% capacity, consolidate before adding

### Duplicate Prevention
- Automatically rejects exact duplicate entries

### Security Scanning
- Memory entries scanned for injection and exfiltration patterns before being accepted
- Content matching threat patterns (prompt injection, credential exfiltration, SSH backdoors) or containing invisible Unicode characters is blocked

### Write Approval Gate
```yaml
memory:
  write_approval: false     # false = write freely (default) | true = require approval
```
- When `true`, writes are staged for review with `/memory pending` and `/memory approve all`
- Applies to both foreground turns and background self-improvement review

### What to Save vs Skip

**Save**: User preferences, environment facts, corrections, conventions, completed work, explicit requests
**Skip**: Trivial/obvious info, easily re-discovered facts, raw data dumps, session-specific ephemera, information already in context files

---

## 5. Cross-Session Recall (`session_search`)

### Architecture

All sessions stored in SQLite (`~/.hermes/state.db`) with **FTS5 full-text search**:

```
~/.hermes/state.db (SQLite, WAL mode)
├── sessions              — Session metadata, token counts, billing
├── messages              — Full message history per session
├── messages_fts          — FTS5 virtual table (content + tool_name + tool_calls)
├── messages_fts_trigram  — FTS5 virtual table with trigram tokenizer (CJK / substring search)
├── state_meta            — Key/value metadata table
└── schema_version        — Single-row table tracking migration state
```

### The `session_search` Tool

**No LLM calls** — returns actual messages from the DB, byte-for-byte. No summarization, no truncation.

#### Three Calling Shapes

**1. Discovery** — pass `query`:
```python
session_search(query="auth refactor", limit=3)
```
- Runs FTS5, dedupes hits by session lineage, returns top N sessions
- Adaptive detail: highest-ranked result gets full context window + bookends; lower-ranked stay compact
- Each result carries: `session_id`, `title`, `when`, `source`, `snippet`, `detail`, `bookend_start`/`bookend_end` (first/last 3 user+assistant messages), `messages` (±5 around match), `match_message_id`, `messages_before`, `messages_after`
- **Wall time**: ~20ms (vs ~90s for old LLM-summarization approach)

**2. Scroll** — pass `session_id` + `around_message_id`:
```python
session_search(session_id="20260510_174648_805cc2", around_message_id=590803, window=10)
```
- Returns ±`window` messages centered on anchor
- No FTS5, no bookends — just the slice
- Scroll forward: pass `messages[-1].id` back as `around_message_id`
- Scroll backward: pass `messages[0].id` back as `around_message_id`
- **Wall time**: 1–2ms per scroll call

**3. Read** — pass `session_id` without anchor:
```python
session_search(session_id="20260510_174648_805cc2")
```
- Returns whole session, or bounded head/tail view for large sessions
- Also resolves `@session:/` links

**4. Browse** — no args:
- Returns recent sessions chronologically (titles, previews, timestamps)
- Useful when user asks "what was I working on" without naming a topic

### FTS5 Query Syntax
- Simple keywords: `docker deployment` (implicit AND)
- Phrases: `"exact phrase"`
- Boolean: `docker OR kubernetes NOT java`
- Prefix: `deploy*`

### Optional Parameters
- `sort` — `newest` or `oldest`, on top of FTS5 ranking
- `detail` — `adaptive` (default) or `full`
- `role_filter` — comma-separated roles (default: `user,assistant`)

### When It's Used
The agent is prompted to use session search automatically:
> "When the user references something from a past conversation or you suspect relevant prior context exists, use session_search to recall it before asking them to repeat themselves."

Typical triggers: "we did this before", "remember when", "last time", "as I mentioned"

### Session Search vs Memory

| Feature | Persistent Memory | Session Search |
|---------|------------------|----------------|
| **Capacity** | ~1,300 tokens total | Unlimited (all sessions) |
| **Speed** | Instant (in system prompt) | ~20ms FTS5 query, ~1ms scroll |
| **Cost** | Token cost in every prompt | Free — no LLM calls |
| **Use case** | Key facts always available | Finding specific past conversations |
| **Management** | Manually curated by agent | Automatic — all sessions stored |
| **Token cost** | Fixed per session (~1,300 tokens) | On-demand (searched when needed) |

### In-Place Compaction + Session Search
- Archived (compacted) content on the current session is discoverable via `session_search`
- Non-compacted (active) content on the current session is filtered out of results
- Compaction handoff summaries are excluded from bookends

### Auto-Prune
- Ended sessions inactive for `sessions.retention_days` (default 90) are removed at startup
- Active sessions are never touched
- `hermes sessions prune` for one-off cleanup
- `hermes sessions optimize` merges FTS5 index segments and VACUUMs

---

## 6. External Memory Providers

For deeper, persistent memory beyond MEMORY.md and USER.md, Hermes ships with 7 external provider plugins:

| Provider | Best For |
|----------|----------|
| **Honcho** | AI-native cross-session user modeling with dialectic reasoning, semantic search, persistent conclusions |
| **OpenViking** | Knowledge graphs, semantic search |
| **Mem0** | Automatic fact extraction, user modeling |
| **Holographic** | Holographic memory |
| **RetainDB** | RetainDB memory |
| **ByteRover** | ByteRover memory |
| **Supermemory** | Supermemory |

External providers run **alongside** built-in memory (never replacing it) and add capabilities like knowledge graphs, semantic search, automatic fact extraction, and cross-session user modeling.

### Honcho (Most Relevant for User Modeling)
- **Two-layer context injection**: base layer (session summary + representation + peer card, refreshed on `contextCadence`) + dialectic supplement (LLM reasoning, refreshed on `dialecticCadence`)
- **Three orthogonal config knobs**: `contextCadence`, `dialecticCadence`, `dialecticDepth`
- **Tools**: `honcho_profile`, `honcho_search`, `honcho_context`, `honcho_reasoning`, `honcho_conclude`
- **Multi-agent support**: Each profile gets a dedicated AI peer while sharing the same user workspace

---

## 7. Key Takeaways for AgentHarness

### Context Compression
- **Dual-layer approach**: Gateway hygiene (85%) as safety net + agent compressor (50%) as primary
- **4-phase algorithm**: Prune tool results → determine boundaries → structured summary → assemble
- **Iterative re-compression**: Previous summary is updated, not regenerated
- **In-place compaction**: Same session id, soft-archived content still searchable
- **Provider anchors**: Persisted token counts survive restarts
- **Failure cooldown**: Escalating backoff prevents thrashing
- **Pluggable engine**: `context.engine` config allows alternative implementations

### Session Search
- **FTS5 full-text search** across all sessions — no LLM calls, no cost
- **Three shapes**: Discovery (query), Scroll (anchored window), Read (full session)
- **Bookend pattern**: First/last 3 messages reconstruct goal → match → resolution
- **~20ms discovery, ~1ms scroll** — fast enough for real-time use
- **Trigram tokenizer** for CJK/substring search

### User Modeling
- **Two-file system**: MEMORY.md (agent notes) + USER.md (user profile)
- **Strict char limits**: 2,200 + 1,375 chars keep system prompts bounded
- **Frozen snapshot**: Injected at session start, never changes mid-session (preserves prefix cache)
- **Automatic**: Agent saves proactively when it learns preferences/corrections
- **Security scanning**: Injection/exfiltration patterns blocked
- **Write approval gate**: Optional review before persistence

### Usage Tracking
- **CanonicalUsage dataclass**: Normalized across providers
- **Per-session counters**: Input, output, cache read/write, reasoning tokens, cost
- **Persisted to SQLite**: Survives restarts
- **Provider-level rate limits**: Via `fetch_account_usage()`

### Analytics
- **InsightsEngine**: Token consumption, cost estimates, tool usage, activity trends, model/platform breakdowns
- **Offline**: All from local SQLite, no external API calls
- **CLI + slash command**: `hermes insights` and `/insights [days]`

### Cross-Session Recall
- **session_search tool**: FTS5-based, no LLM, returns actual messages
- **Automatic triggering**: Agent prompted to search when user references past conversations
- **Recovery pointer in summaries**: Compressed content includes `session_search` pointer for re-access
- **External providers**: Honcho, Mem0, etc. for semantic search and user modeling beyond keyword search
