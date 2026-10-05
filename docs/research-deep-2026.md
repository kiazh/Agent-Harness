# Deep Research Report: LLM Agent Memory, Context Management, and Multi-Agent Patterns (2024–2026)

**Date:** 2026-10-05  
**Author:** AgentHarness Research Initiative  
**Scope:** Production patterns and academic advances applicable to the AgentHarness codebase

---

## Executive Summary

This report synthesizes research across five critical areas for production LLM agent systems: token usage tracking and budget enforcement, multi-agent orchestration patterns, memory management for LLM agents, context window management and compression, and RAG pipeline optimization. Each section includes key papers, production patterns, best practices, and specific recommendations for the AgentHarness codebase.

**Key Takeaway:** The AgentHarness codebase already implements several best practices (hybrid memory retrieval, Ebbinghaus decay, importance scoring, usage budgeting with advisory locks, hybrid RAG with RRF fusion). The highest-impact improvements are: (1) upgrading from advisory to enforced budget guards with circuit breakers, (2) implementing A-MEM-style dynamic memory linking and evolution, (3) adding observation masking + LLM summarization hybrid context management, (4) integrating GraphRAG for entity-relationship-aware retrieval, and (5) adopting the orchestrator-worker multi-agent pattern with per-agent model tiering.

---

## 1. LLM Token Usage Tracking and Budget Enforcement

### 1.1 The Cost Problem in Production Agents

Multi-agent systems amplify token costs in ways single-chatbot budgeting cannot anticipate. Key findings from 2025–2026 production incident reports:

- **The $47K Loop Incident (Nov 2025):** Four LangChain agents entered an infinite loop between Analyzer and Verifier agents, running for 264 hours and costing $47,000. Root cause: no per-agent budget ceiling and no pre-execution enforcement mechanism. [Source: noburn.dev, waxell.ai]
- **15x Token Multiplier:** Anthropic reported that their multi-agent research system consumed roughly 15x the tokens of a standard chat interaction, because orchestrator agents spawn subagents whose exploratory research each carries its own context windows and tool calls. [Source: tryinterlock.com]
- **Context Accumulation Compounds Cost:** A 20-turn conversation does not cost 20× a 1-turn conversation — it costs significantly more, because every turn includes the entire history. By step five, the input token count can be several times the original prompt. [Source: conceptualise.de]
- **2-to-1 Input-to-Output Tax:** A 2026 Concordia University study found a 2-to-1 input-to-output token ratio as a communication tax between agents, meaning every message an agent sends triggers roughly twice as many tokens in context overhead. [Source: getreadyforagents.com]

### 1.2 Core Architecture: Monitoring vs. Enforcement

The most important conceptual distinction in agent cost governance is between **monitoring** (recording what was spent and alerting when thresholds are crossed) and **enforcement** (preventing spend by intercepting execution before the next API call). [Source: letsbuildsolutions.com]

**Monitoring-only approaches fail because:**
- Alerts fire after the spend has already occurred
- Agents can reason around soft warnings
- No mechanism terminates a session before it burns past the ceiling
- Provider-side dashboards are designed for accounting, not engineering — they tell you what you spent after the billing period closes, not before a single call goes over budget [Source: noburn.dev]

**Enforcement requires:**
1. **Per-agent token budget** — a numeric ceiling assigned to a named agent identity before any run begins
2. **Cost circuit breaker** — measures tokens consumed per unit time against a threshold; trips when rate exceeds legitimate bounds
3. **Enforcement gate** — code that sits between the orchestrator and the model API client, checking budget before every call

### 1.3 The Three-Layer Governance Stack

Based on production deployments in 2026, the consensus architecture has three layers: [Source: cordum.io, conceptualise.de]

| Layer | Mechanism | What It Prevents | Typical Impact |
|-------|-----------|------------------|----------------|
| Per-task token budget | Unbounded single requests | Eliminates worst spikes | Agent runtime/orchestrator |
| Max steps & max tool calls | Infinite re-planning loops | High | Agent loop config |
| Per-tenant daily/monthly cap | One team draining the budget | Predictable ceiling | Gateway/policy layer |
| Rate limiting & concurrency caps | Cost from traffic surges | Medium-high | API gateway |
| Model routing | Overuse of frontier models | 40-65% cost reduction | Routing layer |
| Semantic/response caching | Repeated identical work | 15-40% cost reduction | Caching layer |
| Prompt & context trimming | Context bloat per step | 10-25% cost reduction | Prompt assembly |

### 1.4 Budget Setting Methodology

Production deployments follow a consistent methodology for setting budget ceilings: [Source: getreadyforagents.com, tryinterlock.com]

1. **Run baseline measurements** — Run each agent against representative workloads and record the distribution of cost per completed task (the 95th percentile matters more than the mean because that tail is what blows up monthly budgets)
2. **Set initial caps** — At roughly 1.5× the observed p95 cost per task, multiplied by expected daily volume, then divide by 1.3 as a safety margin
3. **Calibrate after 30 days** — Adjust based on actual production data
4. **Graduated thresholds** — Warn at 70% consumption, throttle at 85%, hard stop at 100%

### 1.5 Circuit Breaker Design

The circuit breaker has three states: [Source: getreadyforagents.com]

- **Closed:** Calls proceed normally
- **Open:** Calls are blocked; agent writes current task state to persistent storage before any blocking occurs (preserves ability to resume after human review)
- **Half-open:** One test call is allowed through to check whether the rate has returned to normal

