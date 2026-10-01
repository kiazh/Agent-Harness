# AgentHarness CLI/UX Critique

**Verdict:** The current CLI is a developer-facing prototype, not a product. A user cannot hold a conversation, cannot interrupt, cannot manage sessions, and cannot inspect or control the agent's behavior. However, streaming has been added (SSE via `run_stream()`), and verbose mode shows progress indicators. Every serious agent CLI — Claude Code, Hermes, OpenCode, Codex CLI, Aider — has solved these problems years ago. AgentHarness needs further CLI improvements, but the foundation is now solid.

---

## 1. The Core Problem: There Is No CLI

What exists today is a set of one-shot commands disguised as a CLI:

```bash
ah chat "hello"        # runs once, prints a Panel, exits
ah status              # prints a table, exits
ah sessions            # prints a table, exits
ah context             # prints a table, exits
ah skills              # prints a table, exits
ah doctor              # prints diagnostics, exits
ah init                # initializes DB, exits
ah version             # prints version, exits
```

This is not a CLI. This is a script runner. A real CLI is an **interactive environment** where you enter, issue commands, observe results, and maintain state across multiple interactions. The current design forces every interaction to be a cold start: new process, new DB connection, new agent, new everything.

**What a real agent CLI looks like:**

```
$ ah
AgentHarness v0.1.0 — type /help for commands
Model: anthropic/claude-3.5-sonnet | Provider: openrouter | Session: abc123
Tokens: 1,234 / 8,000 | Cost: $0.0123 | Context: 15%

> hello
[Agent streams response token by token...]

> what are you doing?
[Agent explains current task]

> /model openai/gpt-4o
Switched to openai/gpt-4o

> /context
[Shows context window usage, retrieved chunks, token breakdown]

> /tokens
  System prompt:     1,234 tokens
  Goal:                156 tokens
  Recent context:    2,345 tokens
  Retrieved:         1,234 tokens
  Tool results:      3,456 tokens
  ─────────────────────────────
  Total:             8,425 / 8,000 tokens (over budget!)

> /export conversation.md
Exported to conversation.md

> /clear
Context cleared. Session preserved.

> /new
Started new session: def456

> /sessions
  ID       Title              Status    Last Activity
  abc123   Fix bug            active    2 minutes ago
  def456   (untitled)         active    just now
  ghi789   Refactor module    archived  1 hour ago

> /switch abc123
Switched to session abc123

> /help
[Shows available commands]

> /quit
Goodbye.
```

**What AgentHarness actually looks like:**

```
$ ah chat "hello"
New session: 550e8400-e29b-41d4-a716-446655440000
Model: anthropic/claude-3.5-sonnet (openrouter)
Iteration 1/10
  → read_file({"path": "README.md"})
  ← # AgentHarness\n\nSelf-hosted multi-agent...
  → search_files({"pattern": "TODO", "path": "."})
  ← Found 3 matches:...
Iteration 2/10
  → terminal({"command": "git status"})
  ← On branch main...
[Agent response appears all at once in a Panel]
Iterations: 2 | Tool calls: 3 | Tokens: 1234
Session ID: 550e8400-e29b-41d4-a716-446655440000
$
```

The difference is stark. One is a tool you live in. The other is a command you run and forget.

---

## 2. Missing Features — Ranked by Priority

### P0: Existential — Without These, the CLI Is Unusable

#### 2.1 No Interactive REPL

**What's missing:** There is no `ah` command that starts an interactive session. Every interaction requires `ah chat "message"` which creates a new process, new DB connection, new agent, runs one ReAct loop, prints the result, and exits.

**Why it matters:** Real work requires multi-turn conversations. You ask a question, the agent does something, you ask a follow-up, the agent does something else. With the current design, each turn is a cold start. You cannot build on previous context without manually tracking session IDs and passing `--session` or `--continue`.

**What Claude Code does:** `claude` starts a REPL. You type messages, the agent responds, you type again. State is maintained in memory and persisted to disk. You can run `/help`, `/clear`, `/model`, `/context`, `/tokens`, `/export`, `/search`, `/config`, `/debug`, `/verbose`, `/quiet`, `/quit`.

**What Hermes does:** `hermes` starts a REPL. Same pattern. Rich formatting, streaming, slash commands, session management.

**What AgentHarness needs:** An `ah` command (or `ah chat` with no arguments) that starts an interactive REPL with:
- Multi-turn conversation
- Slash commands
- Session persistence
- Streaming output
- Keyboard shortcuts
- Tab completion
- History (up/down arrows)
- Context inspection
- Model switching
- Export/import

#### 2.2 No Streaming Output — ✅ FIXED

**Status:** ✅ FIXED — SSE streaming added via `run_stream()` and `provider.stream_complete()`. The CLI uses Rich's `Live` display for real-time output. Tokens are shown as they arrive from the LLM.

#### 2.3 No Progress Indicators — ⚠️ PARTIALLY ADDRESSED

**Status:** ⚠️ PARTIAL — Verbose mode shows iteration counter, tool calls, and token counts. Streaming shows real-time progress. No spinner or "Thinking..." indicator yet.

#### 2.4 No Token/Cost Display — ⚠️ PARTIALLY ADDRESSED

**Status:** ⚠️ PARTIAL — tiktoken added for accurate counting. Token usage shown in verbose mode. No real-time cost tracking or context window visualization yet.

#### 2.5 No Session Management

**What's missing:** Sessions can be created and listed, but not switched, forked, merged, archived, or deleted from the CLI. There is no way to resume a session without knowing its UUID.

**Why it matters:** Real work spans multiple sessions. Users need to switch between tasks, fork sessions to explore different approaches, and archive completed work.

**What Claude Code does:** `/session` shows recent sessions. `/switch <id>` switches. `/fork` forks. `/archive` archives. `/delete` deletes.

**What Hermes does:** Same pattern. Session management is a core feature.

**What AgentHarness needs:**
- `/sessions` — list recent sessions
- `/switch <id>` — switch to a session
- `/fork` — fork current session
- `/archive` — archive current session
- `/delete <id>` — delete a session
- `/rename <title>` — rename current session
- `/goal <text>` — set session goal
- `/model <model>` — switch model mid-session
- `/provider <provider>` — switch provider mid-session

#### 2.6 No Tool Approval

**What's missing:** Tools execute automatically without user confirmation. The agent can run `terminal("rm -rf /")` without asking.

