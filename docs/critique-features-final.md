# AgentHarness vs Hermes Agent: Brutal Feature Completeness Audit

**Date:** 2026-10-01  
**Scope:** Every feature area requested — self-improvement, memory, context compression, session search, cron, delegation, user modeling, skills, MCP, themes, slash commands.  
**Method:** Line-by-line source review of AgentHarness (`ah/`) against Hermes Agent's documented feature set (skill references, CLI reference, configuration reference, background systems, native MCP, themes, slash commands).

---

## Executive Summary

AgentHarness is a **well-organized ReAct prototype** with clean architecture (DI container, typed dataclasses, async PostgreSQL+pgvector). It is **not** a competitor to Hermes Agent. Hermes is a production-grade, multi-platform agent framework with 20+ surfaces, 60+ slash commands, native MCP, durable cron, subagent delegation, a skill lifecycle curator, and a skin engine that themes every surface simultaneously. AgentHarness has 12 slash commands, no MCP, no cron, no delegation, no session search, no self-improvement, and a memory system that — while architecturally interesting — is a fraction of Hermes's persistent memory with user modeling.

**Bottom line:** AgentHarness implements roughly **15-20%** of Hermes's feature surface. The gap is not incremental; it is architectural. AgentHarness would need to be rebuilt from the ground up to compete.

---

## 1. Self-Improvement

### What Hermes Has
- **Skill learning from experience** — Hermes saves reusable procedures as skills that automatically load into future sessions. The `/learn <source>` command extracts skills from directories, URLs, or the current chat.
- **Curator system** — Background maintenance for agent-created skills: tracks usage, marks idle skills stale, archives stale ones, keeps pre-run backups. CLI: `hermes curator <verb>` (status, usage, run, pause, resume, pin, unpin, archive, restore, list-archived, prune, backup, rollback). Slash: `/curator <subcommand>`.
- **Journey/Learning timeline** — `/journey` (alias `/learning`) shows learned skills + memories timeline.
- **Skill provenance** — Skills track `created_by: "agent"` vs bundled/hub-installed. Curator only touches agent-created skills.
- **Consolidation pass** — Opt-in aux-model "consolidate overlapping skills into umbrellas" pass (`curator.consolidate: true`).
- **Usage telemetry** — Sidecar at `~/.hermes/skills/.usage.json` with per-skill `use_count`, `view_count`, `patch_count`, `last_activity_at`, `state`, `pinned`.

### What AgentHarness Has
- **Nothing.** The `SkillRegistry` loads SKILL.md files from a directory and matches triggers via simple keyword `in` check. There is no skill creation, no skill learning, no usage tracking, no curation, no consolidation, no provenance tracking. Skills are static files that never evolve.

### Verdict
**0/10.** AgentHarness has no self-improvement capability whatsoever. This is the single largest philosophical gap: Hermes learns from experience; AgentHarness does not.

---

## 2. Memory

### What Hermes Has
- **Persistent cross-session memory** — Remembers user preferences, environment details, lessons learned across all sessions.
- **Pluggable memory backends** — `hermes memory setup|status|off|reset`. Multiple provider support.
- **User profile** — `user_profile_enabled` config. Dedicated user modeling layer.
- **Memory approval gate** — `/memory [pending|approve|reject]` — review pending memory writes before they persist.
- **Secret redaction** — PII and secret redaction in memory writes.
- **Memory in every surface** — Memory is available on CLI, TUI, desktop, gateway, and IDE surfaces.

### What AgentHarness Has
- **MemoryStore** — Async CRUD for long-term memories in PostgreSQL with pgvector. Categories: preference, decision, fact, event, transient.
- **MemoryConsolidator** — LLM-powered extraction pipeline: raw chunks → LLM extraction → importance scoring → dedup → write. Called after each agent run.
- **ImportanceScorer** — Multi-factor heuristic: category base importance + explicit importance + recency boost (30-day decay) + access frequency boost.
- **ForgettingModel** — Ebbinghaus exponential decay with importance modulation. `strength(t) = base_strength × e^(-λ_effective × Δt) + access_count × 0.1`.
- **MemoryRetriever** — Hybrid search: dense vector (pgvector cosine) + sparse keyword (ILIKE) → merge → LLM re-rank → top-K.
- **remember/recall tools** — Agent can explicitly store and retrieve memories.
- **Embeddings** — All memories have 1536-dim embeddings for semantic search.

