# Hermes Agent: Scheduling, Delegation & Multi-Agent Coordination

> Research document for AgentHarness improvement. Covers Hermes Agent's cron
> scheduler, subagent delegation, multi-agent coordination, and
> interrupt-and-redirect mechanisms.

---

## 1. Cron Scheduler

### Architecture

The cron scheduler is a **durable, gateway-driven** system:

- **Storage**: Jobs stored in `~/.hermes/cron/jobs.json` (per-profile)
- **Output**: `~/.hermes/cron/output/{job_id}/{timestamp}.md`
- **Ticker**: Gateway daemon ticks every **60 seconds** from a background thread
- **Locking**: File-based lock (`~/.hermes/cron/.tick.lock`) prevents duplicate ticks across processes
- **Heartbeat**: `ticker_heartbeat` and `ticker_last_success` files for liveness monitoring

### Schedule Formats

| Format | Example | Description |
|--------|---------|-------------|
| Relative delay | `"30m"`, `"2h"` | One-shot after delay |
| Interval | `"every 2h"`, `"every 1h"` | Recurring interval |
| Natural language | `"every monday 9am"`, `"every 1d at 09:00"` | Day/time schedules |
| Cron expression | `"0 9 * * *"` | 5-field cron |
| ISO timestamp | `"2025-12-01T09:00:00Z"` | One-shot at exact time |

### Job Lifecycle

```
create → enabled → tick fires → run → mark_job_run → advance_next_run
                    ↓
              paused (hermes pause / cron pause)
                    ↓
              resumed (hermes resume / cron resume)
                    ↓
              removed (cron remove)
```

### Key Features

- **Per-job model override**: Pin a specific model/provider per job, or use `cron.model` fleet default
- **Per-job reasoning effort**: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `ultra`
- **Skill attachment**: Jobs can load skills before running (like `/skill-name` in chat)
- **Script mode**: `no_agent=True` makes a script the whole job (zero LLM involvement)
- **Context chaining**: `context_from` chains job A's output into job B
- **Workdir isolation**: Jobs can run in a specific directory with its `AGENTS.md`/`CLAUDE.md` loaded
- **Multi-platform delivery**: Results delivered to origin chat, local files, or configured platform targets
- **Pre-dispatch validation**: Validates provider keys, skills, delivery targets, MCP servers before running
- **Failure nudge**: After N consecutive failures (default 3), suggests reviewing/pausing the job
- **Misfire catch-up**: Recurring jobs that missed a slot while paused fire one catch-up run on resume
- **Global pause**: `hermes pause` stops all scheduled fires; `hermes resume` lifts it

### Concurrency

- `cron.max_parallel_jobs` limits total cron concurrency
- Each job runs in an isolated agent session
- In-flight claims prevent duplicate execution
- Stale claim recovery with TTL-based safety valves

### Agent-Managed Scheduling

By default, cron-run agents **cannot** create/edit/remove cron jobs (prevents runaway loops). Opt-in via:

```yaml
cron:
  allow_agent_scheduling: true  # default: false
```

When enabled, scheduled agents can manage the cron table like any chat session.

---

## 2. Subagent Delegation

### Architecture

The `delegate_task` tool spawns child `AIAgent` instances with:

- **Fresh conversation**: Zero knowledge of parent's history
- **Isolated context**: Only `goal` and `context` fields from parent
- **Own terminal session**: Separate from parent
- **Inherited toolsets**: Parent's enabled tools minus child-blocked tools
- **Dedicated SessionDB**: Own database connection (prevents transcript loss)

### Single Task

```python
delegate_task(
    goal="Debug why tests fail",
    context="Error: assertion in test_foo.py line 42"
)
```

### Parallel Batch

```python
delegate_task(tasks=[
    {"goal": "Research topic A", "context": "Focus on recent primary sources"},
    {"goal": "Research topic B", "context": "Compare the leading explanations"},
    {"goal": "Fix the build", "context": "Project root: /home/user/project"}
])
```

- **Concurrency**: Up to 10 concurrent subagents by default (configurable via `delegation.max_concurrent_children`)
- **Thread pool**: `ThreadPoolExecutor` with configured concurrency limit
- **Result ordering**: Results sorted by task index regardless of completion order
- **Background execution**: Top-level calls run in background; parent gets handle immediately

### Structured Output (`output_schema`)

Each task can carry a JSON Schema the child's final answer must validate against:

```python
delegate_task(tasks=[{
    "goal": "Check which endpoints return 200",
    "context": "https://a.example, https://b.example",
    "output_schema": {
        "type": "object",
        "properties": {
            "healthy": {"type": "array", "items": {"type": "string"}},
            "failing": {"type": "array", "items": {"type": "string"}}
        },
        "required": ["healthy", "failing"]
    }
}])
```

