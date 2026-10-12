# Machina

A self-hosted AI agent with a terminal UI, PostgreSQL-backed conversations and memory, tools, delegation, and runtime learning.

[![CI](https://github.com/kiazh/Machina/actions/workflows/ci.yml/badge.svg)](https://github.com/kiazh/Machina/actions/workflows/ci.yml)
[![MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Version 0.2.0.** This is a development release candidate. Local verification is recorded in [release readiness](docs/verification/RELEASE_READINESS.md); production deployment gates remain open. No live-provider quality or performance improvement is claimed from fixture tests.

## Contents

- [Install and first launch](#get-started)
- [Terminal UI](#terminal-ui)
- [Features, persona and runtime learning](#what-is-included)
- [Technology stack](#technology-stack)
- [Architecture and request lifecycle](#architecture)
- [Providers and local models](#providers-and-local-models)
- [Configuration](#configuration-and-http)
- [HTTP API](#http-api-reference)
- [Docker backend](#docker-backend)
- [Research utilities](#research-utilities)
- [Development and verification](#development-and-verification)
- [Project structure](#project-structure)
- [Troubleshooting](#troubleshooting)
- [Documentation](#documentation)

## Get started

### One-command installation

```bash
curl -fsSL https://raw.githubusercontent.com/kiazh/Machina/main/install.sh | bash
```

The [installer](install.sh) clones into `./machina`, or reuses a checkout,
creates a Python virtual environment, installs the backend and TUI dependencies,
creates/fills `.env`, generates API/provenance keys, and automatically runs
`machina init` to initialize the database. If initialization fails, it attempts local
PostgreSQL provisioning through Docker, then Homebrew on macOS where available.
It installs an `machina`
launcher and adds it to your shell PATH. It preserves custom database URLs.

Have Python 3.11+, Git, curl and Node.js 22.19+/npm installed first. Database
provisioning needs running Docker or supported Homebrew tooling; without either,
provide an existing PostgreSQL database. Missing Node skips the TUI dependencies;
missing provider credentials lets installation finish but requires setup before
cloud chat. The installer prints any remaining steps. Review the script before
running it: it installs dependencies, may start a database, and normally updates
your shell startup file. The URL installs the published `main` checkout.

Prompt for an OpenRouter key during the installation:

```bash
curl -fsSL https://raw.githubusercontent.com/kiazh/Machina/main/install.sh | bash -s -- --interactive
```

Choose an installation directory or avoid automatic database/PATH changes:

```bash
curl -fsSL https://raw.githubusercontent.com/kiazh/Machina/main/install.sh | bash -s -- --dir ~/machina
curl -fsSL https://raw.githubusercontent.com/kiazh/Machina/main/install.sh | bash -s -- --no-auto-db --no-shell-rc
```

Other installer options: `--dev`, `--no-ui`, `--with-docker-db`,
`--no-docker-db` and `--yes`. Run `bash install.sh --help` from a checkout for
all options. The Bash installer supports macOS/Linux and Git Bash on Windows;
PowerShell users can follow the manual installation below.

After installation:

```bash
# Restart your terminal, or update PATH for this shell:
export PATH="$HOME/.local/bin:$PATH"
machina setup                  # do this first: configure provider and database settings
machina doctor                 # inspect prerequisites, keys and database health
machina                    # open the terminal UI
```

If the installer reported incomplete database setup, set `DATABASE_URL` in
`machina setup` and run `machina init` before chatting. If setup changes the database URL
after a successful install, run `machina init` again for that database. Cloud API
credentials are yours to supply; the installer does not create a provider
account or API credit.

### Manual source installation

Requirements: Python **3.11+**, Node.js **22.19+** for the TUI, and PostgreSQL **16+** with **pgvector**. Configure a supported provider key, or an accessible Ollama server for local models.

```bash
git clone https://github.com/kiazh/Machina.git
cd Machina
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
npm ci --ignore-scripts --prefix ui
machina setup
```

`machina setup` prompts for configuration. Supply your PostgreSQL URL and provider credentials; review `.env` before initializing your chosen database. Secrets belong in `.env` or a supported secret backend, never in committed files or non-secret YAML.

```bash
machina doctor
machina init
machina                     # interactive terminal UI
machina chat "Explain this project"  # one-shot CLI
```

On Windows, activate with `.venv\Scripts\Activate.ps1` in PowerShell. Shell tools depend on the commands installed on your host.

The [installer](install.sh) can provision a local database through Docker or Homebrew where available. Inspect it before execution; it may install dependencies and create local configuration. For an existing checkout:

```bash
bash install.sh
```

A Python wheel installs the backend, bundled skills and subagents. It does **not** bundle Node dependencies or the terminal UI: use the source checkout above, or set `MACHINA_UI_DIR` to its `ui/` directory and run `npm ci --ignore-scripts` there.

## Terminal UI

The fullscreen interface has a scrolling conversation, framed tool results, a docked composer and approval controls. Themes color the full alternate screen; the normal terminal is restored on exit. Motion can be disabled with `MACHINA_UI_REDUCED_MOTION=1`.

Type `/` for commands. Management commands open pickers or forms; `?` on empty input and F1 show shortcuts. Enter selects, Escape cancels, and Space toggles checklist entries. Drop image files or paste with Ctrl+V (Cmd+V when the terminal forwards it); attachments show as numbered `[image N]` markers and need a vision-capable model. Limits: PNG/JPEG/GIF/WebP, at most 5 images per message, 5 MiB each and 20 MiB total; follow-up turns keep the latest images through tool churn and compaction.

| Command | Behavior |
| --- | --- |
| `/models`, `/provider`, `/effort` | Choose the session model/provider; set reasoning effort where supported |
| `/default-model` | Save the default model for future sessions |
| `/keys` | Manage masked provider keys |
| `/persona` | Add/edit or reset the default persona |
| `/growth` | Inspect learning, teach, rate, forget or restore a revision |
| `/delegate`, `/agents` | Pick an agent, then enter its task; list available roles |
| `/sessions`, `/new`, `/resume`, `/delete` | Browse, start and resume conversations; checklist deletion |
| `/search`, `/recall`, `/export`, `/rename`, `/fork`, `/goal` | Search, recall, export, rename, fork, and goal-track sessions |
| `/memory`, `/skills` | Search memory; browse installed skills |
| `/context`, `/compress` | Inspect and compact session context |
| `/jobs` | Schedule and manage background jobs |
| `/theme` | Preview and save a theme; Escape restores the previous theme |
| `/output` | Inspect retained tool output without rerunning it |
| `/mcp` | Add servers, review connection launches and inspect/disconnect them |
| `/mode`, `/approvals` | Manage execution mode and pending approvals |
| `/status`, `/usage`, `/config` | Feature availability, token usage, non-secret settings |
| `/help`, `/clear`, `/exit` | Command help, clear transcript, quit |

## What is included

- Streaming agent execution through the CLI, TUI/gateway and HTTP API, with cancellation, session ownership and bounded cleanup.
- Persistent conversations, context compaction, memory search and document retrieval. Semantic retrieval needs embedding configuration; keyword retrieval is available without it.
- File, shell, web, memory and document tools, subject to the permission broker and the selected execution backend.
- **42 curated ECC skills**, bundled offline and selected by task evidence. Relevant excerpts are disclosed progressively rather than injecting the entire library into each prompt.
- **71 ECC subagents** plus the existing `harness`, `researcher` and `coder` roles. Only the selected role's instructions enter its delegated context. [Integration details and tool limitations](docs/ECC_AGENTS.md).
- Scheduling, usage accounting and configurable session/agent budgets.
- Default-on persona growth and runtime adaptation, with durable notes, human feedback, revision history and restoration.

### Conversations, memory and document retrieval

Sessions retain conversation history, model/provider choices and operational
state. Resume them through `/sessions`; deleting a session uses a checklist and
confirmation. Context assembly selects evidence for the next request instead of
replaying every stored item. Compaction and archival keep older material
retrievable while making room for recent conversation.

Memory supports durable facts and preferences across conversations. Document
retrieval follows a separate load → chunk → embed/index → search → optional
rerank pipeline. PostgreSQL lexical indexes provide search without an embedding
service; configured embeddings add semantic similarity. Persona-aware ranking
can influence which memories are relevant. Neither retrieval nor storage makes
a model's unsupported claims true: retrieved material remains evidence to assess.

For current web research, `web_research` searches DuckDuckGo by default (or an
optional configured SearXNG instance), fetches up to three public pages through
the guarded HTTP fetcher, and indexes their text with source URLs in the active
session's RAG context. Later turns can retrieve those pages with
`search_documents`. The indexed research belongs to that session and agent; it
does not become global knowledge. Use the returned source URLs when citing
claims. Repeated URLs reuse the saved session snapshot. `web_search` remains
available for discovery without fetching or saving
pages.

### Skills, delegation and installing more

Skill routing uses explicit requests, trigger matches and task evidence to select
relevant workflows. Bundled metadata and bounded excerpts keep unrelated skill
content out of the prompt; complete instructions are loaded when needed. The
curated 42-skill set replaces indiscriminate loading of the upstream collection.

Paste a GitHub repo link in chat to install its skills: the agent stages the
repo (shallow clone, digest-bound plan, nothing installed or executed) and,
after your explicit approval of the digest, installs validated skill content.
Test suites, installers, hooks and package installs never run; MCP servers found
in a repo come back as configuration drafts for human `/mcp` review and are
never connected automatically. Manage skills from the CLI:

```bash
machina learn <file-or-url>        # learn a skill from a file, URL or skill name
machina hub list | search | install # community skill hub
machina curator report              # skill usage, staleness and cleanup
```

Subagents provide specialist instructions for coding, review, research and other
roles. `/delegate` opens the agent picker, then asks for the task. The model can
also delegate through its registered tools. Selected roles inherit the parent's
authority and allowed capabilities; choosing a specialist does not grant new
permissions or install that role's preferred external integrations. The 71 ECC
roles plus three built-ins are available by default, with original provenance
and harness-specific tool mappings documented in [ECC agents](docs/ECC_AGENTS.md).

### Scheduling, usage and diagnostics

Jobs support interval, heartbeat and UTC cron schedules, with durable claims and
leases in PostgreSQL. Headless jobs that need consent pause for approval instead
of granting themselves permission. Administrator-managed script jobs have
additional script/dependency policy; see [operations](OPERATIONS.md).

Usage accounting records provider requests and tokens. Optional session/agent
budgets restrict spending independently of prompt context capacity. Diagnostic
stages, sanitized audit events, gateway logs and authenticated metrics make
failures inspectable. Optional tracing requires a configured OpenTelemetry host;
the project does not install or start an exporter automatically.

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

Configuration persists in `~/.machina/mcp.json`, with installation-local locking, atomic replacement and revisions. Connections remain worker-local and do not automatically restart. To share this inventory across workers, use the same administrator-controlled home directory. Values belong in the existing secret backend; use `secret_refs` to pass named environment secrets to stdio servers, never literal tokens in arguments.

The agent uses `mcp_list`, `mcp_describe` and `mcp_call` after connection. Catalogs are paged and schemas loaded individually. Remote calls require exact approval in every mode unless you explicitly configure `trusted_read` **and** an allowlist of verified read tools. Server descriptions and annotations never grant consent. Schema/configuration/argument changes invalidate approval; uncertain calls are not retried.

Stdio runs on the host and is refused in sandbox mode. HTTP redirects and environment proxy inheritance are disabled; private hosts require explicit local access, and metadata addresses remain forbidden. HTTP servers support OAuth browser authorization (Authorization Code + S256 PKCE, [setup](docs/MCP_OAUTH.md)); HTTP credential-profile authentication is **not implemented** and such connectors report `auth_required`. Resources, prompts and server-initiated sampling/elicitation/roots are not exposed. These network checks do not establish complete DNS-rebinding protection. [Setup and current evidence](docs/MCP.md).

## Technology stack

| Layer | Technology | Role in the harness |
| --- | --- | --- |
| Backend runtime | Python 3.11+, asyncio | Agent execution, concurrent tools, lifecycle and cancellation |
| Terminal UI | TypeScript, Node.js 22.19+, `@earendil-works/pi-tui` | Fullscreen rendering, Markdown, menus, themes and approval controls |
| UI transport | JSON-RPC 2.0 over stdio | Separate Node UI and Python gateway processes; streamed versioned events |
| CLI | Typer + Rich | `machina` commands, setup, diagnostics and formatted output |
| HTTP service | FastAPI + Uvicorn | Authenticated API, SSE prompt streams and health endpoints |
| Database | PostgreSQL 16+ | Sessions, context, memory, documents, jobs, approvals and usage |
| Retrieval indexes | pgvector HNSW, PostgreSQL full-text and pg_trgm GIN | Vector similarity, lexical search and substring matching |
| Database access | asyncpg + raw SQL | Async pools, transactions, locks and explicit schema control |
| Serialization | MessagePack | Compact persisted structured context |
| Token accounting | tiktoken + model/provider metadata | Token estimates and context-capacity planning; estimates vary by tokenizer/model |
| Provider transport | httpx | Cloud/local model APIs, streaming, timeouts and HTTP boundaries |
| Cloud models | OpenRouter and direct vendor adapters | Provider/model selection without requiring a single hosted platform |
| Local models | Ollama | Local inference through an accessible Ollama service |
| Document embeddings | OpenAI-compatible embeddings | Semantic document retrieval; default `text-embedding-3-small` |
| Document reranking | Cohere or passthrough fallback | Optional reranking after retrieval |
| Web search and extraction | DuckDuckGo by default; optional SearXNG; direct HTTP text extraction | Search/fetch tools with network policy checks and session-scoped web research in RAG |
| MCP | Python MCP SDK **2.3.0**, stdio and Streamable HTTP | Reviewed server connection and native remote tool execution |
| Skills and roles | Bundled Markdown + pinned manifests | 42 curated ECC skills and 71 ECC specialist roles, loaded selectively |
| Configuration | YAML + environment + python-dotenv | Non-secret defaults and environment-based overrides |
| Secrets | Environment/file, Vault; optional AWS backend | Named secret resolution and redacted public views |
| Observability | PostgreSQL audit, Prometheus text metrics; optional OpenTelemetry | Operation evidence, metrics and spans when configured |
| Packaging | Hatchling, pip; `uv.lock` | Backend distributions and an available Python resolver lock |
| Deployment | Docker Compose; separate sandbox image | HTTP backend/database deployment and configured tool isolation |
| Verification | pytest, pytest-asyncio, Hypothesis, coverage, Ruff, Bandit | Backend behavior, concurrency, lint and security checks |
| UI verification | TypeScript compiler, node:test, ESLint | UI types, interactions, rendering and gateway integration |
| Optional workflow research | LangGraph + PostgreSQL checkpoints | Offline comparison utilities; native chat does not require LangGraph |

Dependencies and version constraints are declared in [pyproject.toml](pyproject.toml)
and [ui/package.json](ui/package.json). Cohere reranking, tracing exporters,
AWS secrets and workflow trials require their corresponding configuration or
optional dependencies; they are not services started by ordinary chat.

## Architecture

```mermaid
flowchart TD
    TUI[Terminal UI: TypeScript / pi-tui] -->|JSON-RPC over stdio| GW[Python gateway]
    CLI[Typer CLI] --> CORE[Agent runtime]
    HTTP[FastAPI / SSE] --> CORE
    GW --> CORE
    CORE --> CTX[Context assembly / memory / document retrieval]
    CORE --> ROUTE[Skill routing / subagent delegation]
    CORE --> LLM[Provider adapters: cloud APIs / Ollama]
    CORE --> BROKER[Permission broker]
    BROKER --> LOCAL[File / shell / web tools]
    BROKER --> MCP[MCP connector manager / SDK]
    MCP --> REMOTE[Configured stdio / HTTP servers]
    CTX --> DB[(PostgreSQL / pgvector)]
    ROUTE --> DB
    CORE --> DB
    BROKER --> DB
```

The UI handles rendering and interaction; the gateway handles authenticated
method dispatch and translates runtime events. CLI and HTTP entry points use
the same agent core, permission policy and persistence. The runtime owns shared
services, including the connector inventory, rather than spawning a separate
connector stack for each tool call.

### What happens during a turn

1. The harness loads the session, applies its provider/model/mode settings and
   acquires turn ownership so competing requests cannot run the same session.
2. Context assembly selects conversation history, memories, document evidence,
   persona notes and relevant skill excerpts within the model's prompt capacity.
3. The provider streams a response. Requested tools receive schema validation,
   capability checks and approval classification before execution.
4. An action that needs consent pauses with the exact proposal. Resume checks
   the session, action identity and current authority before consuming approval.
5. Results return as tool evidence, and the agent continues until it answers or
   reaches a configured iteration, time or usage limit.
6. Usage and execution evidence are recorded. Optional learning/maintenance and
   client cleanup are lifecycle-managed and bounded; uncertain tool outcomes
   remain explicit rather than being silently rerun.

### Storage boundaries

PostgreSQL holds conversation and operational state. `config.yaml`, `.env`,
MCP configuration and local logs have separate filesystem lifecycles. Bundled
skills/agents are versioned package data. MCP connections, in-process caches and
UI output viewers are process-local; saved server configuration survives restart,
but a connection must be explicitly reopened. Back up both database state and
relevant configuration through the [operations guide](OPERATIONS.md).

## Providers and local models

| Provider | Credential/configuration | Connection |
| --- | --- | --- |
| OpenRouter (default) | `OPENROUTER_API_KEY` | OpenRouter model catalog/API |
| OpenAI | `OPENAI_API_KEY` | Direct OpenAI API |
| Anthropic | `ANTHROPIC_API_KEY` | Direct Messages API |
| Google | `GOOGLE_API_KEY` | Google OpenAI-compatible endpoint |
| Mistral | `MISTRAL_API_KEY` | Direct Mistral API |
| Groq | `GROQ_API_KEY` | Direct Groq API |
| Together | `TOGETHER_API_KEY` | Direct Together API |
| DeepSeek | `DEEPSEEK_API_KEY` | Direct DeepSeek API |
| xAI | `XAI_API_KEY` | Direct xAI API |
| Ollama | Running Ollama service and an installed model | Local service; no cloud key required |

In the TUI, `/keys` manages masked credentials, `/provider` chooses the adapter,
`/models` chooses the session model and `/default-model` saves a future default.
`/effort` sets reasoning effort where the selected provider supports it. Direct
adapters use their vendor's key and endpoint. Cohere's key is for optional
retrieval reranking, not a chat provider.

Example non-secret defaults for an Ollama installation:

```yaml
# ~/.machina/config.yaml
provider: ollama
model: llama3.2
execution_mode: ask
```

Start Ollama and install that model separately. Ollama removes cloud inference
credentials from that setup; the harness still needs PostgreSQL for sessions and
memory. Changing models does not discard persisted persona notes, but model
behavior and context capacity can differ.

## Configuration and HTTP

Non-secret defaults live in `~/.machina/config.yaml`; environment overrides use `MACHINA_<KEY>`. Session overrides take precedence where supported. `.env` fills missing environment values. `MACHINA_ENV_FILE` selects an alternate file; [`.env.example`](.env.example) lists common settings.

Important settings include `DATABASE_URL`, `MACHINA_PROVIDER`, `MACHINA_MODEL`, `MACHINA_PERSONA_DESCRIPTION`, `MACHINA_EXECUTION_MODE`, and the `MACHINA_USAGE_*` budgets (`0` means unlimited). Learning flags are `MACHINA_MEMORY_ENABLED`, `MACHINA_PERSONALITY_GROWTH_ENABLED` and `MACHINA_RSI_ENABLED`.

### Configuration reference

| Setting | Meaning | Default / guidance |
| --- | --- | --- |
| `DATABASE_URL` | Application PostgreSQL connection | Required for persistent operation |
| `MACHINA_PROVIDER` | Chat provider adapter | `openrouter` |
| `MACHINA_MODEL` | Model identifier | `openrouter/free`; availability is provider-dependent |
| `MACHINA_REASONING_EFFORT` | Requested reasoning depth | Blank uses provider default |
| `MACHINA_EXECUTION_MODE` | Default mode for new sessions | `ask` |
| `MACHINA_PERSONA_DESCRIPTION` | Requested tone and character | Empty; maximum 2,000 characters |
| `MACHINA_MEMORY_ENABLED` | Automatic memory integration | `true` |
| `MACHINA_PERSONALITY_GROWTH_ENABLED` | Persistent personality learning | `true` |
| `MACHINA_RSI_ENABLED` | Runtime strategy/ranking adaptation | `true` |
| `MACHINA_PERSONA_MEMORY_ENABLED` | Persona-aware memory ranking | `true` |
| `MACHINA_PERSONA_DEFAULT_EMOTION` | Fallback persona-memory emotion | `trust` |
| `MACHINA_USAGE_SESSION_TOKEN_LIMIT` / `MACHINA_USAGE_SESSION_REQUEST_LIMIT` | Optional session usage budgets | `0` = unlimited |
| `MACHINA_USAGE_AGENT_TOKEN_LIMIT` / `MACHINA_USAGE_AGENT_REQUEST_LIMIT` | Optional agent usage budgets | `0` = unlimited |
| `MACHINA_CONTEXT_BUDGET` | Legacy compatibility setting | Does not impose a cumulative turn-token cap |
| `MACHINA_MAX_TOKENS` | Requested provider response length | `4096`; separate from prompt capacity |
| `MACHINA_MAX_ITERATIONS` | Agent loop iteration bound | `10` |
| `MACHINA_TURN_TIMEOUT` | Turn deadline in seconds | `300` |
| `SEARXNG_URL` | Optional SearXNG search endpoint; blank uses DuckDuckGo | blank |
| `MACHINA_TERMINAL_SANDBOX` | Terminal backend setting | `disabled`; configure `local` or `docker` as appropriate |
| `MACHINA_ENV_FILE` | Alternate secret/environment file | Default resolver uses the project environment file |
| `MACHINA_UI_DIR` | TUI source directory override | Useful for backend wheel installs |
| `MACHINA_UI_REDUCED_MOTION` | Disable working/learning animation | Set `1` |

For example, persist a non-secret persona through `/persona` or YAML:

```yaml
provider: openrouter
model: openrouter/free
persona_description: "Be calm, direct and curious. Explain tradeoffs and remember my preferences."
execution_mode: ask
memory_enabled: true
personality_growth_enabled: true
rsi_enabled: true
```

Keep API keys and database passwords in the secret environment/backend. An
explicit session mode change affects that session; changing the saved default
controls future sessions. Configuration precedence and migration details are in
[OPERATIONS.md](OPERATIONS.md).

Not configuration keys (no `config.yaml` entry, managed through the secret
path instead): `MACHINA_API_KEY` (HTTP API authentication, required for
protected endpoints), `MACHINA_PROVENANCE_KEY` (generated by
setup/installer), and `MACHINA_TEST_DATABASE_URL` (dedicated test
database — never point it at the application database).

### HTTP API reference

```bash
# Configure MACHINA_API_KEY in your environment or .env first.
machina serve --host 127.0.0.1 --port 8000
```

The HTTP API provides sessions, SSE prompts, memory, documents, jobs and approval operations. Requests use `Authorization: Bearer …` or `X-API-Key`; public `/health` and `/ready` expose limited health information. Missing API authentication configuration fails closed. The [wire contract](communications/ui-gateway-protocol.md) describes gateway events and approval semantics.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/health`, `/ready` | Public liveness and limited database/schema readiness |
| GET | `/metrics` | Authenticated Prometheus exposition |
| POST / GET | `/api/v1/sessions` | Create / list sessions |
| GET / PATCH / DELETE | `/api/v1/sessions/{id}` | History / update / delete; active-turn restrictions apply |
| POST | `/api/v1/sessions/{id}/prompt`, `/api/v1/sessions/{id}/chat` | Streaming agent response over SSE (`chat` is an alias) |
| GET | `/api/v1/sessions/{id}/context` | Redacted context previews |
| GET | `/api/v1/sessions/{id}/usage` | Recorded usage and configured budgets |
| POST | `/api/v1/sessions/{id}/jobs` | Schedule session jobs |
| GET | `/api/v1/sessions/{id}/jobs` | List session jobs |
| DELETE | `/api/v1/jobs/{id}` | Remove a scheduled job |
| GET | `/agents` | List available agents and roles |
| POST | `/api/v1/memory`, `/api/v1/persona-memory` | Add agent-scoped memory |
| GET | `/api/v1/memory/search` | Search memory |
| POST | `/api/v1/documents` | Index documents |
| GET | `/api/v1/documents/search` | Search documents |
| GET | `/api/v1/approvals?sessionId=…` | List approval requests (`sessionId` required) |
| POST | `/api/v1/approvals/{id}/resolve` | Record an approval decision |
| POST | `/api/v1/approvals/{id}/resume` | Resume the original reviewed action |
| POST | `/rpc` | Authenticated gateway bridge with method restrictions |

Create a session, then copy its returned ID into the streaming request:

```bash
curl -sS -H "Authorization: Bearer $MACHINA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"title":"First API session"}' \
  http://127.0.0.1:8000/api/v1/sessions

curl -N -H "Authorization: Bearer $MACHINA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text":"Explain the project architecture"}' \
  http://127.0.0.1:8000/api/v1/sessions/SESSION_ID/prompt
```

An SSE consumer must handle completion, error, approval and unknown tool outcomes;
it should not treat a dropped stream as proof an action failed. The API schema is
available at `/docs` while serving. For event versions and resume bindings, use
the [wire contract](communications/ui-gateway-protocol.md).

### Docker backend

```bash
cp .env.example .env
# Replace example keys with your own values before starting.
docker compose up --build
```

Compose runs the **HTTP backend**, not the terminal UI. It provisions an isolated database volume, leaves the database port unpublished and binds the API to `127.0.0.1:8000`. Review credentials, volumes and the [operations guide](OPERATIONS.md) before deployment. Docker execution has not been verified on this review machine; CI builds and smoke-checks the image.

## Research utilities

The repository contains experiments and evaluation adapters alongside the runtime.
They are available to inspect or run separately; ordinary chat does not launch
training jobs or require research dependencies.

| Area | Implementation | Boundary |
| --- | --- | --- |
| Runtime learning | Personality revisions, RSI evidence, feedback, strategy/capability ranking | Persists runtime preferences; no model-weight training |
| Memory policy learning | Policy modules and offline GRPO/training utilities | Research workflow; benchmark quality requires evaluation |
| Memory retention | Scoring, consolidation and reversible forgetting/archive behavior | Retrieval/maintenance mechanisms, separate from chat generation |
| Shared identity | Memory provenance, validation and quarantine | Integrity checks for exchanged memories |
| Persona retrieval | Persona-aware memory scoring and emotion topology | Retrieval interpretation, not a model's private emotional state |
| SoulSpec | Manifest/package handling, adapters and permission merging | Interoperability utilities; no certification claim |
| LoCoMo / identity evaluation | Evaluation harnesses and scenarios | Live accuracy studies need authorized provider calls and budgets |
| Native vs. LangGraph | Workflow trial and benchmark modules | Optional comparison; native execution remains the default |

Relevant code lives in `machina/memory/`, `machina/soulspec/` and `machina/research/`. Install
`.[workflow-trial]` for the LangGraph trial. Historical measurements describe
those experiments' fixtures and environments, not present model intelligence or
performance guarantees.

## Development and verification

```bash
python -m pip install -e '.[dev,connectors,workflow-trial]'
npm ci --ignore-scripts --prefix ui
# Point this only at a disposable database, never your application database.
export MACHINA_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/machina_test
python -m pytest -q --tb=short -p no:cacheprovider
python -m ruff check machina/ tools/
python -m ruff format --check machina/ tools/
python -m bandit -r machina/ -ll -x machina/cli/
npm --prefix ui run check
```

Database tests require the dedicated DSN. Real gateway UI tests additionally require `MACHINA_TEST_PYTHON` and their test environment; the backend's real-gateway wrapper configures those automatically. Missing prerequisites produce explicit skips, not verification evidence. Do not run concurrent backend suites against the same test database.

Build and inspect artifacts:

```bash
python -m pip install hatchling
python -m hatchling build
python tools/check_release_artifacts.py dist/*
```

CI covers Python checks, coverage, optional connector transport tests, the UI and distributable artifacts. The [current evidence](docs/verification/RELEASE_READINESS.md) distinguishes local checks from remote CI and deployment gates. Historical test counts and benchmarks in older documents describe their recorded checkout only.

### Recorded verification

The 2026-10-12 candidate review (HEAD `8c96fe3` with the working tree under
review) recorded **2,478 backend tests passed, 5 skipped**, with
**82.12% coverage**. Standalone UI checks with the real gateway integration
enabled recorded **326 passed, 0 failed, 0 skipped**. Ruff and package checks
passed; Bandit reported zero medium/high findings. These are dated results
for that checkout — re-run the commands above before trusting them for a
later commit or any other platform. Commands, fixture scope and pending hosted
CI/Docker gates are in [release readiness](docs/verification/RELEASE_READINESS.md).

## Project structure

```text
Machina/
├── machina/
│   ├── core/             # agent loop, providers, context, runtime, sessions, jobs, usage
│   ├── db/               # asyncpg pool and PostgreSQL schema
│   ├── gateway/          # JSON-RPC server and feature handlers
│   ├── api/              # FastAPI endpoints, authentication and rate limits
│   ├── permissions/      # action classification, broker, approvals and shell parsing
│   ├── plugins/          # plugin loading and registry
│   ├── protocol/         # versioned gateway event definitions
│   ├── connectors/       # MCP config store, SDK lifecycle, inventory and execution
│   ├── memory/           # retrieval, persona, growth, RSI, identity and policy
│   ├── rag/              # loading, chunking, embeddings, retrieval and reranking
│   ├── tools/            # local and MCP tool registration/execution
│   ├── skills/           # skill routing, runtime, learning and workflow support
│   ├── bundled_agents/   # pinned ECC role definitions, license and manifest
│   ├── soulspec/         # manifests, adapters and permission merge
│   ├── security/         # secret backends and environment-file handling
│   ├── observability/    # audit, diagnostics, metrics and optional tracing
│   ├── research/         # offline evaluations, policy training and workflow trials
│   └── cli/              # machina commands and terminal UI launcher
├── ui/src/               # TypeScript shell, transcript, widgets, features and themes
├── ui/test/              # node:test interaction and integration coverage
├── skills/               # bundled playbooks and curated ECC skill resources
├── tests/                # Python regressions, integration tests and SDK fixtures
├── tools/                # installer helpers, vendoring and artifact verification
├── docs/                 # feature guides, plans, evidence and handoff
├── communications/       # versioned UI/gateway protocol
├── install.sh            # source installer
├── Dockerfile            # HTTP backend image
├── Dockerfile.sandbox    # separate terminal execution image
├── docker-compose.yml    # HTTP backend + pgvector database
├── pyproject.toml        # Python dependencies, packaging and verification settings
└── uv.lock               # Python resolver lock
```

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `machina: command not found` after install | Restart the terminal or add `~/.local/bin` to PATH; manual installs need venv activation |
| Installer finishes but chat cannot run | Read installer warnings, run `machina doctor`, configure the database and a provider/local model |
| TUI unavailable / Node version error | Install Node 22.19+ and npm, then run `npm ci --ignore-scripts --prefix ui` |
| Backend wheel launches no TUI | Point `MACHINA_UI_DIR` at a source checkout's `ui/` and install its Node dependencies |
| Database unavailable | Verify `DATABASE_URL`, service reachability and credentials, then run `machina init` |
| pgvector or pg_trgm missing | Use the pgvector image or install matching extensions; schema creation requires appropriate privileges |
| API returns missing-key / authentication error | Set `MACHINA_API_KEY` server-side and send Bearer or `X-API-Key` credentials |
| Provider returns unauthorized / model unavailable | Use that provider's own key and a model it currently serves; check `/provider`, `/models` and `/keys` |
| A session already has a running turn | Cancel/wait for completion before retrying; do not submit competing turns |
| Response or tool times out | Inspect the recorded outcome; uncertain external side effects must be checked before retrying |
| MCP list is empty | `/mcp` shows this harness's configured servers; add one here even if another app already connects to it |
| MCP tools are unavailable after restart | Saved config persists, live connections do not; explicitly connect again |
| MCP reports `auth_required` | HTTP credential profiles are deferred; OAuth HTTP servers can authorize via `/mcp` ([setup](docs/MCP_OAUTH.md)) |
| MCP stdio refuses sandbox mode | Host stdio is outside that execution boundary; choose a supported transport/backend configuration |
| MCP asks for approval in full mode | Untrusted remote calls still need exact consent; trusted reads require an explicit verified allowlist |
| Persona appears unchanged in a conversation | Saved defaults apply through the profile/context; inspect `/persona` and `/growth`, and distinguish requested persona from trial revisions |
| Semantic retrieval is absent | Configure embeddings and indexed content; lexical retrieval remains available |
| Animation is distracting | Set `MACHINA_UI_REDUCED_MOTION=1` |
| Database/UI tests skip | Configure the dedicated test DSN and real gateway prerequisites; skipped tests are not passing evidence |

Start with `machina doctor` for setup and `machina status` for feature availability. Gateway
logs live in `~/.machina/logs/gateway.log`. Avoid pasting unredacted `.env`,
credentials or private tool output into issue reports.

## Documentation

- [Operations, migrations, backup and rollback](OPERATIONS.md)
- [Security model and limitations](SECURITY.md)
- [Release notes](CHANGELOG.md) and [release checklist](docs/verification/RELEASE_READINESS.md)
- [Feature implementation ledger](FEATURE_LEDGER.md)
- [Historical plans and snapshots](docs/archive/) (superseded; live docs above take precedence)
- [Successor handoff](docs/HANDOFF.md)
- [Production completion gates](docs/archive/plans/PRODUCTION_COMPLETION_PROGRESS.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)

## License

[MIT](LICENSE). Bundled ECC material retains its upstream MIT licenses and revision/hash manifests. See [third-party notices](THIRD_PARTY_NOTICES.md).