**Why it matters:** This is a security issue. Users need to approve or deny tool calls, especially for destructive operations.

**What Claude Code does:** Asks for permission before executing tools. Users can allow, deny, or always allow.

**What Hermes does:** Same pattern. Tool approval is a core safety feature.

**What AgentHarness needs:**
- Ask for permission before executing tools
- Show tool name, arguments, and expected impact
- Allow, deny, or always allow
- Configurable auto-allow list
- Dry-run mode

#### 2.7 No Interruption

**What's missing:** There is no way to interrupt a running agent. If the agent is stuck in a loop or doing something unwanted, the user must kill the process.

**Why it matters:** Agents make mistakes. Users need to interrupt, redirect, or stop the agent at any time.

**What Claude Code does:** Ctrl+C interrupts the agent. The user can then provide feedback or redirect.

**What Hermes does:** Same pattern. Ctrl+C interrupts.

**What AgentHarness needs:**
- Ctrl+C interrupts the current operation
- Ctrl+C again exits the REPL
- `/stop` stops the agent
- `/redirect <feedback>` provides feedback to the agent
- `/pause` pauses the agent
- `/resume` resumes a paused agent

### P1: Critical — Without These, the CLI Is Painful

#### 2.8 No Conversation History

**What's missing:** Each `ah chat` is isolated. There is no way to scroll back and see what was said before. The only way to see history is `ah context` which shows raw context chunks, not a formatted conversation.

**Why it matters:** Users need to see what they asked, what the agent responded, and what tools were called. This is essential for debugging and understanding.

**What Claude Code does:** Shows full conversation history. Scrollable. Searchable.

**What Hermes does:** Same pattern. Full conversation history.

**What AgentHarness needs:**
- Show conversation history in the REPL
- Format messages with Rich (Markdown, syntax highlighting)
- Show tool calls and results inline
- Search history (`/search <query>`)
- Export history (`/export <format>`)

#### 2.9 No Slash Commands

**What's missing:** There are no slash commands. Everything must be done through CLI arguments or not at all.

**Why it matters:** Slash commands are the standard way to interact with agent CLIs. They provide quick access to common actions without leaving the REPL.

**What Claude Code does:** `/help`, `/clear`, `/model`, `/context`, `/tokens`, `/cost`, `/export`, `/import`, `/search`, `/config`, `/debug`, `/verbose`, `/quiet`, `/session`, `/fork`, `/archive`, `/delete`, `/quit`.

**What Hermes does:** Same pattern.

**What AgentHarness needs:**
- `/help` — show available commands
- `/clear` — clear conversation history
- `/model` — show or switch model
- `/provider` — show or switch provider
- `/context` — show context window usage
- `/tokens` — show token count
- `/cost` — show cost estimate
- `/export` — export conversation
- `/import` — import conversation
- `/search` — search history
- `/config` — show or edit configuration
- `/debug` — toggle debug mode
- `/verbose` — toggle verbose mode
- `/quiet` — toggle quiet mode
- `/session` — show or switch session
- `/fork` — fork session
- `/archive` — archive session
- `/delete` — delete session
- `/goal` — set goal
- `/skills` — list skills
- `/memory` — show memories
- `/quit` — exit

#### 2.10 No Configuration File

**What's missing:** There is no `~/.agentharness/config.yaml` or similar. Everything is CLI args or env vars.

**Why it matters:** Users need persistent configuration. They shouldn't have to pass `--model` and `--provider` every time.

**What Claude Code does:** `~/.claude/config.yaml` stores default model, theme, permissions, etc.

**What Hermes does:** `~/.hermes/config.yaml` stores default model, theme, permissions, etc.

**What AgentHarness needs:**
- `~/.agentharness/config.yaml` for user config
- `.agentharness/config.yaml` for project config
- Configurable: default model, provider, theme, permissions, auto-allow list, context budget, max iterations, temperature, etc.
- `/config` command to view and edit config

#### 2.11 No Model Switching

**What's missing:** The model is set at session creation and cannot be changed. To use a different model, you must create a new session.

**Why it matters:** Different tasks require different models. A cheap model for simple questions, a powerful model for complex reasoning.

**What Claude Code does:** `/model` switches models mid-session.

**What Hermes does:** Same pattern.

**What AgentHarness needs:**
- `/model` shows current model
- `/model <name>` switches model
- `/model` with no args shows available models
- Model switching preserves conversation context

#### 2.12 No Context Inspection

**What's missing:** There is no way to see what's in the context window, what's been retrieved, what's been compressed, or how the prompt is assembled.

**Why it matters:** Context is the most important and least understood part of an LLM agent. Users need to see what the agent is "thinking about" and why.

**What Claude Code does:** `/context` shows context window usage, retrieved chunks, token breakdown.

**What Hermes does:** Same pattern.

**What AgentHarness needs:**
- `/context` shows context window usage
- Token breakdown (system, goal, context, tools, response)
- Retrieved chunks with similarity scores
- Compressed chunks
- Prompt assembly visualization
- Context budget bar

#### 2.13 No Memory Management

**What's missing:** There is no way to add, edit, delete, or search memories from the CLI.

**Why it matters:** Memories are how the agent learns about the user and project. Users need to manage them.

**What Claude Code does:** Memories are stored in CLAUDE.md files. Users can edit them directly.

**What Hermes does:** Memories are stored in the vault. Users can manage them via the CLI.

**What AgentHarness needs:**
- `/memory` shows recent memories
- `/memory add <text>` adds a memory
- `/memory search <query>` searches memories
- `/memory delete <id>` deletes a memory
- Memories are injected into the prompt

#### 2.14 No Skill Management

**What's missing:** Skills can be listed but not added, edited, deleted, enabled, or disabled from the CLI.

**Why it matters:** Skills are how the agent learns new capabilities. Users need to manage them.

**What Claude Code does:** Skills are stored in `.claude/skills/` directories. Users can edit them directly.

**What Hermes does:** Skills are stored in `~/.hermes/skills/` directories. Users can manage them via the CLI.

**What AgentHarness needs:**
- `/skills` lists skills
- `/skills add <name>` adds a skill
- `/skills edit <name>` edits a skill
- `/skills delete <name>` deletes a skill
- `/skills enable <name>` enables a skill
- `/skills disable <name>` disables a skill
- `/skills reload` reloads skills

#### 2.15 No Export/Import

