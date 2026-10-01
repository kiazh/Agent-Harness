# Audit Report: All Markdown Files in AgentHarness

**Date:** 2026-10-01
**Auditor:** Automated codebase audit
**Total MD files found:** 44
**Scope:** All `.md` files in the project (excluding `.venv/`)

---

## Summary

| Category | Count | Action |
|----------|-------|--------|
| Accurate / Still valid | 21 | Keep as-is |
| Needs updating | 15 | Update content |
| Should be removed | 8 | Delete or archive |

---

## 1. README.md

**Status:** ⚠️ NEEDS UPDATING

**Issues:**
- **Architecture diagram is wrong.** Shows `skills`, `memories`, `agent_messages` tables — schema now has only 2 tables (`sessions`, `context_chunks`).
- **Project structure is outdated.** Missing `ah/core/models.py`, `ah/core/assembler.py`, `ah/core/container.py`. Lists `ah/memory/` and `ah/rag/` as active packages (they are empty stubs).
- **Roadmap is stale.** Phase 3 (LangGraph) still marked "Pending" — correct, but Phase 4 says "Skills & Memory: Partial" — memory is still not implemented. Phase 5 (Heartbeat & RAG) still "Pending" — correct.
- **Technology stack table is incomplete.** Missing `tiktoken` (now used), `PyYAML` (now used), `cachetools` (now used).
- **Tool list is correct.** All 7 tools still exist.
- **CLI commands are correct.** All 8 commands still exist.
- **Missing features not mentioned:** audit logging, rate limiting, input validation, retry logic, streaming, batch insert, DI container, domain models.

**Recommended updates:**
- Update architecture diagram to show 2 tables only
- Update project structure to match current codebase
- Add `models.py`, `assembler.py`, `container.py` to structure
- Add `tiktoken`, `PyYAML`, `cachetools` to tech stack
- Add "Security & Reliability" section mentioning audit logging, rate limiting, input validation
- Update roadmap to reflect current state

---

## 2. docs/advocate-code-quality.md

**Status:** ⚠️ NEEDS UPDATING

**Issues:**
- **References 10-table schema.** Line 17: "skills / memories / agent_messages / subagent_* tables" — schema now has only 2 tables.
- **References old code structure.** Missing `models.py`, `assembler.py`, `container.py`.
- **Claims "no rate limiting or retry logic" (line 208).** Now implemented in `provider.py` (AsyncTokenBucket, `_call_llm_with_retry`).
- **Claims "no streaming" (line 209).** Now implemented (`run_stream()` method).
- **Claims "no structured logging" (line 207).** Now implemented (audit logging in `provider.py`).
- **Claims "no input validation" (line 208).** Now implemented (`_validate_messages`, `_validate_params`).
- **References `_parse_simple_yaml()` (line 172).** Now uses PyYAML (`yaml.safe_load`).
- **References 1570 lines of tests (line 125).** Now 136 tests.
- **References 3619 lines of code (line 234).** Codebase has changed significantly.

**Recommended updates:**
- Update schema references to 2 tables
- Add new modules to architecture description
- Remove "What's Missing" items that have been addressed
- Update test/code metrics
- Update YAML parser reference

---

## 3. docs/advocate-pragmatic.md

**Status:** ⚠️ NEEDS UPDATING

**Issues:**
- **"Duplicate Tool Definitions (Bug)" (line 69-82).** FIXED — `builtins.py` now only has `web_search`, `web_extract`, `search_files`.
- **"No Error Handling in the ReAct Loop" (line 84-90).** FIXED — retry logic with exponential backoff added.
- **"No CI/CD" (line 92-98).** Still valid — no GitHub Actions workflow exists.
- **References 1570 lines of tests (line 20).** Now 136 tests.
- **References 10-table schema (line 21).** Now 2 tables.
- **References old code structure.** Missing new modules.

**Recommended updates:**
- Remove "Duplicate Tool Definitions" section (fixed)
- Remove "No Error Handling" section (fixed)
- Update test count
- Update schema reference
- Keep "No CI/CD" as still valid

---

## 4. docs/arch-final.md

**Status:** ✅ ACCURATE (as historical synthesis)

**Notes:**
- This document synthesizes 10 architecture proposals and guided the refactoring.
- The implementation plan has been largely executed (models.py, assembler.py, container.py created).
- Still valuable as a historical record of the design decisions made.

**Recommended action:** Keep as-is. Add a note at top: "Implementation status: Phases 1-4 complete."

---

## 5-14. docs/arch-proposal-*.md (10 files)

**Status:** 🗑️ SHOULD BE REMOVED (or archive to `docs/archive/`)

