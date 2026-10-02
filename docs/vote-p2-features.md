# P2 Vote: Third-Priority Fixes for AgentHarness

**Date:** 2026-10-01  
**Author:** Synthesis of 10 brutal critique audits  
**Scope:** Which fixes should be P2 (do third), after P0 (critical) and P1 (high)

---

## Priority Framework

| Tier | Definition | Examples from Audits |
|------|-----------|---------------------|
| **P0** | Ship-blocking, data loss, security holes | Command injection, path traversal, SSRF, blocking I/O, N+1 queries, no Ctrl+C |
| **P1** | Production infrastructure, test infrastructure, code health | Docker, CI/CD, conftest.py, coverage, dead code removal, async tools, caching |
| **P2** | Feature completeness, polish, documentation, scalability beyond single user | Session search, context compression, skill hub, docs overhaul, UX refinements |
| **P3** | Nice-to-have, competitive parity | MCP, cron, delegation, gateway, desktop app, TUI |

---

## The Case for P2

P2 fixes are the **feature completeness and polish layer**. They do not prevent the system from working (P0) or from being deployed and maintained (P1). They make the system **competitive, usable by non-developers, and ready for multi-user scenarios**.

The argument for P2 is simple: **after P0 and P1 are done, AgentHarness will be a secure, deployable, well-tested single-user CLI. P2 transforms it into a product.**

---

## P2 Feature Fixes (Feature Completeness)

### 1. Session Search & Management

**Current state:** List 10 most recent sessions by status. No search, no export, no rename, no fork, no delete.

**Why P2:** Session management is a **core workflow feature**. Without it, users cannot find past conversations, branch from a previous state, or clean up old sessions. This is not a "nice to have" — it is a **baseline expectation** for any agent CLI.

**What to build:**
- `/sessions --search <query>` — full-text search over session titles and content
- `/export <filename>` — export conversation as markdown
- `/fork <session_id>` — branch a conversation from any point
- `/delete <session_id>` — permanently remove a session
- Session title auto-generation from first message
- Session metadata display (created-at, token usage, message count)

**Effort:** Medium. The `SessionManager` already has CRUD operations. Adding search requires a FTS index on `context_chunks.content`. Export requires reading the context chunks and formatting them.

**Impact:** High. This is the difference between a prototype and a usable tool.

---

### 2. Context Compression

**Current state:** Token-budget-aware prompt assembly. When budget is exceeded, chunks are truncated to 200 chars. No summarization, no rolling compaction, no `/compress` command.

**Why P2:** Context compression is **essential for long sessions**. Without it, every session eventually hits the LLM's context window limit and crashes. The current truncation strategy is actively harmful — it cuts mid-token, mid-sentence, mid-JSON.

**What to build:**
- `/compress` command — summarize older context into a compact form
- Rolling compaction — when `token_count` exceeds `context_budget`, summarize the middle and keep the first and last few chunks
- Configurable compression threshold and target ratio
- Preserve tool call/result pairs during compression (never split them)

**Effort:** Medium-High. Requires an LLM call to summarize. The `PromptAssembler` already has the token budget logic; it needs a summarization step.

**Impact:** High. This is the difference between a 10-turn session and a 100-turn session.

---

### 3. Skill Hub & Curator

**Current state:** SKILL.md parser with keyword matching. No skill creation, no skill hub, no curator, no usage telemetry, no `/learn` command.

**Why P2:** The skill system is the **primary extensibility mechanism**. Without a hub, users cannot share skills. Without a curator, stale skills accumulate. Without `/learn`, the agent cannot improve from experience.

**What to build:**
- `/learn <source>` — extract a reusable skill from a directory, URL, or the current chat
- Skill usage telemetry — track `use_count`, `view_count`, `last_activity_at`
- Curator — background maintenance: mark idle skills stale, archive stale ones
- Skill provenance — track `created_by: "agent"` vs bundled
- Better trigger matching — embedding-based or LLM-based instead of substring