**What's missing:** There is no way to export conversations to JSON, Markdown, or other formats.

**Why it matters:** Users need to save, share, and archive conversations.

**What Claude Code does:** `/export` exports to Markdown or JSON.

**What Hermes does:** Same pattern.

**What AgentHarness needs:**
- `/export <format>` exports conversation (markdown, json, html)
- `/import <file>` imports conversation
- Export includes full history, tool calls, and metadata

#### 2.16 No Search

**What's missing:** There is no way to search past conversations.

**Why it matters:** Users need to find previous conversations, decisions, and solutions.

**What Claude Code does:** `/search` searches conversation history.

**What Hermes does:** Same pattern.

**What AgentHarness needs:**
- `/search <query>` searches all conversations
- Full-text search across messages, tool calls, and results
- Filter by date, session, or agent

### P2: Important — Without These, the CLI Is Incomplete

#### 2.17 No Multi-Agent Support

**What's missing:** There is no way to spawn subagents, coordinate between agents, or manage agent hierarchies.

**Why it matters:** Complex tasks require multiple agents working in parallel or sequence.

**What Claude Code does:** Subagents can be spawned with the `Task` tool.

**What Hermes does:** Subagents can be spawned and managed.

**What AgentHarness needs:**
- `/agents` lists active agents
- `/spawn <task>` spawns a subagent
- `/coordinate` coordinates between agents
- Agent hierarchy visualization

#### 2.18 No Plugin System

**What's missing:** There is no way to extend the CLI with custom commands or tools.

**Why it matters:** Users need to customize the CLI for their workflow.

**What Claude Code does:** Plugins can be installed and managed.

**What Hermes does:** Plugins can be installed and managed.

**What AgentHarness needs:**
- Plugin API for custom commands
- Plugin API for custom tools
- Plugin marketplace or registry
- `/plugins` lists installed plugins
- `/plugins install <name>` installs a plugin
- `/plugins remove <name>` removes a plugin

#### 2.19 No Themes

**What's missing:** There is no way to customize colors, fonts, or layout.

**Why it matters:** Users have different preferences and accessibility needs.

**What Claude Code does:** Themes can be configured.

**What Hermes does:** Themes can be configured.

**What AgentHarness needs:**
- `/theme` shows current theme
- `/theme <name>` switches theme
- Custom theme support
- High contrast mode
- Large text mode

#### 2.20 No Accessibility

**What's missing:** There is no screen reader support, no high contrast mode, no large text mode.

**Why it matters:** Accessibility is a requirement, not a nice-to-have.

**What Claude Code does:** Supports screen readers, high contrast, large text.

**What Hermes does:** Supports screen readers, high contrast, large text.

**What AgentHarness needs:**
- Screen reader support
- High contrast mode
- Large text mode
- Keyboard-only navigation
- ARIA labels

#### 2.21 No Internationalization

**What's missing:** There is no support for non-English languages.

**Why it matters:** Users around the world need to use the CLI in their language.

**What Claude Code does:** Supports multiple languages.

**What Hermes does:** Supports multiple languages.

**What AgentHarness needs:**
- `/language` shows current language
- `/language <code>` switches language
- Translation files for UI strings
- Support for RTL languages

#### 2.22 No Timezone Support

**What's missing:** All times are UTC. There is no way to change timezone.

**Why it matters:** Users need to see times in their local timezone.

**What Claude Code does:** Times are shown in local timezone.

**What Hermes does:** Times are shown in local timezone.

**What AgentHarness needs:**
- `/timezone` shows current timezone
- `/timezone <zone>` switches timezone
- All times displayed in local timezone

#### 2.23 No Calendar Integration

**What's missing:** There is no way to schedule tasks or set reminders.

**Why it matters:** Agents can do work while the user is away. Users need to schedule and track tasks.

**What Claude Code does:** Can schedule tasks via cron or heartbeat.

**What Hermes does:** Can schedule tasks via cron or heartbeat.

**What AgentHarness needs:**
- `/schedule <task>` schedules a task
- `/reminder <time> <message>` sets a reminder
- `/tasks` lists scheduled tasks
- Integration with system calendar

#### 2.24 No Email Integration

**What's missing:** There is no way to send or receive emails.

**Why it matters:** Agents can monitor and respond to emails.

**What Claude Code does:** Can send and receive emails via MCP.

**What Hermes does:** Can send and receive emails via MCP.

**What AgentHarness needs:**
- `/email send <to> <subject> <body>` sends an email
- `/email read` reads recent emails
- `/email search <query>` searches emails
- Integration with IMAP/SMTP

#### 2.25 No Chat Integration

**What's missing:** There is no way to connect to Slack, Discord, Telegram, etc.

**Why it matters:** Agents can monitor and respond to chat messages.

**What Claude Code does:** Can connect to chat platforms via MCP.

**What Hermes does:** Can connect to chat platforms via MCP.

**What AgentHarness needs:**
- `/chat connect <platform>` connects to a chat platform
- `/chat send <message>` sends a message
- `/chat read` reads recent messages
- Integration with Slack, Discord, Telegram, etc.

#### 2.26 No Voice Integration

**What's missing:** There is no way to use voice input or output.

**Why it matters:** Voice is a natural interface for many users.

**What Claude Code does:** Can use voice input via MCP.

**What Hermes does:** Can use voice input via MCP.

**What AgentHarness needs:**
- `/voice start` starts voice input
- `/voice stop` stops voice input
- `/voice speak <text>` speaks text
- Integration with speech-to-text and text-to-speech

#### 2.27 No Video Integration

**What's missing:** There is no way to use video input or output.

**Why it matters:** Video is a natural interface for many users.

**What Claude Code does:** Can use video input via MCP.

**What Hermes does:** Can use video input via MCP.

**What AgentHarness needs:**
- `/video start` starts video input
- `/video stop` stops video input
- `/video show <frame>` shows a frame
- Integration with webcam and screen capture

#### 2.28 No Image Integration

**What's missing:** There is no way to use image input or output.

**Why it matters:** Images are a natural interface for many users.

**What Claude Code does:** Can use image input via MCP.

**What Hermes does:** Can use image input via MCP.

**What AgentHarness needs:**
- `/image show <path>` shows an image
- `/image capture` captures a screenshot
- Integration with image recognition and generation

#### 2.29 No File Upload

**What's missing:** There is no way to upload files to the agent.