**Rate threshold calibration:** Run your agent against real workloads and observe the 99th-percentile token rate during normal operation. Multiply that by three to four to get a threshold that distinguishes a loop from a legitimately large task. [Source: getreadyforagents.com]

### 1.6 Common Failure Modes (63-Incident Catalogue)

A June 2026 catalogue documented 63 confirmed token budget overrun incidents across 21 orchestration frameworks: [Source: agoradigest.com]

1. **Retry amplification** — A single malformed tool response triggers a costly retry storm
2. **Delegation leaks** — Subagent spawns inherit parent budgets without deduction, allowing each subagent to independently exhaust the full allowance
3. **Shadow consumption** — 30 out of 30 concurrent agent runs overshot their allocated budget using standard counter-based tracking, because mutable shared state is fundamentally unsuited to budget enforcement in concurrent delegating systems
4. **Cache invalidation storms** — Cost from repeated cache misses on shared context
5. **Concurrent aliasing** — Multiple agents sharing the same budget counter without transactional deduction

### 1.7 Recommendations for AgentHarness

The AgentHarness `UsageStore` in `ah/core/usage.py` already implements session/agent token budgets with advisory locks — a strong foundation. Specific improvements:

| Recommendation | Priority | Effort | Impact |
|----------------|----------|--------|--------|
| **Add circuit breaker** — Track token consumption rate per agent over a sliding window (e.g., 1-minute average); trip when cost exceeds threshold | High | Medium | Catches runaway loops within 60 seconds |
| **Upgrade from advisory to enforced budgets** — Currently the reservation check raises `UsageBudgetExceededError`; add a hard kill switch that terminates the agent loop entirely, not just the current call | High | Medium | Prevents budget drift across long runs |
| **Add per-call token cap** — Reject individual calls estimated to consume too many tokens before the API call goes out | High | Low | Prevents single-call budget busts |
| **Implement subagent budget deduction** — When delegating, atomically deduct from the parent agent's remaining budget before spawning the subagent | High | Medium | Prevents delegation leaks |
| **Add cost anomaly detection** — Flag when an agent's cost per action spikes 3× from its baseline | Medium | Medium | Early warning for prompt regressions |
| **Add fleet-level budget** — Total spend across all agents with graceful degradation (throttle non-critical agents at 80%, pause all at 95%) | Medium | Medium | Prevents system-wide cost overruns |
| **Budget in dollars, not tokens** — Token prices change quarterly; models differ by 10-100× in per-token cost. Track dollars as the primary budget unit, tokens as diagnostic | Low | Low | Future-proof against pricing changes |

**Implementation sketch for circuit breaker:**

```python
class TokenCircuitBreaker:
    """Cost-aware circuit breaker for per-agent token consumption."""
    
    def __init__(self, rate_threshold: int = 10000, window_seconds: int = 60):
        self.rate_threshold = rate_threshold  # tokens per minute
        self.window_seconds = window_seconds
        self.consumption_log: list[tuple[datetime, int]] = []
        self.state = "closed"  # closed, open, half-open
    
    def record_consumption(self, tokens: int) -> None:
        now = datetime.utcnow()
        self.consumption_log.append((now, tokens))
        cutoff = now - timedelta(seconds=self.window_seconds)
        self.consumption_log = [(t, tok) for t, tok in self.consumption_log if t > cutoff]
        
        if self.state == "open":
            self.state = "half-open"
        
    def check_rate(self) -> bool:
        """Returns True if calls are allowed."""
        total = sum(tok for _, tok in self.consumption_log)
        if total > self.rate_threshold:
            self.state = "open"
            return False
        if self.state == "half-open":
            self.state = "closed"
        return True
```

---

## 2. Multi-Agent Orchestration Patterns

### 2.1 The Five Production Patterns

Nearly every production multi-agent system maps to one of five orchestration patterns: [Source: yashbogam.me, seodatapulse.com]

| Pattern | Shape | Best For | Trade-off |
|---------|-------|----------|-----------|
| **Orchestrator-Worker** | One planner delegates to specialist workers | Open-ended tasks where sub-tasks aren't known upfront | Orchestrator is a single point of failure |
| **Hierarchical (Manager-Crew)** | A manager coordinates a fixed team of roles | Structured workflows with clear roles | Less flexible; roles are designed in advance |
| **Sequential Pipeline** | Agents in a fixed chain, output → input | Deterministic, auditable, step-by-step processes | No adaptivity; a bad early step poisons the rest |
| **Swarm / Handoff** | Peers hand control to whichever agent fits next | Routing and triage (e.g., support: billing vs. tech) | Handoff logic can loop; needs guardrails |
| **Network / Decentralized** | Agents discover and negotiate peer-to-peer | Cross-vendor or open agent ecosystems | Hardest to debug; emerging, least mature |

### 2.2 Framework Landscape (2026)

