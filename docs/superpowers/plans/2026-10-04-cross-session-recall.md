# Cross-Session Recall Implementation Plan

> **For agentic workers:** Use the Superpowers test-driven-development and
> verification-before-completion workflows for each task. This plan is one
> slice of `../specs/2026-10-04-research-hermes-program-design.md`.

**Goal:** Let an agent find and inspect its own past conversation evidence,
including archived chunks, without an LLM summary or cross-agent disclosure.

**Architecture:** PostgreSQL full-text search of `context_chunks` and
`context_archive`, scoped by `sessions.agent_id`. The gateway supplies the
current session to derive scope. The existing title search stays compatible.
Results contain source and stable chunk IDs so callers can open a window.

**Stack:** Python 3.11, asyncpg, PostgreSQL/pgvector, JSON-RPC gateway,
TypeScript TUI, pytest.

**Progress (2026-10-04):** The PostgreSQL discovery/window service, scoped
gateway methods, agent tools, and `/recall` TUI command are implemented.
Integration tests cover live and archived matches, agent scope, read-only
archive access, and a fresh-process restart. The full LoCoMo evidence-retrieval
benchmark is recorded in [research evaluation](../../research-evaluation.md).
The title-only comparison and generated-answer evaluation remain open.

## Task 1: Transcript discovery service

**Files:** Create `ah/core/session_recall.py`; add `tests/test_session_recall.py`.

- [ ] Write an integration test with two agents, live and archived chunks, an
  unrelated title, and a unique query. Assert only the requesting agent's
  matching transcript chunks appear, with correct source and ordering.
- [ ] Run that test and confirm it fails before the service exists.
- [ ] Add `SessionRecall.discover(agent_id: str, query: str, limit: int = 20)`
  returning typed hits. Use parameterized SQL, rank title and transcript
  matches, and cap result counts. Empty or stopword-only queries return `[]`.
- [ ] Verify archive hits preserve original chunk ID/time and that the query
  does not increment `resurrection_count` (discovery is read-only).
- [ ] Run the focused tests, lint, and format check.

## Task 2: Anchored transcript windows

**Files:** Extend `ah/core/session_recall.py` and `tests/test_session_recall.py`.

- [ ] Write failing tests for a window around an active and an archived hit,
  chronological order, bounds, deleted anchors, and agent mismatch.
- [ ] Implement `window(agent_id, session_id, chunk_id, before=5, after=5)`
  by merging live and archived conversation chunks. Return the original
  payloads and source markers; never mutate archive resurrection counters.
- [ ] Run focused tests and checks.

## Task 3: Gateway and agent-facing recall

**Files:** Extend `ah/gateway/features/sessions.py`,
`ah/gateway/features/__init__.py`, the existing agent tool registration, and
their tests.

- [ ] Write failing gateway tests proving scope comes from the active session,
  not an untrusted client-supplied `agent_id`.
- [ ] Add `session.recall` discovery and `session.recall.window` methods with
  bounded validated input; wire an agent-callable tool using the same service.
- [ ] Test compacted, restarted, and cross-agent cases through the public
  method. Keep `session.search` title behavior unchanged.

## Task 4: TUI and benchmark

**Files:** Extend `ui/src/features/sessions.ts`, its tests, and
`docs/research-evaluation.md`.

- [ ] Add a `/recall <query>` workflow that lists evidence and opens a window.
- [ ] Run TypeScript tests/build and gateway end-to-end tests.
- [ ] Record recall@k, latency, and retrieved bytes on a fixed labeled set;
  compare title-only and transcript search. Document dataset hash and limits.

## Review focus

- Cross-agent and cross-session disclosure.
- Archived payload decoding and ordering when timestamps tie.
- SQL query syntax and stopword handling.
- Large result bounds and zero LLM or provider cost.
- Behavior after compaction, deletion, and process restart.