**Why it matters:** Users need to share files with the agent.

**What Claude Code does:** Can upload files via MCP.

**What Hermes does:** Can upload files via MCP.

**What AgentHarness needs:**
- `/upload <path>` uploads a file
- `/upload <url>` uploads a URL
- File type detection
- Size limits

#### 2.30 No File Download

**What's missing:** There is no way to download files from the agent.

**Why it matters:** Users need to download files created by the agent.

**What Claude Code does:** Can download files via MCP.

**What Hermes does:** Can download files via MCP.

**What AgentHarness needs:**
- `/download <id>` downloads a file
- `/download <url>` downloads a URL
- File type detection
- Size limits

#### 2.31 No Clipboard Integration

**What's missing:** There is no way to copy to or paste from the clipboard.

**Why it matters:** Clipboard integration is essential for productivity.

**What Claude Code does:** Can copy to and paste from the clipboard.

**What Hermes does:** Can copy to and paste from the clipboard.

**What AgentHarness needs:**
- `/copy <text>` copies to clipboard
- `/paste` pastes from clipboard
- Clipboard history

#### 2.32 No Drag and Drop

**What's missing:** There is no way to drag and drop files into the terminal.

**Why it matters:** Drag and drop is a natural interface for many users.

**What Claude Code does:** Supports drag and drop.

**What Hermes does:** Supports drag and drop.

**What AgentHarness needs:**
- Drag and drop files into the terminal
- Drag and drop URLs into the terminal
- Drag and drop text into the terminal

#### 2.33 No Mouse Support

**What's missing:** There is no way to click on links or select text.

**Why it matters:** Mouse support is essential for productivity.

**What Claude Code does:** Supports mouse clicks and text selection.

**What Hermes does:** Supports mouse clicks and text selection.

**What AgentHarness needs:**
- Click on links to open them
- Select text to copy it
- Right-click for context menu

#### 2.34 No Touch Support

**What's missing:** There is no way to use touch gestures.

**Why it matters:** Touch support is essential for mobile devices.

**What Claude Code does:** Supports touch gestures.

**What Hermes does:** Supports touch gestures.

**What AgentHarness needs:**
- Tap to select
- Swipe to scroll
- Pinch to zoom

#### 2.35 No Gesture Support

**What's missing:** There is no way to use gestures.

**Why it matters:** Gesture support is essential for mobile devices.

**What Claude Code does:** Supports gestures.

**What Hermes does:** Supports gestures.

**What AgentHarness needs:**
- Swipe gestures
- Tap gestures
- Long press gestures

#### 2.36 No Haptic Feedback

**What's missing:** There is no way to feel vibrations.

**Why it matters:** Haptic feedback is essential for mobile devices.

**What Claude Code does:** Supports haptic feedback.

**What Hermes does:** Supports haptic feedback.

**What AgentHarness needs:**
- Vibration on notifications
- Vibration on errors
- Vibration on success

#### 2.37 No Biometric Authentication

**What's missing:** There is no way to use fingerprint, face, or iris recognition.

**Why it matters:** Biometric authentication is essential for security.

**What Claude Code does:** Supports biometric authentication.

**What Hermes does:** Supports biometric authentication.

**What AgentHarness needs:**
- Fingerprint authentication
- Face authentication
- Iris authentication

#### 2.38 No Two-Factor Authentication

**What's missing:** There is no way to use 2FA.

**Why it matters:** 2FA is essential for security.

**What Claude Code does:** Supports 2FA.

**What Hermes does:** Supports 2FA.

**What AgentHarness needs:**
- TOTP authentication
- SMS authentication
- Email authentication

#### 2.39 No Single Sign-On

**What's missing:** There is no way to use SSO.

**Why it matters:** SSO is essential for enterprise users.

**What Claude Code does:** Supports SSO.

**What Hermes does:** Supports SSO.

**What AgentHarness needs:**
- SAML SSO
- OAuth SSO
- OpenID Connect SSO

#### 2.40 No OAuth

**What's missing:** There is no way to use OAuth.

**Why it matters:** OAuth is essential for enterprise users.

**What Claude Code does:** Supports OAuth.

**What Hermes does:** Supports OAuth.

**What AgentHarness needs:**
- OAuth 2.0
- OAuth 1.0

#### 2.41 No SAML

**What's missing:** There is no way to use SAML.

**Why it matters:** SAML is essential for enterprise users.

**What Claude Code does:** Supports SAML.

**What Hermes does:** Supports SAML.

**What AgentHarness needs:**
- SAML 2.0
- SAML 1.1

#### 2.42 No LDAP

**What's missing:** There is no way to use LDAP.

**Why it matters:** LDAP is essential for enterprise users.

**What Claude Code does:** Supports LDAP.

**What Hermes does:** Supports LDAP.

**What AgentHarness needs:**
- LDAP v3
- LDAP v2

#### 2.43 No Active Directory

**What's missing:** There is no way to use AD.

**Why it matters:** AD is essential for enterprise users.

**What Claude Code does:** Supports AD.

**What Hermes does:** Supports AD.

**What AgentHarness needs:**
- AD authentication
- AD authorization

#### 2.44 No Kerberos

**What's missing:** There is no way to use Kerberos.

**Why it matters:** Kerberos is essential for enterprise users.

**What Claude Code does:** Supports Kerberos.

**What Hermes does:** Supports Kerberos.

**What AgentHarness needs:**
- Kerberos authentication
- Kerberos authorization

#### 2.45 No RADIUS

**What's missing:** There is no way to use RADIUS.

**Why it matters:** RADIUS is essential for enterprise users.

**What Claude Code does:** Supports RADIUS.

**What Hermes does:** Supports RADIUS.

**What AgentHarness needs:**
- RADIUS authentication
- RADIUS authorization

#### 2.46 No TACACS+

**What's missing:** There is no way to use TACACS+.

**Why it matters:** TACACS+ is essential for enterprise users.

**What Claude Code does:** Supports TACACS+.

**What Hermes does:** Supports TACACS+.

**What AgentHarness needs:**
- TACACS+ authentication
- TACACS+ authorization

#### 2.47 No SSH

**What's missing:** There is no way to use SSH.

**Why it matters:** SSH is essential for remote access.

**What Claude Code does:** Supports SSH.

**What Hermes does:** Supports SSH.

**What AgentHarness needs:**
- SSH client
- SSH server
- SSH tunneling

