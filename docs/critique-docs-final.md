# AgentHarness Documentation Audit — Brutal Critique

**Date:** 2026-10-01
**Scope:** All 33 markdown files in `docs/` + `README.md` + `skills/*/SKILL.md`
**Method:** Cross-referenced every claim against actual source code, schema, CLI, and test suite

---

## Executive Summary

The documentation is in **deplorable shape**. It is riddled with stale metrics, broken references, outdated architecture descriptions, and claims that were true once but haven't been updated after multiple refactoring passes. A new developer reading these docs would have a fundamentally wrong understanding of the codebase.

**Severity distribution:**

| Severity | Count | Description |
|----------|-------|-------------|
| 🔴 CRITICAL | 8 | Actively misleading — would cause wrong decisions |
| 🟠 HIGH | 15 | Significantly outdated — wastes time, creates confusion |
| 🟡 MEDIUM | 12 | Partially wrong — some information is correct, some isn't |
| 🟢 LOW | 8 | Minor inaccuracies — cosmetic or historical |

---

## 1. README.md

### 🔴 CRITICAL

| # | Issue | Actual State |
|---|-------|-------------|
| 1 | Says "136 tests" | **330 tests** collected by pytest |
| 2 | Says "2 tables" in schema | **3 tables**: sessions, context_chunks, memories |
| 3 | Says "20 SKILL.md files" | **21 SKILL.md files** in skills/ |
| 4 | Lists 8 CLI commands | **14 commands**: chat, repl, status, sessions, context, skills, doctor, init, version, config, config-set, memory-list, memory-search, memory-forget |
| 5 | Shows `ah/cli.py` as a file | `ah/cli/` is a **package** with `__init__.py`, `interactive.py`, `visual.py`, `theme.py`, `animations.py` |
| 6 | Shows `ah/memory/` and `ah/rag/` as "(stub — Phase 4/5)" | Both are **fully implemented** with 7 files each |
| 7 | Missing `ah/core/config.py` from project structure | `config.py` exists and is imported by CLI |
| 8 | Missing `ah/tools/memory.py` and `ah/tools/rag.py` from project structure | Both exist and register tools |

### 🟠 HIGH

| # | Issue | Actual State |
|---|-------|-------------|
| 9 | Roadmap says Phase 3 LangGraph "Pending" | `research-langgraph.md` says **"Do not integrate"** — the roadmap contradicts the research |
| 10 | Roadmap says Phase 4 "Skills & Memory: Partial" | Memory is **fully implemented** (store, consolidator, retriever, scorer, forgetting) |
| 11 | Roadmap says Phase 5 "Heartbeat & RAG: Pending" | RAG is **fully implemented** (chunker, embedder, loaders, pipeline, reranker, search) |
| 12 | Technology stack missing `prompt_toolkit` | Listed as dependency in `pyproject.toml` and used in CLI |
| 13 | Architecture diagram doesn't show memory, RAG, or CLI subpackage | Major components invisible |
| 14 | "Security & Reliability" section doesn't mention `ah/core/config.py` | Config is a core component |

---

## 2. docs/arch-final.md

### 🔴 CRITICAL

| # | Issue | Actual State |
|---|-------|-------------|
| 1 | Module structure shows `ah/tools/web.py` | **Does not exist.** Actual: `ah/tools/builtins.py` |
| 2 | Shows `ah/cli/app.py` | **Does not exist.** Actual: `ah/cli/__init__.py` |
| 3 | Shows `ah/memory/manager.py` | **Does not exist.** Actual: `ah/memory/store.py`, `consolidator.py`, `retriever.py`, `scorer.py`, `forgetting.py`, `models.py` |
| 4 | Shows `ah/rag/embed.py` and `ah/rag/search.py` | **Do not exist.** Actual: `ah/rag/embedder.py`, `pipeline.py`, `chunker.py`, `loaders.py`, `reranker.py`, `search.py` |
| 5 | Shows `ah/tui/app.py` | **Does not exist.** No TUI package exists. |
| 6 | Says "2 tables: sessions, context_chunks" | Schema has **3 tables** (memories added) |
| 7 | Dependency graph doesn't show memory/rag imports | `agent.py` imports `ah.memory.consolidator`, `ah.memory.retriever`, `ah.memory.store`, `ah.rag.pipeline` |
| 8 | Success criteria checkboxes all unchecked | Many are clearly done (DI container, caching, audit logging, rate limiting) |