| Framework | Paradigm | Token Overhead | State Management | Best For |
|-----------|----------|---------------|-----------------|----------|
| **LangGraph** | Graph state machines | ~9% (lowest) | Explicit checkpointing + time travel | Complex conditional workflows, human-in-the-loop |
| **CrewAI** | Role-based teams | ~18% | Task outputs passed sequentially | Fast prototyping, team-of-specialists workflows |
| **AutoGen / AG2** | Conversational actors | Highest (20+ calls/task) | Message history (in-memory) | Research, code-writing agents |
| **OpenAI Agents SDK** | Handoff-via-tool-call | Low | HandoffContext (ephemeral) | OpenAI-centric estates, MCP-first integrations |
| **Google ADK** | Hierarchical agent tree | Low | Session state with pluggable backends | Vertex AI estates, enterprise observability |
| **Pydantic AI** | Type-safe validated agents | Low | Pydantic-typed schemas | Type safety and validation |

### 2.3 The Hybrid Pattern: Production Default for Complex Systems

The emerging consensus for complex production systems is a **hybrid architecture**: LangGraph as the outer orchestrator with CrewAI crews as inner workers. [Source: yashbogam.me]

```
LangGraph Outer Orchestrator
├── Route Decision
├── Research Needed → CrewAI Research Crew (researcher → writer → editor)
├── Code Changes → CrewAI Coding Crew (analyzer → writer → reviewer)
└── Human Approval Gate → LangGraph interrupt node
```

**Why this works:** LangGraph provides control, state management, routing decisions, retry logic, and human approval gates at the macro level. CrewAI provides ergonomic role-based abstractions for the specialist subtasks within each node. Each layer does what it's best at.

### 2.4 Model Tiering: Per-Agent Model Selection

A common production pattern is **model tiering**: use a fast, cheap model for triage and routing agents, and a frontier model for reasoning and synthesis. [Source: developersdigest.tech]

| Agent Role | Model Tier | Example | Cost Savings |
|-----------|-----------|---------|--------------|
| Triage/Routing | Cheap/fast | GPT-5.4-mini, Claude Haiku 4.5 | 80-95% |
| Extraction/Mechanical | Mid-tier | GPT-5.4-mini | 60-80% |
| Reasoning/Synthesis | Frontier | Claude Sonnet 5, GPT-5.4 | — (necessary) |
| Code generation | Specialized | Claude Sonnet 5, Qwen3-Coder | Varies |

### 2.5 Multi-Agent Cost Multipliers

Production measurements show significant cost differences between patterns: [Source: peppereffect.com]

| Framework | P95 Tokens | P95 Latency | Cost per Query | Accuracy |
|-----------|-----------|-------------|---------------|----------|
| LangGraph | ~13,500 | 2-4 sec | $0.30-0.80 | High (consistent) |
| CrewAI | ~1,350,000 | 8-15 sec | $2.00-8.00 | High (sequential) |
| OpenAI Agents SDK | ~15,000 | 2-3 sec | $0.20-0.60 | High (bounded context) |
| AutoGen/AG2 | ~56,700 | 10-18 sec | $4.00-12.00 | Highest (debate refines) |

### 2.6 Common Failure Modes and Mitigations

| Failure | Symptom | Fix |
|---------|---------|-----|
| Premature multi-agent | Huge token bill, no quality gain | Start with one agent; add roles only when justified |
| Vague roles | Agents overlap and contradict each other | Sharpen each role; add explicit "do not" boundaries |
| Context bleed | Workers see the whole transcript, costs explode | Pass each worker only its brief; isolate state |
| Tool overload | Agent picks the wrong tool | Scope tools per-role; give the minimum needed |
| Handoff loops | Two agents ping-pong forever | Cap handoffs; add a terminal escalate-to-human branch |
| No observability | "It broke and I don't know where" | Instrument tracing from day one |
| Ungated actions | An agent emails a customer by mistake | Human-in-the-loop gate on all irreversible actions |
| Unbounded loops | Runaway cost from self-correction | Hard caps on planning, retries, and iterations |

### 2.7 When to Use Multiple Agents

The decision to use multiple agents should be driven by measured need, not preference: [Source: seodatapulse.com]

| Signal | Why It Justifies Multiple Agents |
|--------|--------------------------------|
| Context overflow | The task needs more context than one agent can hold cleanly |
| Tool specialization | Different sub-tasks need different, non-overlapping tools |
| Parallelism | Sub-tasks are independent and can run concurrently |
| Separation of concerns | You want an adversarial check — a separate "critic" agent with fresh context |
| Distinct personas | Sub-tasks reward genuinely different system prompts, models, or temperatures |

### 2.8 Recommendations for AgentHarness

The AgentHarness `delegate` tool in `ah/tools/agents.py` already implements a basic handoff pattern. Specific improvements:

| Recommendation | Priority | Effort | Impact |
|----------------|----------|--------|--------|
| **Adopt orchestrator-worker pattern** — Implement a lead orchestrator agent that decomposes goals into sub-tasks, dispatches to workers, and synthesizes results | High | High | Enables complex multi-step workflows |
| **Add per-agent model tiering** — Allow each agent definition to specify its model tier (cheap/mid/frontier) | High | Medium | 40-65% cost reduction on routine steps |
| **Implement handoff caps** — Add a hard limit on delegation depth and total handoffs per request | High | Low | Prevents infinite delegation loops |
| **Add parallel fan-out** — Allow the orchestrator to dispatch multiple independent workers concurrently and merge results | Medium | Medium | Cuts wall-clock latency for parallelizable tasks |
| **Implement maker-checker pattern** — Add a critic/verifier agent that reviews the orchestrator's output with fresh context | Medium | Medium | Cuts hallucinations in high-stakes domains |
| **Add structured handoff metadata** — Include typed metadata (reason, expected output type) in handoffs | Medium | Low | Improves auditability and debugging |
| **Scope tools per agent** — Give each agent only the tools it needs for its role | High | Medium | Reduces tool selection errors and token costs |

