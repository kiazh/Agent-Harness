# Production Multi-Agent Systems: Research & Architecture

> Research survey for AgentHarness multi-agent extension. Covers communication patterns, task decomposition, result aggregation, conflict resolution, and agent specialization — with references to current production frameworks and academic work.

---

## 1. Why Multi-Agent? When to Add Complexity

Production systems adopt multi-agent orchestration only when a single agent cannot reliably handle the task. The decision spectrum:

| Level | Description | When to use |
|-------|-------------|-------------|
| **Direct model call** | Single LLM call, no agent logic | Classification, summarization, translation |
| **Single agent + tools** | One ReAct loop with tool access | Dynamic tool use within a single domain |
| **Multi-agent orchestration** | Multiple specialized agents coordinate | Cross-domain problems, security boundaries, parallel specialization |

**Key insight from Azure Architecture Center**: "Use the lowest level of complexity that reliably meets your requirements." Multi-agent adds coordination overhead, latency, and failure modes. Justify it only when prompt complexity, tool overload, or security requirements demand it.

**When multi-agent wins:**
- Tasks requiring distinct security boundaries (e.g., one agent has DB write, another has read-only)
- Parallel specialization (e.g., code review + security scan + performance analysis simultaneously)
- Cross-functional problems that exceed a single context window
- Fault isolation — one agent's failure shouldn't cascade

---

## 2. Communication Patterns

### 2.1 Blackboard Architecture

**Concept**: A shared workspace (the "blackboard") where all agents read and write. Agents don't communicate directly — they react to the state of the blackboard.

**How it works:**
1. All agents can see the full blackboard content
2. A controller selects which agent acts next based on blackboard state
3. The selected agent reads the blackboard, contributes its output, and writes back
4. The cycle repeats until a termination condition is met

**Production characteristics:**
- **Pros**: Decoupled agents, emergent coordination, no hard-coded communication topology
- **Cons**: Requires a shared state store; can become a bottleneck; harder to debug
- **Best for**: Open-ended problems where the solution path isn't known in advance

**Reference**: Han & Zhang (2025) — "Exploring Advanced LLM Multi-Agent Systems Based on Blackboard Architecture" (arXiv:2507.01701). Their implementation shows competitive performance with SOTA static and dynamic MAS while using fewer tokens.

### 2.2 Message Passing (Direct Agent-to-Agent)

**Concept**: Agents send messages to each other through a message bus or direct channels.

**Variants:**
- **Broadcast**: All messages visible to all agents (like a group chat)
- **Point-to-point**: Specific agent-to-agent channels
- **Pub/Sub**: Agents subscribe to topics of interest

**Production characteristics:**
- **Pros**: Natural fit for conversational agents; easy to add/remove agents
- **Cons**: Message ordering guarantees needed; can become chatty (token overhead); conversation history grows unbounded
- **Best for**: Small teams of agents (2-5) with clear interaction protocols

**Reference**: AutoGen (Microsoft) — conversational agent pairs/group chats. Now in maintenance mode; Microsoft recommends their new Agent Framework for new projects.

### 2.3 Shared State / Stream-Based Orchestration

**Concept**: A central orchestrator maintains explicit typed state. Agents read from and write to this state. The orchestrator controls the flow.

**How it works:**
1. Define a typed state schema (e.g., `AgentState(TypedDict)`)
2. Nodes (agents) are pure functions: `(state) -> state_update`
3. Edges define transitions between nodes
4. A checkpointer persists state for recovery

**Production characteristics:**
- **Pros**: Explicit, debuggable, checkpointable, supports human-in-the-loop
- **Cons**: Requires upfront schema design; less flexible for dynamic agent creation
- **Best for**: Production systems requiring observability, recovery, and audit trails

**Reference**: LangGraph — directed graph of nodes with explicit typed state, checkpointed via `AsyncPostgresSaver`. The production default for planner-executor and hierarchical agent patterns.

### 2.4 Event-Driven / Stream-Based

**Concept**: Agents emit and react to events. The orchestrator routes events between agents based on type and content.

**How it works:**
1. Agents emit structured events (e.g., `TaskCompleted`, `AnalysisReady`)
2. An event router dispatches to interested agents
3. Agents can subscribe to event streams
4. Backpressure and flow control prevent overwhelming agents