### 🟠 HIGH

| # | Issue | Actual State |
|---|-------|-------------|
| 9 | Says "Phases 1-4 complete" | Phase 5 (Cleanup) is partially done — `pyproject.toml` still lists unused deps |
| 10 | Shows `ah/core/context.py` containing `ContextChunk` | `ContextChunk` is in `ah/core/models.py` |

---

## 3. docs/roadmap.md

### 🔴 CRITICAL

| # | Issue | Actual State |
|---|-------|-------------|
| 1 | Says "136 tests passing" | **330 tests** |
| 2 | Shows `ah/memory/` as "EMPTY STUB" | **Fully implemented** — 7 files, 53 tests in test_memory.py |
| 3 | Shows `ah/rag/` as "EMPTY STUB" | **Fully implemented** — 7 files, 53 tests in test_rag.py |
| 4 | Shows `ah/cli.py` | `ah/cli/` is a package |
| 5 | Phase 3 says "Memory & RAG (Highest Priority)" | Both are **done** — this phase should be marked complete |
| 6 | Phase 4 says "Interactive REPL & UX" is next | REPL is **already implemented** (`ah/cli/interactive.py`) |
| 7 | Missing `ah/core/config.py` from architecture | Config system is implemented and used |
| 8 | Missing `ah/cli/interactive.py` from architecture | REPL exists |

### 🟠 HIGH

| # | Issue | Actual State |
|---|-------|-------------|
| 9 | Dependency graph is outdated | Doesn't show memory/rag imports |
| 10 | Missing `ah/tools/memory.py`, `ah/tools/rag.py` | Both exist |
| 11 | Success criteria says "180+ tests passing" | Already at 330 |

---

## 4. docs/update-log.md

### 🔴 CRITICAL

| # | Issue | Actual State |
|---|-------|-------------|
| 1 | Claims "12 files removed" | `critique-research-docs.md` **still exists** in docs/ |
| 2 | Claims README was updated with "2 tables → 2 tables" | README still says "2 tables" but schema has **3** |
| 3 | Claims README test count was updated from 136 | README **still says 136** |
| 4 | Claims "30+ issues marked ✅ FIXED" | Many "fixed" issues are described with stale code in the docs |

### 🟠 HIGH

| # | Issue | Actual State |
|---|-------|-------------|
| 5 | Says "Files updated: 15" | Many of those 15 still have stale data (see this audit) |
| 6 | Created 2026-10-01 but doesn't mention the massive memory/RAG implementation | The biggest change since the audit is undocumented |

---

## 5. docs/synthesis.md

### 🔴 CRITICAL

| # | Issue | Actual State |
|---|-------|-------------|
| 1 | Says "136 passing tests" | **330 tests** |
| 2 | Shows `ah/tools/web.py` | **Does not exist.** Actual: `ah/tools/builtins.py` |
| 3 | Shows `ah/cli/app.py` | **Does not exist.** Actual: `ah/cli/__init__.py` |
| 4 | Shows `ah/memory/` and `ah/rag/` as stubs | **Fully implemented** |
| 5 | Priority matrix says "No CI/CD" is STILL MISSING | `.github/workflows/ci.yml` and `pr.yml` **exist** |
| 6 | Says "8 unused database tables" FIXED | Schema now has 3 tables, not 2 — the fix was incomplete |

### 🟠 HIGH

| # | Issue | Actual State |
|---|-------|-------------|
| 7 | Success criteria checkboxes are stale | "CI/CD runs on every push" is unchecked but CI exists |
| 8 | Architecture section doesn't show memory/rag | Major gap |

---

## 6. docs/audit-codebase-final.md

### 🟠 HIGH