---

## 3. Memory Management for LLM Agents

### 3.1 MemGPT / Letta: The LLM Operating System

The foundational paper **"MemGPT: Towards LLMs as Operating Systems"** (arXiv:2310.08560, 2023) introduced the concept of an OS-inspired memory hierarchy for LLM agents: [Source: letta.com, arxiv.org]

**Core concepts:**
- **Self-editing memory** — LLMs use tools to edit their own context window and external storage
- **Memory hierarchy** — Distinguishing between in-context memory (core, like RAM) and out-of-context memory (archival, like disk)
- **Context window management** — Intelligent paging and memory consolidation techniques

**Architecture:**
- **In-context memory (core):** A reserved section of the LLM context window that is editable by the agent. The agent can append, replace, or delete sections of this memory.
- **Archival memory:** Long-term storage in a vector DB for memories that don't fit in context. The agent has read/write tools to this store.
- **Recall memory:** A log of all conversational history with date search and text search tools.

**Benchmark results:** Letta's filesystem-based memory agent achieves **74.0% on LoCoMo** with GPT-4o mini and minimal prompt tuning, significantly above Mem0's reported 68.5% for their top-performing graph variant. [Source: letta.com]

### 3.2 A-MEM: Agentic Memory (NeurIPS 2025)

**A-MEM: Agentic Memory for LLM Agents** (arXiv:2502.12110, NeurIPS 2025) introduces a novel memory system inspired by the **Zettelkasten method**: [Source: arxiv.org, neurips.cc]

**Key innovations:**

1. **Dynamic memory structuring** — Instead of predefined memory operations, the system autonomously generates contextual descriptions, forms meaningful connections with related memories, and evolves both content and relationships as new experiences emerge.

2. **Link generation (LG module)** — When a new memory is added, the system analyzes historical memories to identify relevant connections based on semantic similarities and shared attributes, establishing links where meaningful similarities exist.

3. **Memory evolution (ME module)** — As new memories are integrated, they trigger updates to the contextual representations and attributes of existing historical memories, allowing the memory network to continuously refine its understanding.

**Results:**
- Consistently outperforms all baselines across six foundation models
- **35% improvement over LoCoMo** (F1 score 3.45 vs 2.55)
- **192% higher than MemGPT** on multi-hop reasoning tasks
- **85-93% token reduction** per memory operation (1,200 tokens vs 16,900 for baselines)
- Processing time: 5.4 seconds with GPT-4o-mini, 1.1 seconds with Llama 3.2 1B

### 3.3 Mem0: The Memory Layer

**Mem0** (mem0.ai, Y Combinator-backed, 62.5K+ GitHub stars) provides a production-ready memory layer with: [Source: mem0.ai]

- **Four memory layers:** Conversation, session, user, and organizational
- **Single-pass ADD-only extraction:** New facts are added, not overwritten, so history is preserved rather than silently lost
- **Automatic condensation:** Condenses chat history into compact memories that cut token costs
- **Entity linking:** Links entities across memories for relationship-aware retrieval

**Key insight:** "Effective memory management for agents is not a single store. It is a hierarchy of stores with different retention windows, retrieval patterns, and purposes." [Source: mem0.ai]

### 3.4 Memory System Comparison

| System | Approach | Strengths | Weaknesses | LoCoMo Score |
|--------|----------|-----------|------------|--------------|
| **MemGPT/Letta** | OS-inspired hierarchy (core + archival + recall) | Self-editing, production framework, flexible | Requires careful prompt engineering | 74.0% |
| **A-MEM** | Zettelkasten-inspired dynamic linking | Autonomous organization, multi-hop reasoning | Higher compute cost (multiple LLM calls) | F1 3.45 |
| **Mem0** | Four-layer hierarchy with ADD-only extraction | Simple API, production-ready, entity linking | Single-pass extraction may miss context | 68.5% (graph) |
| **MemoryBank** | Ebbinghaus forgetting curve | Well-understood, simple | Limited organization | Lower than MemGPT |

### 3.5 Recommendations for AgentHarness

The AgentHarness memory system (`ah/memory/`) already implements Ebbinghaus decay, importance scoring, hybrid retrieval (dense + sparse), and a consolidator pipeline. Specific improvements:

| Recommendation | Priority | Effort | Impact |
|----------------|----------|--------|--------|
| **Add dynamic memory linking (A-MEM LG module)** — When a new memory is added, analyze existing memories to establish semantic links | High | Medium | Enables multi-hop reasoning over memories |
| **Add memory evolution (A-MEM ME module)** — When new memories are integrated, update contextual representations of related existing memories | High | Medium | Memory network continuously refines understanding |
| **Add four-layer memory hierarchy (Mem0 pattern)** — Separate conversation, session, user, and organizational memory stores | High | Medium | Better retention and retrieval granularity |
| **Add parent-context chunking for memory retrieval** — Store small memory chunks but include parent context during generation | Medium | Medium | Better precision with rich context |
| **Implement sleep-time compute** — Run memory consolidation during idle time, not at inference | Medium | Low | Reduces inference latency |
| **Add memory deduplication via embeddings** — Cosine similarity above 0.85 means "duplicate" (already partially implemented in consolidator) | Medium | Low | Prevents redundant memory storage |
| **Add memory strength decay visualization** — Expose current strength scores for debugging | Low | Low | Improves observability |