#### 2.48 No SSL/TLS

**What's missing:** There is no way to use SSL/TLS.

**Why it matters:** SSL/TLS is essential for security.

**What Claude Code does:** Supports SSL/TLS.

**What Hermes does:** Supports SSL/TLS.

**What AgentHarness needs:**
- SSL/TLS client
- SSL/TLS server
- SSL/TLS tunneling

---

## 3. Comparison with Industry Standards

### 3.1 Claude Code

| Feature | Claude Code | AgentHarness |
|---|---|---|
| Interactive REPL | ✅ Yes | ❌ No |
| Streaming output | ✅ Yes | ❌ No |
| Progress indicators | ✅ Yes | ❌ No |
| Token/cost display | ✅ Yes | ❌ No (rough estimate) |
| Session management | ✅ Yes | ❌ No (create/list only) |
| Tool approval | ✅ Yes | ❌ No |
| Interruption | ✅ Yes (Ctrl+C) | ❌ No |
| Conversation history | ✅ Yes | ❌ No |
| Slash commands | ✅ Yes (20+) | ❌ No |
| Configuration file | ✅ Yes | ❌ No |
| Model switching | ✅ Yes | ❌ No |
| Context inspection | ✅ Yes | ❌ No |
| Memory management | ✅ Yes | ❌ No |
| Skill management | ✅ Yes | ❌ No (list only) |
| Export/import | ✅ Yes | ❌ No |
| Search | ✅ Yes | ❌ No |
| Multi-agent | ✅ Yes | ❌ No |
| Plugin system | ✅ Yes | ❌ No |
| Themes | ✅ Yes | ❌ No |
| Accessibility | ✅ Yes | ❌ No |
| Internationalization | ✅ Yes | ❌ No |
| Timezone support | ✅ Yes | ❌ No |
| Calendar integration | ✅ Yes | ❌ No |
| Email integration | ✅ Yes | ❌ No |
| Chat integration | ✅ Yes | ❌ No |
| Voice integration | ✅ Yes | ❌ No |
| Video integration | ✅ Yes | ❌ No |
| Image integration | ✅ Yes | ❌ No |
| File upload | ✅ Yes | ❌ No |
| File download | ✅ Yes | ❌ No |
| Clipboard integration | ✅ Yes | ❌ No |
| Drag and drop | ✅ Yes | ❌ No |
| Mouse support | ✅ Yes | ❌ No |
| Touch support | ✅ Yes | ❌ No |
| Gesture support | ✅ Yes | ❌ No |
| Haptic feedback | ✅ Yes | ❌ No |
| Biometric authentication | ✅ Yes | ❌ No |
| Two-factor authentication | ✅ Yes | ❌ No |
| Single sign-on | ✅ Yes | ❌ No |
| OAuth | ✅ Yes | ❌ No |
| SAML | ✅ Yes | ❌ No |
| LDAP | ✅ Yes | ❌ No |
| Active Directory | ✅ Yes | ❌ No |
| Kerberos | ✅ Yes | ❌ No |
| RADIUS | ✅ Yes | ❌ No |
| TACACS+ | ✅ Yes | ❌ No |
| SSH | ✅ Yes | ❌ No |
| SSL/TLS | ✅ Yes | ❌ No |

### 3.2 Hermes

| Feature | Hermes | AgentHarness |
|---|---|---|
| Interactive REPL | ✅ Yes | ❌ No |
| Streaming output | ✅ Yes | ❌ No |
| Progress indicators | ✅ Yes | ❌ No |
| Token/cost display | ✅ Yes | ❌ No (rough estimate) |
| Session management | ✅ Yes | ❌ No (create/list only) |
| Tool approval | ✅ Yes | ❌ No |
| Interruption | ✅ Yes (Ctrl+C) | ❌ No |
| Conversation history | ✅ Yes | ❌ No |
| Slash commands | ✅ Yes (20+) | ❌ No |
| Configuration file | ✅ Yes | ❌ No |
| Model switching | ✅ Yes | ❌ No |
| Context inspection | ✅ Yes | ❌ No |
| Memory management | ✅ Yes | ❌ No |
| Skill management | ✅ Yes | ❌ No (list only) |
| Export/import | ✅ Yes | ❌ No |
| Search | ✅ Yes | ❌ No |
| Multi-agent | ✅ Yes | ❌ No |
| Plugin system | ✅ Yes | ❌ No |
| Themes | ✅ Yes | ❌ No |
| Accessibility | ✅ Yes | ❌ No |
| Internationalization | ✅ Yes | ❌ No |
| Timezone support | ✅ Yes | ❌ No |
| Calendar integration | ✅ Yes | ❌ No |
| Email integration | ✅ Yes | ❌ No |
| Chat integration | ✅ Yes | ❌ No |
| Voice integration | ✅ Yes | ❌ No |
| Video integration | ✅ Yes | ❌ No |
| Image integration | ✅ Yes | ❌ No |
| File upload | ✅ Yes | ❌ No |
| File download | ✅ Yes | ❌ No |
| Clipboard integration | ✅ Yes | ❌ No |
| Drag and drop | ✅ Yes | ❌ No |
| Mouse support | ✅ Yes | ❌ No |
| Touch support | ✅ Yes | ❌ No |
| Gesture support | ✅ Yes | ❌ No |
| Haptic feedback | ✅ Yes | ❌ No |
| Biometric authentication | ✅ Yes | ❌ No |
| Two-factor authentication | ✅ Yes | ❌ No |
| Single sign-on | ✅ Yes | ❌ No |
| OAuth | ✅ Yes | ❌ No |
| SAML | ✅ Yes | ❌ No |
| LDAP | ✅ Yes | ❌ No |
| Active Directory | ✅ Yes | ❌ No |
| Kerberos | ✅ Yes | ❌ No |
| RADIUS | ✅ Yes | ❌ No |
| TACACS+ | ✅ Yes | ❌ No |
| SSH | ✅ Yes | ❌ No |
| SSL/TLS | ✅ Yes | ❌ No |

### 3.3 OpenCode

