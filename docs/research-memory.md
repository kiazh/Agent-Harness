# AgentHarness Memory System — Research & Design

> **Status:** Research document. The `ah/memory/` package is currently an empty stub.
> This document surveys production memory systems for AI agents and proposes a
> concrete architecture for AgentHarness grounded in the existing codebase.

---

## 1. Why Memory Matters

AgentHarness already has the skeleton of a memory system:

- `ContextChunk` (in `ah/core/models.py`) stores typed records with embeddings,
  token counts, and access timestamps.
- `ContextManager` (in `ah/core/context.py`) provides CRUD, batch insert, and
  pgvector similarity search.
- `PromptAssembler` (in `ah/core/assembler.py`) assembles prompts within a
  token budget, with a `retrieved_chunks` parameter that is currently always
  passed as `[]`.

What is missing is the **memory lifecycle**: what to store, when to consolidate,
how to score importance, how to forget, and how to retrieve across sessions.
Without these, the system is a passive log — not a memory.

---

## 2. Short-Term vs Long-Term Memory

### 2.1 Short-Term Memory (Working Memory)

**Definition:** The content currently inside the LLM's context window during a
single inference call. In AgentHarness, this is the `messages` list built by
`PromptAssembler.assemble()` and passed to `provider.complete()`.

**Characteristics:**

| Property | Value |
|---|---|
| Latency | 0 ms (already in prompt) |
| Duration | Single session |
| Capacity | Bounded by `context_budget` (default 8000 tokens) |
| Retrieval | Automatic — everything in window is visible |
| Failure mode | Context bloat, truncation, "lost in the middle" |

**Current state in AgentHarness:**

- `PromptAssembler` already enforces a token budget and compresses chunks.
- `ReActAgent` stores every user message, tool call, and assistant response as
  a `ContextChunk` — but only retrieves the last 5 via `get_recent_context()`.
- There is no summarization or sliding-window compression; old chunks are
  simply never re-fetched.

**Production patterns:**

1. **Sliding window with summarization.** Keep the last N turns verbatim;
   compress older turns into a running summary. Reduces token cost by 40–60%
   without meaningful quality loss.
2. **Priority-based retention.** System-critical information (user preferences,
   active task state) is never compressed; chit-chat and redundant
   confirmations are dropped first.
3. **Token budget allocation.** Reserve fixed portions: e.g., 50% system +
   tools, 30% conversation history, 20% retrieved long-term memory.

### 2.2 Long-Term Memory (Persistent Memory)

**Definition:** Information that survives session boundaries. Stored outside
the context window and retrieved on demand.

**Characteristics:**

| Property | Value |
|---|---|
| Latency | 5–100 ms (retrieval) |
| Duration | Cross-session (weeks to indefinite) |
| Capacity | Unlimited (with retrieval limits) |
| Retrieval | Query-based — structured lookup + vector similarity |
| Failure mode | Retrieval precision failure, stale memories, unbounded growth |

**Current state in AgentHarness:**

- `ContextChunk` already has `embedding vector(1536)` and pgvector HNSW index.
- `ContextManager.search_by_embedding()` exists but is never called from the
  agent loop — `retrieved_chunks=[]` is hardcoded in `ReActAgent.run()`.
- There is no cross-session retrieval: `search_by_embedding()` filters by
  `session_id`, so memories from previous sessions are invisible.

**Production patterns:**

1. **Structured memory entries.** Store key-value pairs with metadata, not raw
   chat dumps. Each entry should have: `user_id`, `category` (preference,
   fact, decision, event), `importance` score, `created_at`, `accessed_at`.
2. **Vector search for semantic retrieval.** Embed the query, find top-k
   similar memories, inject into context.
3. **Hybrid retrieval.** Combine dense vector search with sparse BM25 keyword
   search and metadata filters (date range, category, user_id).
4. **Selective injection.** Never flood context — retrieve only what is
   relevant to the current task.

### 2.3 The Handoff Between Layers

The critical design decision is **what moves from short-term to long-term**.
A production system needs an explicit write path:

```
Observation → Extraction → Dedup → Importance Scoring → Write to Long-Term
```

Without this, long-term memory is either empty (nothing written) or a junkyard
(everything written).

---

## 3. Memory Consolidation

### 3.1 What Is Consolidation?

Consolidation is the process of transforming raw episodic traces (conversation
turns, tool calls, observations) into durable semantic memories (facts,
preferences, decisions). It is the agent equivalent of sleep-phase memory
consolidation in biological systems.

### 3.2 When to Consolidate

Three common triggers:

| Trigger | Description | Cost |
|---|---|---|
| **Synchronous** | After every turn, extract and store memories immediately | High latency per turn |
| **Asynchronous batch** | Queue observations; run consolidation every N turns or K seconds | Decoupled from inference |
| **End-of-session** | When a session ends, consolidate the entire conversation | One-time cost, but risks data loss if session crashes |

**Recommendation for AgentHarness:** Start with asynchronous batch
consolidation (every 20 turns or on session archive). This keeps the hot path
fast while ensuring memories are persisted.

### 3.3 What to Extract

Not everything should be consolidated. A well-designed extraction step filters
on two dimensions:

- **Novelty:** Is this meaningfully different from what we already know?
- **Utility:** Is this likely to be useful in future tasks?

**Extract:**
- User preferences ("user prefers concise responses")
- Decisions ("chose PostgreSQL over MongoDB for the project")
- Facts ("user's account ID is 4782, subscription tier: Pro")
- Task outcomes ("deployment failed due to port conflict")

**Do NOT extract:**
- Small talk and greetings
- Redundant confirmations ("ok", "thanks")
- Ephemeral state (weather readings, stock prices) unless the task is
  explicitly about historical time-series data

### 3.4 Deduplication and Conflict Resolution

Before writing a new memory, check for existing memories with similar
embeddings. If a match is found:

- **Update** the existing memory if the new information is newer or more
  specific.
- **Merge** if both contain unique information.
- **Discard** if the new memory is redundant.

Conflict resolution strategies:
- **Last-write-wins:** Simplest; works for most preference updates.
- **Confidence-weighted:** Each memory has a confidence score; higher
  confidence wins.
- **LLM-arbitrated:** Ask the LLM to resolve contradictions (expensive but
  most accurate).

### 3.5 Implementation Sketch for AgentHarness

```python
class MemoryConsolidator:
    """Consolidates raw context chunks into durable long-term memories."""

    def __init__(
        self,
        llm_provider: LLMProvider,
        importance_scorer: ImportanceScorer,
        dedup_threshold: float = 0.85,
    ):
        self.llm = llm_provider
        self.scorer = importance_scorer
        self.dedup_threshold = dedup_threshold

    async def consolidate_session(
        self,
        session_id: uuid.UUID,
        agent_id: str,
    ) -> list[MemoryEntry]:
        """Consolidate a session's context chunks into long-term memories."""
        chunks = await context_manager.get_chunks(session_id, limit=100)
        conversation = self._format_conversation(chunks)

        # Step 1: Extract candidate memories via LLM
        candidates = await self._extract_memories(conversation)

        # Step 2: Score importance
        for candidate in candidates:
            candidate.importance = self.scorer.score(candidate)

        # Step 3: Deduplicate against existing memories
        new_memories = []
        for candidate in candidates:
            existing = await self._find_similar(candidate)
            if existing and self._similarity(candidate, existing) > self.dedup_threshold:
                await self._merge_or_update(existing, candidate)
            else:
                new_memories.append(candidate)

        # Step 4: Write new memories
        for memory in new_memories:
            await self._write_memory(memory, agent_id)

        return new_memories
```

---

## 4. Forgetting Curves

### 4.1 The Ebbinghaus Forgetting Curve

Human memory retention follows exponential decay: without reinforcement,
memories weaken rapidly in the first hours/days, then stabilize at a lower
level. The classic formula:

```
R(t) = e^(-t/S)
```

Where `R` is retention, `t` is time, and `S` is memory strength.

### 4.2 Applying Decay to Agent Memory