**Implementation sketch for dynamic memory linking:**

```python
class MemoryLinker:
    """A-MEM inspired dynamic memory linking."""
    
    def __init__(self, store: MemoryStore, similarity_threshold: float = 0.7):
        self.store = store
        self.threshold = similarity_threshold
    
    async def link_new_memory(self, new_memory: MemoryEntry) -> list[uuid.UUID]:
        """Analyze existing memories and establish links to the new memory."""
        if new_memory.embedding is None:
            return []
        
        # Search for semantically similar memories
        similar = await self.store.search_by_embedding(
            embedding=new_memory.embedding,
            limit=10,
        )
        
        linked_ids = []
        for existing, score in similar:
            if score >= self.threshold and existing.id != new_memory.id:
                # Create bidirectional link
                await self._create_link(new_memory.id, existing.id, score)
                linked_ids.append(existing.id)
        
        return linked_ids
    
    async def _create_link(self, from_id: uuid.UUID, to_id: uuid.UUID, strength: float) -> None:
        """Create a memory link with strength score."""
        await db.execute(
            """
            INSERT INTO memory_links (from_memory_id, to_memory_id, strength, created_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (from_memory_id, to_memory_id) DO UPDATE SET strength = $3
            """,
            from_id, to_id, strength,
        )
```

---

## 4. Context Window Management and Compression

### 4.1 The Context Challenge

As LLM agents tackle longer-horizon tasks, managing unbounded interaction trajectories under fixed context budgets becomes a core systems challenge. Agent trajectories are heterogeneous — they interleave observations, reasoning traces, and tool executions — so compression must preserve temporal dependencies, actionable state, and structural fidelity. [Source: exa.ai]

### 4.2 Compression Method Categories

| Category | Methods | Pros | Cons |
|----------|---------|------|------|
| **Document/retrieval-based** | Selective Context (Li et al., 2023), LLMLingua-2 (Pan et al., 2024) | Preserves original phrasing | Limited to explicit tokens |
| **Dialogue memory summarization** | LLM-generated summaries | Natural language compression | Hallucination risk, extra API cost |
| **Low-level KV cache compression** | LaCache, SnapKV, Quest | Model-agnostic | Requires model access |
| **Learned compression** | MemAgent (Yu et al., 2025) — RL-based memory slot overwrite | Scales to 3.5M tokens | Requires training |

### 4.3 Selective Context and LLMLingua

**Selective Context** (arXiv:2310.06201) identifies and prunes redundancy in the input context to make it more compact. It works by: [Source: arxiv.org]

1. Computing self-information scores for each token/phrase
2. Removing low-information tokens while preserving high-value content
3. Achieving significant reductions in memory cost and generation latency while maintaining comparable performance

**LLMLingua-2** (Pan et al., 2024) extends this with a more efficient token-level compression approach that maintains semantic fidelity better than heuristic methods.

### 4.4 Acon: Agent Context Optimization

**Acon** (arXiv:2510.00615) introduces a unified framework for compressing both environment observations and interaction histories in long-horizon agent tasks: [Source: arxiv.org]

**Key innovations:**

1. **Guideline optimization** — Instead of handcrafting compression prompts, Acon uses a gradient-free optimization pipeline that refines compressor prompts via failure analysis in natural language space. Given paired trajectories where full context succeeds but compressed context fails, capable LLMs analyze the causes of failure and update the compression guideline.

2. **Two-step optimization:**
   - **Compression minimization:** Minimize task failure rate with compressed context
   - **Compression maximization:** Given successful compressed trajectories, minimize context cost (encourage shorter yet sufficient contexts)

3. **Compressor distillation** — Distill the optimized LLM compressor into smaller models (Qwen3-14B, Qwen3-8B, Phi-4) using LoRA. Distilled compressors retain over 95% of the performance of gpt-4.1 compressor while reducing overhead.

**Results:**
- Reduces memory usage by 26-54% (peak tokens) while largely preserving task performance
- Preserves over 95% of accuracy when distilled into smaller compressors
- Enhances smaller LMs as long-horizon agents with up to 46% performance improvement

### 4.5 DTOC: Dynamic Tool Output Compression

**DTOC** (2025) models context updates as explicit and reversible operations within the agent reasoning loop: [Source: exa.ai]

- Retains full tool outputs in external memory
- Inserts compact placeholders into the active context
- Enables selective reconstruction when needed
- Reversibility is critical: disable-only compression variants degraded performance, while full DTOC recovered baseline accuracy at substantially lower context cost

**Results:** For responsive models (Sonnet 4.6, GPT-5.4), DTOC reduces input tokens (10.3% and 12.7%) and agent steps (2.4% and 32.3%), while increasing solve rates (2.5× and 1.5× higher) and lowering cost per solved task (3× and 3.5× lower).

### 4.6 Observation Masking vs. LLM Summarization

A JetBrains Research study (2025) compared two primary context management strategies: [Source: blog.jetbrains.com]

| Approach | Mechanism | Cost Reduction | Performance Impact |
|----------|-----------|---------------|-------------------|
| **Observation masking** | Replace older observations with placeholders once outside a fixed window | >50% | Neutral to slightly positive |
| **LLM summarization** | Use a separate LLM to compress older interactions into summaries | >50% | Can increase turns by 15% (agent runs longer) |
| **Hybrid (masking + occasional summarization)** | Masking as first line; LLM summarization as last resort for very long contexts | 57-61% | +2.6% success rate, saves $35 per benchmark run |

