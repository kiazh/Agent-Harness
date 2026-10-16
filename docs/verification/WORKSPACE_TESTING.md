# Workspace feature — testing runbook

Commands for every evidence layer. Counts here are historical; re-run for current numbers.
Never copy a count forward without running the command.

## Layer 1 — UI unit/widget/model (fast, no gateway)

```bash
cd /Users/shadow/machina/ui
npm run check          # tsc --noEmit + node --test test/*.test.ts + eslint
npm test               # tests only
node --test test/response-layout.test.ts        # single file
```

Baseline at T7 commit 9427928: 602 tests → 596 pass, 6 skip, 0 fail. Skips are
gateway-e2e tests that require `MACHINA_TEST_PYTHON` + `MACHINA_TEST_DATABASE_URL`.

Isolated-worktree pattern (verify committed state while other work is in flight):

```bash
git -C /Users/shadow/machina worktree add /private/tmp/opencode/machina-verify HEAD
ln -sfn /Users/shadow/machina/ui/node_modules /private/tmp/opencode/machina-verify/ui/node_modules
cd /private/tmp/opencode/machina-verify/ui && npm run check
git -C /Users/shadow/machina worktree remove --force /private/tmp/opencode/machina-verify
```

## Layer 2 — Python backend tests (workspace + LSP + features)

Tests live in the sibling `/Users/shadow/Machina-DevTools` checkout (its `machina/`
symlink points at this repo; the symlink shadows `PYTHONPATH`, so runs always exercise
the live checkout — run when agents are idle).

```bash
cd /Users/shadow/Machina-DevTools
MACHINA_REPO=/Users/shadow/machina PYTHON=/Users/shadow/machina/.venv/bin/python \
  ./run-tests.sh tests/test_workspace_service.py tests/test_workspace_gateway.py \
                 tests/test_lsp_transport.py tests/test_lsp_lifecycle.py \
                 tests/test_features_split.py -q
```

Historical: 109 passed in ~5 s (54 workspace + 46 LSP + 9 pins) at T7.

DB-dependent suites (`tests/test_gateway.py` and friends) need
`MACHINA_TEST_DATABASE_URL`; without it they fail identically at baseline — that is
environmental, not a regression (verified at b0db077).

## Layer 3 — Real language servers (plan §24E)

Provisioned as pinned devDependencies (already installed in `ui/node_modules/.bin`):

- `pyright-langserver@1.1.414`
- `typescript-language-server@6.0.2`

```bash
cd /Users/shadow/Machina-DevTools
MACHINA_REPO=/Users/shadow/machina PYTHON=/Users/shadow/machina/.venv/bin/python \
  ./run-tests.sh tests/test_lsp_integration_real.py -q -s
```

These tests must RUN (not skip) wherever the binaries resolve; a skip is not a pass.
Node ≥ 22.19 must be on PATH. First pyright start can take several seconds.

## Layer 4 — Lint/format/security gates (CI parity)

```bash
cd /Users/shadow/machina
.venv/bin/python -m ruff check machina/ tools/
.venv/bin/python -m ruff format --check machina/ tools/   # see note: pre-existing debt
.venv/bin/python -m bandit -r machina/ -ll -x machina/cli/
```

Note: 13 pre-existing unformatted core files predate this feature and are intentionally
left untouched; every file added/modified by this feature is format-clean.

## Layer 5 — Native/PTY captures (plan §24F)

`ui/test/support/capture-reference.ts` dumps frames for skin × size through the real
renderer with a stub terminal (not a PTY). Run:

```bash
cd /Users/shadow/machina/ui && node test/support/capture-reference.ts
```

For the workspace shell, the scripted-App path is `referenceFixture(columns, rows)` in
`ui/test/support/reference.ts`; layout/cursor/focus assertions inspect
`tui.getScreenLines()` after `renderNow(true)`.

## Ordering rules

1. Never run Python suites mid-edit by a workstream that owns `machina/workspace/*`.
2. Never treat a skip as a pass; record skips in the progress doc with the reason.
3. Prefer focused file lists while a wave is in flight; full sweeps at wave end.
4. Every wave-end verification goes into `docs/verification/WORKSPACE_PROGRESS.md`
   with exact commands and counts.