**Effort:** High. This is a new subsystem. But the `SkillRegistry` and `SkillParser` already exist; they need to be extended, not rebuilt.

**Impact:** Medium-High. This is the difference between a static tool and a learning tool.

---

### 4. Memory Approval Gate & User Modeling

**Current state:** Memories are written immediately after each agent run. No approval gate, no user profile, no secret redaction, no pluggable backends.

**Why P2:** Memory is the **persistent identity** of the agent. Without an approval gate, the agent can store incorrect or sensitive information. Without a user profile, the agent cannot personalize responses. Without secret redaction, PII can leak into the database.

**What to build:**
- `/memory pending|approve|reject` — review pending memory writes before they persist
- User profile — persist user preferences, environment details, lessons learned
- Secret redaction — redact PII and secrets before storing in memory
- Memory CLI — `ah memory list|search|forget` (currently in roadmap but not implemented)
- Background forgetting — run `ForgettingModel` as a background job

**Effort:** Medium. The `MemoryStore` and `MemoryConsolidator` already exist. Adding an approval gate is a new write path. User profile is a new table.

**Impact:** Medium-High. This is the difference between a database and a trusted assistant.

---

### 5. Slash Command Expansion

**Current state:** 12 slash commands covering basic session and config management. Hermes has 60+.

**Why P2:** Slash commands are the **primary user interface**. Without `/compress`, `/retry`, `/undo`, `/title`, `/prompt`, `/rollback`, `/diff`, `/snapshot`, `/bg`, `/btw`, `/queue`, `/steer`, `/agents`, `/goal`, `/subgoal`, `/branch`, `/resume`, `/handoff`, `/personality`, `/reasoning`, `/fast`, `/voice`, `/yolo`, `/busy`, `/indicator`, `/footer`, `/statusbar`, `/battery`, `/timestamps`, `/tools`, `/toolsets`, `/skills`, `/bundles`, `/learn`, `/memory`, `/pet`, `/hatch`, `/cron`, `/suggestions`, `/blueprint`, `/curator`, `/kanban`, `/moa`, `/reload`, `/reload-mcp`, `/reload-skills`, `/browser`, `/plugins`, `/approve`, `/deny`, `/restart`, `/sethome`, `/topic`, `/platform`, `/commands`, `/usage`, `/insights`, `/whoami`, `/profile`, `/platforms`, `/journey`, `/subscription`, `/topup`, `/copy`, `/paste`, `/image`, `/update`, `/version`, `/debug` — the CLI feels bare.

**What to build (P2 subset):**
- `/retry` — re-run the last agent turn
- `/undo` — undo the last agent turn
- `/title <name>` — rename the current session
- `/prompt <text>` — inject a system prompt
- `/rollback` — filesystem checkpoint
- `/diff` — show changes since last checkpoint
- `/snapshot` — create a filesystem checkpoint
- `/bg` — run a task in the background
- `/btw` — add a side note without interrupting the current task
- `/queue` — queue a prompt for later
- `/steer` — inject guidance mid-turn
- `/agents` — list active agents
- `/goal` — set a session goal
- `/subgoal` — set a sub-goal
- `/branch` — branch the conversation
- `/resume` — resume a previous session
- `/handoff` — hand off to another agent
- `/personality` — set agent personality
- `/reasoning` — control reasoning effort
- `/fast` — toggle fast mode
- `/voice` — toggle voice output
- `/yolo` — disable approval prompts
- `/busy` — toggle busy mode
- `/indicator` — toggle status indicator
- `/footer` — toggle footer
- `/statusbar` — toggle status bar
- `/battery` — toggle battery display
- `/timestamps` — toggle timestamps
- `/tools` — list available tools
- `/toolsets` — manage tool sets
- `/skills` — search/install/manage skills
- `/bundles` — load skill bundles
- `/learn` — learn a skill from the current chat
- `/memory` — manage memory
- `/pet` — toggle pet mascot
- `/hatch` — hatch a new agent
- `/cron` — manage scheduled tasks
- `/suggestions` — show suggestions
- `/blueprint` — show session blueprint
- `/curator` — manage skill curator
- `/kanban` — manage kanban board
- `/moa` — manage multi-agent orchestration
- `/reload` — reload configuration
- `/reload-mcp` — reload MCP servers
- `/reload-skills` — reload skills
- `/browser` — open browser
- `/plugins` — manage plugins
- `/approve` — approve a pending action
- `/deny` — deny a pending action
- `/restart` — restart the REPL
- `/sethome` — set home directory
- `/topic` — set topic
- `/platform` — switch platform
- `/commands` — list all commands
- `/usage` — show usage statistics
- `/insights` — show insights
- `/whoami` — show current user
- `/profile` — manage profiles
- `/platforms` — list platforms
- `/journey` — show learning timeline
- `/subscription` — manage subscription
- `/topup` — add credits
- `/copy` — copy to clipboard
- `/paste` — paste from clipboard
- `/image` — insert image
- `/update` — update AgentHarness
- `/version` — show version
- `/debug` — toggle debug mode