**Key finding:** "Even though summarization sounds smart, in practice, it's extra costly and doesn't reliably outperform the simpler masking approach." The hybrid approach — observation masking as the primary defense with occasional LLM summarization for very long contexts — provides the best balance of cost efficiency and reliability.

**Recommended parameters:**
- Masking window: Keep the latest 10 turns in full
- Summarization trigger: After collecting a large batch of turns (21+)
- Always retain the most recent 10 turns uncompressed

### 4.7 Recommendations for AgentHarness

The AgentHarness `ContextManager` in `ah/core/context.py` implements LRU caching and batch insert but lacks active context compression. Specific improvements:

| Recommendation | Priority | Effort | Impact |
|----------------|----------|--------|--------|
| **Implement observation masking** — Replace older tool outputs with placeholders when they fall outside a fixed window | High | Medium | >50% cost reduction on long sessions |
| **Add LLM summarization as fallback** — When context grows beyond a threshold, use a separate summarizer LLM to compress older interactions | High | Medium | Enables infinite conversation scaling |
| **Implement hybrid approach** — Observation masking as primary defense; occasional LLM summarization for very long contexts | High | Medium | Best balance of cost and quality |
| **Add DTOC for tool outputs** — Retain full tool outputs in external memory, insert compact placeholders in active context, enable on-demand restoration | Medium | Medium | Reversible compression, maintains accuracy |
| **Implement Acon-style guideline optimization** — Use failure analysis to refine compression prompts | Medium | Low | Gradient-free, works with any LLM |
| **Add context compression metrics** — Track compression ratio, token savings, and quality preservation | Low | Low | Enables data-driven tuning |

**Implementation sketch for observation masking:**

```python
class ObservationMasker:
    """Masks older observations to reduce context size while preserving recent context."""
    
    def __init__(self, window_size: int = 10):
        self.window_size = window_size
    
    def mask_older_turns(self, turns: list[dict]) -> list[dict]:
        """Replace older observations with placeholders."""
        if len(turns) <= self.window_size:
            return turns
        
        masked = []
        for i, turn in enumerate(turns):
            if i < len(turns) - self.window_size:
                # Replace with placeholder
                masked.append({
                    "role": turn["role"],
                    "content": "[Earlier observation omitted for brevity — use search to retrieve if needed]",
                    "masked": True,
                    "original_index": i,
                })
            else:
                masked.append(turn)
        return masked
```

---

## 5. RAG Pipeline Optimization

### 5.1 The RAG Quality Gap

The default "chunk + embed + query" RAG pipeline gets approximately 70% accuracy. Upgrading to hybrid search + re-ranking achieves ~85%. Adding query transformation and multi-hop retrieval reaches 90%+. The biggest ROI improvement for most teams is **adding re-ranking** — it costs ~$0.01 per query and meaningfully improves answer quality. [Source: aidev.fit]

### 5.2 Hybrid Search: Dense + Sparse

Pure vector search misses exact keyword matches. Pure keyword search misses semantic queries. **Hybrid search** — combining dense vector retrieval with sparse BM25 scoring — consistently outperforms either approach alone. [Source: towardsdatascience.com]

**Reciprocal Rank Fusion (RRF)** is the standard way to merge ranked lists from both retrievers. It merges ranked lists by summing the reciprocal ranks — a robust merging strategy that doesn't require tuning the relative weight between the two signals. [Source: brixnex.netlify.app]

```python
def reciprocal_rank_fusion(
    dense_results: list[tuple[str, float]],
    sparse_results: list[tuple[str, float]],
    k: int = 60,
) -> list[tuple[str, float]]:
    """Merge two ranked lists using Reciprocal Rank Fusion."""
    scores: dict[str, float] = {}
    
    for rank, (doc_id, _) in enumerate(dense_results):
        scores[doc_id] = scores.get(doc_id, 0) + 1.0 / (rank + k)
    
    for rank, (doc_id, _) in enumerate(sparse_results):
        scores[doc_id] = scores.get(doc_id, 0) + 1.0 / (rank + k)
    
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)
```

### 5.3 Chunking Strategies

Chunking strategy is the most consequential and least attended decision in RAG pipeline design. [Source: aidev.fit, arxiv.org]

| Strategy | Best For | Pros | Cons | Precision | Recall | F1 |
|----------|----------|------|------|-----------|--------|-----|
| **Fixed-size (512 tokens)** | General documents | Simple, predictable | Splits sentences mid-thought | 0.65 | 0.58 | 0.61 |
| **Sentence-aware** | Articles, docs | Semantically meaningful | Uneven chunk sizes | — | — | — |
| **Semantic chunking** | Thematic content | Groups by meaning | Requires embedding pass | 0.78 | 0.72 | 0.75 |
| **Hierarchical** | Multi-level docs | Multiple granularities | More complex | 0.82 | 0.79 | 0.80 |
| **Parent-context** | High-precision needs | Rich context for LLM | Higher storage cost | 0.88 | 0.85 | 0.86 |

**Optimal chunk size in 2026:** 256-512 tokens with 10-20% overlap. Larger chunks (1,024+) improve context but dilute retrieval precision. [Source: aidev.fit]