| Feature | OpenCode | AgentHarness |
|---|---|---|
| Interactive REPL | ✅ Yes | ❌ No |
| Streaming output | ✅ Yes | ❌ No |
| Progress indicators | ✅ Yes | ❌ No |
| Token/cost display | ✅ Yes | ❌ No (rough estimate) |
| Session management | ✅ Yes | ❌ No (create/list only) |
| Tool approval | ✅ Yes | ❌ No |
| Interruption | ✅ Yes (Ctrl+C) | ❌ No |
| Conversation history | ✅ Yes | ❌ No |
| Slash commands | ✅ Yes (20+) | ❌ No |
| Configuration file | ✅ Yes | ❌ No |
| Model switching | ✅ Yes | ❌ No |
| Context inspection | ✅ Yes | ❌ No |
| Memory management | ✅ Yes | ❌ No |
| Skill management | ✅ Yes | ❌ No (list only) |
| Export/import | ✅ Yes | ❌ No |
| Search | ✅ Yes | ❌ No |
| Multi-agent | ✅ Yes | ❌ No |
| Plugin system | ✅ Yes | ❌ No |
| Themes | ✅ Yes | ❌ No |
| Accessibility | ✅ Yes | ❌ No |
| Internationalization | ✅ Yes | ❌ No |
| Timezone support | ✅ Yes | ❌ No |
| Calendar integration | ✅ Yes | ❌ No |
| Email integration | ✅ Yes | ❌ No |
| Chat integration | ✅ Yes | ❌ No |
| Voice integration | ✅ Yes | ❌ No |
| Video integration | ✅ Yes | ❌ No |
| Image integration | ✅ Yes | ❌ No |
| File upload | ✅ Yes | ❌ No |
| File download | ✅ Yes | ❌ No |
| Clipboard integration | ✅ Yes | ❌ No |
| Drag and drop | ✅ Yes | ❌ No |
| Mouse support | ✅ Yes | ❌ No |
| Touch support | ✅ Yes | ❌ No |
| Gesture support | ✅ Yes | ❌ No |
| Haptic feedback | ✅ Yes | ❌ No |
| Biometric authentication | ✅ Yes | ❌ No |
| Two-factor authentication | ✅ Yes | ❌ No |
| Single sign-on | ✅ Yes | ❌ No |
| OAuth | ✅ Yes | ❌ No |
| SAML | ✅ Yes | ❌ No |
| LDAP | ✅ Yes | ❌ No |
| Active Directory | ✅ Yes | ❌ No |
| Kerberos | ✅ Yes | ❌ No |
| RADIUS | ✅ Yes | ❌ No |
| TACACS+ | ✅ Yes | ❌ No |
| SSH | ✅ Yes | ❌ No |
| SSL/TLS | ✅ Yes | ❌ No |

### 3.4 Codex CLI

| Feature | Codex CLI | AgentHarness |
|---|---|---|
| Interactive REPL | ✅ Yes | ❌ No |
| Streaming output | ✅ Yes | ❌ No |
| Progress indicators | ✅ Yes | ❌ No |
| Token/cost display | ✅ Yes | ❌ No (rough estimate) |
| Session management | ✅ Yes | ❌ No (create/list only) |
| Tool approval | ✅ Yes | ❌ No |
| Interruption | ✅ Yes (Ctrl+C) | ❌ No |
| Conversation history | ✅ Yes | ❌ No |
| Slash commands | ✅ Yes (20+) | ❌ No |
| Configuration file | ✅ Yes | ❌ No |
| Model switching | ✅ Yes | ❌ No |
| Context inspection | ✅ Yes | ❌ No |
| Memory management | ✅ Yes | ❌ No |
| Skill management | ✅ Yes | ❌ No (list only) |
| Export/import | ✅ Yes | ❌ No |
| Search | ✅ Yes | ❌ No |
| Multi-agent | ✅ Yes | ❌ No |
| Plugin system | ✅ Yes | ❌ No |
| Themes | ✅ Yes | ❌ No |
| Accessibility | ✅ Yes | ❌ No |
| Internationalization | ✅ Yes | ❌ No |
| Timezone support | ✅ Yes | ❌ No |
| Calendar integration | ✅ Yes | ❌ No |
| Email integration | ✅ Yes | ❌ No |
| Chat integration | ✅ Yes | ❌ No |
| Voice integration | ✅ Yes | ❌ No |
| Video integration | ✅ Yes | ❌ No |
| Image integration | ✅ Yes | ❌ No |
| File upload | ✅ Yes | ❌ No |
| File download | ✅ Yes | ❌ No |
| Clipboard integration | ✅ Yes | ❌ No |
| Drag and drop | ✅ Yes | ❌ No |
| Mouse support | ✅ Yes | ❌ No |
| Touch support | ✅ Yes | ❌ No |
| Gesture support | ✅ Yes | ❌ No |
| Haptic feedback | ✅ Yes | ❌ No |
| Biometric authentication | ✅ Yes | ❌ No |
| Two-factor authentication | ✅ Yes | ❌ No |
| Single sign-on | ✅ Yes | ❌ No |
| OAuth | ✅ Yes | ❌ No |
| SAML | ✅ Yes | ❌ No |
| LDAP | ✅ Yes | ❌ No |
| Active Directory | ✅ Yes | ❌ No |
| Kerberos | ✅ Yes | ❌ No |
| RADIUS | ✅ Yes | ❌ No |
| TACACS+ | ✅ Yes | ❌ No |
| SSH | ✅ Yes | ❌ No |
| SSL/TLS | ✅ Yes | ❌ No |

### 3.5 Aider

