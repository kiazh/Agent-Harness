# Machina

A self-hosted AI agent with a terminal UI, PostgreSQL-backed conversations and memory, tools, delegation, and runtime learning.

![Machina terminal UI](docs/screenshot.png)

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
curl -fsSL https://raw.githubusercontent.com/kiazh/Machina-Harness/main/install.sh | bash
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
curl -fsSL https://raw.githubusercontent.com/kiazh/Machina-Harness/main/install.sh | bash -s -- --interactive
```

Choose an installation directory or avoid automatic database/PATH changes:

```bash
curl -fsSL https://raw.githubusercontent.com/kiazh/Machina-Harness/main/install.sh | bash -s -- --dir ~/machina
curl -fsSL https://raw.githubusercontent.com/kiazh/Machina-Harness/main/install.sh | bash -s -- --no-auto-db --no-shell-rc
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
git clone https://github.com/kiazh/Machina-Harness.git
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

A headless `machina chat` turn exits 0 when it completes, 1 when it fails
(including a session already busy with another live turn) and 3 when it paused
for an approval decision.

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
| `/delegate`, `/agents` | Pick an agent, then enter its task; `sessions` picks a delegated subagent to view (or press `↓` on an empty composer); list available roles |
| `/sessions`, `/new`, `/resume`, `/delete` | Browse, start and resume conversations; checklist deletion |
| `/search`, `/recall`, `/export`, `/rename`, `/fork`, `/goal` | Search, recall, export, rename, fork, and goal-track sessions |
| `/memory`, `/skills` | Search memory; browse installed skills, review/roll back learned skills |
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
- Persistent conversations, context compaction, memory search and document retrieval. Semantic retrieval runs through an OpenAI-compatible endpoint or an optional zero-API local runtime (`pip install 'Machina[embeddings-onnx]'` for in-process ONNX, or `'Machina[embeddings-local]'` for static model2vec; backend `auto` is local-first). Optional local cross-encoder reranking (`'Machina[rerank]'`, or a llama.cpp `/v1/rerank` server) keeps query and candidate text on-host. Both remain opt-in by measurement: the LoCoMo replay set the defaults to keyword-only (`embedding_backend=off`) and no reranker (`reranker_backend=none`), since the dense and reranked variants missed their default-on bars.
- File, shell, web, memory and document tools, subject to the permission broker and the selected execution backend.
- Deterministic directory learning: the read-only `learn_directory` tool builds a bounded, cached repository digest (totals, largest modules, entry points, tests, import hubs) with **no model calls**; repeat calls are served from the content-hash cache.
- **42 curated ECC skills**, bundled offline and selected by task evidence. Relevant excerpts are disclosed progressively rather than injecting the entire library into each prompt.
- **71 ECC subagents** plus the existing `harness`, `researcher` and `coder` roles. Only the selected role's instructions enter its delegated context. [Integration details and tool limitations](docs/ECC_AGENTS.md).
- **Delegated subagents at a glance**: the footer shows the count of a session's delegated children (e.g. `2 subagents`). Press `↓` on an empty composer to pick one to watch (or `Ctrl+Shift+←/→` to cycle); selecting swaps the transcript via the normal session-resume path. Hidden sessions buffer a bounded live tail so switching back shows their recent work.
- Scheduling, usage accounting and configurable session/agent budgets.
- Default-on persona growth and runtime adaptation, with durable notes, human feedback, revision history and restoration.

### Conversations, memory and document retrieval

Sessions retain conversation history, model/provider choices and operational
state. Resume them through `/sessions`; deleting a session uses a checklist and
confirmation. Context assembly selects evidence for the next request instead of
replaying every stored item. Compaction and archival keep older material
retrievable while making room for recent conversation.

Resumed history and Markdown exports include retained archived originals as well as active
messages. They preserve assistant text before tool calls and mark interrupted replies.
New tool output is stored up to 65,536 characters; exports label storage truncation and
older records that contain only previews. Text never saved by older versions cannot be recovered.

Memory supports durable facts and preferences across conversations. Document
retrieval follows a separate load → chunk → embed/index → search → optional
rerank pipeline. PostgreSQL lexical indexes provide search without an embedding
service; the OpenAI-compatible adapter or the local `model2vec` runtime adds
semantic similarity. Persona-aware ranking
can influence which memories are relevant. Neither retrieval nor storage makes
a model's unsupported claims true: retrieved material remains evidence to assess.

For current web research, `web_research` searches through the configured
self-hosted SearXNG instance when `SEARXNG_URL` is set (a loopback address is
allowed, e.g. `http://localhost:8088`) and falls back to keyless DuckDuckGo
when it is unset or the instance fails; it fetches up to three public pages
through the guarded HTTP fetcher and indexes their text with source URLs in the
active session's RAG context. Later turns can retrieve those pages with
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