**Adaptive Chunking** (arXiv:2603.25333) proposes selecting the most suitable chunking method per document rather than using a single strategy: [Source: arxiv.org]

1. LLM-guided regex splitter
2. Split-then-merge recursive splitter
3. Post-processing to enforce size constraints
4. Evaluation pipeline measuring both retriever quality and downstream RAG performance

### 5.4 Re-ranking

Re-ranking involves reordering the most relevant items at the top using a more sophisticated (but slower) model than the initial retrieval. [Source: medium.com]

**Why it matters:** Vector search compresses information into a single vector, losing information in the process. The top few vector search results may miss relevant information that falls below the top-k cutoff. A cross-encoder reranker (like Cohere Rerank, BGE-Reranker, or snowflake-arcticembed-l-v2.0) computes relevance scores between the query and each candidate, dramatically improving recall. [Source: arxiv.org, Dify]

**Production pattern:** Retrieve 20-50 chunks via hybrid search, then use a cross-encoder to score relevance and keep the top 3-5. Adds ~100ms latency but dramatically improves precision. [Source: aidev.fit]

### 5.5 GraphRAG: Knowledge Graph-Enhanced RAG

**GraphRAG** (Microsoft Research, arXiv:2404.16130) combines text extraction, network analysis, and LLM prompting into a single end-to-end system: [Source: microsoft.github.io]

**Process:**
1. Extract entities and relationships from raw text
2. Build a knowledge graph
3. Detect communities using the Leiden algorithm
4. Generate summaries for each community
5. Use graph structure to retrieve context

**Results:** Substantial improvement over naive RAG for complex, multi-hop questions that require understanding relationships between entities. On the internal manufacturing dataset, GraphRAG achieved: [Source: mdpi.com]

| Metric | Naive RAG | GraphRAG | Improvement |
|--------|-----------|----------|-------------|
| Recall@10 | 0.055 | 0.075 | +36% |
| Context Precision | 0.485 | 0.498 | +3% |
| Context Recall | 0.729 | 0.794 | +9% |

**When to use GraphRAG vs. naive RAG:**
- **Use GraphRAG** when questions require understanding relationships, multi-hop reasoning, or thematic analysis across a corpus
- **Use naive RAG** when questions are factoid and can be answered by retrieving similar passages

### 5.6 Query Transformation

Rewrite user queries before retrieval: [Source: aidev.fit]

- **Decompose** complex questions into sub-questions
- **Expand** queries with related terms and synonyms
- **Generate hypothetical answers** to use as search queries (HyDE pattern)

### 5.7 Embedding Model Selection

| Model | Dimensions | MTEB Score | Cost (per 1M tokens) | Best For |
|-------|-----------|------------|---------------------|----------|
| OpenAI text-embedding-3-large | 256-3,072 (Matryoshka) | 64.6 | $0.13 | General purpose, best quality |
| Cohere Embed v4 | 1,024 | 65.2 | $0.10 | Multilingual, long documents |
| BGE-M3 (BAAI) | 1,024 | 63.8 | Free (OSS) | Self-hosted, multilingual, dense+sparse hybrid |

### 5.8 Multi-Hop Retrieval

For complex questions like "What was the revenue impact of the pricing change announced in Q3?", first retrieval finds context about the pricing change, then second retrieval uses that context to find revenue impact information. [Source: aidev.fit]

### 5.9 Recommendations for AgentHarness

The AgentHarness RAG pipeline (`ah/rag/pipeline.py`, `ah/tools/rag.py`) already implements hybrid search (BM25 + dense + RRF fusion) and multi-document indexing. Specific improvements:

| Recommendation | Priority | Effort | Impact |
|----------------|----------|--------|--------|
| **Add cross-encoder re-ranking** — After hybrid retrieval, re-rank candidates with a cross-encoder (Cohere Rerank, BGE-Reranker) | High | Medium | ~15% accuracy improvement |
| **Implement adaptive chunking** — Select chunking strategy per document type rather than fixed-size for all | High | Medium | Better retrieval quality for mixed document types |
| **Add parent-context chunking** — Store small chunks for retrieval but include parent context during generation | Medium | Medium | Better precision with rich context |
| **Implement query transformation** — Decompose complex questions, expand with related terms, generate hypothetical answers | Medium | Medium | Handles complex multi-hop queries |
| **Add GraphRAG for entity-aware retrieval** — Extract entities and relationships, build community hierarchy, use graph structure for retrieval | High | High | Enables multi-hop reasoning |
| **Add multi-hop retrieval** — First retrieval finds context, second retrieval uses that context to find more specific information | Medium | Medium | Handles complex questions |
| **Add RAG evaluation pipeline** — Measure context precision, context recall, and answer quality with RAGAS or custom metrics | Medium | Medium | Enables data-driven RAG tuning |
| **Optimize embedding model selection** — Benchmark OpenAI, Cohere, and BGE-M3 on your specific query distribution | Low | Low | Cost and quality optimization |

---

## 6. Cross-Cutting Recommendations

### 6.1 Priority-Ordered Roadmap