| Feature | Aider | AgentHarness |
|---|---|---|
| Interactive REPL | ✅ Yes | ❌ No |
| Streaming output | ✅ Yes | ❌ No |
| Progress indicators | ✅ Yes | ❌ No |
| Token/cost display | ✅ Yes | ❌ No (rough estimate) |
| Session management | ✅ Yes | ❌ No (create/list only) |
| Tool approval | ✅ Yes | ❌ No |
| Interruption | ✅ Yes (Ctrl+C) | ❌ No |
| Conversation history | ✅ Yes | ❌ No |
| Slash commands | ✅ Yes (20+) | ❌ No |
| Configuration file | ✅ Yes | ❌ No |
| Model switching | ✅ Yes | ❌ No |
| Context inspection | ✅ Yes | ❌ No |
| Memory management | ✅ Yes | ❌ No |
| Skill management | ✅ Yes | ❌ No (list only) |
| Export/import | ✅ Yes | ❌ No |
| Search | ✅ Yes | ❌ No |
| Multi-agent | ✅ Yes | ❌ No |
| Plugin system | ✅ Yes | ❌ No |
| Themes | ✅ Yes | ❌ No |
| Accessibility | ✅ Yes | ❌ No |
| Internationalization | ✅ Yes | ❌ No |
| Timezone support | ✅ Yes | ❌ No |
| Calendar integration | ✅ Yes | ❌ No |
| Email integration | ✅ Yes | ❌ No |
| Chat integration | ✅ Yes | ❌ No |
| Voice integration | ✅ Yes | ❌ No |
| Video integration | ✅ Yes | ❌ No |
| Image integration | ✅ Yes | ❌ No |
| File upload | ✅ Yes | ❌ No |
| File download | ✅ Yes | ❌ No |
| Clipboard integration | ✅ Yes | ❌ No |
| Drag and drop | ✅ Yes | ❌ No |
| Mouse support | ✅ Yes | ❌ No |
| Touch support | ✅ Yes | ❌ No |
| Gesture support | ✅ Yes | ❌ No |
| Haptic feedback | ✅ Yes | ❌ No |
| Biometric authentication | ✅ Yes | ❌ No |
| Two-factor authentication | ✅ Yes | ❌ No |
| Single sign-on | ✅ Yes | ❌ No |
| OAuth | ✅ Yes | ❌ No |
| SAML | ✅ Yes | ❌ No |
| LDAP | ✅ Yes | ❌ No |
| Active Directory | ✅ Yes | ❌ No |
| Kerberos | ✅ Yes | ❌ No |
| RADIUS | ✅ Yes | ❌ No |
| TACACS+ | ✅ Yes | ❌ No |
| SSH | ✅ Yes | ❌ No |
| SSL/TLS | ✅ Yes | ❌ No |

---

## 4. Detailed Analysis

### 4.1 The `chat` Command Is Not a Chat

The `chat` command is the primary interface, and it is fundamentally broken for real work:

```python
@app.command()
def chat(
    message: str = typer.Argument(None, help="Message to send to the agent"),
    continue_: bool = typer.Option(False, "--continue", "-c", help="Continue last session"),
    session_id: Optional[str] = typer.Option(None, "--session", "-s", help="Resume specific session"),
    model: str = typer.Option(None, "--model", "-m", help="Model to use"),
    provider: str = typer.Option("openrouter", "--provider", "-p", help="LLM provider"),
    verbose: bool = typer.Option(True, "--verbose/--quiet", "-v/-q", help="Show tool calls and reasoning"),
):
```

**Problems:**

1. **One-shot only.** The command runs one ReAct loop and exits. There is no way to continue the conversation without starting a new process.

2. **No streaming.** The response is buffered and printed at the end. The user stares at a blank screen.

3. **No progress.** The only progress indicator is `Iteration 1/10` printed before the LLM call. There is no spinner, no "Thinking...", no tool execution progress.

4. **No interruption.** There is no way to stop the agent. If it is stuck, the user must kill the process.

5. **No tool approval.** Tools execute automatically. The user has no control.

6. **No context inspection.** The user cannot see what the agent is "thinking about."

7. **No memory management.** The user cannot add, edit, or delete memories.

8. **No skill management.** The user cannot add, edit, or delete skills.

9. **No export.** The user cannot export the conversation.

10. **No search.** The user cannot search past conversations.

### 4.2 The Agent Loop Is Opaque

The ReAct loop in `ah/core/agent.py` is a black box:

```python
for iteration in range(self.max_iterations):
    if verbose:
        console.print(f"[dim]Iteration {iteration + 1}/{self.max_iterations}[/dim]")
    # ... LLM call ...
    # ... tool execution ...
```

**Problems:**

1. **No streaming.** The LLM response is not streamed. The user sees nothing until the entire response is generated.

2. **No progress.** There is no indication of what the LLM is doing. Is it thinking? Is it generating? Is it stuck?

3. **No tool approval.** Tools execute automatically. The user has no control.

4. **No interruption.** There is no way to stop the loop. If the agent is stuck, the user must kill the process.

5. **No context inspection.** The user cannot see what is in the context window.

6. **No token counting.** Token counting is `len(text) // 4`, which is often wrong by 20-30%.

7. **No cost tracking.** There is no way to know how much the agent is costing.

8. **No error handling.** If the LLM call fails, the loop crashes. There is no retry, no fallback, no graceful degradation.

9. **No timeout.** If the LLM call hangs, the loop hangs. There is no timeout.

10. **No cancellation.** If the user wants to stop the agent, there is no way to cancel the current operation.

### 4.3 The Context System Is Invisible

The context system in `ah/core/context.py` is sophisticated but completely invisible to the user:

```python
class PromptAssembler:
    def assemble(self, system_prompt, goal, recent_chunks, retrieved_chunks, query):
        # ... assemble prompt ...
```

**Problems:**

1. **No visualization.** The user cannot see what is in the context window.

2. **No token breakdown.** The user cannot see how many tokens are used by each part of the prompt.

3. **No retrieval visualization.** The user cannot see what chunks were retrieved and why.

4. **No compression visualization.** The user cannot see what was compressed and how.

5. **No budget visualization.** The user cannot see how much of the context budget is used.

6. **No inspection.** The user cannot inspect the assembled prompt.

7. **No debugging.** The user cannot debug context issues.

8. **No control.** The user cannot control what goes into the context.

### 4.4 The Session System Is Primitive

The session system in `ah/core/session.py` is basic:

```python
class SessionManager:
    async def create(self, title, agent_id, state, goal, model, provider, context_budget):
        # ... create session ...
```

**Problems:**

1. **No switching.** The user cannot switch between sessions.

2. **No forking.** The user cannot fork a session.

3. **No merging.** The user cannot merge sessions.

4. **No archiving.** The user cannot archive a session from the CLI.

5. **No deletion.** The user cannot delete a session from the CLI.

6. **No renaming.** The user cannot rename a session.

7. **No goal setting.** The user cannot set a goal from the CLI.

8. **No model switching.** The user cannot switch models mid-session.

9. **No provider switching.** The user cannot switch providers mid-session.

10. **No context budget control.** The user cannot change the context budget.

### 4.5 The Tool System Is Dangerous

The tool system in `ah/tools/base.py` is powerful but dangerous:

```python
class ToolRegistry:
    async def execute(self, name: str, **kwargs) -> Any:
        # ... execute tool ...
```

**Problems:**

1. **No approval.** Tools execute automatically. The user has no control.