Post-turn learning publishes a validated reusable workflow as an enabled learned
skill directly — creation-only, bounded per session and by a rolling 24-hour
per-agent cap, redacted, injection-scanned and audited — with no standing
approval gate. `/skills proposals` is then a post-hoc review/rollback queue;
`/skills revert <id>` unpublishes a learned skill, and a precision auto-disable
returns the reviewer to stage-only if too many published skills get reverted.

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

SoulSpec supplies the authored identity, values, traits and voice through one shared runtime resolver. `/persona` saves the requested tone and character as the installation default. Learned notes belong to the agent's profile and persist across sessions. `/growth` exposes evidence and controls; the model can save durable preferences using `learn_personality`.

“RSI” here means adapting runtime preferences, retrieval, strategies and capability ranking from execution evidence and human ratings. It **does not train model weights or autonomously rewrite the harness**. Agent-written persona changes undergo a human-rated trial; direct human teaching establishes a stable revision. Automatic outcomes cannot promote a persona trial. See [personality growth](docs/PERSONALITY_GROWTH.md).

Offline policy-training and evaluation modules are research utilities, separate from normal chat. Their presence does not establish improved model quality.

### Permissions and shell execution

The default mode is `ask`; `workspace`, `sandbox` and `full` are also available. In `ask`/`workspace`, routine permitted project operations proceed without repeated prompts while risky actions ask. Activating `full` for a session issues an explicit session-scoped grant: host operations in that session then proceed without further approval, including credential access, privilege elevation, and destructive system operations. The grant covers that session only — it never changes the default for future sessions — and ends when the session leaves `full` mode or `/approvals revoke` runs.

Shell syntax such as `&&`, `;` and pipes is supported through the shared shell parser. Approval classification examines the command stages; syntax alone is neither an unconditional refusal nor a bypass. Tools remain subject to path, backend and execution limits. Host execution is not an OS sandbox. Read [SECURITY.md](SECURITY.md) before using elevated modes, plugins or scheduled scripts.

### Context and token limits

Prompt capacity follows the selected model/provider's context-window metadata or a conservative fallback, reserving space for output. There is no default cumulative turn-token cutoff; usage is still recorded. Optional session/agent usage limits and provider output limits are separate. The legacy `context_budget` setting is retained for compatibility and does not set a turn spend cap. A million-token window applies only when the selected serving reports that capacity. An opt-in prompt-cache pilot (`prompt_cache_enabled`, default off) can mark stable-prefix breakpoints for Anthropic-family models; it stays off until validated against live providers.

### MCP scope

MCP is included in the default Python installation. `/mcp` opens Add and server actions. Configure a local executable with a separate JSON argument array, or an HTTP endpoint. Adding a server saves configuration without launching it. Connect shows the exact launch preview and requires confirmation; a changed configuration needs a fresh preview.

Configuration persists in `~/.machina/mcp.json`, with installation-local locking, atomic replacement and revisions. Connections remain worker-local and do not automatically restart. To share this inventory across workers, use the same administrator-controlled home directory. Values belong in the existing secret backend; use `secret_refs` to pass named environment secrets to stdio servers, never literal tokens in arguments.

The agent uses `mcp_list`, `mcp_describe` and `mcp_call` after connection, and
manages the inventory through approval-gated `mcp_connect` / `mcp_disconnect`
(you can say "connect to this MCP server" in chat). Catalogs are paged and
schemas loaded individually. Remote calls require exact approval in every mode
unless you explicitly configure `trusted_read` **and** an allowlist of verified
read tools. Server descriptions and annotations never grant consent.
Schema/configuration/argument changes invalidate approval; uncertain calls are
not retried.

Stdio runs on the host and is refused in sandbox mode. HTTP redirects and environment proxy inheritance are disabled; private hosts require explicit local access, and metadata addresses remain forbidden. HTTP servers support OAuth browser authorization (Authorization Code + S256 PKCE, [setup](docs/MCP_OAUTH.md)); HTTP credential-profile authentication is **not implemented** and such connectors report `auth_required`. Resources, prompts and server-initiated sampling/elicitation/roots are not exposed. These network checks do not establish complete DNS-rebinding protection. [Setup and current evidence](docs/MCP.md).

### Machina as an MCP server

Machina also serves its own phase-1 shim over stdio (`python -m machina.mcp_server`), so MCP hosts such as OpenCode, Codex and Claude Desktop can call it. Register it with a Python that has Machina installed — a checkout virtualenv is easiest:

```bash
opencode mcp add machina -- /path/to/machina/.venv/bin/python -m machina.mcp_server
```