| Phase | Timeline | Focus Items | Expected Impact |
|-------|----------|-------------|-----------------|
| **Phase 1: Foundation** | 1-2 weeks | Circuit breaker, per-call caps, handoff caps, observation masking | 30-50% cost reduction, prevents runaway loops |
| **Phase 2: Quality** | 2-4 weeks | Cross-encoder re-ranking, adaptive chunking, query transformation | 10-15% accuracy improvement |
| **Phase 3: Intelligence** | 4-8 weeks | Dynamic memory linking, memory evolution, orchestrator-worker pattern | Multi-hop reasoning, complex workflow support |
| **Phase 4: Scale** | 8-12 weeks | GraphRAG, multi-hop retrieval, fleet-level budgets, model tiering | Enterprise-scale deployment readiness |

### 6.2 Key Metrics to Track

| Metric | Target | Current State |
|--------|--------|---------------|
| Token cost per task | < $0.50 for routine tasks | Not measured |
| Context compression ratio | > 50% for long sessions | `RollingCompaction` + `ContextCompressor` shipped (truncate/LLM summarize) |
| Memory retrieval accuracy (LoCoMo F1) | > 3.0 | Not measured |
| RAG answer accuracy | > 85% | ~70% (baseline hybrid) |
| Multi-hop reasoning accuracy | > 60% | Not measured |
| Agent loop termination rate | > 99% clean stops | Not measured |
| Per-agent budget adherence | > 95% within budget | Enforced via `llm_usage` reservations and `UsageBudgetExceededError` |

### 6.3 Anti-Patterns to Avoid

1. **Over-orchestrating simple agents** — Start with one agent; add roles only when measurements show the single-agent design is the bottleneck
2. **Full conversation replay** — Never send all previous messages with each LLM call; use memory extraction and retrieval instead
3. **Single chunking strategy** — Different document types need different chunking approaches
4. **Advisory-only budgets** — Budgets without enforcement are suggestions; agents can and will exceed them
5. **No re-ranking** — The biggest ROI improvement in RAG is adding re-ranking; skipping it leaves 15% accuracy on the table
6. **Ignoring the "lost in the middle" effect** — Even with huge context windows, LLMs pay less attention to middle content; feed focused, relevant chunks rather than large blocks

---

## 7. References

### Academic Papers
1. **MemGPT: Towards LLMs as Operating Systems** — arXiv:2310.08560 (2023)
2. **A-MEM: Agentic Memory for LLM Agents** — arXiv:2502.12110, NeurIPS 2025
3. **Selective Context** — arXiv:2310.06201 (2023)
4. **Acon: Optimizing Context Compression for Long-horizon Agents** — arXiv:2510.00615 (2025)
5. **GraphRAG: From Local to Global** — arXiv:2404.16130, Microsoft Research (2024)
6. **Retrieval-Augmented Generation with Graphs (GraphRAG Survey)** — arXiv:2501.00309 (2025)
7. **Pretraining Context Compressor for LLMs** — ACL 2025
8. **LaCache: Ladder-shaped KV Caching** — ICML 2025
9. **Adaptive Chunking for RAG** — arXiv:2603.25333 (2026)
10. **Knowledge Graph-Guided RAG (KG2RAG)** — NAACL 2025

### Production Systems and Frameworks
- **Letta (formerly MemGPT)** — letta.com, github.com/letta-ai/letta
- **Mem0** — mem0.ai, github.com/mem0ai/mem0
- **LangGraph** — github.com/langchain-ai/langgraph
- **CrewAI** — github.com/crewAIInc/crewAI
- **AutoGen / AG2** — github.com/microsoft/autogen
- **OpenAI Agents SDK** — platform.openai.com/docs/agents
- **Microsoft GraphRAG** — github.com/microsoft/graphrag
- **Google ADK** — Agent Development Kit

### Industry Reports and Production Incident Analyses
- "Agent Cost Runaway Detection" — getreadyforagents.com (2026)
- "63 Production Incidents, 21 Frameworks" — agoradigest.com (2026)
- "AI Agent Cost Governance" — conceptualise.de (2026)
- "Designing an AI Agent Cost Governance System" — letsbuildsolutions.com (2026)
- "Multi-Agent Orchestration Playbook" — seodatapulse.com (2026)
- "LangGraph vs CrewAI & AutoGen — 2026 Comparison" — humaineeti.ai (2026)

---

## Appendix: AgentHarness Codebase Mapping

| Research Area | Current Implementation | File Location | Gap |
|---------------|----------------------|---------------|-----|
| Token budgeting | UsageStore with advisory locks | `ah/core/usage.py` | Optional; enforced via `usage_*` budgets (no circuit breaker) |
| Multi-agent | Basic delegate tool | `ah/tools/agents.py` | No orchestrator-worker, no model tiering |
| Memory decay | Ebbinghaus model | `ah/memory/forgetting.py` | No dynamic linking, no memory evolution |
| Memory retrieval | Hybrid dense+sparse | `ah/memory/retriever.py` | No parent-context, no dedup |
| Memory importance | Multi-factor scoring | `ah/memory/scorer.py` | Solid, research-backed |
| Memory consolidation | LLM extraction pipeline | `ah/memory/consolidator.py` | No sleep-time compute |
| Context management | LRU cache, batch insert, archive | `ah/core/context.py` | Compression exists; no observation masking |
| RAG pipeline | Hybrid BM25+dense+RRF, reranker interface | `ah/rag/pipeline.py` | No adaptive chunking |
| RAG tools | index/search documents | `ah/tools/rag.py` | No query transformation, no GraphRAG |

---

*Report compiled: 2026-10-04. Next review: 2027-01-04.*