**Production characteristics:**
- **Pros**: Loose coupling, natural for async workflows, scales well
- **Cons**: Event schema governance needed; debugging distributed flows is hard
- **Best for**: Long-running workflows, systems with external integrations

**Reference**: Megagon Labs' "Compound AI Systems" blueprint (arXiv:2406.00584, 2504.08148) — uses "streams" as the key orchestration concept to coordinate data and instructions among agents in enterprise settings.

### 2.5 Hierarchical / Supervisor Pattern

**Concept**: A supervisor agent manages worker agents. The supervisor decides which worker to invoke, when to escalate, and how to aggregate results.

**How it works:**
1. Supervisor receives the task and decomposes it
2. Workers execute sub-tasks independently
3. Supervisor collects results, checks quality, and either accepts or re-routes
4. Supervisor has final authority on completion

**Production characteristics:**
- **Pros**: Clear authority chain, easy to add workers, natural escalation path
- **Cons**: Supervisor is a bottleneck; supervisor LLM quality caps system quality
- **Best for**: Most production multi-agent systems — the most common pattern

**Reference**: LangGraph's Agent Supervisor pattern; Kore.ai's Supervisor Pattern docs.

---

## 3. Task Decomposition

### 3.1 Why Decompose?

Monolithic approaches fail for complex tasks because:
1. **Context window limits** — complex instructions exceed the LLM's context
2. **Reasoning complexity** — LLMs perform better on smaller, well-defined problems
3. **Tool specificity** — tools are designed for specific functions; decomposition maps sub-tasks to appropriate tools
4. **Error recovery** — isolate failures and retry only the affected sub-task
5. **Modularity** — decomposed sub-tasks become reusable skills

### 3.2 Decomposition Strategies

#### LLM-Driven Decomposition
The LLM itself breaks down the goal into steps.

- **Zero-shot**: "Given this goal, produce a plan." Simple but unreliable for complex tasks.
- **Few-shot**: Provide examples of good decompositions. Better quality, more tokens.
- **Chain-of-thought**: Ask the LLM to reason step-by-step before producing the plan. Improves decomposition quality significantly.

#### Hierarchical Decomposition
Break down recursively: goal → sub-goals → sub-sub-goals → atomic tasks.

```
Goal: "Plan a conference"
├── Sub-goal: "Find speakers"
│   ├── Task: "Search for AI researchers"
│   ├── Task: "Check availability"
│   └── Task: "Send invitations"
├── Sub-goal: "Arrange logistics"
│   ├── Task: "Book venue"
│   ├── Task: "Arrange catering"
│   └── Task: "Set up AV equipment"
└── Sub-goal: "Create budget"
    ├── Task: "Estimate costs"
    └── Task: "Get approvals"
```

#### DAG-Based Decomposition
Model tasks as a Directed Acyclic Graph with dependencies. Tasks with no dependencies run in parallel; dependent tasks wait.

**Production benefit**: Maximum parallelism while respecting dependencies. This is how LangGraph and similar frameworks model multi-agent workflows.

#### Constraint-Induced Decomposition (ACONIC)
A systematic framework that models constraints (precedence, resource, temporal) and decomposes based on constraint analysis. More principled than pure LLM-driven approaches.

**Reference**: "An Approach for Systematic Decomposition of Complex LLM Tasks" (arXiv:2510.07772).

### 3.3 Decomposition in AgentHarness Context

The current `ReActAgent` uses a flat loop (Thought → Action → Observation). For multi-agent:

- **Planner agent**: Decomposes the user goal into a task DAG
- **Executor agents**: Each handles one node in the DAG
- **Critic agent**: Validates results against the original goal

The existing `Session.state` dict and `ContextChunk` model can store the DAG and per-task results.

---

## 4. Result Aggregation

### 4.1 Aggregation Strategies

#### Majority Voting
Each agent produces an answer; the most common answer wins.

- **Plurality**: Most first-preference votes (relative majority)
- **Absolute majority**: More than half the votes
- **Unanimous**: All agents agree (consensus)

**When to use**: Classification, fact-checking, any task with a verifiable correct answer.

**Risk**: "Representational collapse" — agents with the same base model and similar prompts produce correlated errors. Majority vote doesn't help if all agents are wrong in the same way.