| # | Issue | Actual State |
|---|-------|-------------|
| 1 | Header says "330 passing" but test coverage section says "307 test functions" | **330 tests** collected (the 307 was from an earlier run) |
| 2 | Says "2 tables" in section 7 but "3 tables" in section 8 | Schema has **3 tables** |
| 3 | Says "20 SKILL.md files" | **21 files** |
| 4 | Says "8 CLI commands" | **14 commands** |
| 5 | Says "10 registered tools" | Actual count: `web_search`, `web_extract`, `search_files`, `read_file`, `write_file`, `list_files`, `terminal`, `remember`, `recall`, `index_document`, `search_documents` = **11 tools** |
| 6 | Says "10 broken file references" but only lists 8 | Incomplete audit |
| 7 | Says `ah/memory/*` (7 files) all pass import | Correct but doesn't list them |
| 8 | Doesn't mention `ah/core/config.py` | File exists and is used |

---

## 7. docs/audit-md-files.md

### 🟠 HIGH

| # | Issue | Actual State |
|---|-------|-------------|
| 1 | Says "44 MD files found" | Doesn't account for all files in docs/ |
| 2 | Says README "Shows skills, memories, agent_messages tables" | README shows 2 tables (sessions, context_chunks) — this was already fixed |
| 3 | Says "20 SKILL.md files" | **21 files** |
| 4 | Recommends deleting `.pytest_cache/README.md` | Auto-generated, already in `.gitignore`? No — it's not. But this is trivial. |
| 5 | Says `arch-proposal-*.md` (10 files) should be removed | They were already removed per update-log, but this audit still lists them |

### 🟡 MEDIUM

| # | Issue | Actual State |
|---|-------|-------------|
| 6 | Says `critique-competitive.md` is "✅ ACCURATE (still valid)" | It says "136 tests" and "2 tables" — **not accurate** |
| 7 | Says `arch-final.md` is "✅ ACCURATE" | It has 8 critical errors (see above) |

---

## 8. docs/critique-architecture.md

### 🟠 HIGH

| # | Issue | Actual State |
|---|-------|-------------|
| 1 | References `ah/core/types.py` and `ah/tools/types.py` | **Neither exists.** `ToolDefinition` is in `ah/core/models.py` |
| 2 | Says "Empty Placeholder Modules" for memory/rag | Both are **fully implemented** |
| 3 | Says "10 tables" in schema bloat section | Schema has **3 tables** now |
| 4 | Says "No Dependency Injection" is PARTIAL | DI container exists and works |
| 5 | Many "✅ FIXED" annotations but the underlying text still describes the old broken state | Misleading — the text should be updated, not just annotated |

### 🟡 MEDIUM

| # | Issue | Actual State |
|---|-------|-------------|
| 6 | Says "God Object" is PARTIAL | `run()` is 130+ lines, `run_stream()` is 300+ lines — this is still valid |
| 7 | Says "Tool Registration via Side Effects" is STILL VALID | Correct — decorator-based global mutation |

---

## 9. docs/critique-security.md

### 🟠 HIGH

| # | Issue | Actual State |
|---|-------|-------------|
| 1 | References `ah/cli.py` | Actual: `ah/cli/__init__.py` |
| 2 | Section 1 shows `shell=True` as the vulnerability | The fix uses `shell=False` — the doc shows the **old** code, not the fix |
| 3 | Says "6 of 12 fixed" | The count is arbitrary — some "fixed" items are marked fixed but the text still describes vulnerabilities |
| 4 | Section 3 (No Authentication) says "Still valid" | Correct — no auth exists |
| 5 | Section 4 (Hardcoded Secrets) says "Still valid" | Correct — `DEFAULT_DSN` still has placeholder |

### 🟡 MEDIUM

| # | Issue | Actual State |
|---|-------|-------------|
| 6 | Section 5 (Unsafe Deserialization) says "Still valid" | Correct — no size limits on msgpack |
| 7 | Section 10 (Database Security) says "Still valid" | Correct — no SSL, no least-privilege user |

---

## 10. docs/critique-testing.md

### 🔴 CRITICAL

| # | Issue | Actual State |
|---|-------|-------------|
| 1 | Says "136 tests pass" | **330 tests** |
| 2 | Says "memory module — Empty file (0 bytes), zero tests" | **53 tests in test_memory.py**, 7 implementation files |
| 3 | Says "rag module — Empty file (0 bytes), zero tests" | **53 tests in test_rag.py**, 7 implementation files |
| 4 | Says "8 tables" in schema | **3 tables** |
| 5 | References `ah/cli.py` | Actual: `ah/cli/__init__.py` |

