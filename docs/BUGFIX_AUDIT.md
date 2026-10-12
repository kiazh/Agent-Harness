# Post-OpenCode bug-fix audit

Date: 2026-10-10. Reviewed OpenCode commits `e7fe0e6`, `e9cd169`, and `639e833`, plus the existing uncommitted harness changes. The user authorized fixing remaining bugs through verification. Existing work is preserved; this audit does not restore the removed ECC bundle or add the planned MCP feature.

## Fixed behavior

- Configuration loading publishes one shared object before persona validation imports the memory/provider packages. Provider settings and durable usage budgets now observe the same live configuration.
- Persistent config saves replace files atomically and retain the old defaults if saving fails. Failed model/provider saves also restore the gateway's live route.
- Persona forms save and count expanded paste contents. Each dialog owns its overlay and restores the interrupted dialog's focus; cancellation/exit settles promises. Form height preserves the editing cursor and controls in short terminals. Selection fill supports both truecolor and indexed ANSI colors.
- Normal and streaming providers receive the configured completion limit. Reservations cover Anthropic's effective thinking output limit. Cache identities include the effective provider payload, including output limit, temperature, and reasoning effort.
- Missing vendor usage stays explicitly estimated, includes tool schemas and generated arguments, and retains conservative durable reservations. Special-token spellings in user text do not abort streaming.
- A tool batch uses one assistant message containing the complete call list. All fresh results survive the next model read; stale condensation is idempotent. Durable storage documentation accurately describes result previews.
- Delegation retains parent mode, isolation, and intersected tool restrictions, including when a child is resumed directly. Parent cancellation drains child tasks. Child pauses reach parent responses and RPC/SSE results with their continuation identity; sequential RPC returns early pauses instead of raising a strict-zip error.
- Child usage counts against inherited session and agent budgets under shared advisory locks. Immutable ancestry preserves these limits after an ancestor is deleted; forks read fresh state, and migration backfills historical child spend.
- Full-mode children inherit only a live matching parent grant. The persisted grant source is checked on subsequent actions and resumes, so revoking or expiring the parent's grant blocks the child.
- Failed and timed-out children return their charged usage, including descendants and conservative reservations, to the parent turn limit. The orchestrator owns the tool deadline and drains cancellation before returning its receipt.
- If production usage accounting is unavailable, a dedicated error stops both normal and streaming parent turns before another provider call. It drains sibling tools and flushes pending interruption history instead of silently continuing with zero usage.
- Conversational and informational mentions of technologies do not activate implementation workflows. Explicit actions following a question or acknowledgement still activate relevant skills.
- Memory scoring accepts a fixed evaluation time, and breakdown totals use one recency calculation. The identity example is tracked; the optional graph benchmark counts checkpoints through the saver API rather than querying an external SQL schema directly.
- Test configuration is isolated from developer settings and secrets. Existing test doubles are updated for the session schema. Repository lint debt is cleaned without dropping assertions or test behavior.

## Runtime upgrade

Gateway initialization, CLI chat, and shared runtime startup apply required additive migrations to existing native databases. The migration is transactional, serialized across concurrent startup, and idempotent; regression coverage preserves existing session data and historical usage. Startup reports an unavailable database if required migration fails. Fresh database setup still uses `machina db init` and the full schema loader.

## Verification

Final combined verification completed after all source edits were frozen:

| Check | Executed result |
| --- | --- |
| `.venv/bin/python -m pytest -q --tb=short -rs` | **1,927 passed, 5 skipped, no failures**, 58.53 seconds |
| Full `npm run check`, run by `tests/test_personality_growth.py::test_ui_checks_with_real_gateway_restart` | **127 passed, zero skipped**; TypeScript checking and ESLint passed |
| `.venv/bin/ruff check ah tests tools/vendor_ecc.py` | Passed |
| `.venv/bin/ruff format --check ah tests tools/vendor_ecc.py` | Passed; 281 files already formatted |
| `git diff --check` | Passed |
| `.venv/bin/python -m hatchling build -t wheel` | Passed; wheel 707,086 bytes |
| Wheel archive inspection | All three SQL schema/migration assets and `context_windows.py` included; no bytecode/cache files; 63 total bundled `SKILL.md` files, including the unchanged 42 curated ECC skills |

The five backend skips are one Windows-only path-semantics test on macOS and four optional workflow-trial tests requiring uninstalled LangGraph dependencies. No native database or TUI gateway test was skipped. The optional graph integration itself was not executed; its public checkpoint-count adapter has passing native regression coverage.

The initial combined run after the production fixes exposed one outdated API lifespan test double. It now models and verifies schema migration before audit/scheduler startup. The fresh complete run above includes that correction.

The backend suite covers native agent loops, permission ownership/resume, schema conformance, concurrency, personality growth, and the Node-to-Python gateway restart path. The full TUI check includes type checking, terminal input/render tests, feature tests, and lint. Real gateway tests use the dedicated test database and deterministic inputs; they do not spend a developer's model API keys.

External model behavior, Docker daemon operation, and optional third-party research integrations require their corresponding services. Passing local tests cannot establish that every external model follows every skill instruction.

## Continuation

All bugs found in this audit have fixes and the available native checks pass. Changes are left uncommitted for review; no commit, push, or database-history rewrite was requested. Preserve the uncommitted implementation and new test/migration files. The broader TUI and MCP roadmap remains in [the handoff](HANDOFF.md); MCP support was not added during this bug-fix pass.
