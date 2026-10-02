# AgentHarness CLI Redesign Spec

**Version:** 1.0  
**Date:** 2026-10-01  
**Status:** Draft  
**Author:** AgentHarness Team

---

## Table of Contents

1. [Overview](#overview)
2. [Design Principles](#design-principles)
3. [Command Tree](#command-tree)
4. [Global Flags](#global-flags)
5. [Subcommand Specifications](#subcommand-specifications)
6. [Interactive REPL](#interactive-repl)
7. [Slash Commands](#slash-commands)
8. [Autocomplete Behavior](#autocomplete-behavior)
9. [Help Menu Layout](#help-menu-layout)
10. [Color Scheme](#color-scheme)
11. [Animation Specifications](#animation-specifications)
12. [Loading Bar Design](#loading-bar-design)
13. [Error Display Format](#error-display-format)
14. [Output Contract](#output-contract)
15. [Exit Codes](#exit-codes)
16. [Configuration & Environment](#configuration--environment)
17. [Safety Rules](#safety-rules)
18. [Shell Completion](#shell-completion)
19. [Example Invocations](#example-invocations)
20. [Implementation Notes](#implementation-notes)

---

## 1. Overview

### 1.1 Name

`ah` (AgentHarness)

### 1.2 One-liner

Self-hosted multi-agent AI orchestration framework with PostgreSQL-backed context, RAG, streaming, and an interactive REPL.

### 1.3 Purpose

This spec defines the complete CLI surface area for AgentHarness, covering both one-shot commands and the interactive REPL. It follows the [CLI Guidelines](https://clig.dev/) and draws inspiration from Claude Code, Hermes, OpenCode, and Codex CLI.

### 1.4 Primary Users

- **Humans** — interactive REPL, rich output, animations, autocomplete
- **Scripts** — `--json`, `--plain`, `--no-input`, exit codes, stdin/stdout piping

### 1.5 Input Sources

| Source | Supported | Notes |
|--------|-----------|-------|
| CLI arguments | ✅ | Primary input for one-shot commands |
| stdin | ✅ | Pipe text to `ah chat` for scripting |
| Files | ✅ | Via `read_file` tool, not CLI args directly |
| URLs | ✅ | Via `web_extract` tool |
| Secrets | ❌ | Never via flags; use env vars or config file |

### 1.6 Output Contract

| Mode | Flag | Use Case |
|------|------|----------|
| Human | default | Rich formatting, colors, animations |
| Machine | `--json` | Structured JSON to stdout |
| Plain | `--plain` | Stable line-based text, no colors |
| Quiet | `--quiet` | Suppress all non-essential output |
| Verbose | `--verbose` | Show tool calls, reasoning, token counts |

---

## 2. Design Principles

1. **Human-first, script-friendly** — Rich output for humans, `--json`/`--plain` for scripts
2. **Progressive disclosure** — Simple by default, powerful when needed
3. **Consistency** — Same patterns across all commands
4. **Discoverability** — `--help` everywhere, autocomplete, slash commands
5. **Safety** — Confirmations for destructive ops, `--dry-run`, `--force`
6. **Graceful degradation** — Works without colors, without TTY, without database
7. **Fast feedback** — Streaming, loading bars, animations for long operations

---

## 3. Command Tree

```
ah
├── chat [message] [flags]          # One-shot or interactive chat
├── repl [flags]                    # Launch interactive REPL
├── status                          # Show system status
├── sessions [flags]               # List sessions
├── context [session_id] [flags]    # View context chunks
├── skills [flags]                 # List skills
├── doctor                          # Diagnostics
├── init [flags]                    # Initialize database
├── version                         # Show version
├── config [flags]                  # Show configuration
├── config-set <key> <value>       # Set config value
├── memory-list [flags]            # List memories
├── memory-search <query>          # Search memories
├── memory-forget <memory_id>      # Delete memory
└── help [command]                  # Show help
```

### 3.1 Command Details

#### `ah chat`

```bash
ah chat [message] [flags]
```

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--continue` | `-c` | bool | false | Continue last session |
| `--session` | `-s` | string | — | Resume specific session |
| `--model` | `-m` | string | config | Model to use |
| `--provider` | `-p` | string | config | LLM provider |
| `--verbose` | `-v` | bool | true | Show tool calls and reasoning |
| `--quiet` | `-q` | bool | false | Suppress non-essential output |
| `--interactive` | `-i` | bool | false | Launch interactive REPL |
| `--json` | — | bool | false | Output as JSON |
| `--plain` | — | bool | false | Output as plain text |
| `--no-input` | — | bool | false | Disable prompts |
| `--dry-run` | — | bool | false | Show what would happen |

**Behavior:**
- If `message` is provided: one-shot chat, prints response, exits
- If no `message` and `--interactive`: launches REPL
- If no `message` and no `--interactive`: shows help
- If `--continue`: resumes most recent active session
- If `--session`: resumes specified session

#### `ah repl`

```bash
ah repl [flags]
```

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--model` | `-m` | string | config | Model to use |
| `--provider` | `-p` | string | config | LLM provider |
| `--verbose` | `-v` | bool | config | Show tool calls |
| `--quiet` | `-q` | bool | false | Suppress non-essential output |
| `--session` | `-s` | string | — | Resume specific session |
| `--no-input` | — | bool | false | Disable prompts |

#### `ah status`

```bash
ah status [flags]
```

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--json` | — | bool | false | Output as JSON |
| `--plain` | — | bool | false | Output as plain text |

#### `ah sessions`

```bash
ah sessions [flags]
```

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--limit` | `-n` | int | 10 | Number of sessions |
| `--status` | `-s` | string | — | Filter by status |
| `--json` | — | bool | false | Output as JSON |
| `--plain` | — | bool | false | Output as plain text |

#### `ah context`

```bash
ah context [session_id] [flags]
```

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--limit` | `-n` | int | 10 | Number of chunks |
| `--json` | — | bool | false | Output as JSON |
| `--plain` | — | bool | false | Output as plain text |

#### `ah skills`

```bash
ah skills [flags]
```

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--json` | — | bool | false | Output as JSON |
| `--plain` | — | bool | false | Output as plain text |

#### `ah doctor`

```bash
ah doctor [flags]
```

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--json` | — | bool | false | Output as JSON |
| `--plain` | — | bool | false | Output as plain text |

#### `ah init`

```bash
ah init [flags]
```

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--db-url` | — | string | env | PostgreSQL connection URL |
| `--force` | `-f` | bool | false | Reinitialize even if exists |
| `--dry-run` | — | bool | false | Show what would happen |

#### `ah version`

```bash
ah version
```

Prints `AgentHarness v{version}` to stdout.

#### `ah config`

```bash
ah config [flags]
```

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--json` | — | bool | false | Output as JSON |
| `--plain` | — | bool | false | Output as plain text |

#### `ah config-set`

```bash
ah config-set <key> <value> [flags]
```

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--persist` | `-p` | bool | false | Save to config file |

#### `ah memory-list`

```bash
ah memory-list [flags]
```

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--limit` | `-n` | int | 20 | Number of memories |
| `--category` | `-c` | string | — | Filter by category |
| `--json` | — | bool | false | Output as JSON |
| `--plain` | — | bool | false | Output as plain text |

#### `ah memory-search`

```bash
ah memory-search <query> [flags]
```

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--limit` | `-n` | int | 5 | Number of results |
| `--json` | — | bool | false | Output as JSON |
| `--plain` | — | bool | false | Output as plain text |

#### `ah memory-forget`

```bash
ah memory-forget <memory_id> [flags]
```

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--force` | `-f` | bool | false | Skip confirmation |

#### `ah help`

```bash
ah help [command]
```

Shows help for the specified command, or general help if no command given.

---

## 4. Global Flags

These flags are available on all commands:

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--help` | `-h` | bool | false | Show help and exit |
| `--version` | — | bool | false | Show version and exit |
| `--json` | — | bool | false | Output as JSON |
| `--plain` | — | bool | false | Output as plain text |
| `--quiet` | `-q` | bool | false | Suppress non-essential output |
| `--verbose` | `-v` | bool | false | Show detailed output |
| `--no-color` | — | bool | false | Disable colored output |
| `--no-input` | — | bool | false | Disable interactive prompts |

**Precedence:** `--help` > `--version` > command-specific flags > global flags

---

## 5. Subcommand Specifications

### 5.1 `chat` — One-shot or Interactive Chat

**Idempotence:** No — creates or modifies sessions  
**State changes:** Creates session, adds context chunks, consolidates memories  
**Streaming:** Yes — tokens displayed as they arrive  
**Timeout:** 120s per LLM call, 3 retries with exponential backoff

**Flow:**
1. Parse message and flags
2. Determine session (new, continue, or specific)
3. Create LLM provider
4. Stream response with loading bar
5. Display final response in panel
6. Show metadata (iterations, tool calls, tokens)

### 5.2 `repl` — Interactive REPL

**Idempotence:** N/A — persistent session  
**State changes:** Same as chat, but across multiple turns  
**Streaming:** Yes  
**Timeout:** None — runs until user exits

**Flow:**
1. Load or create session
2. Display welcome panel
3. Enter input loop
4. Handle slash commands or send to agent
5. Stream response
6. Repeat

### 5.3 `status` — System Status

**Idempotence:** Yes — read-only  
**State changes:** None  
**Streaming:** No  
**Timeout:** 5s

**Checks:**
- PostgreSQL connection
- Session count
- Context chunk count
- Registered tools
- Environment variables

### 5.4 `sessions` — List Sessions

**Idempotence:** Yes — read-only  
**State changes:** None  
**Streaming:** No  
**Timeout:** 5s

### 5.5 `context` — View Context

**Idempotence:** Yes — read-only  
**State changes:** None  
**Streaming:** No  
**Timeout:** 5s

### 5.6 `skills` — List Skills

**Idempotence:** Yes — read-only  
**State changes:** None  
**Streaming:** No  
**Timeout:** 5s

### 5.7 `doctor` — Diagnostics

**Idempotence:** Yes — read-only  
**State changes:** None  
**Streaming:** No  
**Timeout:** 10s

**Checks:**
- Python version
- Dependencies (typer, rich, asyncpg, httpx, msgpack, prompt_toolkit)
- PostgreSQL connection
- Environment variables

### 5.8 `init` — Initialize Database

**Idempotence:** Yes — safe to run multiple times  
**State changes:** Creates database schema  
**Streaming:** No  
**Timeout:** 30s

**Safety:**
- `--dry-run`: Show what would be created
- `--force`: Drop and recreate schema
- Without `--force`: Skip if schema exists

### 5.9 `config` / `config-set` — Configuration

**Idempotence:** `config` is read-only; `config-set` modifies config  
**State changes:** `config-set` may write to config file  
**Streaming:** No  
**Timeout:** 5s

### 5.10 `memory-list` / `memory-search` / `memory-forget` — Memory Management

**Idempotence:** `memory-list` and `memory-search` are read-only; `memory-forget` deletes  
**State changes:** `memory-forget` deletes a memory  
**Streaming:** No  
**Timeout:** 5s

**Safety:**
- `memory-forget` requires confirmation unless `--force`

---

## 6. Interactive REPL

### 6.1 Startup

```
┌─────────────────────────────────────────────────────────────┐
│  AgentHarness Interactive REPL v0.1.0                       │
│                                                             │
│  Session: abc12345-6789-abcd-ef01-23456789abcd              │
│  Model: anthropic/claude-3.5-sonnet | Provider: openrouter   │
│  Type /help for commands, /exit to quit.                    │
└─────────────────────────────────────────────────────────────┘
```

### 6.2 Prompt

```
ah (abc12345) >
```

- `ah` — bold cyan
- `(abc12345)` — dim, first 8 chars of session ID
- `>` — bold white

### 6.3 Input Handling

| Key | Action |
|-----|--------|
| Enter | Submit message |
| Ctrl+C | Interrupt current operation / exit if idle |
| Ctrl+D | Exit REPL |
| Up/Down | Navigate history |
| Tab | Accept autocomplete suggestion |
| Ctrl+L | Clear screen |
| Ctrl+R | Search history |

### 6.4 Streaming Output

While the agent is generating a response:

```
ah (abc12345) > What is the capital of France?

⠋ Thinking...                                    [████░░░░░░░░░░░░░░░░] 20%
```

After completion:

```
┌─────────────────────────────────────────────────────────────┐
│  The capital of France is Paris.                            │
│                                                             │
│  Paris is the largest city in France and serves as the      │
│  country's political, economic, and cultural center.        │
└─────────────────────────────────────────────────────────────┘

Iterations: 1 | Tool calls: 0 | Tokens: 156
Session: abc12345-6789-abcd-ef01-23456789abcd
```

### 6.5 Tool Call Display (Verbose Mode)

```
┌─────────────────────────────────────────────────────────────┐
│  I'll search for that information.                          │
└─────────────────────────────────────────────────────────────┘

  → web_search({"query": "capital of France", "limit": 5})
  ← Search results for 'capital of France':
      Paris - Wikipedia
      https://en.wikipedia.org/wiki/Paris
      Paris is the capital and most populous city of France...

Iterations: 1 | Tool calls: 1 | Tokens: 234
```

---

## 7. Slash Commands

### 7.1 Command List

| Command | Aliases | Description |
|---------|---------|-------------|
| `/help` | `/?` | Show available slash commands |
| `/status` | — | Show current session and agent status |
| `/sessions` | — | List recent sessions |
| `/new` | — | Create a new session |
| `/switch <id>` | — | Switch to a different session |
| `/context` | — | Show context for current session |
| `/model [name]` | — | Show or set the model |
| `/provider [name]` | — | Show or set the provider |
| `/budget [n]` | — | Show or set context budget |
| `/verbose` | — | Toggle verbose mode |
| `/config` | — | Show current configuration |
| `/clear` | — | Clear the screen |
| `/export [format]` | — | Export conversation (markdown, json) |
| `/search <query>` | — | Search conversation history |
| `/goal [text]` | — | Set session goal |
| `/fork` | — | Fork current session |
| `/archive` | — | Archive current session |
| `/delete <id>` | — | Delete a session |
| `/rename <title>` | — | Rename current session |
| `/memory` | — | Show recent memories |
| `/memory-add <text>` | — | Add a memory |
| `/memory-search <query>` | — | Search memories |
| `/memory-delete <id>` | — | Delete a memory |
| `/skills` | — | List skills |
| `/debug` | — | Toggle debug mode |
| `/stop` | — | Stop the agent |
| `/redirect <feedback>` | — | Redirect the agent |
| `/exit` | `/quit` | Exit the REPL |

### 7.2 Slash Command Behavior

- All slash commands are case-insensitive
- Arguments are separated by spaces
- Quoted arguments are supported: `/goal "My new goal"`
- Unknown commands show an error with suggestion
- `/help` shows a categorized table of commands

---

## 8. Autocomplete Behavior

### 8.1 Trigger

Autocomplete activates when:
1. User types `/` at the start of an empty prompt → shows all slash commands
2. User types `/partial` → filters slash commands by prefix
3. User types a space after a slash command → shows relevant arguments

### 8.2 Slash Command Completion

```
> /s<TAB>
  /sessions    /status     /search     /switch     /stop
```

### 8.3 Argument Completion

| Command | Completions |
|---------|-------------|
| `/model` | Available model names |
| `/provider` | `openrouter`, `ollama` |
| `/switch` | Recent session IDs |
| `/delete` | Recent session IDs |
| `/memory-delete` | Recent memory IDs |
| `/export` | `markdown`, `json` |

### 8.4 File Path Completion

When a command expects a file path (e.g., `/export`), Tab completes file paths.

### 8.5 History Completion

- Up/Down arrows navigate input history
- `Ctrl+R` searches history
- History is persisted to `~/.agent-harness/repl_history`

### 8.6 Completion UI

```
> /sw<TAB>
┌──────────────────────────────────┐
│ /switch abc12345  (2 min ago)    │
│ /switch def67890  (1 hour ago)   │
└──────────────────────────────────┘
```

- Selected item: bold cyan with `▸` prefix
- Unselected items: dim
- Metadata (time, description): dim, right-aligned

---

## 9. Help Menu Layout

### 9.1 General Help (`ah --help`)

```
AgentHarness — self-hosted multi-agent AI orchestration framework

USAGE:
  ah [global flags] <subcommand> [args]

SUBCOMMANDS:
  chat [message]     Chat with the agent (one-shot or interactive)
  repl               Launch interactive REPL
  status             Show system status
  sessions           List recent sessions
  context [id]       View context chunks
  skills             List skills
  doctor             Check dependencies and configuration
  init               Initialize database
  version            Show version
  config             Show configuration
  config-set         Set a configuration value
  memory-list        List memories
  memory-search      Search memories
  memory-forget      Delete a memory
  help               Show help

GLOBAL FLAGS:
  -h, --help         Show help
  --version           Show version
  -q, --quiet        Suppress non-essential output
  -v, --verbose      Show detailed output
  --json             Output as JSON
  --plain            Output as plain text
  --no-color         Disable colored output
  --no-input         Disable interactive prompts

EXAMPLES:
  ah chat "hello"                    # One-shot chat
  ah repl                            # Interactive REPL
  ah chat -c                         # Continue last session
  ah sessions --limit 20             # List 20 sessions
  ah config-set model gpt-4o         # Set default model

ENVIRONMENT:
  OPENROUTER_API_KEY    OpenRouter API key
  DATABASE_URL          PostgreSQL connection URL
  AGENT_HARNESS_*       Config overrides (e.g., AGENT_HARNESS_MODEL)

Run 'ah help <command>' for command-specific help.
```

### 9.2 Command-Specific Help (`ah help chat`)

```
ah chat — Chat with the agent

USAGE:
  ah chat [message] [flags]

FLAGS:
  -c, --continue       Continue last session
  -s, --session ID     Resume specific session
  -m, --model NAME     Model to use
  -p, --provider NAME  LLM provider (openrouter, ollama)
  -v, --verbose        Show tool calls and reasoning (default: true)
  -q, --quiet          Suppress non-essential output
  -i, --interactive    Launch interactive REPL
  --json               Output as JSON
  --plain              Output as plain text
  --no-input           Disable prompts
  --dry-run            Show what would happen

EXAMPLES:
  ah chat "hello"                          # New session
  ah chat "hello" -c                       # Continue last session
  ah chat "hello" -s abc12345              # Resume specific session
  ah chat -i                               # Interactive REPL
  ah chat "hello" -m gpt-4o                 # Use specific model
  echo "hello" | ah chat                   # Pipe stdin
```

### 9.3 Slash Command Help (`/help`)

```
Slash Commands
──────────────
  /help              Show this help
  /status            Show session and agent status
  /sessions          List recent sessions
  /new               Create a new session
  /switch <id>       Switch to a session
  /context           Show context chunks
  /model [name]      Show or set model
  /provider [name]   Show or set provider
  /budget [n]        Show or set context budget
  /verbose           Toggle verbose mode
  /config            Show configuration
  /clear             Clear screen
  /export [format]   Export conversation
  /search <query>    Search history
  /goal [text]       Set session goal
  /fork              Fork current session
  /archive           Archive current session
  /delete <id>       Delete a session
  /rename <title>    Rename current session
  /memory            Show memories
  /memory-add <text> Add a memory
  /memory-search <q> Search memories
  /memory-delete <id> Delete a memory
  /skills             List skills
  /debug             Toggle debug mode
  /stop              Stop the agent
  /redirect <text>   Redirect the agent
  /exit              Exit the REPL

Press ↑/↓ to navigate, Tab to complete, Enter to execute.
```

---

## 10. Color Scheme

### 10.1 Palette

| Role | Hex | ANSI | Usage |
|------|-----|------|-------|
| Primary | `#00D4FF` | Bright Cyan | Prompt, headers, accents |
| Secondary | `#7C3AED` | Bright Purple | Session IDs, metadata |
| Success | `#10B981` | Green | Success messages, tool results |
| Warning | `#F59E0B` | Yellow | Warnings, tool calls |
| Error | `#EF4444` | Red | Errors, failures |
| Info | `#3B82F6` | Blue | Info messages, hints |
| Muted | `#6B7280` | Bright Black | Timestamps, dim text |
| Text | `#E5E7EB` | White | Primary text |
| Background | `#111827` | — | Panel backgrounds |

### 10.2 Semantic Colors

| Element | Color | Example |
|---------|-------|---------|
| Prompt symbol | Primary (bold) | `ah` |
| Session ID | Secondary (dim) | `(abc12345)` |
| Agent response | Text | `The capital is Paris.` |
| Tool call (→) | Warning | `→ web_search(...)` |
| Tool result (←) | Success | `← Search results...` |
| Error message | Error | `Error: Session not found` |
| Info message | Info | `Model set to: gpt-4o` |
| Muted text | Muted | `Iterations: 1` |
| Loading bar fill | Primary | `████░░░░` |
| Loading bar empty | Muted | `░░░░░░░░` |
| Panel border | Primary | `┌─────┐` |
| Table header | Primary (bold) | `ID Title Status` |
| Table border | Muted | `├───┤` |
| JSON key | Primary | `"model":` |
| JSON string | Success | `"gpt-4o"` |
| JSON number | Warning | `42` |
| JSON boolean | Info | `true` |

### 10.3 Theme Support

| Theme | Description |
|-------|-------------|
| `default` | Dark background, bright colors |
| `light` | Light background, dark colors |
| `high-contrast` | Maximum contrast for accessibility |
| `no-color` | Plain text, no ANSI codes |

### 10.4 Color Disabling

Colors are disabled when:
- `--no-color` flag is set
- `NO_COLOR` environment variable is set
- `TERM=dumb`
- Output is not a TTY (unless `--color` is explicitly set)

---

## 11. Animation Specifications

### 11.1 Spinner (Thinking Indicator)

**When:** Agent is generating a response  
**Style:** Claude Code-style dot spinner  
**Speed:** 100ms per frame

```
Frame 1:  ⠋ Thinking...
Frame 2:  ⠙ Thinking...
Frame 3:  ⠹ Thinking...
Frame 4:  ⠸ Thinking...
Frame 5:  ⠼ Thinking...
Frame 6:  ⠴ Thinking...
Frame 7:  ⠦ Thinking...
Frame 8:  ⠧ Thinking...
Frame 9:  ⠇ Thinking...
Frame 10: ⠏ Thinking...
Frame 11: ⠋ Thinking...  (loops)
```

### 11.2 Loading Bar (Progress Indicator)

**When:** Long operations (LLM calls, tool execution)  
**Style:** Square brackets with block characters  
**Width:** 20 characters

```
[░░░░░░░░░░░░░░░░░░░░] 0%
[████░░░░░░░░░░░░░░░░] 20%
[████████░░░░░░░░░░░░] 40%
[████████████░░░░░░░░] 60%
[████████████████░░░░] 80%
[████████████████████] 100%
```

### 11.3 Streaming Text Effect

**When:** Tokens arrive from LLM  
**Style:** Typewriter effect with cursor

```
The capital of France is Paris.
```

- Each token appears immediately
- Cursor blinks at end of text
- No animation delay — instant display

### 11.4 Tool Execution Animation

**When:** Tool is executing  
**Style:** Spinner with tool name

```
⠋ Executing web_search...
```

### 11.5 Session Switch Animation

**When:** Switching sessions  
**Style:** Brief fade

```
Switching to session abc12345...
```

### 11.6 Error Shake Animation

**When:** Error occurs  
**Style:** Brief red flash

```
┌─────────────────────────────────────────────────────────────┐
│  ✗ Error: Session not found                                  │
└─────────────────────────────────────────────────────────────┘
```

### 11.7 Success Checkmark

**When:** Operation succeeds  
**Style:** Green checkmark

```
✓ Model set to: gpt-4o
✓ Session created: abc12345
✓ Memory deleted
```

### 11.8 ASCII Art Animations

#### Startup Animation

```
  _   _   _   _   _   _   _   _   _   _   _   _   _   _   _   _
 / \ / \ / \ / \ / \ / \ / \ / \ / \ / \ / \ / \ / \ / \ / \ / \
( A | g | e | n | t | H | a | r | n | e | s | s )
 \_/ \_/ \_/ \_/ \_/ \_/ \_/ \_/ \_/ \_/ \_/ \_/ \_/ \_/ \_/ \_/
```

#### Processing Animation

```
  ┌─────────────────────────────────────────┐
  │  ╔═══════════════════════════════════╗  │
  │  ║  Processing...                   ║  │
  │  ║  ┌───┐ ┌───┐ ┌───┐ ┌───┐ ┌───┐  ║  │
  │  ║  │ █ │ │ ░ │ │ ░ │ │ ░ │ │ ░ │  ║  │
  │  ║  └───┘ └───┘ └───┘ └───┘ └───┘  ║  │
  │  ╚═══════════════════════════════════╝  │
  └─────────────────────────────────────────┘
```

#### Completion Animation

```
  ╔═══════════════════════════════════════════╗
  ║                                           ║
  ║   ✓ Complete!                             ║
  ║                                           ║
  ║   ┌─────────────────────────────────────┐ ║
  ║   │  Tokens: 1,234  |  Cost: $0.0123    │ ║
  ║   └─────────────────────────────────────┘ ║
  ║                                           ║
  ╚═══════════════════════════════════════════╝
```

#### Thinking Animation (Extended)

```
  ┌─────────────────────────────────────────┐
  │                                         │
  │   🤔 Thinking...                        │
  │                                         │
  │   ┌─────────────────────────────────┐   │
  │   │ ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ │   │
  │   └─────────────────────────────────┘   │
  │                                         │
  └─────────────────────────────────────────┘
```

### 11.9 Animation Constraints

- All animations respect `--no-color` and `NO_COLOR`
- Animations are disabled when output is not a TTY
- Maximum animation duration: 30 seconds (then static)
- Animations must not interfere with streaming text
- Use `rich.live.Live` for animations that update in place

---

## 12. Loading Bar Design

### 12.1 Claude Code-Style Square Loading Bar

**Appearance:**

```
[████████████████████] 100% ✓
```

**Characters:**

| Character | Purpose |
|-----------|---------|
| `[` | Left bracket |
| `]` | Right bracket |
| `█` | Filled block |
| `░` | Empty block |
| `▒` | Partial fill (optional) |

### 12.2 Loading Bar States

| State | Appearance | Color |
|-------|------------|-------|
| Idle | `[░░░░░░░░░░░░░░░░░░░░]` | Muted |
| Active | `[████████░░░░░░░░░░░░]` | Primary |
| Complete | `[████████████████████]` | Success |
| Error | `[xxxxxxxxxxxxxxxxxxxx]` | Error |
| Indeterminate | `[▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓]` | Warning |

### 12.3 Loading Bar with Label

```
Thinking... [████████████████░░░░] 80%
```

### 12.4 Loading Bar with Tokens

```
[████████████████████] 1,234 / 8,000 tokens
```

### 12.5 Loading Bar with Cost

```
[████████████████████] $0.0123
```

### 12.6 Loading Bar Width

- Default: 20 characters
- Minimum: 10 characters
- Maximum: 40 characters
- Auto-adjusts to terminal width

### 12.7 Loading Bar Behavior

- Updates in place (no scrolling)
- Smooth transitions between states
- Shows percentage when applicable
- Shows token count when known
- Shows cost when available
- Disappears on completion (or shows final state briefly)

---

## 13. Error Display Format

### 13.1 Error Panel

```
┌─────────────────────────────────────────────────────────────┐
│  ✗ Error: Session not found                                  │
│                                                             │
│  Session abc12345-6789-abcd-ef01-23456789abcd does not     │
│  exist.                                                     │
│                                                             │
│  Suggestion: Use /sessions to list available sessions.      │
└─────────────────────────────────────────────────────────────┘
```

### 13.2 Error Colors

| Element | Color |
|---------|-------|
| Border | Error (red) |
| Icon (✗) | Error (bold) |
| Title | Error (bold) |
| Message | Text |
| Suggestion | Info (blue) |

### 13.3 Error Types

| Type | Icon | Color | Example |
|------|------|-------|---------|
| Not found | ✗ | Red | `Session not found` |
| Invalid input | ⚠ | Yellow | `Invalid model name` |
| Permission denied | 🔒 | Red | `Permission denied` |
| Timeout | ⏱ | Yellow | `Request timed out` |
| Connection | 🔌 | Red | `Database connection failed` |
| Unknown | ✗ | Red | `An unexpected error occurred` |

### 13.4 Error with Stack Trace (Debug Mode)

```
┌─────────────────────────────────────────────────────────────┐
│  ✗ Error: LLM provider error                                │
│                                                             │
│  API rate limit exceeded. Retrying in 5s...                 │
│                                                             │
│  Stack trace (use /debug for full trace):                   │
│    File "ah/core/provider.py", line 209, in complete         │
│      resp.raise_for_status()                                │
│    httpx.HTTPStatusError: 429 Too Many Requests             │
└─────────────────────────────────────────────────────────────┘
```

### 13.5 Error Recovery

| Error | Recovery |
|-------|----------|
| Session not found | Suggest `/sessions` |
| Invalid model | Suggest `/model` to see available models |
| Database connection failed | Suggest `ah doctor` |
| LLM rate limit | Auto-retry with backoff |
| LLM timeout | Auto-retry, then suggest checking network |
| Tool execution failed | Show error, continue agent loop |

### 13.6 Error Output Stream

- Errors go to **stderr**
- Warnings go to **stderr**
- Info messages go to **stdout** (unless `--quiet`)
- Success messages go to **stdout**

---

## 14. Output Contract

### 14.1 stdout

- Agent responses
- Command results (tables, panels)
- JSON output (with `--json`)
- Plain text output (with `--plain`)
- Success messages

### 14.2 stderr

- Error messages
- Warning messages
- Debug information (with `--verbose`)
- Audit logs
- Progress indicators (when not using `rich.live`)

### 14.3 TTY Detection

| Condition | Behavior |
|-----------|----------|
| TTY + no flags | Rich output with colors and animations |
| TTY + `--json` | JSON to stdout, no colors |
| TTY + `--plain` | Plain text, no colors |
| TTY + `--no-color` | Rich output, no colors |
| Not a TTY | Plain text, no colors, no animations |
| Not a TTY + `--json` | JSON to stdout |

### 14.4 JSON Output Format

```json
{
  "success": true,
  "data": {
    "message": "The capital of France is Paris.",
    "session_id": "abc12345-6789-abcd-ef01-23456789abcd",
    "model": "anthropic/claude-3.5-sonnet",
    "provider": "openrouter",
    "iterations": 1,
    "tool_calls": 0,
    "tokens_used": 156,
    "cost": 0.000234
  },
  "meta": {
    "timestamp": "2026-10-01T12:00:00Z",
    "version": "0.1.0"
  }
}
```

### 14.5 Plain Text Output Format

```
The capital of France is Paris.

Session: abc12345-6789-abcd-ef01-23456789abcd
Model: anthropic/claude-3.5-sonnet
Tokens: 156
```

---

## 15. Exit Codes

| Code | Meaning | When |
|------|---------|------|
| 0 | Success | Command completed successfully |
| 1 | Generic failure | Unhandled exception |
| 2 | Invalid usage | Bad arguments, unknown command |
| 3 | Not found | Session, memory, or resource not found |
| 4 | Permission denied | Insufficient permissions |
| 5 | Timeout | Operation timed out |
| 6 | Connection error | Database or API connection failed |
| 7 | Rate limited | API rate limit exceeded |
| 8 | Cancelled | User cancelled (Ctrl+C) |

---

## 16. Configuration & Environment

### 16.1 Config File

**Path:** `~/.agent-harness/config.yaml`

```yaml
model: anthropic/claude-3.5-sonnet
provider: openrouter
context_budget: 8000
max_iterations: 10
verbose: true
agent_id: harness
temperature: 0.7
max_tokens: 4096
rate_limit_calls_per_minute: 10
memory_enabled: true
rag_enabled: true
streaming: true
theme: default
history_size: 100
```

### 16.2 Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `OPENROUTER_API_KEY` | OpenRouter API key | `sk-or-...` |
| `DATABASE_URL` | PostgreSQL connection URL | `postgresql://...` |
| `AGENT_HARNESS_MODEL` | Override model | `gpt-4o` |
| `AGENT_HARNESS_PROVIDER` | Override provider | `ollama` |
| `AGENT_HARNESS_VERBOSE` | Override verbose | `true` |
| `AGENT_HARNESS_THEME` | Override theme | `dark` |
| `NO_COLOR` | Disable colors | `1` |

### 16.3 Precedence

```
CLI flags > Environment variables > Config file > Defaults
```

### 16.4 Config Commands

```bash
ah config                          # Show all config
ah config --json                   # Show as JSON
ah config-set model gpt-4o         # Set in memory
ah config-set model gpt-4o -p      # Persist to file
```

---

## 17. Safety Rules

### 17.1 Destructive Operations

| Operation | Confirmation | Bypass |
|-----------|--------------|--------|
| `ah init` (without `--force`) | Skip if exists | `--force` to recreate |
| `ah memory-forget <id>` | Yes | `--force` |
| `/delete <id>` | Yes | `--force` |
| `/archive` | No | — |
| `/fork` | No | — |
| `/clear` | No | — |

### 17.2 Confirmation Prompt

```
┌─────────────────────────────────────────────────────────────┐
│  ⚠ Delete memory abc12345?                                  │
│                                                             │
│  This action cannot be undone.                              │
│                                                             │
│  [y/N]                                                      │
└─────────────────────────────────────────────────────────────┘
```

### 17.3 Dry Run

```bash
ah init --dry-run
# Output:
# Would create table: sessions
# Would create table: context_chunks
# Would create table: memories
# No changes made.
```

### 17.4 No-Input Mode

- `--no-input` disables all prompts
- Commands that require input will fail with an error
- Useful for scripting and CI/CD

### 17.5 Ctrl-C Handling

| State | Behavior |
|-------|----------|
| Idle (at prompt) | Exit REPL |
| Agent running | Interrupt agent, return to prompt |
| Tool executing | Cancel tool, return to prompt |
| Loading | Cancel loading, return to prompt |

---

## 18. Shell Completion

### 18.1 Supported Shells

| Shell | Installation |
|-------|-------------|
| Bash | `ah completion bash > ~/.bash_completion.d/ah` |
| Zsh | `ah completion zsh > ~/.zsh/completions/_ah` |
| Fish | `ah completion fish > ~/.config/fish/completions/ah.fish` |

### 18.2 Completion Commands

```bash
ah completion bash    # Generate Bash completion script
ah completion zsh     # Generate Zsh completion script
ah completion fish    # Generate Fish completion script
```

### 18.3 Completion Behavior

- Completes subcommands
- Completes flags
- Completes model names (for `--model`)
- Completes provider names (for `--provider`)
- Completes session IDs (for `--session`, `/switch`)
- Completes config keys (for `config-set`)

---

## 19. Example Invocations

### 19.1 Basic Usage

```bash
# One-shot chat
ah chat "What is the capital of France?"

# Interactive REPL
ah repl

# Continue last session
ah chat -c "Tell me more"

# Resume specific session
ah chat -s abc12345 "Continue where we left off"
```

### 19.2 Piping and Scripting

```bash
# Pipe stdin to chat
echo "Summarize this" | ah chat

# JSON output for scripting
ah chat "hello" --json | jq '.data.message'

# Plain text for grep
ah status --plain | grep "Sessions"

# Quiet mode for CI
ah doctor --quiet && echo "OK"
```

### 19.3 Session Management

```bash
# List sessions
ah sessions --limit 20

# Switch session in REPL
/switch abc12345

# Fork session
/fork

# Archive session
/archive
```

### 19.4 Configuration

```bash
# Show config
ah config

# Set model
ah config-set model gpt-4o --persist

# Set provider
ah config-set provider ollama --persist
```

### 19.5 Memory Management

```bash
# List memories
ah memory-list --limit 50

# Search memories
ah memory-search "project decisions"

# Delete memory
ah memory-forget abc12345 --force
```

### 19.6 Diagnostics

```bash
# Check system
ah doctor

# Initialize database
ah init

# Initialize with custom DB URL
ah init --db-url postgresql://user:pass@host:5432/db
```

### 19.7 Interactive REPL Session

```bash
$ ah repl

┌─────────────────────────────────────────────────────────────┐
│  AgentHarness Interactive REPL v0.1.0                       │
│                                                             │
│  Session: abc12345-6789-abcd-ef01-23456789abcd              │
│  Model: anthropic/claude-3.5-sonnet | Provider: openrouter   │
│  Type /help for commands, /exit to quit.                    │
└─────────────────────────────────────────────────────────────┘

ah (abc12345) > What is the capital of France?

⠋ Thinking... [████████████████████] 100% ✓

┌─────────────────────────────────────────────────────────────┐
│  The capital of France is Paris.                            │
└─────────────────────────────────────────────────────────────┘

Iterations: 1 | Tool calls: 0 | Tokens: 156

ah (abc12345) > /model gpt-4o
✓ Model set to: gpt-4o

ah (abc12345) > /sessions
┌──────────┬─────────────────────┬────────┬─────────────────────┐
│ ID       │ Title               │ Status │ Last Activity       │
├──────────┼─────────────────────┼────────┼─────────────────────┤
│ abc12345 │ Interactive REPL    │ active │ 2026-10-01 12:00   │
│ def67890 │ Code review         │ active │ 2026-10-01 11:30   │
└──────────┴─────────────────────┴────────┴─────────────────────┘

ah (abc12345) > /exit
Goodbye!
```

---

## 20. Implementation Notes

### 20.1 Technology Stack

| Component | Library | Purpose |
|-----------|---------|---------|
| CLI framework | Typer | Command parsing, help generation |
| Rich output | Rich | Panels, tables, colors, live display |
| REPL | prompt_toolkit | Input handling, autocomplete, history |
| Async | asyncio | Async I/O |
| Database | asyncpg | PostgreSQL driver |
| HTTP | httpx | API calls |
| YAML | pyyaml | Config file parsing |
| Token counting | tiktoken | Accurate token counts |

### 20.2 Key Dependencies

```
typer>=0.9.0
rich>=13.0.0
asyncpg>=0.29.0
httpx>=0.27.0
python-dotenv>=1.0.0
msgpack>=1.0.0
pyyaml>=6.0.0
tiktoken>=0.5.0
prompt-toolkit>=3.0.0
```

### 20.3 File Structure

```
ah/
├── __init__.py              # Version
├── __main__.py              # Entry point
├── cli/
│   ├── __init__.py          # Typer app
│   ├── interactive.py       # REPL implementation
│   ├── commands.py          # One-shot commands
│   └── autocomplete.py      # Autocomplete logic
├── core/
│   ├── agent.py             # ReAct loop
│   ├── assembler.py         # Prompt assembly
│   ├── config.py            # Configuration
│   ├── context.py           # Context management
│   ├── models.py            # Domain models
│   ├── provider.py          # LLM provider
│   └── session.py           # Session management
├── db/
│   ├── connection.py        # Database connection
│   └── schema.sql           # Database schema
├── memory/                  # Memory system
├── rag/                     # RAG pipeline
├── skills/                  # Skill system
└── tools/                   # Tool registry
```

### 20.4 Animation Implementation

Use `rich.live.Live` for in-place updates:

```python
from rich.live import Live
from rich.text import Text

with Live(console=console, refresh_per_second=10) as live:
    for frame in spinner_frames:
        live.update(Text(frame, style="cyan"))
        await asyncio.sleep(0.1)
```

### 20.5 Autocomplete Implementation

Use `prompt_toolkit.completion.Completer`:

```python
from prompt_toolkit.completion import Completer, Completion

class SlashCommandCompleter(Completer):
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if text.startswith("/"):
            for cmd in SLASH_COMMANDS:
                if cmd.startswith(text):
                    yield Completion(cmd, start_position=-len(text))
```

### 20.6 Error Handling Pattern

```python
try:
    result = await operation()
except Exception as e:
    console.print(Panel(
        f"[red]✗ Error:[/red] {e}\n\n"
        f"[dim]Suggestion: {suggestion}[/dim]",
        border_style="red",
    ))
    raise typer.Exit(1)
```

### 20.7 Testing Strategy

- Unit tests for each command
- Integration tests for REPL
- Snapshot tests for output format
- Property-based tests for autocomplete
- Mock LLM provider for testing

---

## Appendix A: Quick Reference

### A.1 Command Shortcuts

| Shortcut | Command |
|----------|---------|
| `ah` | `ah repl` (if no args) |
| `ah c` | `ah chat` |
| `ah r` | `ah repl` |
| `ah s` | `ah status` |
| `ah v` | `ah version` |

### A.2 Keyboard Shortcuts (REPL)

| Key | Action |
|-----|--------|
| Enter | Submit |
| Ctrl+C | Interrupt / Exit |
| Ctrl+D | Exit |
| Ctrl+L | Clear screen |
| Ctrl+R | Search history |
| Up/Down | History navigation |
| Tab | Accept completion |

### A.3 Environment Variables Quick Reference

| Variable | Required | Default |
|----------|----------|---------|
| `OPENROUTER_API_KEY` | Yes (for OpenRouter) | — |
| `DATABASE_URL` | No | `postgresql://localhost/agentharness` |
| `AGENT_HARNESS_MODEL` | No | `anthropic/claude-3.5-sonnet` |
| `AGENT_HARNESS_PROVIDER` | No | `openrouter` |
| `NO_COLOR` | No | — |

---

**End of Spec**