**Files:**
- `arch-proposal-ddd.md`
- `arch-proposal-dx.md`
- `arch-proposal-functional.md`
- `arch-proposal-microservices.md`
- `arch-proposal-minimalist.md`
- `arch-proposal-performance.md`
- `arch-proposal-pragmatic.md`
- `arch-proposal-purist.md`
- `arch-proposal-security.md`
- `arch-proposal-testability.md`

**Issues:**
- All are historical proposals from 2026-09-30 that have been superseded by the refactoring.
- The `arch-final.md` synthesis document captures the relevant decisions.
- They reference the old 10-table schema, old code structure, and old architecture.
- Keeping them creates confusion about which architecture is current.

**Recommended action:** Move to `docs/archive/proposals/` or delete. The `arch-final.md` and `synthesis.md` documents preserve the key decisions.

---

## 15. docs/critique-architecture.md

**Status:** ⚠️ NEEDS UPDATING

**Issues:**
- **"God Object" critique (line 7-34).** Partially addressed — `run()` and `run_stream()` still share duplicate code, but the loop is now cleaner with retry logic.
- **"Tangled Module Organization" (line 38-72).** FIXED — `PromptAssembler` extracted to `assembler.py`, `ContextChunk` moved to `models.py`.
- **"Global Singletons" (line 75-104).** Partially addressed — `reset()` methods added, DI container created, but singletons still exist.
- **"Circular Dependency" (line 108-121).** FIXED — `ToolDefinition` moved to `models.py`.
- **"No Domain Model" (line 160-170).** FIXED — typed dataclasses in `models.py`.
- **"Speculative Design — Schema Bloat" (line 174-193).** FIXED — schema reduced to 2 tables.
- **"No Transaction Management" (line 197-210).** Still valid.
- **"Prompt Assembly via String Concatenation" (line 214-231).** Still valid.
- **"Error Handling is Ad-Hoc" (line 234-252).** Partially addressed — retry logic added, but still returns error strings.
- **"Tool Registration via Side Effects" (line 256-273).** Still valid.
- **"Empty Placeholder Modules" (line 276-280).** Still valid — `ah/memory/` and `ah/rag/` still empty.
- **"tools/registry.py Re-export" (line 284-293).** Still valid.

**Recommended action:** Update to reflect which issues have been fixed. Keep as a historical critique with annotations.

---

## 16. docs/critique-cli-ux.md

**Status:** ⚠️ NEEDS UPDATING

**Issues:**
- **"No CLI" critique (line 7-22).** Partially addressed — streaming added via `run_stream()`, but still no interactive REPL.
- **"No session management" (line 23-30).** Still valid — no interactive session switching.
- **"No progress indicators" (line 31-38).** Partially addressed — streaming shows progress.
- **"No interruption" (line 39-45).** Still valid.
- **"No token counters" (line 46-52).** Still valid in CLI (token usage shown in verbose mode).
- **"No markdown rendering" (line 53-59).** Still valid.

**Recommended action:** Update to reflect streaming addition. Keep remaining critiques as valid.

---

## 17. docs/critique-competitive.md

**Status:** ✅ ACCURATE (still valid)

**Notes:**
- Competitive analysis against LangGraph, CrewAI, AutoGen, OpenAI Agents SDK is still accurate.
- AgentHarness still lacks multi-agent, MCP support, observability, etc.
- The "educational project" framing is still appropriate.

**Recommended action:** Keep as-is.

---

## 18. docs/critique-research-docs.md

**Status:** 🗑️ SHOULD BE REMOVED

**Issues:**
- References research documents (`PLAN.md`, `SOURCES.md`, `RESEARCH-AGENDA.md`, `SKILLS.md`, `HANDOFF.md`) that **do not exist** in the project.
- These were external research documents, not part of the codebase.
- The critique is irrelevant to the current project state.

**Recommended action:** Delete. The referenced files don't exist in the repository.

---

## 19. docs/critique-scalability.md

**Status:** ⚠️ NEEDS UPDATING

**Issues:**
- **"Connection Pool: Hardcoded and Starved" (line 7-35).** Still valid — pool still `min_size=2, max_size=10`.
- **"Zero Caching Layer" (line 38-57).** Partially addressed — LRU cache added for sessions, tool definition cache added.
- **"No Horizontal Scaling" (line 60-78).** Still valid.
- **"Single-Threaded Agent Loop" (line 81-106).** Still valid — tool calls still sequential.
- **"No Backpressure" (line 109-133).** Still valid.
- **"Database Bottlenecks" (line 136-157).** Partially addressed — batch insert added, column projection added.
- **"Memory Growth" (line 160-185).** Still valid.
- **"Blocking I/O in Async Context" (line 188-211).** Still valid — `subprocess.run()` still synchronous.
- **"No Streaming" (line 214-230).** FIXED — streaming added.
- **"No Multi-Tenancy" (line 233-250).** Still valid.
- **"No Observability" (line 253-272).** Partially addressed — audit logging added.
- **"Schema Design Issues" (line 275-294).** FIXED — schema reduced to 2 tables.
- **"No Configuration Management" (line 297-316).** Still valid.
- **"No Error Recovery or Retry Logic" (line 319-342).** FIXED — retry logic added.
- **"asyncio.run() Anti-Pattern" (line 345-366).** Still valid.