**Effort:** Low-Medium. Each command is a small addition to the `InteractiveREPL._handle_slash_command()` method. The infrastructure already exists.

**Impact:** Medium. This is the difference between a bare CLI and a rich CLI.

---

## P2 Production Readiness Fixes

### 6. Documentation Overhaul

**Current state:** Documentation is **worse than no documentation**. Test count wrong in 12+ docs (says 136, actual 330). Module structure wrong in 10+ docs. Memory/RAG described as "stubs" in 8+ docs. CI/CD described as missing in 5+ docs (but `.github/workflows/ci.yml` exists).

**Why P2:** Documentation is the **first impression** for new users and contributors. Stale docs actively mislead. A new developer reading these docs would have a fundamentally wrong understanding of the codebase.

**What to build:**
- Update `README.md` — fix test count, table count, skill count, CLI commands, project structure
- Update `docs/roadmap.md` — mark Phase 3 (Memory & RAG) as done, mark Phase 4 (REPL) as done
- Update `docs/synthesis.md` — fix test count, module structure, CI/CD status
- Update `docs/arch-final.md` — fix module structure, table count, success criteria
- Update `docs/update-log.md` — remove false claims, document the memory/RAG implementation
- Update all critique docs — fix test counts, table counts, module structures
- Create `docs/api-reference.md` — no API documentation exists for any module
- Create `docs/architecture-current.md` — a single source of truth for the current architecture
- Create a real changelog

**Effort:** Low. This is a documentation task, not a code task.

**Impact:** High. This is the difference between a project that attracts contributors and one that repels them.

---

### 7. Observability & Metrics

**Current state:** Zero metrics. No latency histograms, no throughput counters, no error rate tracking, no token usage tracking per session. Audit logging is synchronous and blocks the event loop.

**Why P2:** Observability is **essential for production operations**. Without metrics, you cannot answer "How many LLM calls failed in the last hour?" Without distributed tracing, you cannot reconstruct the execution path of a single request. Without cost tracking, you cannot answer "How much did this session cost?"

**What to build:**
- Prometheus metrics endpoint (`/metrics`) with key counters/histograms
- Structured logging with correlation IDs (trace_id, span_id)
- Cost estimation per LLM call (input_tokens × price + output_tokens × price)
- Token usage breakdown by component (system prompt, context, tool results, LLM calls)
- Async audit logging (background task or async file handler)
- Slow query logging

**Effort:** Medium. The `audit_log()` function already exists; it needs to be made async. Metrics need to be added to the agent loop and tool execution.

**Impact:** Medium-High. This is the difference between a black box and a transparent system.

---

### 8. Backup & Disaster Recovery