### What AgentHarness is Missing
- No user profile / user modeling layer
- No memory approval gate (memories are written immediately)
- No secret redaction in memory writes
- No pluggable memory backends (hardcoded to PostgreSQL)
- No memory CLI commands (`ah memory list/search/forget` are in the roadmap but not implemented)
- No memory surface outside the REPL (no gateway, no desktop, no IDE)
- Forgetting model exists but is never invoked (no background eviction job)
- No memory consolidation scheduling (only runs after agent run, not as a background process)

### Verdict
**4/10.** AgentHarness has a genuinely interesting memory architecture — the Ebbinghaus decay model and hybrid retrieval are well-designed. But it is a single-backend, single-surface, write-immediately system with no user modeling, no approval gate, and no background maintenance. Hermes's memory is a platform-wide feature; AgentHarness's is a database table with a consolidator that runs once per turn.

---

## 3. Context Compression

### What Hermes Has
- **Configurable compression** — `compression.enabled`, `compression.threshold` (0.50), `compression.target_ratio` (0.20).
- **/compress command** — `/compress (/compact)` with `here [N]` to keep N turns, `--preview` to preview before committing.
- **Hard invariant** — "Never break prompt caching — don't change past context, toolsets, or the system prompt mid-conversation. The only exception is context compression."
- **Compression is a first-class operation** — It is a deliberate, user-triggered or threshold-triggered action that summarizes older context into a compact form.

### What AgentHarness Has
- **PromptAssembler with token budget** — Assembles prompts within a session budget (default 8000 tokens). Includes system prompt + goal + query + recent chunks (last 3) + retrieved chunks (fill remaining budget).
- **Token counting** — tiktoken (cl100k_base) with len//4 fallback.
- **Chunk compression** — `_compress_chunk()` truncates tool results to 200 chars, formats chunks as minimal text.
- **No true compression** — The assembler selects which chunks to include but never summarizes or compresses the conversation history. When the budget is exceeded, it simply stops adding chunks. There is no summarization, no rolling compaction, no `/compress` command.

### Verdict
**2/10.** AgentHarness has token-budget-aware prompt assembly, which is a form of context management. But it has no compression — no summarization of old context, no user-triggered compact operation, no threshold-based auto-compression. The "compression" is just truncation and selection.

---

## 4. Session Search

### What Hermes Has
- **session_search toolset** — Full toolset for searching past conversations.
- **/sessions command** — Browse and resume previous sessions.
- **hermes sessions list|browse|rename|delete|export|prune|stats** — Full session management CLI.
- **SQLite + FTS5** — Canonical session store with full-text search.
- **Session transcripts** — `*.jsonl` transcripts for every session.
- **Session export** — Export sessions to files.

### What AgentHarness Has
- **SessionManager** — Create, get, list, archive sessions. List supports status filter and limit.
- **TTLCache** — 5-second LRU cache for session hot paths.
- **/sessions slash command** — Lists recent sessions in a table.
- **No search** — No full-text search, no fuzzy search, no session content search. You can only list sessions by status and recency.
- **No session export** — No way to export a session's conversation.
- **No session rename** — Title is set at creation, never updated.
- **No session stats** — No token usage, no message count, no duration tracking.

### Verdict
**1/10.** AgentHarness can list sessions. That is the entirety of its session search capability. Hermes has a full session management platform with search, export, rename, and stats.

---

## 5. Cron

### What Hermes Has
- **Durable cron scheduler** — `cron/jobs.py` + `cron/scheduler.py`. Survives process restarts.
- **cronjob tool** — Agent can create scheduled tasks.
- **hermes cron CLI** — `list`, `add`, `edit`, `pause`, `resume`, `run`, `remove`, `status`.
- **/cron slash command** — Manage scheduled tasks in-session.
- **Flexible schedules** — Duration (`"30m"`, `"2h"`), "every" phrase (`"every monday 9am"`), 5-field cron (`"0 9 * * *"`), or ISO timestamp.
- **Per-job configuration** — `skills`, `model`/`provider` override, `script` (pre-run data collection), `context_from` (chain job output), `workdir`, multi-platform delivery.
- **Invariants** — 3-minute hard interrupt per run, `.tick.lock` prevents duplicate ticks, cron sessions pass `skip_memory=True`, delivery framed with header/footer.
- **Webhook triggers** — `hermes webhook subscribe|list|remove|test`. Event-driven runs.

