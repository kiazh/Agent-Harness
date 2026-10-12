# AgentHarness

A self-hosted AI agent with a terminal UI, PostgreSQL-backed conversations and memory, tools, delegation, and runtime learning.

[![CI](https://github.com/kiazh/Agent-Harness/actions/workflows/ci.yml/badge.svg)](https://github.com/kiazh/Agent-Harness/actions/workflows/ci.yml)
[![MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Version 0.2.0.** This is a development release candidate. Local verification is recorded in [release readiness](docs/verification/RELEASE_READINESS.md); production deployment gates remain open. No live-provider quality or performance improvement is claimed from fixture tests.

## Get started

Requirements: Python **3.11+**, Node.js **22.19+** for the TUI, and PostgreSQL **16+** with **pgvector**. Configure a supported provider key, or an accessible Ollama server for local models.

```bash
git clone https://github.com/kiazh/Agent-Harness.git
cd Agent-Harness
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
npm ci --ignore-scripts --prefix ui
ah setup
```

`ah setup` prompts for configuration. Supply your PostgreSQL URL and provider credentials; review `.env` before initializing your chosen database. Secrets belong in `.env` or a supported secret backend, never in committed files or non-secret YAML.

```bash
ah doctor
ah init
ah                         # interactive terminal UI
ah chat "Explain this project"  # one-shot CLI
```

On Windows, activate with `.venv\Scripts\Activate.ps1` in PowerShell. Shell tools depend on the commands installed on your host.

The [installer](install.sh) can provision a local database through Docker or Homebrew where available. Inspect it before execution; it may install dependencies and create local configuration. For an existing checkout:

```bash
bash install.sh
```

A Python wheel installs the backend, bundled skills and subagents. It does **not** bundle Node dependencies or the terminal UI: use the source checkout above, or set `AH_UI_DIR` to its `ui/` directory and run `npm ci --ignore-scripts` there.

## Terminal UI

The fullscreen interface has a scrolling conversation, framed tool results, a docked composer and approval controls. Themes color the full alternate screen; the normal terminal is restored on exit. Motion can be disabled with `AH_UI_REDUCED_MOTION=1`.

Type `/` for commands. Management commands open pickers or forms; `?` on empty input and F1 show shortcuts. Enter selects, Escape cancels, and Space toggles checklist entries.

| Command | Behavior |
| --- | --- |
| `/models`, `/provider` | Choose the session model/provider |
| `/default-model` | Save the default model for future sessions |
| `/keys` | Manage masked provider keys |
| `/persona` | Add/edit or reset the default persona |
| `/growth` | Inspect learning, teach, rate, forget or restore a revision |
| `/delegate` | Pick an agent, then enter its task |
| `/sessions` | Browse and resume conversations |
| `/delete` | Select sessions with checkboxes, then confirm deletion |
| `/theme` | Preview and save a theme; Escape restores the previous theme |
| `/output` | Inspect retained tool output without rerunning it |
| `/mcp` | Add servers, review connection launches and inspect/disconnect them |
| `/mode`, `/approvals` | Manage execution mode and pending approvals |

## What is included

- Streaming agent execution through the CLI, TUI/gateway and HTTP API, with cancellation, session ownership and bounded cleanup.
- Persistent conversations, context compaction, memory search and document retrieval. Semantic retrieval needs embedding configuration; keyword retrieval is available without it.
- File, shell, web, memory and document tools, subject to the permission broker and the selected execution backend.
- **42 curated ECC skills**, bundled offline and selected by task evidence. Relevant excerpts are disclosed progressively rather than injecting the entire library into each prompt.
- **71 ECC subagents** plus the existing `harness`, `researcher` and `coder` roles. Only the selected role's instructions enter its delegated context. [Integration details and tool limitations](docs/ECC_AGENTS.md).
- Scheduling, usage accounting and configurable session/agent budgets.
- Default-on persona growth and runtime adaptation, with durable notes, human feedback, revision history and restoration.

### Persona, growth and RSI

`/persona` saves the requested tone and character as the installation default. Learned notes belong to the agent's profile and persist across sessions. `/growth` exposes evidence and controls; the model can save durable preferences using `learn_personality`.

“RSI” here means adapting runtime preferences, retrieval, strategies and capability ranking from execution evidence and human ratings. It **does not train model weights or autonomously rewrite the harness**. Agent-written persona changes undergo a human-rated trial; direct human teaching establishes a stable revision. Automatic outcomes cannot promote a persona trial. See [personality growth](docs/PERSONALITY_GROWTH.md).

Offline policy-training and evaluation modules are research utilities, separate from normal chat. Their presence does not establish improved model quality.

### Permissions and shell execution

The default mode is `ask`; `workspace`, `sandbox` and `full` are also available. Routine permitted project operations proceed without repeated prompts. Sensitive actions, including credential access, elevation and destructive system operations, still require exact approval. Changing mode does not grant blanket sensitive consent.

Shell syntax such as `&&`, `;` and pipes is supported through the shared shell parser. Approval classification examines the command stages; syntax alone is neither an unconditional refusal nor a bypass. Tools remain subject to path, backend and execution limits. Host execution is not an OS sandbox. Read [SECURITY.md](SECURITY.md) before using elevated modes, plugins or scheduled scripts.

### Context and token limits

Prompt capacity follows the selected model/provider's context-window metadata or a conservative fallback, reserving space for output. There is no default cumulative turn-token cutoff; usage is still recorded. Optional session/agent usage limits and provider output limits are separate. The legacy `context_budget` setting is retained for compatibility and does not set a turn spend cap. A million-token window applies only when the selected serving reports that capacity.

### MCP scope

MCP is included in the default Python installation. `/mcp` opens Add and server actions. Configure a local executable with a separate JSON argument array, or an HTTP endpoint. Adding a server saves configuration without launching it. Connect shows the exact launch preview and requires confirmation; a changed configuration needs a fresh preview.

Configuration persists in `~/.agent-harness/mcp.json`, with installation-local locking, atomic replacement and revisions. Connections remain worker-local and do not automatically restart. To share this inventory across workers, use the same administrator-controlled home directory. Values belong in the existing secret backend; use `secret_refs` to pass named environment secrets to stdio servers, never literal tokens in arguments.

The agent uses `mcp_list`, `mcp_describe` and `mcp_call` after connection. Catalogs are paged and schemas loaded individually. Remote calls require exact approval in every mode unless you explicitly configure `trusted_read` **and** an allowlist of verified read tools. Server descriptions and annotations never grant consent. Schema/configuration/argument changes invalidate approval; uncertain calls are not retried.

Stdio runs on the host and is refused in sandbox mode. HTTP redirects and environment proxy inheritance are disabled; private hosts require explicit local access, and metadata addresses remain forbidden. OAuth and HTTP credential-profile authentication are **not implemented**; such connectors report `auth_required`. Resources, prompts and server-initiated sampling/elicitation/roots are not exposed. These network checks do not establish complete DNS-rebinding protection. [Setup and current evidence](docs/MCP.md).

## Configuration and HTTP

Non-secret defaults live in `~/.agent-harness/config.yaml`; environment overrides use `AGENT_HARNESS_<KEY>`. Session overrides take precedence where supported. `.env` fills missing environment values. `AH_ENV_FILE` selects an alternate file; [`.env.example`](.env.example) lists common settings.

Important settings include `DATABASE_URL`, `AGENT_HARNESS_PROVIDER`, `AGENT_HARNESS_MODEL`, `AGENT_HARNESS_PERSONA_DESCRIPTION`, `AGENT_HARNESS_EXECUTION_MODE`, and the `AGENT_HARNESS_USAGE_*` budgets (`0` means unlimited). Learning flags are `AGENT_HARNESS_MEMORY_ENABLED`, `AGENT_HARNESS_PERSONALITY_GROWTH_ENABLED` and `AGENT_HARNESS_RSI_ENABLED`.

```bash
# Configure AGENT_HARNESS_API_KEY in your environment or .env first.
ah serve --host 127.0.0.1 --port 8000
```

The HTTP API provides sessions, SSE prompts, memory, documents, jobs and approval operations. Requests use `Authorization: Bearer …` or `X-API-Key`; public `/health` and `/ready` expose limited health information. Missing API authentication configuration fails closed. The [wire contract](communications/ui-gateway-protocol.md) describes gateway events and approval semantics.

### Docker backend

```bash
cp .env.example .env
# Replace example keys with your own values before starting.
docker compose up --build
```

Compose runs the **HTTP backend**, not the terminal UI. It provisions an isolated database volume, leaves the database port unpublished and binds the API to `127.0.0.1:8000`. Review credentials, volumes and the [operations guide](OPERATIONS.md) before deployment. Docker execution has not been verified on this review machine; CI builds and smoke-checks the image.

## Development and verification

```bash
python -m pip install -e '.[dev,connectors,workflow-trial]'
npm ci --ignore-scripts --prefix ui
# Point this only at a disposable database, never your application database.
export AGENT_HARNESS_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/agentharness_test
python -m pytest -q --tb=short -p no:cacheprovider
python -m ruff check ah/ tools/
python -m ruff format --check ah/ tools/
python -m bandit -r ah/ -ll -x ah/cli/
npm --prefix ui run check
```

Database tests require the dedicated DSN. Real gateway UI tests additionally require `AH_TEST_PYTHON` and their test environment; the backend's real-gateway wrapper configures those automatically. Missing prerequisites produce explicit skips, not verification evidence. Do not run concurrent backend suites against the same test database.

Build and inspect artifacts:

```bash
python -m pip install hatchling
python -m hatchling build
python tools/check_release_artifacts.py dist/*
```

CI covers Python checks, coverage, optional connector transport tests, the UI and distributable artifacts. The [current evidence](docs/verification/RELEASE_READINESS.md) distinguishes local checks from remote CI and deployment gates. Historical test counts and benchmarks in older documents describe their recorded checkout only.

## Documentation

- [Operations, migrations, backup and rollback](OPERATIONS.md)
- [Security model and limitations](SECURITY.md)
- [Release notes](CHANGELOG.md) and [release checklist](docs/verification/RELEASE_READINESS.md)
- [Feature implementation ledger](FEATURE_LEDGER.md)
- [Successor handoff](docs/HANDOFF.md)
- [Production completion gates](docs/plans/PRODUCTION_COMPLETION_PROGRESS.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)

## License

[MIT](LICENSE). Bundled ECC material retains its upstream MIT licenses and revision/hash manifests. See [third-party notices](THIRD_PARTY_NOTICES.md).