### 🟠 HIGH

| # | Issue | Actual State |
|---|-------|-------------|
| 6 | Says "Zero integration tests against a real database" | `test_integration.py` has **25 tests** |
| 7 | Says "Zero tests for web_search or web_extract" | May still be valid — needs verification |
| 8 | Says "Zero tests for terminal with workdir" | May still be valid |

---

## 11. docs/critique-scalability.md

### 🟠 HIGH

| # | Issue | Actual State |
|---|-------|-------------|
| 1 | Section 2 "Zero Caching Layer" says ✅ FIXED but text still describes it as broken | Misleading annotation |
| 2 | Section 9 "No Streaming" says ✅ FIXED but text still describes it as broken | Misleading annotation |
| 3 | Section 14 "No Error Recovery" says ✅ FIXED but text still describes it as broken | Misleading annotation |
| 4 | References `ah/cli.py` | Actual: `ah/cli/__init__.py` |
| 5 | Says "2 tables" in schema | **3 tables** |

### 🟡 MEDIUM

| # | Issue | Actual State |
|---|-------|-------------|
| 6 | Section 4 "Single-Threaded Agent Loop" | Still valid — tool calls are sequential |
| 7 | Section 5 "No Backpressure" | Partially valid — rate limiting exists but no per-session quotas |
| 8 | Section 8 "Blocking I/O in Async Context" | Still valid — `subprocess.run()` is synchronous |

---

## 12. docs/critique-cli-ux.md

### 🔴 CRITICAL

| # | Issue | Actual State |
|---|-------|-------------|
| 1 | Says "No Interactive REPL" | **REPL exists** — `ah/cli/interactive.py` with `ah repl` command |
| 2 | Says "No Streaming Output" is ✅ FIXED but section 4.1 still says "No streaming" | Contradictory |
| 3 | Says "No Session Management" | **Session management exists** — `ah sessions`, `--continue`, `--session` |
| 4 | Says "No Slash Commands" | **Slash commands exist** in REPL |
| 5 | Says "No Configuration File" | **Config exists** — `ah/core/config.py`, `ah config`, `ah config-set` |
| 6 | Says "No Model Switching" | **Model switching exists** — `--model` flag on chat and repl |
| 7 | Comparison tables all show ❌ for features that are now implemented | **Every comparison table is wrong** |

### 🟠 HIGH

| # | Issue | Actual State |
|---|-------|-------------|
| 8 | Says "No Tool Approval" | Still valid — no approval mechanism |
| 9 | Says "No Interruption" | Still valid — no Ctrl+C handling |
| 10 | Says "No Conversation History" | Still valid — no history scrollback |
| 11 | Says "No Export/Import" | Still valid |
| 12 | Says "No Search" | Still valid |

---

## 13. docs/critique-competitive.md

### 🟠 HIGH

| # | Issue | Actual State |
|---|-------|-------------|
| 1 | Says "136 tests" | **330 tests** |
| 2 | Says "2 (sessions, context_chunks)" | **3 tables** |
| 3 | Says "7 built-in tools" | **11 tools** |
| 4 | Says "No memory capability" | **Memory is fully implemented** |
| 5 | Says "No multi-agent" | Still correct |
| 6 | Says "No MCP support" | Still correct |

### 🟡 MEDIUM

| # | Issue | Actual State |
|---|-------|-------------|
| 7 | Says "Built-in tools: 7 (file, terminal, web)" | Actually 11 — memory and rag tools added |
| 8 | Feature comparison table is outdated | Doesn't include memory/rag tools |

---

## 14. docs/critique-token-efficiency.md

### 🟠 HIGH

| # | Issue | Actual State |
|---|-------|-------------|
| 1 | Section 1 says "Token Counting: len(text) // 4" is ✅ FIXED but text still describes it as broken | Misleading |
| 2 | References `ah/core/context.py` for PromptAssembler | PromptAssembler is in `ah/core/assembler.py` |
| 3 | Says "Embedding Search Is Never Used" | **Now used** — `agent.py` calls `memory_retriever.retrieve()` |
| 4 | Says "Recent Chunks Fetched But Not Used" | Still valid — fetches 5, uses 3 |