### What AgentHarness Has
- **Nothing.** No cron, no scheduling, no webhooks, no background jobs. The roadmap mentions `ah/scheduler/cron.py` as a Phase 6 item, but it does not exist.

### Verdict
**0/10.** Zero cron capability. This is a fundamental missing feature for any agent that needs to run autonomously.

---

## 6. Delegation

### What Hermes Has
- **delegate_task tool** — Spawn a subagent with isolated context + terminal session.
- **Single mode** — `delegate_task(goal, context)`.
- **Batch mode** — `delegate_task(tasks=[{goal, ...}, ...])` runs children in parallel, capped by `delegation.max_concurrent_children` (default 3).
- **Background mode** — `delegate_task(background=true)` returns a handle immediately; child's result re-enters conversation as a new turn when it finishes.
- **Roles** — `leaf` (default; cannot re-delegate) vs `orchestrator` (can spawn its own workers, bounded by `delegation.max_spawn_depth`).
- **Config** — `delegation.*` in config.yaml: `model`, `provider`, `max_concurrent_children`, `max_iterations` (50), `max_spawn_depth`.
- **Spawning Hermes processes** — Run additional Hermes processes as fully independent subprocesses (separate sessions, tools, environments). Interactive PTY mode via tmux.
- **Multi-agent coordination** — Agent A (backend) + Agent B (frontend) with context relay between them.
- **Kanban board** — Durable SQLite board for multi-profile/multi-worker collaboration. `hermes kanban <verb>` with dispatcher, worker toolsets, auto-blocking on failure.

### What AgentHarness Has
- **Nothing.** No delegation, no subagent spawning, no multi-agent orchestration, no kanban. The `agent_id` field on sessions and context chunks is a vestige of a multi-agent design that was never implemented. The name "AgentHarness" implies multi-agent orchestration, but the codebase is a single-agent ReAct loop.

### Verdict
**0/10.** Zero delegation capability. The gap between the project's name and its actual capability is embarrassing.

---

## 7. User Modeling

### What Hermes Has
- **User profile** — `user_profile_enabled` config. Dedicated user profile that persists across sessions.
- **Memory with user context** — Memories are associated with users, enabling personalized responses.
- **/whoami** — Slash command to check slash-command access level (admin/user).
- **/personality** — Set a personality for the agent.
- **/reasoning** — Control reasoning effort/display (none..xhigh|max|ultra).
- **Approval modes** — `approvals.mode` (smart/manual/off), per-user approval settings.
- **Profile system** — Multiple independent Hermes instances with isolated configs, sessions, skills, and memory. `hermes profile list|create|use|show|delete|rename|alias|export|import`.

### What AgentHarness Has
- **agent_id field** — A string field on sessions and memories, defaulting to "harness". This is not user modeling; it is a single-agent identifier.
- **Memory categories** — "preference" category exists, but there is no user profile, no per-user memory isolation, no user preferences beyond what the LLM extracts.
- **No /whoami, no /personality, no /reasoning, no approval modes, no profiles.**

### Verdict
**0/10.** AgentHarness has no user modeling. The `agent_id` field is a placeholder for a multi-user design that does not exist.

---

## 8. Skills

### What Hermes Has
- **Skill hub** — `hermes skills browse|search|inspect|install|check|update|uninstall|publish`. Install skills from hub identifiers or direct URLs.
- **Skill bundles** — `/bundles` — one `/<name>` alias loads several skills.
- **Curator** — Background maintenance: usage tracking, stale detection, archiving, consolidation. See Self-Improvement section.
- **Skill config** — `hermes skills config` — enable/disable skills per platform.
- **Skill taps** — `hermes skills tap add REPO` — add a GitHub repo as a skill source.
- **/skills command** — Search/install/manage skills in-session.
- **/learn command** — Learn a reusable skill from dirs/URLs/this chat.
- **/reload-skills** — Re-scan skills directory.
- **Skill provenance** — `created_by: "agent"` vs bundled/hub-installed.
- **Usage telemetry** — Per-skill use_count, view_count, patch_count, last_activity_at.
- **Skill templates** — Linked files in skill system (templates/, references/, scripts/).
- **Skill safety rule** — Skills with `[SKILL_PRUNED]` markers are detected and reloaded.