**Reference**: "Representational Collapse in Multi-Agent LLM Committees" (arXiv:2604.03809) — shows that naively replicating the same model under different role prompts and aggregating by majority vote often fails because agents don't contribute complementary evidence.

#### Weighted Aggregation
Weight each agent's output by a confidence score or historical accuracy.

```
final_output = Σ(weight_i × output_i) / Σ(weight_i)
```

**When to use**: Agents have different expertise levels or track records.

#### Conflict-Aware Aggregation (Signed Graph)
Model inter-agent relationships as a signed graph: positive edges (agreement/trust) and negative edges (conflict/distrust). Use signed message passing to reinforce trustworthy signals and suppress conflicting ones.

**When to use**: High-stakes decisions where some agents may be unreliable or adversarial.

**Reference**: SIGMA framework (arXiv:2605.19418) — "Conflict-Resilient Multi-Agent Reasoning via Signed Graph Modeling." Outperforms SOTA baselines on six benchmark datasets.

#### Structured Aggregation
Instead of voting on final answers, aggregate structured outputs (e.g., merge code changes, combine document sections).

**When to use**: Code generation, document writing, any task where outputs are compositional.

### 4.2 Aggregation in Practice

**Production pattern**: The orchestrator/supervisor performs aggregation. Workers don't aggregate each other's outputs — that's the supervisor's job.

```python
# Pseudocode for supervisor-based aggregation
async def orchestrate(task: str, workers: list[Agent]) -> AgentResponse:
    # 1. Decompose
    subtasks = await planner.decompose(task)
    
    # 2. Execute in parallel
    results = await asyncio.gather(*[
        worker.execute(subtask) for worker, subtask in zip(workers, subtasks)
    ])
    
    # 3. Aggregate
    final = await supervisor.aggregate(task, subtasks, results)
    
    # 4. Validate
    if not await critic.validate(task, final):
        # Re-plan and retry
        return await orchestrate(task, workers)
    
    return final
```

---

## 5. Conflict Resolution

### 5.1 Sources of Conflict

Conflicts arise when:
- Agents produce contradictory outputs
- Agents disagree on facts or interpretations
- Agents have incompatible assumptions
- One agent's output invalidates another's work

### 5.2 Resolution Strategies

#### Supervisor Arbitration
The supervisor agent reviews conflicting outputs and decides which to trust. The supervisor can:
- Ask agents to justify their reasoning
- Request additional evidence
- Fall back to a tie-breaking agent
- Escalate to human review

**Most common in production** — simple, debuggable, and the supervisor already has full context.

#### Consensus Protocols
Agents iterate until they reach agreement:
1. Each agent produces an initial answer
2. Agents share answers and reasoning
3. Each agent revises its answer based on others' reasoning
4. Repeat until consensus or max rounds

**Risk**: Groupthink — agents converge on a wrong answer because they influence each other. The "consensus" may just be the most persuasive (not most correct) agent winning.

**Reference**: "An Electoral Approach to Diversify LLM-based Multi-Agent Collective Decision-Making" (EMNLP 2024) — analyzes majority voting vs. consensus in multi-agent decision-making.

#### Signed Graph Conflict Resolution
Explicitly model trust and conflict relationships:
1. Construct a signed graph: nodes = agents, edges = agreement (+) or conflict (-)
2. Weight edges by confidence
3. Run signed message passing: reinforce + edges, suppress - edges
4. Aggregate with structure-aware weighting

**Best for**: Systems where agent reliability varies and conflicts are expected.

#### Validation Gates
Before accepting any agent's output, validate it against:
- Schema constraints (does it match the expected format?)
- Consistency checks (does it contradict known facts?)
- Quality thresholds (is it complete and coherent?)

**Reference**: "Multi-Agent Error Recovery Patterns" (Naitive Cloud, 2026) — validation gates stop wrong or incomplete outputs before they move downstream. In one invoice workflow, recovery controls pushed success from 78% to 96%.

### 5.3 Error Recovery Patterns