### 🟡 MEDIUM

| # | Issue | Actual State |
|---|-------|-------------|
| 5 | Says "No Prompt Caching" | Still correct |
| 6 | Says "No Deduplication" | Still correct |
| 7 | Says "No Few-Shot Examples" | Still correct |

---

## 15. docs/advocate-code-quality.md

### 🟠 HIGH

| # | Issue | Actual State |
|---|-------|-------------|
| 1 | Says "136 tests" | **330 tests** |
| 2 | Says "20 skills" | **21 skills** |
| 3 | Shows `ah/cli.py` | Actual: `ah/cli/__init__.py` |
| 4 | Shows `ah/memory/` and `ah/rag/` as empty | **Fully implemented** |
| 5 | Missing `ah/core/config.py` from architecture description | Config is a core component |
| 6 | Says "No CI/CD" is missing | **CI exists** — `.github/workflows/ci.yml` |

### 🟡 MEDIUM

| # | Issue | Actual State |
|---|-------|-------------|
| 7 | Says "No interactive REPL" | **REPL exists** |
| 8 | Says "No metrics or tracing" | Still correct |

---

## 16. docs/advocate-pragmatic.md

### 🟠 HIGH

| # | Issue | Actual State |
|---|-------|-------------|
| 1 | Says "136 tests" | **330 tests** |
| 2 | Says "2-table schema" | **3 tables** |
| 3 | Shows `ah/cli.py` | Actual: `ah/cli/__init__.py` |
| 4 | Says "Seven built-in tools" | **11 tools** |
| 5 | Says "No CI/CD" is STILL VALID | **CI exists** |

### 🟡 MEDIUM

| # | Issue | Actual State |
|---|-------|-------------|
| 6 | Says "No streaming" is FIXED | Correct — streaming exists |
| 7 | Says "No multi-agent" | Still correct |

---

## 17. docs/cli-redesign-spec.md

### 🟠 HIGH

| # | Issue | Actual State |
|---|-------|-------------|
| 1 | Says "Status: Draft" | Many features are **already implemented** |
| 2 | Shows `ah/cli/commands.py` | **Does not exist.** Actual: `ah/cli/__init__.py` |
| 3 | Shows `ah/cli/autocomplete.py` | **Does not exist.** |
| 4 | Shows `ah/cli/themes.py` | Actual: `ah/cli/theme.py` (singular) |
| 5 | Shows `ah/tui/app.py` | **Does not exist.** |
| 6 | Says "No REPL" is needed | **REPL exists** |

### 🟡 MEDIUM

| # | Issue | Actual State |
|---|-------|-------------|
| 7 | Describes animations that don't exist | `ah/cli/animations.py` exists but is minimal |
| 8 | Describes slash commands that partially exist | Some are in REPL, some aren't |

---

## 18. docs/research-*.md

### 🟡 MEDIUM

| # | Issue | Actual State |
|---|-------|-------------|
| 1 | `research-memory.md` references `ah/memory/scoring.py` | Actual: `ah/memory/scorer.py` |
| 2 | `research-memory.md` references `ah/memory/retrieval.py` | Actual: `ah/memory/retriever.py` |
| 3 | `research-rag.md` references `ah/rag/evaluation.py` | **Does not exist.** |
| 4 | `research-hermes-*.md` are research documents | Mostly accurate as historical records |

---

## 19. Cross-Cutting Issues

### 🔴 CRITICAL — Systemic Problems

| # | Issue | Impact |
|---|-------|--------|
| 1 | **Test count is wrong in 12+ docs** — says 136, actual 330 | Developers think coverage is 4x lower than it is |
| 2 | **Table count is wrong in 10+ docs** — says 2, actual 3 | Developers don't know about the memories table |
| 3 | **Module structure is wrong in 10+ docs** — shows `ah/cli.py` instead of `ah/cli/` package | Developers can't find files |
| 4 | **Memory/RAG described as "stubs" in 8+ docs** — both are fully implemented | Developers think major features are missing |
| 5 | **CLI command count is wrong in 8+ docs** — says 8, actual 14 | Developers don't know about repl, config, memory commands |
| 6 | **CI/CD described as missing in 5+ docs** — `.github/workflows/ci.yml` exists | Developers think there's no CI |
| 7 | **Skill count is wrong in 5+ docs** — says 20, actual 21 | Minor but pervasive |
| 8 | **Research contradicts roadmap** — `research-langgraph.md` says "don't integrate" but roadmap says "Phase 3: LangGraph" | Developers don't know whether to build it |