**Recommended action:** Update to reflect fixes (caching, streaming, retry, schema). Keep remaining critiques.

---

## 20. docs/critique-security.md

**Status:** ⚠️ NEEDS UPDATING

**Issues:**
- **"Command Injection via Terminal Tool" (line 16-96).** FIXED — `shell=False`, allowlist, dangerous character rejection.
- **"Path Traversal in File Tools" (line 99-198).** FIXED — `_resolve_path()` with base directory validation.
- **"No Authentication or Authorization" (line 202-270).** Still valid.
- **"Hardcoded Secrets and Credentials" (line 274-330).** Still valid — `DEFAULT_DSN` still has placeholder.
- **"Unsafe Deserialization with MessagePack" (line 333-377).** Still valid.
- **"No Input Validation" (line 381-450).** Partially addressed — `_validate_messages`, `_validate_params` added to provider; tool validation added to registry.
- **"No Rate Limiting" (line 454-528).** FIXED — `AsyncTokenBucket` added.
- **"SSRF in Web Tools" (line 532-625).** FIXED — `_is_safe_url()` validation added.
- **"No Audit Logging" (line 629-691).** FIXED — `audit_log()` added throughout.
- **"Database Security Issues" (line 695-741).** Still valid.
- **"No Sandboxing or Isolation" (line 745-792).** Still valid.
- **"LLM Prompt Injection" (line 796-868).** Still valid.

**Recommended action:** Update to reflect security fixes. Keep remaining valid critiques.

---

## 21. docs/critique-testing.md

**Status:** ⚠️ NEEDS UPDATING

**Issues:**
- **"136 tests pass" (line 3).** Accurate.
- **"Zero integration tests against a real database" (line 11).** Still valid.
- **"Zero tests for actual ReAct loop with real tools" (line 12).** Still valid.
- **"Zero tests for web_search or web_extract" (line 13).** Still valid.
- **"test_assemble_token_budget — False Assurance" (line 51-64).** Still valid.
- **"test_agent_run_with_tool_calls — Doesn't Test Tool Execution" (line 66-74).** Still valid.
- **"test_session_crud — Skipped in Practice" (line 76-87).** Still valid.
- **"test_status_command_no_db — Asserts Failure as Success" (line 89-97).** Still valid.
- **"Duplicate Tool Definitions" (line 328-346).** FIXED — duplicates removed.
- **"Inconsistent Error Handling" (line 348-357).** Still valid.
- **"No Input Validation" (line 359-373).** Partially addressed — validation added to registry.
- **"No Timeout on Tool Execution" (line 375-382).** Still valid.
- **"No Retry Logic" (line 384-386).** FIXED — retry added.
- **"No Logging" (line 388-394).** Partially addressed — audit logging added.
- **"No Metrics" (line 396-404).** Still valid.

**Recommended action:** Update to reflect fixes. Keep remaining valid critiques.

---

## 22. docs/critique-token-efficiency.md

**Status:** ⚠️ NEEDS UPDATING

**Issues:**
- **"Token Counting: len(text) // 4" (line 15-71).** FIXED — `tiktoken` added with `cl100k_base` encoding.
- **"Prompt Assembler Is Naive" (line 75-149).** Partially addressed — assembler extracted, but still uses string concatenation.
- **"Context Budget System Doesn't Actually Save Tokens" (line 152-217).** Still valid.
- **"MessagePack Storage Reduction Is Misleading" (line 220-250).** Still valid.
- **"Tool Definitions Sent Every Iteration" (line 255-262).** Still valid.
- **"No Streaming or Early Exit" (line 264-270).** Partially addressed — streaming added.
- **"Embedding Search Is Never Used" (line 272-282).** Still valid — `search_by_embedding` never called in agent loop.
- **"Recent Chunks Fetched But Not Used" (line 284-293).** Still valid — fetches 5, uses 3.

**Recommended action:** Update to reflect tiktoken addition. Keep remaining valid critiques.

---

## 23. docs/synthesis.md

**Status:** ⚠️ NEEDS UPDATING

**Issues:**
- **"4 CRITICAL security vulnerabilities" (line 13).** Partially fixed — command injection, path traversal, SSRF fixed; auth still missing.
- **"1 God Object" (line 14).** Still valid.
- **"7 global singletons" (line 15).** Still valid (5 main + 2 re-exports).
- **"Duplicate tool registrations" (line 16).** FIXED.
- **"Zero streaming" (line 17).** FIXED.
- **"Token counting off by 2-4x" (line 18).** FIXED — tiktoken added.
- **"No CI/CD, no error handling, no caching, no rate limiting" (line 19).** Partially fixed — error handling, caching, rate limiting added; CI/CD still missing.
- **"8 unused database tables" (line 20).** FIXED — schema reduced to 2 tables.
- **"CLI scores 0/10" (line 21).** Partially improved — streaming added.