### What AgentHarness Has
- **SkillParser** — Parses SKILL.md files with YAML frontmatter (name, description, triggers, content, version).
- **SkillRegistry** — Loads all skills from a directory, matches triggers via keyword `in` check, provides skill content for prompt injection.
- **No skill creation, no skill hub, no skill install, no skill bundles, no curator, no usage telemetry, no skill config, no skill taps, no /learn, no /reload-skills.**
- **Trigger matching** — Simple substring check: `if trigger.lower() in query_lower`. No fuzzy matching, no embedding-based matching, no LLM-based matching.
- **No skill versioning** — Version field exists in the parser but is never used.
- **No skill dependencies** — Skills cannot depend on other skills.
- **No skill testing** — No way to test a skill before installing.

### Verdict
**2/10.** AgentHarness has a SKILL.md parser and a keyword matcher. This is the absolute minimum viable skill system. Hermes has a full skill platform with hub, curator, bundles, provenance, and telemetry.

---

## 9. MCP (Model Context Protocol)

### What Hermes Has
- **Native MCP client** — Built-in, no bridge CLI needed. Connects to MCP servers at startup, discovers tools, makes them first-class.
- **Stdio transport** — Launch MCP servers as subprocesses (npx, uvx, any command).
- **HTTP/StreamableHTTP transport** — Connect to remote MCP servers.
- **Tool naming convention** — `mcp_{server_name}_{tool_name}` with hyphens/dots replaced.
- **Auto-injection** — MCP tools injected into all platform toolsets.
- **Connection lifecycle** — Persistent connections, automatic reconnection with exponential backoff (5 retries, max 60s).
- **Security** — Environment variable filtering for stdio servers (only safe baseline vars inherited). Credential stripping in error messages.
- **Sampling** — MCP servers can request LLM completions through the agent. Per-server config: `sampling.enabled`, `model`, `max_tokens_cap`, `timeout`, `max_rpm`, `allowed_models`, `max_tool_rounds`, `log_level`.
- **hermes mcp CLI** — `add`, `remove`, `list`, `test`, `catalog`, `install`, `configure`, `serve`.
- **/reload-mcp** — Reload MCP servers in-session.
- **Idempotent discovery** — Failed servers retried on subsequent calls.

### What AgentHarness Has
- **Nothing.** No MCP client, no MCP server support, no tool discovery from external sources. The tool registry is a hardcoded set of Python functions registered via decorators.

### Verdict
**0/10.** Zero MCP support. In 2025-2026, MCP is becoming the standard for tool interoperability. The lack of MCP is a critical gap that makes AgentHarness incompatible with the broader tool ecosystem.

---

## 10. Themes

### What Hermes Has
- **Skin engine** — `hermes_cli/skin_engine.py`. One YAML file themes CLI, TUI, and desktop GUI simultaneously.
- **Live repaint** — Edit the active skin's YAML and every surface repaints within ~1 second.
- **hermes skin CLI** — `list`, `use <name>`, `set <key> <hex>`.
- **/skin command** — Change theme in-session.
- **Semantic theming** — One key colors every element that plays that role (e.g., `ui_accent` colors tool markers AND headings/links/chevrons).
- **Built-in skins** — `default`, `mono`, `slate`, `cyberpunk`, `nous`, `midnight`, `ember`.
- **Custom skins** — Drop a YAML file in `~/.hermes/skins/` and it is available on every surface.
- **Skin templates** — `templates/skin.yaml` with full schema.
- **Contrast enforcement** — GUI enforces WCAG AA contrast.
- **Skin watcher** — Gateway watches for skin changes and pushes to all surfaces.