Project-scope registration writes `opencode.json`, which this repository gitignores because the interpreter path is host-specific; other hosts use their equivalent stdio configuration. The shim exposes exactly three tools: `machina_status` (read-only), `machina_chat` (one headless `machina chat` turn) and `machina_session_create`. Phase 1 is **text-only** — attachments/images are refused — and a turn that pauses for approval (CLI exit 3) returns `{"status": "paused_for_approval", ...}` with the pending request ids instead of hanging; resolve the request in Machina, then resume the session. Child turns run in the host's launch directory with a minimal environment allowlist, their own process group and a bounded kill-tree deadline; stdout carries the reply and stderr is diagnostics-only.

**Interop.** The same registration is scriptable: `machina interop status` reports which harnesses (OpenCode, Codex, Hermes) are installed, running and registered; `machina interop enable <platform>` registers this server idempotently through the platform CLI and verifies the result (`--scope project|global`; Codex and Hermes register user-level only, so a `project` scope clamps to `global`; `--dry-run` prints the exact command and runs nothing); `machina interop disable <platform>` removes the registration where the CLI supports it — OpenCode has no remove subcommand and reports the manual step instead. Project-scope OpenCode registration writes `opencode.json` (gitignored; see above). The installer's `--interop auto|all|none` picks the setup sweep: `auto` (default) prompts on a TTY and otherwise only prints the detection summary — a non-interactive install writes nothing unless you pass `all`, which registers every detected harness without prompting; `none` skips.

The `j9` gate journey certifies this path end to end and **skips** unless `opencode` is installed and the project has a registered `machina` server, so run the registration step above in any environment you accept the gate from. The recorded round trip and database check are in the [interop proof](docs/verification/proofs/interop_2026-10-14/README.md).

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
| Document embeddings | OpenAI-compatible or local ONNX/model2vec (`embeddings-onnx`/`embeddings-local` extras) | Semantic document/memory retrieval; off by default (LoCoMo: dense below keyword-only at k=5); `auto` is local-first: explicit endpoint, then ONNX, then model2vec, then an API key |
| Document reranking | Local ONNX cross-encoder (`rerank` extra), Cohere API or llama.cpp `/v1/rerank`; passthrough fallback | Optional reranking after retrieval; off by default (measured below the default-on bars) |
| Web search and extraction | Self-hosted SearXNG preferred when `SEARXNG_URL` is set; keyless DuckDuckGo fallback; direct HTTP text extraction | Search/fetch tools with network policy checks and session-scoped web research in RAG |
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

By design, everything the harness needs has a local path: only the agent's LLM
API is expected to be a paid external service, while embeddings, reranking and
web search have in-process or self-hosted runtimes (extras above; SearXNG
preferred when configured, keyless DuckDuckGo fallback) and Ollama can serve
chat on-host.

Dependencies and version constraints are declared in [pyproject.toml](pyproject.toml)
and [ui/package.json](ui/package.json). Local reranking (`Machina[rerank]`),
Cohere reranking, tracing exporters, AWS secrets and workflow trials require
their corresponding configuration or optional dependencies; they are not
services started by ordinary chat.

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
   reaches a configured time or usage limit.
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
| ChatGPT account | `machina auth login` (no API key) | ChatGPT subscription through the Codex protocol |
| GitHub Copilot | `machina auth copilot login` (or `gh` import) | Copilot seat; live-proven wire |
| OpenCode Zen | `OPENCODE_API_KEY` | OpenCode Zen gateway (free + paid models) |
| Kimi Code | `KIMI_API_KEY` (Kimi Code Console) | Kimi Code membership endpoint; OpenAI-compatible |
| Custom | `CUSTOM_BASE_URL` and optional `CUSTOM_API_KEY` | Any OpenAI-compatible endpoint; keyless local servers supported |
| Ollama | Running Ollama service and an installed model | Local service; no cloud key required |

In the TUI, `/keys` manages masked credentials - including the keys of
user-added providers, which appear automatically after `/provider add` -
`/provider` (alias `/providers`) chooses or manages adapters, and `/models`
opens the catalog grouped provider-by-provider with the session's provider
first: search matches provider names and model ids (`deepseek`, `gpt`), and
picking a model from another provider asks a one-line confirm - never a silent
switch. `/default-model` saves a future default; `/effort` sets reasoning
effort where the selected provider supports it, and an unset alias such as
`off` clears it. Direct adapters use their vendor's key and endpoint. Cohere's
key is for the optional Cohere reranker, not a chat provider; local ONNX
reranking needs no key.

