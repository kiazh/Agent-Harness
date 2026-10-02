# Documentation Update Log

**Date:** 2026-10-01  
**Trigger:** Full codebase audit to align documentation with current implementation.

---

## Summary of Changes

### Files Updated (5)

#### 1. `README.md`
- **Updated test count:** 332 -> 436
- **Updated table count:** 3 -> 5 (sessions, context_chunks, memories, pending_memories, user_profiles)
- **Updated CLI command count:** 12 -> 31
- **Updated Architecture diagram** to show 5 tables
- **Updated Features section** to include compression, user profiles, memory approval, PII redaction
- **Updated Project Structure** to match current codebase:
  - Added `ah/core/compression.py`
  - Added `ah/memory/approval.py`, `ah/memory/redaction.py`, `ah/memory/user_profile.py`
  - Updated CLI command list to show all 31 commands
  - Updated schema description to 5 tables

#### 2. `docs/roadmap.md`
- **Updated test count:** 332 -> 436
- **Updated Current Architecture** to match actual codebase:
  - Added `ah/core/compression.py`
  - Added `ah/memory/approval.py`, `ah/memory/redaction.py`, `ah/memory/user_profile.py`
  - Updated schema description to 5 tables
  - Updated CLI description to 31 commands

#### 3. `docs/research-memory.md`
- **Fixed module names:**
  - `ah/memory/scoring.py` -> `ah/memory/scorer.py`
  - `ah/memory/retrieval.py` -> `ah/memory/retriever.py`

#### 4. `docs/research-langgraph.md`
- **Updated test count:** 136 -> 436 (4 occurrences)

#### 5. `docs/research-testing.md`
- **Updated test count:** 136 -> 436

#### 6. `docs/cli-redesign-spec.md`
- **Removed references to deleted modules:**
  - Removed `ah/cli/animations.py`
  - Removed `ah/cli/themes.py`

---

## Key Findings from Codebase Audit

### Modules That Exist (not in old docs)
| Module | Description |
|---|---|
| `ah/core/compression.py` | Context compression with token budget enforcement |
| `ah/memory/approval.py` | MemoryApproval (human-in-the-loop approval gate) |
| `ah/memory/redaction.py` | PII redaction for memory content |
| `ah/memory/user_profile.py` | UserProfileManager (per-user preferences) |

### Database Schema
- **5 tables** (not 3 as in old docs): `sessions`, `context_chunks`, `memories`, `pending_memories`, `user_profiles`
- `context_chunks` has `search_text` column for BM25 full-text search
- `memories` table with importance scoring, access tracking, and embedding support
- `pending_memories` table for approval gate workflow
- `user_profiles` table for per-user preference tracking

### Test Count
- **436 tests** across 10 test files (not 332 as in old docs)
- Test files: test_basic.py, test_chaos.py, test_comprehensive.py, test_compression.py, test_integration.py, test_memory.py, test_memory_approval.py, test_property_based.py, test_rag.py

### CLI Commands
- **31 commands** (not 12 as in old docs):
  - Original: chat, status, sessions, context, skills, doctor, init, version
  - Added: repl, config, config-set, memory-list, memory-search, memory-forget, memory-pending, memory-approve, memory-reject, memory-approve-all, memory-reject-all, memory-stats, user-profile, user-profile-update, user-profile-list, sessions-search, export, fork, delete, compress, learn, curator, hub

---

## Stale References Removed

| Old Reference | Reality |
|---|---|
| 3 database tables | 5 tables (sessions, context_chunks, memories, pending_memories, user_profiles) |
| 332 tests | 436 tests |
| 12 CLI commands | 31 CLI commands |
| `ah/memory/scoring.py` | `ah/memory/scorer.py` |
| `ah/memory/retrieval.py` | `ah/memory/retriever.py` |
| `ah/cli/animations.py` | Does not exist (deleted) |
| `ah/cli/themes.py` | Does not exist (deleted) |

---

*Update completed: 2026-10-01*
