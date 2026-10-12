# 0.2.0 release-candidate review

Date: 2026-10-12. Starting checkout: `main`, HEAD `54d79d5`. This document describes local preparation, not a published GitHub release or production deployment.

## Scope

Source checkout: CLI, terminal UI, gateway/HTTP, persistence, tools, runtime learning, curated skills, default ECC subagents and broker-integrated MCP tools. Python wheels contain the backend and bundled resources; TUI sources and Node dependencies require a checkout/`AH_UI_DIR`. Docker is the HTTP backend.

MCP now includes installation-local saved configuration, human-reviewed launch, discovery and native broker-bound calls. OAuth/HTTP credential profiles, resources/prompts, server callbacks and complete DNS-rebinding protections remain outside the release scope. See [MCP setup](../MCP.md).

## Repository preparation

- Replaced the long, stale README with install instructions, supported behavior and explicit limitations.
- Removed the three tracked `.venv.broken.bak` artifacts, retaining unrelated local environment files. Consolidated duplicated ignore rules; excluded Node dependencies and backup environments from Git/build contexts.
- Reproduced a source-package defect: 1,239 installed `node_modules` files entered the source archive. Build exclusions now prevent this; the source check verifies absence of local dependencies/state.
- Consolidated duplicate PR checks into the CI workflow with its dedicated pgvector service. Added default/optional connector coverage, UI lint, distribution checks and a Docker build/CLI smoke job. These hosted jobs have not been run from this working tree.
- Added the Python resolver lockfile (`uv.lock`; 96 packages), offline freshness validation, release notes and ECC subagent license/provenance notices. Existing pip setup uses declared constraints; the lockfile is available for uv-based reproducible environments.
- Preserved historical handoff, ledger and reviews. Their dated counts are historical evidence, not the present release result.

## Verification commands

All database checks used only the dedicated `agentharness_test` database. The local Python subprocess environment redirected HOME/config and removed live credentials through a disposable bootstrap outside the repository.

```sh
AGENT_HARNESS_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/agentharness_test \
  PYTHONPATH=/tmp/agentharness-ui-reference-runtime:/home/user/Agent-Harness1 \
  .venv/bin/python -m pytest -q --tb=short -p no:cacheprovider \
  --cov=ah --cov-report=term --cov-fail-under=60
npm --prefix ui run check
.venv/bin/python -m ruff check ah/ tools/
.venv/bin/python -m ruff format --check ah/ tools/
.venv/bin/python -m bandit -r ah/ tools/ -ll -x ah/cli/
uv lock --check --offline
.venv/bin/python -m hatchling build -d /tmp/ah-release-dist
.venv/bin/python tools/check_release_artifacts.py /tmp/ah-release-dist/*
git diff --check
```

The `/tmp` bootstrap is a local safety wrapper, not a required project dependency. In CI the dedicated service and disposable runner provide isolation. Real gateway UI tests run inside the backend wrapper with the dedicated DSN and interpreter; standalone UI checks have five environment-gated skips.

## Regression and review evidence

Initial native MCP tests failed because no tools/execution adapter existed. The fixture call now goes through an actual agent tool block, pauses before dispatch, consumes human approval and returns an SDK result. Additional tests cover timeout/unknown outcomes, truncation, server errors, actual elicitation refusal, stale configuration/schema/arguments, absence of a broker snapshot and explicit trusted-read policy.

Independent review reproduced an unapproved-config launch race and cwd/executable mismatches. Launch now holds configuration ownership, uses the approved snapshot and dispatches the reviewed absolute launcher and effective directory. Resolving a venv Python symlink to its real binary broke SDK availability; preserving the absolute launcher fixes that tested regression. Concurrency and configuration waits are bounded before dispatch; dispatched uncertainty remains typed through agent, gateway/HTTP and TUI events.

The first integrated full run had three regressions: obsolete gateway method inventory, an obsolete “no management methods” assertion, and the real UI wrapper's empty-state expectation. All were updated to the new contract; focused checks passed. No pre-existing failure claim or clean-HEAD comparison is needed for those intentional interface changes.

## Final local results

Final isolated backend run: **2,237 passed, 5 skipped, 0 failed in 127.22s**;
coverage **82.26%**, exceeding the 60% gate. This includes the real gateway UI
wrapper running all **295 UI tests without UI skips**. Standalone UI check:
**290 passed, 5 environment-gated skips**, with typecheck and lint passing.
Ruff check passed; all 152 backend/tool files passed format verification.
Bandit reported **0 medium, 0 high and 67 low** findings. Offline lockfile
freshness and `git diff --check` passed.

Wheel and source-distribution checks passed: isolated wheel imports, all 71 ECC
agents, bundled resource hashes, and absence of installed dependencies or local
state in the source archive. SDK fixture coverage includes stdio and loopback
HTTP, fail-closed elicitation and repeated same-task/cross-task disconnects with
no fixture process leaks. Native execution also verifies authenticated gateway
configuration/preview/connect/discovery/disconnect/reload and broker-approved
agent calls. These are fixture-based integration results, not live vendor or
paid-model compatibility claims.

## Remaining publication and deployment gates

- Run the prepared GitHub CI jobs, including Linux Docker build/Compose validation. Docker is unavailable on this local machine.
- Validate fresh installs on the intended OS/Python/Node matrix; local verification used macOS, Python 3.14 and Node 26. CI targets Python 3.11/Node 24, but its pending run is not evidence yet. Windows locking has simulated branch tests, not a new real Windows run.
- Exercise explicitly authorized live providers and the specific third-party MCP services you intend to support. Synthetic SDK fixtures do not establish vendor compatibility or quality gains.
- Complete authorized staging, backup/restore, rollback, monitoring/alerts and operational budgets in [production completion](../archive/plans/PRODUCTION_COMPLETION_PROGRESS.md).
- Review the candidate diff, commit/tag and publish through the repository owner's release process. No commit, push, git configuration change, publication or deployment was performed here.

Passing local checks prepares a candidate; it does not close the production gates or authorize publishing v1.0.0.
