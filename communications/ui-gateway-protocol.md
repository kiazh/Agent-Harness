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
- Authentication: the launcher sets `AH_GATEWAY_TOKEN`; once the gateway has a token,
  every method except `initialize` must carry it as a `token` param (else error `1005`).
- Streaming is pushed as notifications: `{"jsonrpc":"2.0","method":"event","params":{"type":...}}`.
- Turns run under `turn_timeout` (default 300s). On timeout the UI gets an `error`
  event (`"turn timed out"`) followed by an empty `message.complete`.
- Handler failures never crash the gateway and return sanitized `internal error`
  messages (no stack traces or exception text cross the protocol).

## Methods (UI → gateway)

| Method | Params | Result |
|---|---|---|
| `initialize` | `{model?, provider?}` | `{version, model, provider, cwd, branch}` — connects the database |
| `session.create` | `{title?}` | `{session}` |
| `session.list` | `{limit?, cursor?}` | `{sessions: [session], nextCursor?}` — cursor is an offset string |
| `session.resume` | `{sessionId}` | `{session, history: [{role, content, tool?}]}` |
| `usage.get` | `{sessionId, agent?}` | `{sessionId, agentId, session, agent}` with calls, accounted tokens, unknown calls, limits, and remaining budgets |
| `prompt.submit` | `{sessionId, text}` | `{turnId}` — then events stream; one turn per session at a time |
| `prompt.cancel` | `{sessionId}` | `{cancelled: bool}` |
| `config.set` | `{key, value, persist?}` | `{model, provider, key, value}` — `model`/`provider` apply to this gateway; any other non-secret setting updates config (saved to `config.yaml` when `persist`). Secrets are rejected. |
| `shutdown` | `{}` | `{}` — gateway exits after replying |

### Feature methods (`ah/gateway/features/`)

| Method | Params | Result |
|---|---|---|
| `session.fork` | `{sessionId, title?}` | `{session}` (history copied, order kept) |
| `session.delete` | `{sessionId}` | `{deleted}` |
| `session.rename` | `{sessionId, title}` | `{session}` |
| `session.setGoal` | `{sessionId, goal}` | `{goal}` |
| `session.search` | `{query, limit?}` | `{sessions}` |
| `session.recall` | `{sessionId, query, limit?}` | `{hits: [{sessionId, chunkId, title, source, preview, score, occurredAt}]}` — agent-scoped evidence search over live + archived chunks |
| `session.recall.window` | `{sessionId, targetSessionId, chunkId, before?, after?}` | `{messages}` — bounded window around a recall hit (anchored by chunk UUID) |
| `session.export` | `{sessionId}` | `{markdown}` |
| `context.get` | `{sessionId, limit?}` | `{chunks[{type, agent, tokens, createdAt, preview}], totalTokens, budget, goal}` |
| `context.compress` | `{sessionId}` | `{compressed: false}` or `{compressed: true, originalCount, newCount, originalTokens, compressedTokens, ratio, method}` |
| `memory.list` | `{sessionId?, category?, limit?}` | `{memories, total}` — with `sessionId`, scoped to that session's agent |
| `memory.search` | `{query, sessionId?, category?, limit?}` | `{results}` (memories with `score`) — with `sessionId`, scoped to that session's agent |
| `memory.add` | `{content, category?, importance?, sessionId?}` | `{memory}` — with `sessionId`, owned by that session's agent |
| `memory.forget` | `{id, sessionId?}` | `{deleted}` — with `sessionId`, refuses other agents' memories |
| `memory.share` | `{sessionId, id, recipientAgent}` | `{status}` — provenance + identity gate enforced before the recipient sees it |
| `memory.pending` | `{limit?}` | `{pending}` |
| `memory.approve` / `memory.reject` | `{id, note?}` | `{memory}` / `{rejected}` |
| `memory.approveAll` / `memory.rejectAll` | `{note?}` | `{count}` |
| `memory.stats` | `{}` | `{approval: {pending, approved, rejected}, total}` |
| `skills.list` | `{}` | `{skills}` |
| `skills.show` | `{name}` | `{skill}` (with `content`) |
| `skills.learn` | `{source, name?, description?, triggers?}` | `{skill}` — source is a file, http(s) URL, or skill name |
| `skills.delete` | `{name}` | `{deleted}` |
| `skills.curator` | `{days?}` | health report + `unused`, `stale` |
| `learning.list` | `{sessionId, limit?}` | `{reviews}` — post-turn skill review proposals |
| `learning.approve` / `learning.reject` | `{sessionId, id}` | `{review}` |
| `config.get` | `{}` | `{config, secrets}` — secret values are reported only as `true`/`false` |
| `profile.get` / `profile.set` / `profile.list` | `{userId, ...}` | `{profile}` / `{profiles}` |
| `status` | `{}` | `{postgres, sessions, contextChunks, memories, pendingMemories, tools, openrouterKeySet, model, provider}` |
| `agents.list` | `{}` | `{agents: [agent]}` |
| `agents.save` | `{name, description?, systemPrompt?, tools?, model?, provider?, maxIterations?}` | `{agent}` |
| `agents.delete` | `{name}` | `{deleted}` |
| `agents.run` | `{steps: [{agent, task}], mode?, sessionId?}` (1–16 steps) | `{results: [{agent, task, response, sessionId, tokens, iterations, status}]}` |
| `agents.history` | `{sessionId, limit?}` | `{messages}` |
| `jobs.create` | `{sessionId, kind?, prompt?, intervalSeconds?, cronExpression?, name?, agent?, model?, provider?, noAgent?, scriptPath?}` | `{job}` — `scriptPath` requires `noAgent=true` (no LLM; script must live in `AGENT_HARNESS_SCRIPTS_DIR`) |
| `jobs.list` | `{sessionId?, limit?}` | `{jobs}` |
| `jobs.setEnabled` | `{id, enabled, sessionId?}` | `{job}` — with `sessionId`, refuses other sessions' jobs |
| `jobs.delete` | `{id, sessionId?}` | `{deleted}` — with `sessionId`, refuses other sessions' jobs |

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
`1004` operation failed (e.g. a skill source could not be read), `1005` unauthorized
(token missing or wrong).