**Current state:** No backup scripts, no WAL archiving, no backup automation, no backup verification, no off-site storage, no PITR.

**Why P2:** Backup is **essential for any system that stores user data**. Without it, data loss is permanent. The `sessions`, `context_chunks`, and `memories` tables contain irreplaceable user data.

**What to build:**
- `pg_dump` scripts for daily backups
- WAL archiving for point-in-time recovery
- Backup automation (cron job or Kubernetes CronJob)
- Backup verification (restore testing, integrity checks)
- Off-site storage (S3, GCS, Azure Blob)
- Backup retention policy

**Effort:** Low-Medium. This is an infrastructure task, not a code task.

**Impact:** High. This is the difference between a system that can recover from disaster and one that cannot.

---

## P2 Scalability Fixes

### 9. Connection Pool & Database Scalability

**Current state:** Hardcoded pool size (min=2, max=10). No connection retry, no pool timeout, no health checks. No pagination on `list_sessions()`. No partitioning on `context_chunks`.

**Why P2:** Scalability is **essential for multi-user scenarios**. The current pool size is a death sentence with 5+ concurrent users. The lack of pagination will degrade with thousands of sessions.

**What to build:**
- Make pool size configurable via environment variable (`DB_POOL_MIN`, `DB_POOL_MAX`)
- Add `pool_timeout` (e.g., 5s) so callers fail fast instead of hanging
- Add connection health check (`SELECT 1`) on acquire
- Add `OFFSET` parameter and cursor-based pagination to `list_sessions()`
- Add time-based partitioning to `context_chunks` (e.g., by month)
- Reduce `ef_construction` to 16-32 for better write performance
- Add covering index: `CREATE INDEX idx_context_chunks_recent ON context_chunks(session_id, created_at DESC) INCLUDE (payload_msgpack, chunk_type, token_count)`

**Effort:** Low-Medium. Most of these are configuration changes or SQL DDL.

**Impact:** Medium-High. This is the difference between a single-user tool and a multi-user service.

---

### 10. Multi-Tenancy & Session Isolation

**Current state:** No tenant/organization concept. No row-level security. No quota enforcement. No authentication or authorization. The `agent_id` field is a string with no enforcement.

**Why P2:** Multi-tenancy is **essential for any hosted service**. Without it, one user can see another user's session list, context chunks, and memories.

**What to build:**
- Add `organization_id` or `user_id` to all tables
- Implement PostgreSQL row-level security policies
- Add per-tenant quotas (max sessions, max context chunks, max tokens)
- Add authentication and authorization to the CLI
- Enforce `agent_id` uniqueness per session

**Effort:** High. This is an architectural change that touches every table and every query.

**Impact:** High. This is the difference between a local tool and a hosted service.

---

## P2 Polish Fixes

### 11. UX Consistency & Accessibility

**Current state:** Two visual languages in the same app (Typer commands use raw `console.print()`, REPL uses `VisualContext`). No accessibility features. No high contrast theme. No reduced motion option. No internationalization.

**Why P2:** Polish is the **difference between a developer tool and a product**. Accessibility is not optional — it is a moral and legal requirement.

**What to build:**
- Unify output formatting — use `VisualContext` everywhere
- Add screen reader support — text alternatives for visual output
- Add high contrast theme
- Add reduced motion option — disable animations
- Add font size adjustment
- Add text labels or patterns for colorblind users (not just color)
- Add internationalization (i18n)
- Add consistent spacing/padding rules
- Add consistent command naming
- Add consistent flag naming
- Add consistent output formatting
- Add consistent status messages
- Add consistent error messages

**Effort:** Medium. This is a combination of small changes across the entire CLI.

**Impact:** Medium. This is the difference between a tool that excludes users and one that includes everyone.

---

### 12. Config System Hardening

**Current state:** No config validation. No config migration. No interactive config editor. Config changes in REPL don't persist. No config reset. No config diff.