**ChatGPT account.** The `chatgpt` provider runs turns on your ChatGPT
subscription instead of a metered API key. Sign in with `machina auth login`
(browser PKCE flow) or the TUI's `/auth`; `machina auth status` and
`machina auth logout` round out the surface, and
`machina auth login --import-codex` copies an existing Codex CLI login. Requests
are served by your ChatGPT plan, not the metered API. Tokens live in
`~/.machina/auth/chatgpt.json` — Machina's own store; logout never touches
`~/.codex/auth.json`. The transport follows the open-source Codex protocol: the
backend is undocumented and can drift, so protocol mismatches surface as typed
errors rather than silent wrong answers. The model list is pulled from your
account rather than hardcoded: `machina providers models chatgpt` (or
`/models catalog` in the TUI) fetches the live Codex catalog with each model's
context window and its own reasoning-effort set; only effort levels the wire
accepts for that model are offered, and a rejected level names the supported
set. When the live fetch is unavailable the listing falls back to the Codex
CLI's read-only cache and then to a dated snapshot of verified rows.

**More subscription sign-ins.** `copilot` (a GitHub Copilot seat - live-proven:
model list, completions and tool calls through the real wire) signs in with
`machina auth copilot login|status|logout`, imports an existing local CLI
login read-only where one exists, and keeps tokens under `~/.machina/auth/`
(0600). The TUI's `/auth` card currently covers ChatGPT; copilot is CLI-first
for now.

**Removed subscription flows (2026-10-14).** The Claude Pro/Max and Gemini
Code Assist OAuth flows were removed: Anthropic's legal-and-compliance docs
(2026-02-19) declare subscription OAuth tokens "not permitted" in any product
other than Claude Code / native Anthropic apps, and Google's Gemini CLI FAQ
declares third-party OAuth reuse a direct ToS violation subject to account
suspension. Both remain fully supported through their API-key providers
(`anthropic` with `ANTHROPIC_API_KEY`, `google` with `GOOGLE_API_KEY`); a
session or config pinned to the removed names now receives the standard
unknown-provider error. The research verdicts for further targets live in
`docs/verification/proofs/oauth_research_2026-10-14/`: Kiro is not feasible as
a native provider (AWS enforces its ToS against third-party clients; an
ACP-interop path via the official `kiro-cli` is scoped), and Cursor/Windsurf/
Antigravity/iFlow are not feasible.

**Custom endpoints.** The `custom` provider reaches any OpenAI-compatible
endpoint (LM Studio, vLLM, llama.cpp, a Hermes loopback proxy, …) through
`custom_base_url` / `CUSTOM_BASE_URL` and optional `custom_api_key` /
`CUSTOM_API_KEY`; keyless local servers are supported. A model id is required.

**OpenCode Zen.** The `opencode` provider presets the OpenCode Zen gateway
(`OPENCODE_API_KEY`; the base URL is overridable with `opencode_base_url` /
`OPENCODE_BASE_URL`). Zen lists free and paid models, but the free tier is gated
to the OpenCode client: keyless or placeholder-key requests answer HTTP 403
(`FreeTierError`), so use a genuine console key.

**Kimi Code.** The `kimi` provider reaches Kimi Code's official third-party
API endpoint (`https://api.kimi.ai/coding/v1`; China uses
`https://api.kimi.com/coding/v1`) with a `KIMI_API_KEY` created in the Kimi
Code Console - a membership is required for coding quota, and each model id
maps to a tier: `k3` (up to 1M context for higher tiers, 256K below), `k3-256k`
(256K), `kimi-for-coding` (default; up to 1M) and `kimi-for-coding-highspeed`
(256K, faster output). The wire documents a top-level `reasoning_effort`
(`low`/`high`/`max`); the harness sends it only when explicitly set, so use
`low`/`high` for this provider (its wire does not accept `medium`). Docs:
<https://www.kimi.com/code/docs/en/>.