| Pattern | Best for | Trade-off |
|---------|----------|-----------|
| **Retry + backoff** | Short outages (429s, timeouts) | More latency, repeat cost |
| **Circuit breaker** | Failing shared tools/models | Needs threshold tuning |
| **Validation gates** | Wrong/incomplete outputs | Extra checks add cost |
| **Idempotent sagas** | Partial side effects | More build work |
| **Checkpointing** | Long workflows | Extra state storage |
| **Budget guardrails** | Runaway loops | Hard caps can stop valid tasks |
| **Human escalation** | High-risk decisions | Slowest, most expensive |

**Critical insight**: "If five agents each succeed 98% of the time, the full chain still lands at only about 90% success. In a 10-step flow at 95% per step, end-to-end success drops to 59.9%."

---

## 6. Agent Specialization

### 6.1 Specialization Dimensions

Agents can specialize along multiple axes:

| Dimension | Example | Benefit |
|-----------|---------|---------|
| **Role** | Planner, Executor, Critic | Separation of concerns |
| **Domain** | Code agent, data agent, search agent | Deep expertise |
| **Model** | Fast/cheap model for simple tasks, powerful model for complex reasoning | Cost optimization |
| **Tools** | One agent has shell access, another has file access | Security isolation |
| **Context** | Each agent sees only relevant context | Reduced noise, larger effective context |

### 6.2 The Planner-Executor-Critic Pattern

The most reliable production pattern for agent internal structure:

**Planner** — What should happen
- Interprets the goal, breaks it into steps
- Applies constraints, defines success criteria
- Does NOT call tools or execute actions

**Executor** — What actually happens
- Carries out the plan exactly as specified
- Calls tools, APIs, or code
- Does NOT reason about correctness or change the plan

**Critic** — Was it acceptable
- Evaluates outcomes against original intent
- Checks correctness, risk, scope, completeness
- Does NOT execute fixes or take actions

**Why this works**: "No component does another's job. This prevents improvised execution, self-justified mistakes, and uncontrolled retries. The difference is not intelligence. It is reliability."

**Reference**: "Planner–Executor–Critic: Engineering Reliable AI Agents" (2026).

### 6.3 Role-Based Specialization (CrewAI Model)