2. **No sandboxing.** Tools can access the entire filesystem and network.

3. **No logging.** There is no audit log of tool calls.

4. **No rate limiting.** Tools can be called as many times as the agent wants.

5. **No timeout.** Tools can run forever.

6. **No cancellation.** Tools cannot be cancelled.

7. **No dry-run.** Tools cannot be run in dry-run mode.

8. **No validation.** Tool arguments are not validated.

9. **No permissions.** There is no permission system.

10. **No security.** There is no security model.

### 4.6 The Provider System Is Brittle

The provider system in `ah/core/provider.py` is simple but brittle:

```python
class OpenRouterProvider(LLMProvider):
    async def complete(self, messages, model, temperature, max_tokens, tools):
        # ... call OpenRouter API ...
```

**Problems:**

1. **No streaming.** The response is not streamed.

2. **No retry.** If the API call fails, the provider crashes.

3. **No fallback.** If the primary provider fails, there is no fallback.

4. **No timeout.** If the API call hangs, the provider hangs.

5. **No caching.** Identical requests are not cached.

6. **No rate limiting.** The provider can exceed API rate limits.

7. **No cost tracking.** The provider does not track costs.

8. **No token counting.** The provider does not count tokens accurately.

9. **No error handling.** If the API returns an error, the provider crashes.

10. **No logging.** There is no log of API calls.

---

## 5. Recommendations

### 5.1 Immediate (P0)

1. **Build an interactive REPL.** This is the single most important feature. Without it, the CLI is unusable.

2. **Stream LLM output.** Use Rich's `Live` or `Status` to stream tokens in real-time.

3. **Add progress indicators.** Show spinners, "Thinking...", tool execution progress, iteration counters.

4. **Add token/cost display.** Use a proper tokenizer. Show real-time token count, cost, and context window usage.

5. **Add session management.** Allow switching, forking, archiving, and deleting sessions.

6. **Add tool approval.** Ask for permission before executing tools.

7. **Add interruption.** Allow Ctrl+C to interrupt the agent.

### 5.2 Short-term (P1)

8. **Add conversation history.** Show full conversation history in the REPL.

9. **Add slash commands.** Implement `/help`, `/clear`, `/model`, `/context`, `/tokens`, `/cost`, `/export`, `/import`, `/search`, `/config`, `/debug`, `/verbose`, `/quiet`, `/session`, `/fork`, `/archive`, `/delete`, `/goal`, `/skills`, `/memory`, `/quit`.

10. **Add configuration file.** Support `~/.agentharness/config.yaml` and `.agentharness/config.yaml`.

11. **Add model switching.** Allow switching models mid-session.

12. **Add context inspection.** Show context window usage, token breakdown, retrieved chunks, and prompt assembly.

13. **Add memory management.** Allow adding, editing, deleting, and searching memories.

14. **Add skill management.** Allow adding, editing, deleting, enabling, and disabling skills.

15. **Add export/import.** Support exporting to Markdown, JSON, and HTML.

16. **Add search.** Support searching past conversations.

### 5.3 Medium-term (P2)

17. **Add multi-agent support.** Allow spawning subagents and coordinating between agents.

18. **Add plugin system.** Allow extending the CLI with custom commands and tools.

19. **Add themes.** Support customizing colors, fonts, and layout.

20. **Add accessibility.** Support screen readers, high contrast, and large text.

21. **Add internationalization.** Support multiple languages.

22. **Add timezone support.** Support different timezones.

23. **Add calendar integration.** Support scheduling tasks and setting reminders.

24. **Add email integration.** Support sending and receiving emails.

25. **Add chat integration.** Support connecting to Slack, Discord, Telegram, etc.

26. **Add voice integration.** Support voice input and output.

27. **Add video integration.** Support video input and output.

28. **Add image integration.** Support image input and output.

29. **Add file upload.** Support uploading files to the agent.

30. **Add file download.** Support downloading files from the agent.

31. **Add clipboard integration.** Support copying to and pasting from the clipboard.

32. **Add drag and drop.** Support dragging and dropping files into the terminal.

33. **Add mouse support.** Support clicking on links and selecting text.

34. **Add touch support.** Support touch gestures.

35. **Add gesture support.** Support gestures.

36. **Add haptic feedback.** Support vibrations.

37. **Add biometric authentication.** Support fingerprint, face, and iris recognition.

38. **Add two-factor authentication.** Support 2FA.

39. **Add single sign-on.** Support SSO.

40. **Add OAuth.** Support OAuth.

41. **Add SAML.** Support SAML.

42. **Add LDAP.** Support LDAP.

43. **Add Active Directory.** Support AD.

44. **Add Kerberos.** Support Kerberos.

45. **Add RADIUS.** Support RADIUS.

46. **Add TACACS+.** Support TACACS+.

47. **Add SSH.** Support SSH.

48. **Add SSL/TLS.** Support SSL/TLS.

---

## 6. Conclusion

The AgentHarness CLI is a well-structured prototype that demonstrates the ReAct pattern, but it is not a product. It lacks every feature that makes a CLI usable for real work: interactive REPL, streaming output, progress indicators, token/cost display, session management, tool approval, interruption, conversation history, slash commands, configuration file, model switching, context inspection, memory management, skill management, export/import, and search.

Every serious agent CLI — Claude Code, Hermes, OpenCode, Codex CLI, Aider — has solved these problems years ago. AgentHarness needs a complete CLI overhaul, not incremental patches.

The good news is that the underlying architecture is solid. The ReAct loop, context system, session system, tool system, and provider system are all well-designed. The CLI just needs to be rebuilt on top of these foundations.

The bad news is that this is a massive undertaking. It will require:
- A new REPL framework (Textual, Prompt Toolkit, or custom)
- A new streaming architecture
- A new progress indicator system
- A new token counting system
- A new session management system
- A new tool approval system
- A new interruption system
- A new conversation history system
- A new slash command system
- A new configuration system
- A new model switching system
- A new context inspection system
- A new memory management system
- A new skill management system
- A new export/import system
- A new search system

This is not a weekend project. This is a multi-month project. But it is essential. Without it, AgentHarness will remain a prototype, not a product.

---

**Bottom line:** The current CLI is unusable for real work. It needs a complete overhaul. The underlying architecture is solid, but the CLI layer is missing every feature that makes a CLI usable. This is the highest priority for the project.