**Your own providers.** `machina providers add <name> <base_url>` (or the
TUI's `/provider add`) registers any OpenAI-compatible endpoint; the key is
stored as `MACHINA_PROVIDER_<NAME>_API_KEY` in the git-ignored `.env` (never
in the metadata file — `--no-key` marks a keyless local endpoint) and its
model catalog is pulled from `GET {base_url}/models`. Added providers behave
like built-ins everywhere: `/provider`, `/models`, `MACHINA_PROVIDER=<name>`,
and the gateway. Model commands resolve against the resumed session's pinned
route (not the gateway default), and built-in provider names cannot be
shadowed. `machina providers list|remove|models` manages them; `remove`
deletes every trace - the store entry, its cached catalog, and the `.env` key
line - and if it was the active provider the session falls back to
`openrouter` with a visible notice. Metadata lives in
`~/.machina/providers.json` (0600).

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

Important settings include `DATABASE_URL`, `MACHINA_PROVIDER`, `MACHINA_MODEL`, `MACHINA_PERSONA_DESCRIPTION`, `MACHINA_EXECUTION_MODE`, and the `MACHINA_USAGE_*` budgets (`0` means unlimited). Learning flags are `MACHINA_MEMORY_ENABLED`, `MACHINA_PERSONALITY_GROWTH_ENABLED`, `MACHINA_RSI_ENABLED`, the learned-skill review keys (`MACHINA_LEARNING_REVIEW_AUTO_APPLY`, `MACHINA_LEARNING_REVIEW_AUTO_APPLY_PER_DAY`) and `MACHINA_LEARNED_SKILLS_DIR`.

### Configuration reference

| Setting | Meaning | Default / guidance |
| --- | --- | --- |
| `DATABASE_URL` | Application PostgreSQL connection | Required for persistent operation |
| `MACHINA_PROVIDER` | Chat provider adapter | `openrouter` |
| `MACHINA_MODEL` | Model identifier | `openrouter/free`; availability is provider-dependent |
| `MACHINA_REASONING_EFFORT` | Requested reasoning depth | Blank uses provider default |
| `OPENCODE_API_KEY` | OpenCode Zen gateway key (`opencode` provider) | Blank; the Zen free tier is gated to the OpenCode client, so keyless requests answer HTTP 403 |
| `OPENCODE_BASE_URL` | Optional base-URL override for the OpenCode Zen preset | Blank uses `https://opencode.ai/inference/v1` |
| `CUSTOM_BASE_URL` | OpenAI-compatible endpoint for the `custom` provider (LM Studio, vLLM, llama.cpp) | Blank; required by `custom` |
| `CUSTOM_API_KEY` | Optional key for the `custom` provider | Blank; keyless local servers are supported |
| `MACHINA_PROVIDERS_FILE` | User-added provider store (names, base URLs, pulled catalogs) | `~/.machina/providers.json` (0600); keys live in `.env` as `MACHINA_PROVIDER_<NAME>_API_KEY` |
| `MACHINA_EMBEDDING_BACKEND` | Dense-retrieval embedding backend | `off` (keyword-only default; LoCoMo showed static and bge-small dense hybrids below keyword-only at k=5). `auto` = endpoint → local ONNX → local model2vec → OpenAI/OpenRouter keys → keyword-only; `onnx`, `local`, `openai` force a choice |
| `MACHINA_EMBEDDING_MODEL` | Embedding model id for the selected backend | Blank uses the backend default (`text-embedding-3-small`, `BAAI/bge-small-en-v1.5` or `minishlab/potion-base-8M`) |
| `MACHINA_EMBEDDING_BASE_URL` | Explicit OpenAI-compatible embedding endpoint (llama.cpp, Ollama, LM Studio) | Blank; keyless local servers are supported |
| `MACHINA_RERANKER_BACKEND` | Cross-encoder reranker backend | `none` (opt-in; local ONNX on LoCoMo keyword-only: Δrecall +0.015, ΔMRR +0.022, p50 ~250 ms). `auto` = local ONNX → Cohere key; `local`, `cohere`, `http` force a choice |
| `MACHINA_RERANKER_LOCAL_MODEL` | Local ONNX reranker model | Blank uses `Xenova/ms-marco-MiniLM-L-12-v2`; e.g. `BAAI/bge-reranker-v2-m3-int8` for multilingual |
| `MACHINA_RERANKER_HTTP_URL` | Local llama.cpp `/v1/rerank` endpoint (`llama-server --reranking`) | Blank; host and port of your server |
| `MACHINA_RERANKER_HTTP_MODEL` | Model name sent to the HTTP reranker | Blank; server-defined label |
| `MACHINA_RRF_KEYWORD_WEIGHT` | Hybrid-search RRF weight for the keyword branch | `1.0` |
| `MACHINA_RRF_DENSE_WEIGHT` | Hybrid-search RRF weight for the dense branch | `0.25` (measured: keeps bge-small recall +0.013 while limiting MRR drag to −0.011 vs keyword-only; 1.0 dragged MRR −0.033) |
| `MACHINA_EXECUTION_MODE` | Default mode for new sessions | `ask` |
| `MACHINA_PERSONA_DESCRIPTION` | Requested tone and character | Empty; maximum 2,000 characters |
| `MACHINA_MEMORY_ENABLED` | Automatic memory integration | `true` |
| `MACHINA_PERSONALITY_GROWTH_ENABLED` | Persistent personality learning | `true` |
| `MACHINA_RSI_ENABLED` | Runtime strategy/ranking adaptation | `true` |
| `MACHINA_LEARNING_REVIEW_AUTO_APPLY` | Publish validated post-turn learned skills automatically (creation-only, bounded, versioned and revertible; no standing approval gate) | `true`; `false` stages every proposal for manual review |
| `MACHINA_LEARNING_REVIEW_AUTO_APPLY_PER_DAY` | Rolling 24h per-agent cap on auto-applied learned skills | `5` (0–100; `0` disables auto-apply — stage-only) |
| `MACHINA_LEARNED_SKILLS_DIR` | Directory for agent-learned skills (runtime data, never package data) | `~/.machina/skills`; setting `MACHINA_SKILLS_DIR` keeps learned skills under that root |
| `MACHINA_PERSONA_MEMORY_ENABLED` | Persona-aware memory ranking | `true` |
| `MACHINA_PERSONA_DEFAULT_EMOTION` | Fallback persona-memory emotion | `trust` |
| `MACHINA_USAGE_SESSION_TOKEN_LIMIT` / `MACHINA_USAGE_SESSION_REQUEST_LIMIT` | Optional session usage budgets | `0` = unlimited |
| `MACHINA_USAGE_AGENT_TOKEN_LIMIT` / `MACHINA_USAGE_AGENT_REQUEST_LIMIT` | Optional agent usage budgets | `0` = unlimited |
| `MACHINA_CONTEXT_BUDGET` | Legacy compatibility setting | Does not impose a cumulative turn-token cap |
| `MACHINA_MAX_TOKENS` | Requested provider response length | `4096`; separate from prompt capacity |
| `MACHINA_PROMPT_CACHE_ENABLED` | Prompt-cache breakpoints pilot (Anthropic / OpenRouter-anthropic only; payloads are byte-identical when off) | `false`; awaiting live-provider validation |
| `MACHINA_MAX_ITERATIONS` | Deprecated; the agent loop is unbounded (no iteration cap) | Ignored |
| `MACHINA_TURN_TIMEOUT` | Turn deadline in seconds | `300` |
| `SEARXNG_URL` | SearXNG search endpoint, preferred when set (loopback self-hosts allowed, e.g. `http://localhost:8088`); blank or failing falls back to DuckDuckGo | blank |
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
# Replace example keys with your own values before starting; generate a real
# API key (e.g. `openssl rand -hex 32`). The server refuses to start while
# MACHINA_API_KEY is still the .env.example placeholder.
docker compose up --build
```

Compose runs the **HTTP backend**, not the terminal UI. It provisions an isolated database volume, leaves the database port unpublished and binds the API to `127.0.0.1:8000`. Review credentials, volumes and the [operations guide](OPERATIONS.md) before deployment. Docker execution has not been verified on this review machine; CI builds and smoke-checks the image.

Build the terminal sandbox image on the host when using `terminal_sandbox=docker` or `sandbox` mode:

```bash
docker build -f Dockerfile.sandbox -t machina-tool-sandbox:latest .
```

Sandbox execution needs the host Docker daemon, so it is unavailable inside the Compose deployment by design (the app container has neither the CLI nor the daemon socket; absence fails closed instead of silently degrading to host execution).

Document retrieval has a reproducible source-level quality baseline and evaluation command; see [RAG quality review](docs/verification/RAG_QUALITY_REVIEW.md).

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
| SoulSpec | Manifest/package handling, adapters and permission merging | Unified runtime identity, human style and owned learning; no certification claim |
| LoCoMo / identity evaluation | Evaluation harnesses and scenarios | Live accuracy studies need authorized provider calls and budgets |
| Head-to-head harness | `machina/research/head_to_head.py`: 3-task matrix vs OpenCode on the DeepSeek flash layer | One small-N run; model mapping is best-effort; [report](docs/verification/HEAD_TO_HEAD_2026-10-13.md) |
| Judged QA pilot | `tools/harness_evals_pilot.py`: harness-evals judged LoCoMo QA over production retrieval | Opt-in pilot; ≈$0.36–0.72 per full 1,986-question pass (cache-miss upper bound); [report](docs/verification/HARNESS_EVALS_PILOT.md) |
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

CI uses the Ruff version pinned in the `dev` dependencies. Before pushing, run both
Ruff checks across `machina/ tools/`, even when focused tests pass. To correct import
ordering and formatting, run `python -m ruff check --select I --fix machina/ tools/`
and `python -m ruff format machina/ tools/`, then rerun the checks above.

Build and inspect artifacts:

```bash
python -m pip install hatchling
python -m hatchling build
python tools/check_release_artifacts.py dist/*
```

CI covers Python checks, coverage, optional connector transport tests, the UI and distributable artifacts. The [current evidence](docs/verification/RELEASE_READINESS.md) distinguishes local checks from remote CI and deployment gates. Historical test counts and benchmarks in older documents describe their recorded checkout only.

Reproducible numbers for a checkout are regenerated by the unified benchmark:
`python -m machina.research.benchmark` (offline incident replay, retrieval quality,
LoCoMo memory evaluation, prompt-prefix composition, architecture headroom — plus
the full suite/UI with `--with-suite --with-ui`). The LoCoMo memory section needs
the external dataset: pass `--locomo-data locomo10.json`; add `--locomo-db` to
score the same questions through the production archive search and cross-session
recall, and `--locomo-rerank` to replay archive search with the configured
reranker and report the off/on delta. Without the dataset the section reports
SKIPPED rather than fabricated results. The 2026-10-14 measurement wave set the
current defaults from that replay: keyword-only stays the default, and the best
fully-local configuration (bge-small ONNX hybrid, dense weight 0.25, local ONNX
reranker) reached recall@5 0.448 / MRR 0.373 against the TF-IDF baseline's
0.440 / 0.354 — still opt-in because dense retrieval and reranking missed their
isolated default-on bars ([matrix](docs/verification/proofs/rerank_wave_2026-10-14/summary.md)).

The backend CI job excludes `ui_integration` and `workflow_trial` tests. The UI job
owns typechecking, lint, all UI tests and real gateway restart coverage. Optional
native-versus-LangGraph trials run in the separate **Workflow research** workflow
when research files or dependency declarations change, weekly on Mondays, or manually.
To run those groups locally, use `python -m pytest -m ui_integration` or
`python -m pytest -m workflow_trial`; the unfiltered local suite still runs all groups.

### Recorded verification

The 2026-10-13 remediation program (incident fixes, security hardening, delegation
correctness, retrieval faithfulness, deterministic-first routing) closed out at
**3,576 backend tests passed, 0 failed, 1 skipped, 1 xfailed** (exact commands in the
[completion bundle](docs/verification/COMPLETION_BUNDLE.md)), with standalone UI
checks recorded at **337 passed, 0 failed, 6 skipped**. The 2026-10-14 follow-up
waves (local ONNX/model2vec retrieval stack, adversarial re-verification, agent
tabs, the Machina MCP server, auto-applied skill learning) close out at
**3,776 backend tests passed, 0 failed**, standalone UI at **370 tests /
364 pass / 0 fail / 6 env-gated skips**, and a final j1–j9 gate of
**9 PASS / 0 FAIL / 0 SKIP** ([proof](docs/verification/proofs/2026-10-14-e2e-gate.md));
architecture ratchets are green (deferred imports 682/682, mcp_server 0/0).
Fixture-replay metrics, before/after token measurements and the full
artifact/commit record are in the completion bundle; remaining uncertainties and
operator-level deferrals are listed there as well.

The earlier 2026-10-12 candidate review (starting checkout `54d79d5`) recorded
**2,237 backend tests passed, 5 skipped, 0 failed** with **82.26% coverage**, and a
standalone UI check of **290 passed, 5 environment-gated skips** (the real-gateway
wrapper inside that run covered all 295 UI tests).
These are dated results for their respective checkouts — re-run the commands above
before trusting them for a later commit or any other platform. Commands, fixture
scope and pending hosted CI/Docker gates are in
[release readiness](docs/verification/RELEASE_READINESS.md).

### End-to-end acceptance gate

Acceptance evidence is the recorded E2E gate, not unit-test counts: every
feature or "fixed" claim must be proved by a journey that asserts observable
end state on real components (PostgreSQL rows, indexed RAG chunks, tool and
API outcomes) and writes its proof to `docs/verification/proofs/`. Run:

```bash
# The gate refuses a non-test database unless `--db-url` names it (the flag is
# the consent). The default .env DATABASE_URL database is `machina` (non-test):
DB=postgresql://postgres:postgres@localhost:5432/machina
python tools/e2e_gate.py --db-url "$DB"     # j1..j9; j4/j9 skip without Firecrawl / opencode+registration
python tools/e2e_gate.py --db-url "$DB" --journeys j1,j5 # cheap real subset
python tools/e2e_gate.py --db-url "$DB" --json /tmp/e2e.json
# Or point MACHINA_DATABASE_URL/DATABASE_URL at a test-named DB and run flagless.
```

Journeys j1–j9 cover memory consolidation roundtrip, live web research and
web crawl into session RAG, the self-hosted Firecrawl MCP path, a CLI
smoke check, compaction triggers across the SSE, gateway and CLI paths,
cross-transport turn parity, the provider/usage facade, and the
OpenCode → Machina MCP-server roundtrip (j9; skips without `opencode` and a
registered `machina` server). Exit code is 0
only when no journey failed (skips allowed), 1 on any failure and 2 on
usage/environment errors, including the non-test-database refusal above; every
run writes
`<UTC date>-e2e-gate.json`/`.md` under `docs/verification/proofs/`. The
latest recorded full run (2026-10-14) passed all nine journeys
(9 PASS / 0 FAIL / 0 SKIP;
[proof](docs/verification/proofs/2026-10-14-e2e-gate.md)), including the j4
Firecrawl connector path and the j9 OpenCode roundtrip on this machine. j9
still skips cleanly where `opencode` is absent or no `machina` server is
registered, so it is certified only in environments that satisfy that
requirement; its standalone interop validation remains at the
[interop proof](docs/verification/proofs/interop_2026-10-14/README.md).
Policy, proof rules and the three adversarial pre-merge personas are in the
[E2E protocol](docs/verification/E2E_PROTOCOL.md).

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
├── tools/                # installer helpers, E2E gate, pilot runners, vendoring and artifact verification
├── docs/                 # feature guides, plans, evidence and handoff
├── communications/       # versioned UI/gateway protocol
├── install.sh            # source installer
├── Dockerfile            # HTTP backend image
├── Dockerfile.sandbox    # terminal execution image (host daemon; built by CI)
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
| API refuses to start | `MACHINA_API_KEY` is still the `.env.example` placeholder; generate a real key (`openssl rand -hex 32`) — the server fails closed rather than run behind a public value |
| Sandbox mode unavailable | Requires the host Docker daemon and a built `Dockerfile.sandbox` image (`docker build -f Dockerfile.sandbox -t machina-tool-sandbox:latest .`); inside Compose it is unavailable by design |
| Provider returns unauthorized / model unavailable | Use that provider's own key and a model it currently serves; check `/provider`, `/models` and `/keys` |
| A session already has a running turn | Cancel/wait for completion before retrying; do not submit competing turns |
| Response or tool times out | Inspect the recorded outcome; uncertain external side effects must be checked before retrying |
| MCP list is empty | `/mcp` shows this harness's configured servers; add one here even if another app already connects to it |
| MCP tools are unavailable after restart | Saved config persists, live connections do not; explicitly connect again |
| MCP reports `auth_required` | HTTP credential profiles are deferred; OAuth HTTP servers can authorize via `/mcp` ([setup](docs/MCP_OAUTH.md)) |
| MCP stdio refuses sandbox mode | Host stdio is outside that execution boundary; choose a supported transport/backend configuration |
| MCP asks for approval in full mode | Untrusted remote calls still need exact consent; trusted reads require an explicit verified allowlist |
| Persona appears unchanged in a conversation | Saved defaults apply through the profile/context; inspect `/persona` and `/growth`, and distinguish requested persona from trial revisions |
| Semantic retrieval is absent | Install a local runtime (`pip install 'Machina[embeddings-onnx]'` or `'Machina[embeddings-local]'`) and enable it (`MACHINA_EMBEDDING_BACKEND=onnx` or `local`), configure `MACHINA_EMBEDDING_BASE_URL`, or set `OPENAI_API_KEY`; lexical retrieval remains available |
| Reranking has no effect | It is off by default (`reranker_backend=none`): enable `MACHINA_RERANKER_BACKEND=local` (extra `'Machina[rerank]'`), `cohere` (Cohere key) or `http` (llama.cpp `/v1/rerank` URL) |
| Animation is distracting | Set `MACHINA_UI_REDUCED_MOTION=1` |
| Database/UI tests skip | Configure the dedicated test DSN and real gateway prerequisites; skipped tests are not passing evidence |

Start with `machina doctor` for setup and `machina status` for feature availability. Gateway
logs live in `~/.machina/logs/gateway.log`. Avoid pasting unredacted `.env`,
credentials or private tool output into issue reports.

## Documentation

- [Operations, migrations, backup and rollback](OPERATIONS.md)
- [Security model and limitations](SECURITY.md)
- [Completion bundle — 2026-10-13 remediation record](docs/verification/COMPLETION_BUNDLE.md)
- [Head-to-head harness comparison](docs/verification/HEAD_TO_HEAD_2026-10-13.md) and [harness-evals judged-QA pilot](docs/verification/HARNESS_EVALS_PILOT.md)
- [Release notes](CHANGELOG.md) and [release checklist](docs/verification/RELEASE_READINESS.md)
- [Feature implementation ledger](FEATURE_LEDGER.md)
- [Historical plans and snapshots](docs/archive/) (superseded; live docs above take precedence)
- [Successor handoff](docs/HANDOFF.md)
- [Production completion gates](docs/archive/plans/PRODUCTION_COMPLETION_PROGRESS.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)

## License

[MIT](LICENSE). Bundled ECC material retains its upstream MIT licenses and revision/hash manifests. See [third-party notices](THIRD_PARTY_NOTICES.md).