Each memory has a **retrieval strength** that decays over time but is
reinforced on access:

```
strength(t) = base_strength × e^(-λ × Δt) + access_boost
```

- `λ` (decay rate): Controls how fast memories fade. A half-life of 14 days
  is a reasonable default: `λ = ln(2) / 14 ≈ 0.05`.
- `access_boost`: Each time a memory is retrieved, its strength is boosted
  (simulating spaced repetition).

### 4.3 Importance-Modulated Decay

Not all memories should decay at the same rate. High-importance memories
(user preferences, critical decisions) should decay slowly or not at all.
Low-importance memories (one-off events, transient state) should decay
quickly.

```
effective_decay = base_decay × (1 - importance)
```

A memory with `importance = 0.9` decays at 10% of the base rate.
A memory with `importance = 0.1` decays at 90% of the base rate.

### 4.4 Eviction Policies

When memory storage reaches capacity:

| Policy | Description | Best For |
|---|---|---|
| **LRU** | Evict least recently accessed | General purpose |
| **LFU** | Evict least frequently accessed | Stable knowledge |
| **Importance-based** | Evict lowest importance score | Production agents |
| **TTL** | Evict after a time window | Ephemeral data |

**Recommendation:** Use importance-based eviction as the primary policy,
with TTL as a safety net for low-importance memories.

### 4.5 Implementation Sketch

```python
class ForgettingModel:
    """Ebbinghaus-inspired decay with importance modulation."""

    def __init__(self, half_life_days: float = 14.0):
        self.base_lambda = 0.693 / half_life_days  # ln(2) / half_life

    def current_strength(self, memory: MemoryEntry) -> float:
        """Compute current retrieval strength of a memory."""
        days_since_access = (datetime.utcnow() - memory.accessed_at).days
        importance_factor = 1.0 - memory.importance
        effective_lambda = self.base_lambda * importance_factor
        decay = math.exp(-effective_lambda * days_since_access)
        return memory.base_strength * decay + memory.access_count * 0.1

    def should_forget(self, memory: MemoryEntry, threshold: float = 0.05) -> bool:
        """Decide whether a memory should be evicted."""
        return self.current_strength(memory) < threshold
```

---

## 5. Importance Scoring

### 5.1 Why Importance Matters

Importance scoring determines:
- **What gets written** to long-term memory (write path)
- **What gets evicted** when storage is full (forgetting)
- **What gets retrieved** first (read path ranking)

### 5.2 Multi-Factor Importance Model

Research (arXiv 2606.12945, "Learning What to Remember") shows that a
multi-factor value model outperforms single-factor (recency or similarity)
policies. The proposed value function:

```
V(m) = Σ wᵢ × fᵢ(m)
```

Where `fᵢ` are interpretable factors and `wᵢ` are learned weights.

**Candidate factors:**

| Factor | Description | Weight (learned) |
|---|---|---|
| **Reliability** | Is this from a trusted source? | 0.64 |
| **Emotional intensity** | Does this carry emotional weight? | 0.55 |
| **Self/user relevance** | Is this about the user or their goals? | 0.23 |
| **Goal relevance** | Is this relevant to the current task? | 0.00 (blind) |
| **Recency** | How recently was this created/accessed? | varies |
| **Frequency** | How often has this been accessed? | varies |
| **Task utility** | Does this help complete the current task? | varies |

**Key insight from the research:** Goal relevance is correctly down-weighted
for the *forgetting* decision (because the future query is unknown at
consolidation time), but is the dominant factor for *retrieval* (where the
query is known).

### 5.3 Practical Scoring for AgentHarness

For the initial implementation, use a simpler heuristic model that can be
learned later:

```python
class ImportanceScorer:
    """Heuristic importance scoring for memory entries."""

    def score(self, memory: MemoryEntry) -> float:
        """Return importance score in [0, 1]."""
        scores = []

        # Category-based base importance
        category_weights = {
            "preference": 0.9,
            "decision": 0.8,
            "fact": 0.7,
            "event": 0.5,
            "transient": 0.2,
        }
        scores.append(category_weights.get(memory.category, 0.5))

        # User explicit importance (if user said "remember this")
        if memory.explicitly_important:
            scores.append(1.0)

        # Recency boost (decays over 30 days)
        days_old = (datetime.utcnow() - memory.created_at).days
        recency_score = max(0, 1.0 - days_old / 30.0)
        scores.append(recency_score * 0.3)

        # Access frequency boost
        freq_score = min(1.0, memory.access_count / 10.0)
        scores.append(freq_score * 0.2)

        return min(1.0, max(0.0, sum(scores) / len(scores)))
```

### 5.4 Learning the Weights

The weights can be learned from downstream task performance:

1. Log all memory decisions (what was stored, what was retrieved, what was
   forgotten).
2. Measure task success (did the agent produce the correct answer?).
3. Use a gradient-free optimizer (e.g., Bayesian optimization) to find
   weights that maximize task success.

This is a future enhancement — start with hand-tuned weights.

---

## 6. Memory Retrieval

### 6.1 The Retrieval Problem

Given a user query, find the most relevant memories from long-term storage
and inject them into the context window. The challenge is doing this
precisely (no irrelevant memories) and efficiently (low latency).

### 6.2 Retrieval Strategies

| Strategy | Description | Best For |
|---|---|---|
| **Dense vector search** | Embed query, find nearest neighbors | Semantic similarity |
| **Sparse keyword search** | BM25 / TF-IDF on memory content | Exact matches, IDs |
| **Hybrid search** | Combine dense + sparse | Production systems |
| **Metadata filter** | Filter by date, category, user | Structured queries |
| **Graph traversal** | Walk a knowledge graph from seed nodes | Multi-hop reasoning |

### 6.3 Hybrid Retrieval Pipeline

```
Query
  │
  ├──→ Dense embedding → Vector search → Top-K candidates
  │
  ├──→ Sparse keywords → BM25 search → Top-K candidates
  │
  └──→ Metadata filter → Filter by user_id, date, category
         │
         ▼
   Merge & deduplicate
         │
         ▼
   Re-rank (cross-encoder or LLM)
         │
         ▼
   Inject top-N into context
```

### 6.4 Query Formulation

The agent's immediate input is often a poor retrieval query. A user asking
"Why did that crash?" needs the agent to retrieve the crash log from two
sessions ago, not the most semantically similar sentence.

**Strategies:**
- **LLM-reformulated queries:** Ask the LLM to generate a better retrieval
  query from the user's input.
- **Multi-query fan-out:** Generate multiple retrieval queries and merge
  results.
- **Subgoal-based retrieval:** Use the current task subgoal as an
  additional retrieval signal.

### 6.5 Re-ranking

After initial retrieval, pass candidates through a cross-encoder or a
small LLM to score relevance more precisely. This is especially important
when the candidate set is large (>20).

### 6.6 Implementation Sketch for AgentHarness

```python
class MemoryRetriever:
    """Hybrid retrieval for long-term memory."""

    def __init__(
        self,
        embedding_model: EmbeddingModel,
        top_k: int = 5,
        rerank: bool = True,
    ):
        self.embedding_model = embedding_model
        self.top_k = top_k
        self.rerank = rerank

    async def retrieve(
        self,
        query: str,
        user_id: str,
        category: str | None = None,
        date_range: tuple[datetime, datetime] | None = None,
    ) -> list[RetrievedMemory]:
        """Retrieve relevant memories for a query."""
        # Step 1: Dense vector search
        query_embedding = await self.embedding_model.embed(query)
        dense_results = await self._vector_search(
            query_embedding, user_id, category, date_range
        )

        # Step 2: Sparse keyword search
        sparse_results = await self._keyword_search(
            query, user_id, category, date_range
        )

        # Step 3: Merge and deduplicate
        candidates = self._merge_results(dense_results, sparse_results)

        # Step 4: Re-rank
        if self.rerank:
            candidates = await self._rerank(query, candidates)

        return candidates[: self.top_k]
```

### 6.7 Integration with AgentHarness