**Recommended action:** Update to reflect all fixes. Keep as a historical synthesis with current status annotations.

---

## 24-43. skills/*/SKILL.md (20 files)

**Status:** ✅ ACCURATE (all 20 skill files)

**Files:**
1. `skills/alembic-migrations/SKILL.md`
2. `skills/cli-tui/SKILL.md`
3. `skills/context-runtime/SKILL.md`
4. `skills/cost-optimization/SKILL.md`
5. `skills/deployment-devops/SKILL.md`
6. `skills/docs-communication/SKILL.md`
7. `skills/embedding-finetuning/SKILL.md`
8. `skills/heartbeat-system/SKILL.md`
9. `skills/langgraph/SKILL.md`
10. `skills/llm-provider/SKILL.md`
11. `skills/memory-management/SKILL.md`
12. `skills/multi-agent/SKILL.md`
13. `skills/postgresql-pgvector/SKILL.md`
14. `skills/prompt-engineering/SKILL.md`
15. `skills/rag-pipeline/SKILL.md`
16. `skills/research-writing/SKILL.md`
17. `skills/security-sandboxing/SKILL.md`
18. `skills/skills-system/SKILL.md`
19. `skills/testing-qa/SKILL.md`
20. `skills/web-search/SKILL.md`
21. `skills/yaml-agent-config/SKILL.md`

**Notes:**
- All skill files follow the SKILL.md format with YAML frontmatter (name, description, triggers, version).
- They are loaded by `ah/skills/registry.py` and displayed by `ah skills` command.
- Content is still relevant as learning/practice guides.
- Some reference future features (LangGraph, multi-agent, heartbeat) that are not yet implemented — this is appropriate for skill definitions.

**Recommended action:** Keep all as-is.

---

## 44. .pytest_cache/README.md

**Status:** 🗑️ SHOULD BE REMOVED

**Issues:**
- Auto-generated by pytest.
- Should be in `.gitignore`.
- Not part of the project documentation.

**Recommended action:** Delete and add `.pytest_cache/` to `.gitignore`.

---

## Summary Table

| # | File | Status | Action |
|---|------|--------|--------|
| 1 | `README.md` | ⚠️ Update | Fix architecture diagram, project structure, tech stack, roadmap |
| 2 | `docs/advocate-code-quality.md` | ⚠️ Update | Update schema refs, add new modules, update metrics |
| 3 | `docs/advocate-pragmatic.md` | ⚠️ Update | Remove fixed issues, update metrics |
| 4 | `docs/arch-final.md` | ✅ Keep | Add implementation status note |
| 5-14 | `docs/arch-proposal-*.md` (10) | 🗑️ Remove | Archive or delete — superseded by refactoring |
| 15 | `docs/critique-architecture.md` | ⚠️ Update | Annotate which issues are fixed |
| 16 | `docs/critique-cli-ux.md` | ⚠️ Update | Note streaming addition |
| 17 | `docs/critique-competitive.md` | ✅ Keep | Still valid |
| 18 | `docs/critique-research-docs.md` | 🗑️ Remove | References non-existent files |
| 19 | `docs/critique-scalability.md` | ⚠️ Update | Note caching, streaming, retry fixes |
| 20 | `docs/critique-security.md` | ⚠️ Update | Note security fixes (SSRF, path traversal, etc.) |
| 21 | `docs/critique-testing.md` | ⚠️ Update | Note fixed issues |
| 22 | `docs/critique-token-efficiency.md` | ⚠️ Update | Note tiktoken addition |
| 23 | `docs/synthesis.md` | ⚠️ Update | Update priority matrix with fixes |
| 24-43 | `skills/*/SKILL.md` (20) | ✅ Keep | All accurate |
| 44 | `.pytest_cache/README.md` | 🗑️ Remove | Auto-generated, not project docs |

---

## Recommended Actions (Priority Order)

1. **Delete `.pytest_cache/README.md`** and add to `.gitignore`
2. **Delete `docs/critique-research-docs.md`** (references non-existent files)
3. **Archive `docs/arch-proposal-*.md`** (10 files) to `docs/archive/proposals/`
4. **Update `README.md`** with current architecture, structure, and tech stack
5. **Update `docs/synthesis.md`** with current implementation status
6. **Update critique docs** to reflect fixed issues (annotate with ✅ FIXED or ⚠️ PARTIAL)
7. **Add implementation status note** to `docs/arch-final.md`

---

*End of audit report.*