**Why P2:** Config is the **primary way users customize the agent**. Without validation, invalid values silently break the agent. Without persistence, users lose their changes on restart.

**What to build:**
- Config validation — validate values on set, reject invalid values
- Config migration — if the schema changes, migrate old config files
- Interactive config editor — `/config` should allow setting values, not just showing them
- Config persistence in REPL — `/model`, `/provider`, `/budget` should persist by default
- Config reset — reset to defaults
- Config diff — show what's changed from defaults
- Sensitive values in plain text — encrypt or redact sensitive config values

**Effort:** Low-Medium. The `Config` class already exists; it needs validation and persistence logic.

**Impact:** Medium. This is the difference between a config system that works and one that breaks silently.

---

### 13. Error Handling & Recovery

**Current state:** Errors are printed as `[red]Agent error:[/red] {e}` — raw exception string, no formatting, no panel, no animation, no suggestion. No error recovery. No error categorization. No stack trace in verbose mode.

**Why P2:** Error handling is the **difference between a tool that helps users and one that confuses them**. Good error messages suggest fixes. Bad error messages are just noise.

**What to build:**
- Error panels — use `PanelStyles.error()` for error display
- Error recovery suggestions — "Did you mean...", "Try /switch to another session", etc.
- Error categorization — network errors, auth errors, rate limit errors, validation errors
- Stack trace in verbose mode — `logger.exception()` logs to stderr but the user never sees it
- Ctrl+C handling during agent run — catch `KeyboardInterrupt` in the streaming loop and stop gracefully
- Timeout for user input — idle timeout, cancel pending input

**Effort:** Low-Medium. This is a combination of small changes across the CLI and agent loop.

**Impact:** Medium. This is the difference between a tool that helps users recover and one that leaves them stranded.

---

### 14. Streaming & Display Polish

**Current state:** No streaming cursor. No markdown rendering during streaming. Tool calls shown as raw text. No token counter during streaming. No cost display. No interrupt. No ETA. Verbose mode is all-or-nothing.

**Why P2:** Streaming is the **primary feedback mechanism**. Without it, users think the agent is hung.

**What to build:**
- Streaming cursor — use `StreamingAnimation` cursor (`▌`)
- Markdown rendering during streaming — render as `Markdown` in a `Panel` during streaming, not just after
- Tool call display — show as a separate formatted block, not raw text
- Token counter during streaming — show `tokens/budget` and estimated cost
- Interrupt — stop a streaming response mid-generation
- ETA — indicate how long the response will take
- Verbose levels — not all-or-nothing, but configurable levels

**Effort:** Medium. The `run_stream()` method already yields `StreamEvent` objects; the CLI needs to render them better.

**Impact:** Medium. This is the difference between a tool that feels alive and one that feels broken.

---

### 15. Autocomplete & Input

**Current state:** Only completes slash commands. No autocomplete for session IDs, model names, providers, file paths, tool names. No fuzzy matching. No multi-line input. No paste handling.

**Why P2:** Autocomplete is the **primary way users interact with the CLI**. Without it, users must type full UUIDs and model names from memory.

**What to build:**
- Autocomplete for session IDs — `/switch` should suggest recent sessions
- Autocomplete for model names — `/model` should suggest known models
- Autocomplete for providers — `/provider` should suggest known providers
- Autocomplete for file paths — in tool arguments
- Autocomplete for tool names — in the REPL
- Fuzzy matching — not just exact prefix
- Multi-line input — support paste with `Alt+Enter` or `Ctrl+D` to finish
- Paste handling — pasting a large block of text should not trigger autocomplete on every line

**Effort:** Medium. The `SlashCommandCompleter` already exists; it needs to be extended.

**Impact:** Medium. This is the difference between a CLI that helps users and one that makes them type everything.

---

## P2 Code Quality Fixes

### 16. Dead Code Removal