The retrieval should be integrated into `ReActAgent.run()`:

```python
# In ReActAgent.run(), replace:
retrieved_chunks=[],

# With:
retrieved_memories = await memory_retriever.retrieve(
    query=user_message,
    user_id=session.agent_id,  # or a real user_id
)
retrieved_chunks = [
    (ContextChunk(
        id=m.id,
        session_id=session_id,
        agent_id=self.agent_id,
        chunk_type="memory",
        payload={"content": m.content, "importance": m.importance},
        token_count=len(m.content) // 4,
    ), m.score)
    for m in retrieved_memories
]
```

---

## 7. Proposed Architecture for AgentHarness

### 7.1 Memory Layers

```
┌─────────────────────────────────────────────────────┐
│                  LLM Context Window                  │
│  (Short-term memory — current session, 8K tokens)   │
└──────────────────────┬──────────────────────────────┘
                       │ retrieve
┌──────────────────────▼──────────────────────────────┐
│              Long-Term Memory Store                  │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────┐ │
│  │  Semantic   │  │   Episodic   │  │ Procedural │ │
│  │  (facts,    │  │  (past       │  │ (tool      │ │
│  │  policies)  │  │  sessions)   │  │  registry) │ │
│  └─────────────┘  └──────────────┘  └────────────┘ │
│  ┌──────────────────────────────────────────────┐   │
│  │         Importance Scorer + Forgetting        │   │
│  └──────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘
                       ▲
                       │ consolidate
┌──────────────────────┴──────────────────────────────┐
│              Write Path (Extraction)                 │
│  Raw chunks → Extract → Dedup → Score → Write       │
└─────────────────────────────────────────────────────┘
```

### 7.2 New Components

| Component | Location | Responsibility |
|---|---|---|
| `MemoryEntry` | `ah/core/models.py` | New dataclass for long-term memories |
| `MemoryStore` | `ah/memory/store.py` | CRUD for long-term memories (new table) |
| `MemoryConsolidator` | `ah/memory/consolidator.py` | Extract, dedup, score, write |
| `ImportanceScorer` | `ah/memory/scorer.py` | Multi-factor importance scoring |
| `ForgettingModel` | `ah/memory/forgetting.py` | Decay and eviction |
| `MemoryRetriever` | `ah/memory/retriever.py` | Hybrid retrieval pipeline |
| `MemoryManager` | `ah/memory/__init__.py` | Facade coordinating all components |

### 7.3 Schema Changes

New table for long-term memories:

```sql
CREATE TABLE IF NOT EXISTS memories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN (
        'preference', 'decision', 'fact', 'event', 'transient'
    )),
    content TEXT NOT NULL,
    importance FLOAT NOT NULL DEFAULT 0.5,
    base_strength FLOAT NOT NULL DEFAULT 1.0,
    access_count INT NOT NULL DEFAULT 0,
    embedding vector(1536),
    source_session_id UUID REFERENCES sessions(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    accessed_at TIMESTAMPTZ
);

CREATE INDEX idx_memories_user_category ON memories(user_id, category);
CREATE INDEX idx_memories_embedding ON memories
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX idx_memories_importance ON memories(importance DESC);
```

### 7.4 Integration Points

1. **`ReActAgent.run()`:** Call `MemoryRetriever.retrieve()` before prompt
   assembly; pass results as `retrieved_chunks`.
2. **`ReActAgent` (post-run):** Call `MemoryConsolidator.consolidate_session()`
   asynchronously after the agent loop completes.
3. **`SessionManager.archive()`:** Trigger consolidation before marking
   session as archived.
4. **Background task:** Periodic forgetting pass (evict low-strength memories).

---

## 8. Production Lessons

### 8.1 Anti-Patterns to Avoid