### What AgentHarness Has
- **ThemeRegistry** — 4 built-in themes: dark (Tokyo Night), light, claude (Catppuccin), hermes (GitHub dark).
- **ColorPalette** — Semantic color definitions (bg, fg, accent, success, warning, error, syntax, border, prompt, status, diff, spinner).
- **StylePreset** — Named style presets for panels, tables, text, status indicators, prompt, headers.
- **VisualContext** — Context manager for consistent styling across CLI components.
- **Theme detection** — `detect_terminal_theme()` checks COLORFGBG env var.
- **Custom themes** — `create_custom_theme()` function to register new themes at runtime.
- **No live repaint** — Changing theme requires restarting the REPL.
- **No skin files** — Themes are Python dataclasses, not YAML files. Users cannot drop a YAML file to create a theme.
- **No skin engine** — Themes only affect the CLI REPL. There is no TUI, no desktop, no gateway to theme.
- **No semantic theming** — Colors are mapped to Rich style strings, not semantic roles. Changing one element requires changing its specific color key.
- **No contrast enforcement** — No WCAG AA validation.
- **No skin watcher** — No live update mechanism.

### Verdict
**3/10.** AgentHarness has a reasonable theme system for a CLI REPL — 4 built-in themes, semantic color palettes, custom theme support. But it is CLI-only, Python-dataclass-based (not file-based), has no live repaint, no semantic theming, and no skin engine. Hermes's skin engine is a platform-wide theming system; AgentHarness's is a CLI color scheme.

---

## 11. Slash Commands

### What Hermes Has (60+ commands)

**Session:** /new, /clear, /retry, /undo, /title, /prompt, /compress, /stop, /rollback, /diff, /snapshot, /bg, /btw, /queue, /steer, /agents, /goal, /subgoal, /branch, /resume, /sessions, /handoff, /status, /redraw

**Configuration:** /config, /model, /personality, /reasoning, /fast, /verbose, /voice, /yolo, /busy, /indicator, /footer, /skin, /statusbar, /battery, /timestamps, /codex-runtime

**Tools & Skills:** /tools, /toolsets, /skills, /bundles, /learn, /memory, /pet, /hatch, /cron, /suggestions, /blueprint, /curator, /kanban, /moa, /reload, /reload-mcp, /reload-skills, /browser, /plugins

**Gateway:** /approve, /deny, /restart, /sethome, /topic, /platform, /commands

**Info:** /help, /usage, /insights, /whoami, /profile, /platforms, /journey, /subscription, /topup, /copy, /paste, /image, /update, /version, /debug

**Exit:** /quit

### What AgentHarness Has (12 commands)

**General:** /help, /clear, /exit (alias: /quit)

**Session:** /status, /sessions, /new, /switch, /context

**Config:** /model, /provider, /budget, /verbose, /config

### What AgentHarness is Missing (48+ commands)
- /compress — context compression
- /retry, /undo, /title, /prompt — session editing
- /rollback, /diff, /snapshot — filesystem checkpoints
- /bg, /btw, /queue, /steer — background/side prompts
- /agents, /goal, /subgoal — task management
- /branch, /resume, /handoff — session branching
- /personality, /reasoning, /fast, /voice — agent behavior
- /yolo, /busy, /indicator, /footer, /statusbar, /battery, /timestamps — UI controls
- /tools, /toolsets, /skills, /bundles, /learn — tool/skill management
- /memory, /pet, /hatch, /cron, /suggestions, /blueprint, /curator, /kanban, /moa — platform features
- /reload, /reload-mcp, /reload-skills, /browser, /plugins — runtime management
- /approve, /deny, /restart, /sethome, /topic, /platform, /commands — gateway
- /usage, /insights, /whoami, /profile, /platforms, /journey, /subscription, /topup, /copy, /paste, /image, /update, /version, /debug — info

### Verdict
**1/10.** AgentHarness has 12 slash commands that cover basic session and config management. Hermes has 60+ commands covering every aspect of agent operation. The gap is not just quantitative — it is qualitative. Hermes's slash commands enable workflows (compression, checkpointing, background tasks, skill management, cron, delegation) that AgentHarness cannot support at all.

---

## 12. Additional Features Not Requested But Relevant

### Multi-Platform Gateway
- **Hermes:** 20+ messaging platforms (Telegram, Discord, Slack, WhatsApp, iMessage, Signal, Matrix, Teams, Email, SMS, LINE, and more). Full tool access on every platform.
- **AgentHarness:** CLI only. No gateway, no messaging platform support.

### Desktop App
- **Hermes:** Native Electron app with streaming chat, session list, Cmd+K palette, drag-and-drop, native notifications, UI plugins.
- **AgentHarness:** None.

### Web Dashboard
- **Hermes:** Full admin panel with messaging channels, MCP catalog, webhooks, memory, profile builder, embedded chat.
- **AgentHarness:** None.