**Current state:** `ah/cli/animations.py` is 930 lines of dead code. None of its classes or functions are imported or used anywhere in the codebase.

**Why P2:** Dead code is **technical debt that confuses contributors**. They spend time reading it, trying to understand it, and eventually realizing it is never called.

**What to build:**
- Delete `ah/cli/animations.py` — 930 lines of dead code
- Delete unused methods — 25+ unused methods across the codebase
- Delete unused imports — 10+ unused imports across the codebase
- Delete unused variables — `_batch_size`, `_pending` in `ah/core/context.py`

**Effort:** Low. This is a deletion task.

**Impact:** Low-Medium. This is the difference between a clean codebase and a confusing one.

---

### 17. Code Duplication Elimination

**Current state:** `_row_to_chunk` duplicated 4 times. Embedding string conversion duplicated 8 times. Tool call execution duplicated in `run()` and `run_stream()`. Retry logic duplicated. Provider `complete()` methods duplicated.

**Why P2:** Duplication is **technical debt that multiplies bugs**. A bug fix in one copy must be applied to all copies. Eventually, someone forgets.

**What to build:**
- Extract `_row_to_chunk` to a shared utility — eliminates 4x duplication
- Extract embedding string conversion to a utility — eliminates 8x duplication
- Refactor `ReActAgent.run()` and `run_stream()` — extract common logic, reduce to <100 lines each
- Extract retry logic to a shared decorator or context manager
- Use template method pattern for provider `complete()` methods

**Effort:** Medium. This is a refactoring task that requires careful testing.

**Impact:** Medium. This is the difference between a maintainable codebase and a fragile one.

---

### 18. Magic Numbers & Named Constants

**Current state:** 25+ magic numbers across the codebase. `4` (chars per token), `100` (minimum remaining tokens), `50` (stop adding chunks), `200` (max result string length), `500` (max result preview length), `1000` (max tool result in message), `50_000` (max token budget), `128` (cache max size), `5` (cache TTL in seconds), `0.7` (default temperature), `4096` (default max tokens), `10` (default rate limit), `0.85` (dedup threshold), `0.2` (min importance to keep), `0.05` (eviction threshold), `14.0` (half-life in days), `0.1` (access boost), `0.7` (dense weight), `0.3` (sparse weight), `0.3` (min similarity threshold), `60` (RRF k parameter), `1536` (embedding dimensions).

**Why P2:** Magic numbers are **unmaintainable**. When a developer sees `if remaining < 50:`, they have to guess what 50 means. When they see `if remaining < MIN_TOKENS_FOR_RETRIEVAL:`, they know exactly what it means.

**What to build:**
- Add named constants for all magic numbers — 25+ instances
- Group related constants into enums or dataclasses

**Effort:** Low. This is a find-and-replace task.

**Impact:** Low-Medium. This is the difference between readable code and cryptic code.

---

### 19. Type Hints & Docstrings

**Current state:** Missing type hints in many places. `Any` overuse. Missing docstrings. Inadequate docstrings. Outdated docstrings.

**Why P2:** Type hints and docstrings are **the primary way developers understand code**. Without them, developers have to read the implementation to understand the interface.

**What to build:**
- Add missing type hints — especially return types
- Remove `Any` overuse — use specific types or unions
- Add proper docstrings — especially for public APIs
- Fix outdated docstrings — update or delete

**Effort:** Low-Medium. This is a documentation task.

**Impact:** Low-Medium. This is the difference between a codebase that is easy to understand and one that is not.

---

## P2 Testing Fixes

### 20. Test Infrastructure

**Current state:** No `conftest.py`. No coverage measurement. No CI/CD integration. Global singleton pollution. Duplicate test name. Chaos tests are vacuous. Integration tests don't integrate.

**Why P2:** Test infrastructure is **essential for code quality**. Without it, tests rot. They pass today and break tomorrow with no one noticing.

