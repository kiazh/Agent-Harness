# AgentHarness UI ↔ Gateway Protocol

**Status:** implemented (`ui/` + `ah/gateway/`). Supersedes the Python TUI port in `pi-ui-port-plan.md`.

## Architecture

```
ah  (Python CLI, Typer)
 └─ spawns ──> node ui/src/main.ts          TypeScript UI, built on @earendil-works/pi-tui (npm, MIT)
                └─ spawns ──> python -m ah.gateway   JSON-RPC server: sessions, agent turns, config
                               └─ ReActAgent, PostgreSQL, tools (unchanged Python core)
```

Same split as Hermes (`ui-tui/` ↔ `tui_gateway/`) and opencode (TS TUI ↔ backend). The Python core
stays UI-agnostic; any future client (web, API server) can speak the same protocol.

- Transport: the gateway's **stdin/stdout**, one JSON object per line (UTF-8, `\n`-delimited).
- The gateway's stdout carries **only** protocol frames; logs and audit lines go to stderr.
- Framing is JSON-RPC 2.0. Requests have an `id`; the gateway answers each with a `result` or `error`.
- Streaming is pushed as notifications: `{"jsonrpc":"2.0","method":"event","params":{"type":...}}`.

## Methods (UI → gateway)

| Method | Params | Result |
|---|---|---|
| `initialize` | `{}` | `{version, model, provider, cwd, branch}` — connects the database |
| `session.create` | `{title?}` | `{session}` |
| `session.list` | `{limit?}` | `{sessions: [session]}` |
| `session.resume` | `{sessionId}` | `{session, history: [{role, content, tool?}]}` |
| `prompt.submit` | `{sessionId, text}` | `{turnId}` — then events stream |
| `prompt.cancel` | `{sessionId}` | `{cancelled: bool}` |
| `config.set` | `{key, value, persist?}` | `{model, provider, key, value}` — `model`/`provider` apply to this gateway; any other non-secret setting updates config (saved to `config.yaml` when `persist`). Secrets are rejected. |
| `shutdown` | `{}` | `{}` — gateway exits after replying |

### Feature methods (`ah/gateway/features.py`)

| Method | Params | Result |
|---|---|---|
| `session.fork` | `{sessionId, title?}` | `{session}` (history copied, order kept) |
| `session.delete` | `{sessionId}` | `{deleted}` |
| `session.rename` | `{sessionId, title}` | `{session}` |
| `session.setGoal` | `{sessionId, goal}` | `{goal}` |
| `session.search` | `{query, limit?}` | `{sessions}` |
| `session.export` | `{sessionId}` | `{markdown}` |
| `context.get` | `{sessionId, limit?}` | `{chunks[{type, agent, tokens, createdAt, preview}], totalTokens, budget, goal}` |
| `context.compress` | `{sessionId}` | `{compressed: false}` or `{compressed: true, originalCount, newCount, originalTokens, compressedTokens, ratio, method}` |
| `memory.list` | `{category?, limit?}` | `{memories, total}` |
| `memory.search` | `{query, category?, limit?}` | `{results}` (memories with `score`) |
| `memory.add` | `{content, category?, importance?, sessionId?}` | `{memory}` |
| `memory.forget` | `{id}` | `{deleted}` |
| `memory.pending` | `{limit?}` | `{pending}` |
| `memory.approve` / `memory.reject` | `{id, note?}` | `{memory}` / `{rejected}` |
| `memory.approveAll` / `memory.rejectAll` | `{note?}` | `{count}` |
| `memory.stats` | `{}` | `{approval: {pending, approved, rejected}, total}` |
| `skills.list` | `{}` | `{skills}` |
| `skills.show` | `{name}` | `{skill}` (with `content`) |
| `skills.learn` | `{source, name?, description?, triggers?}` | `{skill}` — source is a file, http(s) URL, or skill name |
| `skills.delete` | `{name}` | `{deleted}` |
| `skills.curator` | `{days?}` | health report + `unused`, `stale` |
| `config.get` | `{}` | `{config, secrets}` — secret values are reported only as `true`/`false` |
| `profile.get` / `profile.set` / `profile.list` | `{userId, ...}` | `{profile}` / `{profiles}` |
| `status` | `{}` | `{postgres, sessions, contextChunks, memories, pendingMemories, tools, openrouterKeySet, model, provider}` |

`session` = `{id, title, model, provider, status, lastActivity}`.

## Events (gateway → UI)

All carry `sessionId` and `turnId`.

| `type` | Extra fields |
|---|---|
| `message.start` | — |
| `message.delta` | `text` |
| `tool.start` | `id, name, args` |
| `tool.complete` | `id, name, result, isError` |
| `usage` | `tokens` |
| `message.complete` | `text, tokens, iterations, toolCalls, cancelled` |
| `error` | `message` |

## Error codes

JSON-RPC standard: `-32700` parse, `-32600` invalid request, `-32601` unknown method,
`-32602` invalid params, `-32603` internal. Application: `1001` database unavailable,
`1002` not found (session, memory, skill), `1003` a turn is already running for this session,
`1004` operation failed (e.g. a skill source could not be read).