- Child sees schema as output contract
- On validation failure: one bounded correction turn with errors verbatim
- After retry: result keeps `status: completed` with `schema_valid: false` and raw text

### Roles

| Role | Can Delegate | Can Spawn Workers |
|------|-------------|-------------------|
| `leaf` (default) | No | No |
| `orchestrator` | Yes (bounded by `max_spawn_depth`) | Yes |

- **Depth limit**: `delegation.max_spawn_depth` (default 1 = flat)
- **Orchestrator kill switch**: `delegation.orchestrator_enabled: false`

### Model Override

```yaml
delegation:
  model: "google/gemini-flash-2.0"  # Cheaper model for subagents
  provider: "openrouter"
```

Resolution order: `delegation.base_url` → `delegation.provider` → inherit parent.

### Child-Blocked Tools

Leaf subagents **cannot** call:
- `delegate_task` (retained for orchestrators)
- `clarify` (no user interaction)
- `memory` (no shared memory writes)
- `send_message` (no cross-platform side effects)
- `cronjob` (no scheduling in parent's name)

Both roles retain `execute_code` for batch mechanical work.

### Stall Detection

Progress-based stall monitor (on by default, zero config):

- Samples: API-call count, current tool, last-activity timestamp
- **450s idle** between turns → stale
- **1200s** while inside a tool → stale
- Stale child: interrupted, 120s grace window, then force-finalized as `stalled`
- Progressing children are never touched

### Worktree Isolation

```yaml
delegation:
  worktree_isolation: true  # default: false
```

- Each child gets its own git worktree: `<repo>/.worktrees/subagent-<id>`
- Branch: `hermes-subagent/subagent-<id>`
- Parent's checkout stays untouched
- Clean worktrees pruned automatically; work-holding ones kept

---

## 3. Multi-Agent Coordination

### Three Coordination Primitives

| Primitive | Shape | Parent Blocks | Child Identity | Resumability | Human-in-Loop |
|-----------|-------|---------------|----------------|--------------|---------------|
| `delegate_task` | RPC (fork → join) | Yes | Anonymous | No | No |
| Kanban | Durable queue | No | Named profile | Yes (block→unblock→re-run) | Yes |
| Spawning | Full process | No | Full process | Yes (hours/days) | Yes (PTY) |

### Kanban Board

Durable SQLite board for multi-profile collaboration:

- **Storage**: `~/.hermes/kanban.db`
- **Tasks**: Rows with title, body, assignee (profile), status, tenant namespace
- **Statuses**: `triage | todo | ready | running | blocked | review | done | archived`
- **Dispatcher**: Runs inside gateway, reclaims stale claims, promotes ready tasks, spawns assigned profiles
- **Worker tools**: `kanban_show`, `kanban_complete`, `kanban_block`, `kanban_heartbeat`, `kanban_comment`, `kanban_create`, `kanban_link`
- **CLI**: `hermes kanban <verb>` — `init`, `create`, `list`, `show`, `assign`, `link`, `complete`, `block`, `unblock`, `archive`, `tail`

### Spawning Independent Processes

```bash
# One-shot mode
hermes chat -q 'Research GRPO papers and write summary to ~/research/grpo.md'

# Background for long tasks
hermes chat -q 'Set up CI/CD for ~/myapp' &

# Interactive PTY mode (via tmux)
tmux new-session -d -s agent1 -x 120 -y 40 'hermes'
```

- Fully independent subprocesses
- Separate sessions, tools, environments
- Full tool access
- Interactive (PTY mode)

### Multi-Agent Patterns

1. **Parallel Research**: Multiple `delegate_task` calls with different topics
2. **Code Review + Fix**: Delegate review-and-fix workflow to fresh context
3. **Multi-File Refactoring**: Delegate large refactoring to avoid flooding parent context
4. **Kanban Fleet**: Dispatcher spawns workers for queued tasks
5. **Process Spawning**: Long autonomous missions via independent processes

---

## 4. Interrupt-and-Redirect

### Steering a Running Subagent

Interrupting throws away in-flight work; **steering** redirects without stopping:

```json
{"action": "list"}
{"action": "steer", "subagent_id": "sa-0-1a2b3c4d", "message": "focus on pricing instead"}
{"action": "stop", "subagent_id": "sa-0-1a2b3c4d"}
```

- **`list`**: Returns live children with `subagent_id`, goal, status, `running_seconds`, `accepting_steer`
- **`steer`**: Queues course correction into running child without stopping it
- **`stop`**: Ends child early at next iteration boundary; partial result still returns

### Steering Delivery Semantics

- Text appended to child's **last tool result** at next iteration boundary
- In-flight tool call is **never cut**
- Child sees it as an out-of-band user message
- **Queued ≠ delivered**: `"queued"` means accepted before completion boundary, not necessarily seen
- If child finished before steer landed: `missed_steer` with note in summary

### Thread-Scoped Interrupt Signaling

```python
# tools/interrupt.py
set_interrupt(active=True, thread_id=tid, reason="user redirect")
is_interrupted()  # Check current thread
request_yield(thread_id)  # Ask tool to yield (hand to background)
```

- Thread-scoped: interrupting one agent doesn't kill tools in other sessions
- Gateway runs many agents in one process
- Yield: hand long-running foreground command to background instead of killing

### Cancellation Behavior

- `/stop` (gateway, CLI, Desktop/TUI Stop button, ACP cancel) ends background children
- Closing/resetting owning session ends its background children
- Stop recurses down spawn tree (orchestrator's workers interrupted first)
- Each stopped child returns `status="interrupted"` with partial output
- Normal follow-up messages do **not** cancel background children

### Heartbeat Staleness Monitor

- Parent sends heartbeats every 30s during delegation
- Child progress signals: API calls, tool transitions, activity ticks
- **450s idle** → stale threshold
- **1200s in-tool** → stale threshold
- Stale child: interrupted, 120s grace, then `stalled` completion

---

## 5. Key Configuration

```yaml
# Cron
cron:
  max_parallel_jobs: 5
  model: "anthropic/claude-sonnet-4-20250514"
  model_provider: "anthropic"
  preflight: true
  allow_agent_scheduling: false
  failure_nudge_threshold: 3
  catch_up_missed: true

# Delegation
delegation:
  max_iterations: 250
  max_concurrent_children: 10
  independent_completions: false
  worktree_isolation: false
  max_spawn_depth: 1
  orchestrator_enabled: true
  model: "google/gemini-3-flash-preview"
  provider: "openrouter"
  child_timeout_seconds: 0  # 0 = no timeout
  compression_threshold_tokens: 0  # 0 = no cap
  surface_child_process_notifications: false
```

---

## 6. Lessons for AgentHarness

### Cron Scheduler

1. **Durable storage**: Jobs survive restarts (JSON file + file locking)
2. **Gateway-driven ticker**: 60s tick interval, background thread
3. **Per-job isolation**: Each job runs in fresh agent session
4. **Pre-dispatch validation**: Fail fast before spending tokens
5. **Model flexibility**: Per-job or fleet-wide model override
6. **Skill attachment**: Jobs can load reusable workflows
7. **Context chaining**: Job outputs can feed into other jobs
8. **Failure handling**: Nudge after consecutive failures, misfire catch-up

### Subagent Delegation

1. **Fresh context**: Subagents know nothing; parent must pass everything
2. **Parallel batches**: ThreadPoolExecutor with configurable concurrency
3. **Structured output**: JSON Schema validation with bounded retry
4. **Background execution**: Parent gets handle immediately, result re-enters later
5. **Stall detection**: Progress-based, not wall-clock
6. **Worktree isolation**: Git worktrees for parallel code editing
7. **Model override**: Cheaper workers, frontier planner

### Multi-Agent Coordination

1. **Three primitives**: delegate_task (RPC), Kanban (queue), spawning (process)
2. **Kanban for durability**: Survives restarts, human-in-the-loop, named identities
3. **Dispatcher pattern**: Reclaims stale claims, promotes ready tasks, spawns workers
4. **Process spawning**: For long autonomous missions

### Interrupt-and-Redirect

1. **Steering over interrupting**: Redirect without throwing away work
2. **Thread-scoped interrupts**: Per-agent, not per-process
3. **Yield mechanism**: Hand long-running commands to background
4. **Delivery semantics**: Queued ≠ delivered; missed_steer tracking
5. **Heartbeat monitoring**: Progress-based stall detection

---

## 7. Source Files

| Component | Path |
|-----------|------|
| Cron scheduler | `cron/scheduler.py` |
| Cron jobs | `cron/jobs.py` |
| Cron delivery | `cron/delivery_queue.py` |
| Cron preflight | `cron/scheduler_preflight.py` |
| Delegate tool | `tools/delegate_tool.py` |
| Delegate dispatch | `tools/delegate_tool_dispatch.py` |
| Delegate registry | `tools/delegate_tool_registry.py` |
| Subagent lifecycle | `agent/subagent_lifecycle.py` |
| Interrupt signaling | `tools/interrupt.py` |
| Kanban | `hermes_cli/subcommands/kanban.py` |

---

## 8. References

- [Hermes Agent Cron Docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron)
- [Hermes Agent Delegation Docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation)
- [Hermes Agent Kanban Docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban)
- [Hermes Agent GitHub](https://github.com/NousResearch/hermes-agent)
- [Delegate Task Concurrency Diagnosis](https://hermes-agent.nousresearch.com/docs/reference/delegate-task-concurrency-diagnosis)