### TUI
- **Hermes:** Ink TUI with docked widget apps, TUI widgets, pet mascots.
- **AgentHarness:** prompt_toolkit REPL with autocomplete. No TUI widgets, no pets.

### IDE Integration
- **Hermes:** ACP server for VS Code / Zed / JetBrains.
- **AgentHarness:** None.

### OpenAI-Compatible Proxy
- **Hermes:** `hermes proxy` — local OpenAI API backed by OAuth provider.
- **AgentHarness:** None.

### Provider Support
- **Hermes:** 20+ providers (OpenRouter, Anthropic, OpenAI, Google, DeepSeek, xAI, local models, and more). Credential pools with automatic rotation.
- **AgentHarness:** 2 providers (OpenRouter, Ollama). No credential pools, no fallback chain.

### Security
- **Hermes:** Secret redaction, PII redaction, approval modes, website blocklist, tirith security, environment variable filtering for MCP, credential stripping in errors.
- **AgentHarness:** SSRF protection on web tools, command allowlist on terminal tool, path traversal protection on file tools. No secret redaction, no approval modes, no PII redaction.

### Observability
- **Hermes:** Usage analytics, insights, debug report upload, cost tracking, token usage per session.
- **AgentHarness:** Audit logging (JSON to stdout). No metrics, no tracing, no analytics, no cost tracking.

### Voice
- **Hermes:** STT (faster-whisper, Groq, OpenAI, Mistral, ElevenLabs, Deepinfra) + TTS (Edge, ElevenLabs, OpenAI, MiniMax, Mistral, Gemini, NeuTTS, Piper, KittenTTS).
- **AgentHarness:** None.

---

## Summary Scorecard

| Feature | AgentHarness | Hermes Agent | Score |
|---------|-------------|--------------|-------|
| Self-Improvement | None | Skill learning, curator, journey, provenance | 0/10 |
| Memory | PostgreSQL + pgvector, consolidator, forgetting model | Pluggable backends, user profiles, approval gate | 4/10 |
| Context Compression | Token budget assembly | Configurable compression, /compress, summarization | 2/10 |
| Session Search | List only | FTS5, browse, export, rename, stats | 1/10 |
| Cron | None | Durable scheduler, CLI, webhooks, per-job config | 0/10 |
| Delegation | None | delegate_task, batch, background, roles, kanban | 0/10 |
| User Modeling | None | User profiles, /whoami, /personality, approval modes | 0/10 |
| Skills | SKILL.md parser, keyword match | Hub, curator, bundles, provenance, telemetry | 2/10 |
| MCP | None | Native client, stdio/HTTP, sampling, security | 0/10 |
| Themes | 4 CLI themes, Python dataclasses | Skin engine, live repaint, YAML files, all surfaces | 3/10 |
| Slash Commands | 12 basic | 60+ comprehensive | 1/10 |
| **Overall** | | | **~1.1/10** |

---

## Conclusion

AgentHarness is a **clean, well-architected ReAct prototype** that demonstrates solid software engineering practices: dependency injection, typed dataclasses, async I/O, PostgreSQL+pgvector integration, and comprehensive test coverage (330 tests). The memory system's Ebbinghaus decay model and hybrid retrieval pipeline are genuinely interesting design choices.

But it is **not a competitor to Hermes Agent**. It is not a competitor to any production agent framework. The feature gap is not a matter of "a few missing features" — it is a matter of entire subsystems that do not exist: no cron, no delegation, no MCP, no gateway, no desktop, no TUI, no IDE integration, no user modeling, no self-improvement, no session search, no context compression.

The project's own roadmap acknowledges this: Phases 3-7 are all unimplemented. But the roadmap is a list of features that competitors have already shipped. By the time AgentHarness implements them, the market will have moved further ahead.

**AgentHarness should be explicitly positioned as an educational project** — a well-executed teaching implementation of the ReAct pattern with PostgreSQL+pgvector integration. It should not be positioned as a production framework or a competitor to Hermes Agent, LangGraph, CrewAI, or any other mature agent framework.

The code quality is good. The architecture is clean. The tests are comprehensive. But you cannot audit feature completeness against a product that has 10x the features and conclude anything other than: **the gap is existential, not incremental.**