| Anti-Pattern | Why It Fails | Fix |
|---|---|---|
| **Context window maximalism** | Filling the full context window causes cost explosion and "lost in the middle" degradation | Use retrieval, not stuffing |
| **No decay policy** | Storing every interaction without decay leads to context drift | Implement Ebbinghaus decay from day one |
| **Single embedding model** | General-purpose embeddings fail on domain-specific content | Fine-tune or use domain-specific models |
| **No namespace isolation** | Sharing one vector index across users creates data leakage | Always partition by `user_id` |
| **Storing raw chat dumps** | Unstructured data degrades retrieval precision | Extract structured facts before storing |
| **No deduplication** | Redundant memories dilute retrieval results | Always dedup before writing |

### 8.2 Key Metrics to Track

| Metric | Target | Why |
|---|---|---|
| **Retrieval precision@5** | > 0.8 | Are we retrieving the right memories? |
| **Retrieval latency (p99)** | < 100 ms | Is retrieval fast enough? |
| **Memory growth rate** | < 100 memories/day/user | Is the store growing unboundedly? |
| **Forgetting accuracy** | > 0.9 | Are we forgetting the right things? |
| **Token cost per turn** | < 4000 tokens | Is the context window being used efficiently? |

### 8.3 Existing Solutions

| Solution | Strength | Best For |
|---|---|---|
| **Mem0** | Extraction + vector + KV hybrid | Personalization agents |
| **Zep** | Temporal knowledge graph | Conversational agents, customer support |
| **Letta (MemGPT)** | OS-inspired paging hierarchy | Long-horizon tasks, research agents |
| **LangChain Memory** | Modular, multiple memory types | LangChain-based agents |

---

## 9. Implementation Roadmap

### Phase 1: Foundation (Week 1)
- [ ] Add `MemoryEntry` dataclass to `ah/core/models.py`
- [ ] Create `memories` table in `ah/db/schema.sql`
- [ ] Implement `MemoryStore` with CRUD operations
- [ ] Write tests for `MemoryStore`

### Phase 2: Write Path (Week 2)
- [ ] Implement `ImportanceScorer` with heuristic weights
- [ ] Implement `MemoryConsolidator` with LLM-based extraction
- [ ] Add deduplication logic
- [ ] Integrate consolidation into `ReActAgent` (async, post-run)
- [ ] Write tests for consolidation pipeline

### Phase 3: Read Path (Week 3)
- [ ] Implement `MemoryRetriever` with dense vector search
- [ ] Add metadata filtering (user_id, category, date range)
- [ ] Integrate retrieval into `ReActAgent.run()`
- [ ] Write tests for retrieval pipeline

### Phase 4: Forgetting (Week 4)
- [ ] Implement `ForgettingModel` with Ebbinghaus decay
- [ ] Add importance-modulated decay
- [ ] Implement eviction policies
- [ ] Add background forgetting task
- [ ] Write tests for forgetting

### Phase 5: Hardening (Week 5)
- [ ] Add hybrid retrieval (dense + sparse)
- [ ] Implement re-ranking
- [ ] Add metrics and logging
- [ ] Load testing and tuning
- [ ] Documentation

---

## 10. References

1. **Generative Agents** (Park et al., 2023) — Reflection-based consolidation,
   importance scoring, multi-signal retrieval. arXiv:2304.03442.
2. **MemGPT / Letta** — Virtual context management, agent-managed memory paging.
3. **MemoryBank** (AAAI 2024) — Ebbinghaus forgetting curve applied to chatbot memory.
4. **Learning What to Remember** (arXiv 2606.12945) — Multi-factor memory value
   model with learned weights.
5. **Memory for Autonomous LLM Agents** (arXiv 2603.07670) — Comprehensive survey
   of memory mechanisms, evaluation, and frontiers.
6. **SF-AMS** (arXiv 2607.22562) — Strategic forgetting with composite importance
   scoring and hierarchical memory structure.
7. **FSFM** (arXiv 2604.20300) — Biologically-inspired selective forgetting framework.
8. **CraniMem** (arXiv 2603.15642) — Neurocognitively motivated gated memory with
   episodic buffer and knowledge graph consolidation.
9. **FadeMem** (arXiv 2601.18642) — Dual-layer memory with adaptive forgetting.
10. **Agentic Memory** (ACL 2026) — RL-based unified memory management.