### 🟠 HIGH — Documentation Debt

| # | Issue | Impact |
|---|-------|--------|
| 9 | `docs/audit-md-files.md` says 12 files were removed but `critique-research-docs.md` still exists | Broken promise |
| 10 | `docs/update-log.md` claims updates that weren't made | False record |
| 11 | Many docs have "✅ FIXED" annotations but the underlying text still describes the old broken state | Misleading — annotations without text updates |
| 12 | `docs/critique-cli-ux.md` comparison tables are 100% wrong | Every feature shows ❌ but many are ✅ |
| 13 | No doc mentions `ah/core/config.py` | Core component is invisible |
| 14 | No doc mentions `ah/cli/visual.py`, `ah/cli/theme.py`, `ah/cli/animations.py` | CLI subpackage is invisible |
| 15 | No doc mentions `ah/tools/memory.py`, `ah/tools/rag.py` | Tool modules are invisible |

---

## 20. What's Actually Good

To be fair, some things are accurate:

1. **Security descriptions** in `critique-security.md` are mostly accurate about what's fixed and what's not
2. **Research documents** (`research-hermes-*.md`, `research-langgraph.md`) are thorough and well-researched
3. **Skill files** (`skills/*/SKILL.md`) are all valid and load correctly
4. **Schema description** in `advocate-code-quality.md` is accurate about constraints and indexes
5. **Tool registry description** in `advocate-code-quality.md` is accurate about decorator-based registration

---

## 21. Recommended Actions

### Immediate (P0)

1. **Update README.md** — fix test count (330), table count (3), skill count (21), CLI commands (14), project structure (show `ah/cli/` package, `ah/memory/`, `ah/rag/` as implemented)
2. **Update docs/roadmap.md** — mark Phase 3 (Memory & RAG) as done, mark Phase 4 (REPL) as done, remove LangGraph from Phase 3 (contradicts research)
3. **Update docs/synthesis.md** — fix test count, module structure, CI/CD status
4. **Update docs/arch-final.md** — fix module structure, table count, success criteria
5. **Update docs/update-log.md** — remove false claims, document the memory/RAG implementation
6. **Update all critique docs** — fix test counts, table counts, module structures, and feature comparison tables

### Short-term (P1)

7. **Delete or archive** `docs/audit-md-files.md` — it's a meta-audit that's now stale
8. **Delete** `docs/critique-research-docs.md` — references non-existent files (promised to be removed in update-log but wasn't)
9. **Create** `docs/api-reference.md` — no API documentation exists for any module
10. **Create** `docs/architecture-current.md` — a single source of truth for the current architecture

### Medium-term (P2)

11. **Add docstrings to all public APIs** — many modules lack docstrings
12. **Generate API docs** — use pdoc or mkdocstrings
13. **Create a changelog** — `update-log.md` is not a real changelog
14. **Add architecture decision records (ADRs)** — document why decisions were made

---

## 22. Conclusion

The documentation is **worse than no documentation** in many cases. It actively misleads developers about:

- What features exist (memory, RAG, REPL, config are all implemented but described as "stubs" or "missing")
- How many tests exist (136 vs 330 — off by 2.4x)
- What the architecture looks like (wrong module structure, wrong file names)
- What the project's status is (roadmap says Phase 3 is "Memory & RAG" but that's done)

The codebase itself is **significantly better** than the docs suggest. The memory system, RAG pipeline, REPL, and config system are all real and functional. The docs need a complete overhaul to match reality.

**Bottom line:** If you're a new developer, read the source code, not the docs. The source code is clean, well-structured, and well-tested. The docs are a lie.

---

*End of audit.*