CrewAI uses role-based "crews" where each agent has:
- A **role** (e.g., "Senior Researcher", "Code Reviewer")
- A **goal** (what it's trying to achieve)
- A **backstory** (context that shapes its behavior)
- A set of **tools** it can access

Agents in a crew can be organized sequentially (one after another) or hierarchically (with a manager).

### 6.4 Domain-Specialized Agents

For production systems, domain specialization means:
- **Custom system prompts** with domain-specific instructions
- **Curated tool sets** — only relevant tools for that domain
- **Domain-specific context** — relevant knowledge, schemas, examples
- **Model selection** — some domains need stronger models (code), others don't (extraction)

### 6.5 Specialization in AgentHarness

Current state: Single `ReActAgent` with `agent_id` field. The `agent_id` is a string identifier but doesn't drive specialization.

**Path to specialization:**
1. Define agent profiles (role, system prompt, tool allowlist, model preference)
2. The `agent_id` in `Session` maps to a profile
3. The `Container` can wire different provider configs per agent
4. The `ToolRegistry` supports per-agent tool filtering

---

## 7. Production Framework Comparison

| Framework | Core Model | State Management | Best For |
|-----------|-----------|-----------------|----------|
| **LangGraph** | Directed graph of nodes/edges | Explicit typed state, checkpointed | Complex control flow, planner-executor, hierarchical agents |
| **CrewAI** | Role-based crews + event-driven Flows | Sequential/hierarchical task pipeline | Quick prototyping, role-based teams |
| **AutoGen** | Conversational agent pairs/group chats | In-memory conversation history | Research, code-running agents (now in maintenance mode) |

**Production recommendation (2026)**: LangGraph is the default for new production systems. CrewAI is fastest for prototyping. AutoGen is in maintenance mode — don't start new projects with it.

---

## 8. Architecture Recommendations for AgentHarness

### 8.1 Proposed Multi-Agent Architecture

```
┌─────────────────────────────────────────────────┐
│                  Orchestrator                    │
│  (supervisor pattern — manages the workflow)     │
├─────────────────────────────────────────────────┤
│                                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐      │
│  │ Planner  │  │ Executor │  │  Critic  │      │
│  │  Agent   │  │  Agent   │  │  Agent   │      │
│  │          │  │          │  │          │      │
│  │ Decomposes│  │ Executes │  │ Validates│      │
│  │ goal into │  │ subtasks │  │ results  │      │
│  │ task DAG  │  │          │  │          │      │
│  └──────────┘  └──────────┘  └──────────┘      │
│                                                  │
│  ┌──────────────────────────────────────────┐   │
│  │         Shared State (Blackboard)         │   │
│  │  Task DAG | Results | Context | Status    │   │
│  └──────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
```

### 8.2 Mapping to Existing Code

| Component | Current | Multi-Agent Extension |
|-----------|---------|----------------------|
| `ReActAgent` | Single agent loop | Base class for Planner/Executor/Critic agents |
| `Session.state` | Dict for arbitrary state | Store task DAG, per-task results, agent assignments |
| `ContextChunk` | Per-agent context chunks | Tag with `task_id` for task-scoped context |
| `Container` | Wires singletons | Add `AgentRegistry` for multi-agent wiring |
| `ToolRegistry` | Global tool access | Per-agent tool filtering |
| `PromptAssembler` | Single prompt assembly | Per-role prompt templates |

### 8.3 Implementation Phases

**Phase 1 — Agent Registry & Profiles**
- Define `AgentProfile` dataclass (role, system_prompt, tool_allowlist, model)
- Extend `Container` to hold multiple agents
- Route tasks to agents based on profile

**Phase 2 — Supervisor Orchestration**
- Implement supervisor that decomposes tasks and routes to workers
- Use `asyncio.gather` for parallel execution
- Store task DAG in `Session.state`

**Phase 3 — Result Aggregation & Validation**
- Implement majority voting and weighted aggregation
- Add validation gates before accepting results
- Implement retry with backoff for failed subtasks

**Phase 4 — Conflict Resolution & Recovery**
- Add circuit breakers for failing agents/tools
- Implement checkpointing for long workflows
- Add budget guardrails to prevent cost blowups
- Human escalation path for high-risk decisions

---

## 9. Key Takeaways

1. **Start simple**: Single agent + tools is often enough. Add multi-agent only when justified.
2. **Supervisor pattern is the production default**: Clear authority, easy to debug, natural escalation.
3. **Planner-Executor-Critic is the most reliable internal structure**: Separation of thinking, acting, and judging.
4. **Explicit state > implicit conversation**: Typed, checkpointed state (LangGraph model) beats chat history for production.
5. **Diversity matters in aggregation**: Same model + different prompts ≠ diverse opinions. Use different models or genuinely different reasoning strategies.
6. **Error recovery is not optional**: Budget guardrails, circuit breakers, and validation gates are production requirements.
7. **Token costs multiply**: 5 agents × 10 iterations × 3 retries = 150 LLM calls per task. Budget guardrails prevent runaway costs.
8. **Observability is critical**: Every agent decision, tool call, and aggregation step must be auditable.

---

## References

1. Han, B. & Zhang, S. "Exploring Advanced LLM Multi-Agent Systems Based on Blackboard Architecture." arXiv:2507.01701, 2025.
2. Kandogan, E. et al. "A Blueprint Architecture of Compound AI Systems for Enterprise." arXiv:2406.00584, 2024.
3. Kandogan, E. et al. "Orchestrating Agents and Data for Enterprise." arXiv:2504.08148, 2025.
4. He, L. et al. "Conflict-Resilient Multi-Agent Reasoning via Signed Graph Modeling." arXiv:2605.19418, 2026.
5. "AI Agent Orchestration Patterns." Microsoft Azure Architecture Center.
6. "Multi-Agent Error Recovery Patterns." Naitive Cloud, 2026.
7. "Planner–Executor–Critic: Engineering Reliable AI Agents." 2026.
8. "LangGraph vs CrewAI vs AutoGen: Best AI Agent Framework 2026." Spheron Network, 2026.
9. "LLM Agent Task Decomposition Strategies." ApX Machine Learning.
10. "An Electoral Approach to Diversify LLM-based Multi-Agent Collective Decision-Making." EMNLP 2024.
11. "Representational Collapse in Multi-Agent LLM Committees." arXiv:2604.03809, 2026.
12. "An Approach for Systematic Decomposition of Complex LLM Tasks." arXiv:2510.07772, 2025.