**What to build:**
- Add `conftest.py` with `autouse` fixture to reset all global singletons between tests
- Install `pytest-cov` and set a coverage threshold (start at 30%, aim for 70%)
- Fix the duplicate `test_load_directory` — rename one to `test_load_directory_raises`
- Add CI/CD (GitHub Actions) with PostgreSQL service container, `pytest --cov`, and linting (`ruff`)
- Rewrite chaos tests to assert meaningful behavior
- Add tests for all CLI commands using `typer.testing.CliRunner`
- Add security tests for terminal tool, URL validation, path traversal
- Add real integration tests with PostgreSQL (not mocked)
- Add negative tests for all public functions

**Effort:** Medium-High. This is a combination of infrastructure and test writing.

**Impact:** High. This is the difference between a test suite that provides false confidence and one that provides real confidence.

---

## Summary: The P2 Argument

P2 fixes are the **feature completeness and polish layer**. They transform AgentHarness from a **secure, deployable, well-tested single-user CLI** into a **competitive, accessible, multi-user product**.

The P2 fixes are not optional. They are the difference between:

| Without P2 | With P2 |
|-----------|---------|
| A prototype that only the developer can use | A product that anyone can use |
| A tool that crashes on long sessions | A tool that handles 100+ turn sessions |
| A CLI with 12 commands | A CLI with 60+ commands |
| A memory system that writes immediately | A memory system with approval gates |
| A skill system that never evolves | A skill system that learns from experience |
| Documentation that misleads | Documentation that informs |
| A single-user tool | A multi-user service |
| A tool that excludes users | A tool that includes everyone |

**The P2 fixes are the difference between a project that is technically sound and one that is actually useful.**

---

## Recommended P2 Order

1. **Documentation Overhaul** — Low effort, high impact. Do this first.
2. **Dead Code Removal** — Low effort, low risk. Do this second.
3. **Code Duplication Elimination** — Medium effort, medium risk. Do this third.
4. **Magic Numbers & Named Constants** — Low effort, low risk. Do this fourth.
5. **Type Hints & Docstrings** — Low effort, low risk. Do this fifth.
6. **Test Infrastructure** — Medium effort, high impact. Do this sixth.
7. **Config System Hardening** — Low effort, medium impact. Do this seventh.
8. **Error Handling & Recovery** — Low effort, medium impact. Do this eighth.
9. **Streaming & Display Polish** — Medium effort, medium impact. Do this ninth.
10. **Autocomplete & Input** — Medium effort, medium impact. Do this tenth.
11. **UX Consistency & Accessibility** — Medium effort, medium impact. Do this eleventh.
12. **Slash Command Expansion** — Low effort, medium impact. Do this twelfth.
13. **Session Search & Management** — Medium effort, high impact. Do this thirteenth.
14. **Context Compression** — Medium effort, high impact. Do this fourteenth.
15. **Memory Approval Gate & User Modeling** — Medium effort, medium impact. Do this fifteenth.
16. **Observability & Metrics** — Medium effort, medium impact. Do this sixteenth.
17. **Backup & Disaster Recovery** — Low effort, high impact. Do this seventeenth.
18. **Connection Pool & Database Scalability** — Low effort, medium impact. Do this eighteenth.
19. **Skill Hub & Curator** — High effort, medium impact. Do this nineteenth.
20. **Multi-Tenancy & Session Isolation** — High effort, high impact. Do this last.

---

## Conclusion

P2 is not "nice to have." P2 is the layer that makes AgentHarness a **product** rather than a **prototype**. Without P2, AgentHarness is a well-engineered ReAct loop with a CLI bolted on. With P2, it is a competitive agent framework that can stand alongside Hermes, Claude Code, and Cursor.

The P2 fixes are achievable. They do not require architectural rewrites. They require **disciplined execution** of well-understood features. The critiques have identified the gaps. The roadmap has prioritized them. Now it is time to build them.

**Vote P2. Build the product.**
