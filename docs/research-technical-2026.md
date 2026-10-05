# Technical Research Report: AgentHarness Codebase Patterns

**Date:** 2026-10-05  
**Scope:** PostgreSQL+pgvector, context serialization, agent loop optimization, LLM provider abstraction, session management

---

## Table of Contents

1. [PostgreSQL + pgvector for LLM Memory](#1-postgresql--pgvector-for-llm-memory)
2. [MessagePack vs Protocol Buffers vs JSON for Context Storage](#2-messagepack-vs-protocol-buffers-vs-json-for-context-storage)
3. [ReAct Agent Loop Optimization](#3-react-agent-loop-optimization)
4. [LLM Provider Abstraction Patterns](#4-llm-provider-abstraction-patterns)
5. [Agent Session Management and Fork Patterns](#5-agent-session-management-and-fork-patterns)
6. [Consolidated Recommendations for AgentHarness](#6-consolidated-recommendations-for-agentharness)

---

## 1. PostgreSQL + pgvector for LLM Memory

### 1.1 Architecture Overview

AgentHarness uses PostgreSQL with pgvector as its primary memory store — embeddings live in the same rows as the data they describe, so a memory query is one SQL statement with ordinary `WHERE` filters instead of a fan-out to a second system plus the pipeline that keeps them in sync.

### 1.2 HNSW vs IVFFlat: The Index Decision

| Property | HNSW | IVFFlat |
|---|---|---|
| Build time | Slow (10–30× slower) | Fast |
| Index size | 2–5× larger | Small |
| Query latency (p99) | 5–20ms @ 1M vectors | 12ms+ @ 1M vectors |
| Recall ceiling | High (0.96+) | Moderate (0.92 default) |
| Build on empty table | Yes (no training step) | No (needs data for k-means) |
| Handles incremental inserts | Good | Degrades, needs re-train |
| Memory during build | High | Low |
| Tunable at query time | `ef_search` (runtime) | `probes` (runtime) |
| Default for most workloads | **Yes** | No |

**Recommendation: Default to HNSW for AgentHarness.** It is the correct choice for read-heavy agent workloads where recall matters and data arrives continuously. IVFFlat is only appropriate for bulk-loaded, mostly-static datasets or when memory is genuinely constrained.

### 1.3 HNSW Tuning Parameters

```sql
-- Production HNSW index for 1536-dimension embeddings (OpenAI Ada-002, text-embedding-3)
CREATE INDEX CONCURRENTLY idx_chunks_emb_hnsw
ON chunks USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 128);

-- Runtime tuning per session or per query
SET LOCAL hnsw.ef_search = 100;  -- 40=default, 100=sweet spot for k=10-30, 200+=recall priority
```

#### Parameter Reference

| Parameter | Default | Production | What it trades |
|---|---|---|---|
| `m` | 16 | 16 (32 for hard recall) | Higher = better recall, larger index, slower build |
| `ef_construction` | 64 | 128–200 | Higher = better graph quality, slower build |
| `ef_search` | 40 | 100 (60–80 live, 200+ rerank) | Higher = better recall, slower query |

**Sizing formula:** Index size ≈ `rows × dimensions × 4 bytes` + `m × 8 × rows` (graph edges). A 5M × 1536-dim index is ~30 GB.

### 1.4 Quantization Strategies for Memory Reduction

#### Scalar Quantization (halfvec)

```sql
-- Store as float32, index as halfvec (2-byte floats)
CREATE INDEX idx_chunks_emb_halfvec
ON chunks USING hnsw ((halfvec(embedding, 1536)) halfvec_cosine_ops)
WITH (m = 16, ef_construction = 128);
```

- **2× storage reduction** with negligible recall loss
- Clear win for dimensions ≥ 512
- Faster index builds (fewer pages)

#### Binary Quantization (bit)

```sql
-- Store as float32, index as bit (1 bit per dimension)
CREATE INDEX idx_chunks_emb_bq
ON chunks USING hnsw ((binary_quantize(embedding)::bit(1536)) bit_hamming_ops)
WITH (m = 16, ef_construction = 128);
```

- **32× storage reduction** (367 GB → 38 GB at 100M × 768-dim)
- Requires reranking step: fetch top-N candidates with Hamming distance, rescore with full vectors
- Works best on distributions with high bit-diversity; validate recall on your data
- Elasticsearch BBQ (with corrective factors) achieves 29× compression with recall@10 of 0.994

#### When to Use Which

| Strategy | Compression | Recall Impact | When |
|---|---|---|---|
| Full float32 | 1× | None | < 10M vectors, memory available |
| halfvec | 2× | Negligible | 10–50M vectors, default choice |
| Binary (BQ) | 32× | 5–15% (recover via rerank) | 50M+ vectors, memory-constrained |

### 1.5 Iterative Scans (pgvector 0.8+)

```sql
SET LOCAL hnsw.iterative_scan = relaxed_order;
SET LOCAL hnsw.max_scan_tuples = 20000;
```

Prevents the "not enough rows returned" problem when combining an ANN index with a restrictive `WHERE` clause. Without this, pgvector fetches top-k from the index first, then filters — potentially returning fewer rows than requested.

**For AgentHarness:** Enable `iterative_scan` on any memory query that filters by `tenant_id`, `agent_id`, or `session_id`.

### 1.6 Production Configuration

```ini
# postgresql.conf — pgvector-tuned
shared_buffers = 8GB                    # 25% of RAM; HNSW index should fit
effective_cache_size = 24GB             # 75% of total RAM
maintenance_work_mem = 8GB              # Critical for HNSW index builds
work_mem = 32MB
max_parallel_maintenance_workers = 7    # Parallel index builds
random_page_cost = 1.1                  # SSD + many in-memory pages
```

### 1.7 Hybrid Search Pattern

```sql
-- Combine vector similarity with full-text search using RRF
SELECT id, content,
       ts_rank(content_tsv, query) AS text_rank,
       1 - (embedding <=> $1) AS vector_score
FROM chunks, plainto_tsquery('english', $2) query
WHERE tenant_id = $3
ORDER BY ts_rank(content_tsv, query) + (1 - (embedding <=> $1)) DESC
LIMIT 10;
```

Hybrid search beats pure semantic search when users expect exact keyword matches. Use `pg_trgm` or `pg_bm25` for BM25 scoring.

### 1.8 Common Pitfalls

| Pitfall | Symptom | Fix |
|---|---|---|
| `maintenance_work_mem=64MB` (default) | HNSW build 10–50× slower, OOM | Set to 8GB+ |
| Wrong operator class | Index ignored, seq scan | Match `<=>` with `vector_cosine_ops`, `<->` with `vector_l2_ops` |
| Filter without iterative scan | Under-filled results | Enable `iterative_scan` (0.8+) |
| IVFFlat on empty table | Useless clusters | Build after data loaded |
| No index rebuild after heavy updates | 3–5× latency degradation | Schedule weekly `REINDEX CONCURRENTLY` |
| Cold cache after deploy | p99 spikes | Pre-warm: `SELECT pg_prewarm('idx_name')` |

### 1.9 Scaling Bands

| Vectors | P50 | P95 | QPS | RAM | Strategy |
|---|---|---|---|---|---|
| 1M | 3ms | 8ms | ~800 | ~6 GB | Default HNSW |
| 10M | 5ms | 18ms | ~500 | ~55 GB | Tuned HNSW + halfvec |
| 50M | 12ms | 40ms | ~280 | ~250 GB | HNSW+BQ + pgvectorscale |
| 100M | 28ms | 95ms | ~150 | ~500 GB | Dedicated vector DB or Citus sharding |

**Single-node limit:** ~50M vectors with HNSW. Beyond this, use Citus for sharding or a dedicated vector database.

### 1.10 Ingest Patterns

```sql
-- Idempotent ingest via content hash
INSERT INTO chunks (document_id, content, content_hash, embedding)
VALUES ($1, $2, md5($2), $3)
ON CONFLICT (content_hash) DO NOTHING;
```

Batch upserts with `ON CONFLICT DO NOTHING` are essential — naive per-row inserts exhaust connections fast. Use PgBouncer in transaction-pooling mode at 1:5 ratio (DB connections to application threads).

---

## 2. MessagePack vs Protocol Buffers vs JSON for Context Storage

### 2.1 Format Comparison

| Property | JSON | MessagePack | Protocol Buffers |
|---|---|---|---|
| Schema | None (self-describing) | None (self-describing) | Required (.proto) |
| Size | Baseline (1×) | ~22–40% smaller | ~3–10× smaller |
| Serialize speed | Baseline | 3–10× faster | 5–20× faster |
| Deserialize speed | Baseline | 2–5× faster | 5–10× faster |
| Human-readable | Yes | No | No |
| Binary data | Base64 (+33% overhead) | Native | Native |
| Cross-language | Universal | Excellent | Excellent |
| Streaming | Line-delimited | Yes | No (length-prefixed) |
| Ecosystem maturity | Universal | Very high | High |

### 2.2 Size Benchmarks

For a typical agent message payload (30 records with mixed types):

| Format | Size | vs JSON |
|---|---|---|
| JSON | 7,951 bytes | 1× |
| MessagePack | 6,363 bytes | 0.78× (22% smaller) |
| Protocol Buffers | ~800–1,500 bytes | ~0.1–0.2× (80–90% smaller) |

### 2.3 Token Efficiency for LLM Context

The choice of serialization format directly impacts token consumption when sending context to LLMs. Research shows:

- **TOON (Token-Optimized Numeric)** format achieves 40–60% token reduction vs JSON for structured data, with LLM interpretation accuracy matching JSON (100%).
- **CSV** is 2–3× more token-efficient than JSON for tabular data.
- **MessagePack** is 22% smaller than JSON but still requires base64 encoding for LLM context, negating some savings.
- **Protobuf** is most compact for storage/transmission but requires deserialization before LLM ingestion.

### 2.4 AgentHarness Recommendation

| Use Case | Format | Rationale |
|---|---|---|
| Inter-agent messages (storage) | **MessagePack** | 22% smaller than JSON, faster parse, no schema migration needed, good cross-language support |
| Inter-agent messages (LLM context) | **JSON** (compressed) | LLMs need human-readable text; use minified JSON or TOON if token budget is critical |
| Database persistence | **JSONB** (PostgreSQL) | Native indexing, querying, GIN indexes on metadata |
| Wire protocol between services | **MessagePack** or **Protobuf** | MessagePack for flexibility, Protobuf if schema is stable |
| Caching layer (Redis) | **MessagePack** | 22% memory savings, faster serialization |

### 2.5 Implementation Pattern

```python
import msgpack
import json

class ContextSerializer:
    """Dual-format serializer for agent context."""
    
    def to_storage(self, messages: list[dict]) -> bytes:
        """Serialize for inter-agent transport — compact binary."""
        return msgpack.packb(messages, use_bin_type=True)
    
    def from_storage(self, data: bytes) -> list[dict]:
        """Deserialize from inter-agent transport."""
        return msgpack.unpackb(data, raw=False)
    
    def to_llm_context(self, messages: list[dict]) -> str:
        """Serialize for LLM consumption — minified JSON."""
        return json.dumps(messages, separators=(',', ':'), ensure_ascii=False)
    
    def to_db(self, messages: list[dict]) -> str:
        """Serialize for PostgreSQL JSONB — standard JSON."""
        return json.dumps(messages, ensure_ascii=False)
```

### 2.6 Common Pitfalls

- **Protobuf schema evolution:** Adding fields is safe; removing fields or changing types requires careful migration. For rapidly-evolving agent message schemas, MessagePack is safer.
- **MessagePack `raw` vs `bin`:** Always use `use_bin_type=True` for correct string/binary distinction.
- **JSON in JSONB:** PostgreSQL JSONB adds ~20% overhead over plain JSON but enables indexing. Use it for metadata fields you query on.

---

## 3. ReAct Agent Loop Optimization

### 3.1 The Canonical Loop Structure

```python
async def agent_loop(
    goal: str,
    max_steps: int = 12,
    deadline_s: float = 60.0,
    max_tokens_total: int = 200_000,
) -> AgentResult:
    messages = [{"role": "user", "content": goal}]
    started = time.monotonic()
    tokens_used = 0
    last_calls: list[tuple] = []  # For no-progress detection
    
    for step in range(max_steps):
        # --- Check budgets BEFORE the call ---
        if time.monotonic() - started > deadline_s:
            raise TimeoutError(f"Deadline exceeded at step {step}")
        if tokens_used > max_tokens_total:
            raise RuntimeError(f"Token budget exhausted at step {step}")
        
        # --- Generate ---
        response = await model.generate(messages, tools)
        tokens_used += response.usage.input_tokens + response.usage.output_tokens
        messages.append({"role": "assistant", "content": response.content})
        
        # --- Natural exit: no tool calls ---
        if response.stop_reason != "tool_use":
            return AgentResult(answer=response.text, steps=step)
        
        # --- Dispatch tool calls ---
        for block in response.tool_calls:
            # No-progress detection
            call_sig = (block.name, json.dumps(block.input, sort_keys=True))
            last_calls.append(call_sig)
            if last_calls[-3:].count(call_sig) >= 3:
                raise RuntimeError(f"No-progress: repeated {block.name} 3×")
            
            try:
                output = await run_tool(block.name, block.input)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(output),
                    "is_error": False,
                })
            except Exception as e:
                # Errors become observations, not exceptions
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": f"error: {type(e).__name__}: {e}",
                    "is_error": True,
                })
        
        messages.append({"role": "user", "content": results})
    
    raise RuntimeError(f"Step cap reached: {max_steps}")
```

### 3.2 Stopping Conditions (Disjunction)

The loop must stop on the **first** of these conditions:

| Condition | Implementation | Typical Value |
|---|---|---|
| `stop_reason != "tool_use"` | Natural exit | — |
| Step cap | `for step in range(max_steps)` | 12–20 (not 5–50) |
| Wall-clock deadline | `time.monotonic()` check | 60s |
| Token budget | Sum of input+output tokens | 200K |
| No-progress detector | Hash of last 3 `(tool, args)` tuples | 3 identical calls |
| Goal predicate | `hasToolCall("submit_final_answer")` | — |

**Critical insight:** `generate calls = tool rounds + 1`. The +1 is the final round where the model has everything it needs and just answers. This is always there for an agent that finishes normally.

### 3.3 Retry Patterns for LLM Calls

#### Exponential Backoff with Full Jitter

```python
import random

class RetryBudget:
    """Per-task retry budget with token cap."""
    
    def __init__(self, max_attempts: int = 4, max_tokens: int = 500_000):
        self.max_attempts = max_attempts
        self.max_tokens = max_tokens
        self.tokens_consumed = 0
    
    def compute_delay(self, attempt: int) -> float:
        """Full jitter: random.uniform(0, cap)."""
        cap = min(2 ** attempt, 128.0)
        return random.uniform(0, cap)
    
    def should_retry(self, error: Exception, attempt: int) -> bool:
        if attempt >= self.max_attempts:
            return False
        if self.tokens_consumed > self.max_tokens:
            return False
        # Only retry transient errors
        return isinstance(error, (ConnectionError, TimeoutError, RateLimitError, ServiceUnavailableError))
```

#### Error Classification

| Error Type | Examples | Action |
|---|---|---|
| **Transient** | 5xx, timeout, connection reset, rate limit (429) | Retry with backoff |
| **Fatal** | 4xx (except 429), auth failure, invalid config | Fail immediately |
| **Recoverable** | Tool returned unexpected format | Feed error to model as observation |

#### Idempotency Keys

```python
# Deterministic idempotency key — NOT uuid4()
idempotency_key = hashlib.sha256(
    f"{task_id}:{tool_name}:{canonical_args_hash}:{attempt_number}".encode()
).hexdigest()
```

The `attempt_number` increments only after a confirmed transient error, never on network ambiguity.

### 3.4 Circuit Breaker Pattern

```python
class CircuitBreaker:
    """Three-arm breaker: provider, tool, cost."""
    
    def __init__(self):
        self.arms = {
            "provider": BreakerArm(failure_threshold=5, cooldown_s=60),
            "tool": BreakerArm(failure_threshold=10, cooldown_s=30),
            "cost": BreakerArm(failure_threshold=1, cooldown_s=3600, cost_threshold=100.0),
        }
    
    def call(self, arm: str, fn: Callable, *args, **kwargs):
        if self.arms[arm].is_open():
            raise CircuitOpenError(f"{arm} circuit is open")
        try:
            result = fn(*args, **kwargs)
            self.arms[arm].record_success()
            return result
        except Exception as e:
            self.arms[arm].record_failure()
            raise
```

### 3.5 Streaming Architecture

```python
async def stream_agent_loop(goal: str) -> AsyncIterator[AgentEvent]:
    """Yield events in real-time for SSE/WebSocket delivery."""
    messages = [{"role": "user", "content": goal}]
    
    yield AgentEvent(type="loop_start", data={"message": goal})
    
    for step in range(max_steps):
        yield AgentEvent(type="iteration_start", data={"number": step})
        
        # Stream model response
        async for chunk in model.stream(messages, tools):
            yield AgentEvent(type="thinking", data={"chunk": chunk})
        
        # Execute tools
        for tool_call in response.tool_calls:
            yield AgentEvent(type="action_start", data={"tool": tool_call.name})
            result = await run_tool(tool_call.name, tool_call.input)
            yield AgentEvent(type="action_complete", data={"tool": tool_call.name, "result": result})
        
        yield AgentEvent(type="iteration_end", data={"number": step})
    
    yield AgentEvent(type="complete", data={"text": final_answer})
```

### 3.6 Tool Failure Fallbacks

```python
@tool
def search_database(query: str) -> str:
    """Search with graceful degradation."""
    try:
        result = db.query(query, timeout=5)  # Socket timeout at client level
        if not result:
            return "No results found. Try a broader search term."
        return f"Found: {result}"
    except socket.timeout:
        # Use ToolException, not string signaling
        raise ToolException("Database timeout. Will retry with cache backend.")
    except PermissionError:
        raise ToolException("Authentication failed. Try alternative query method.")
```

**Key insight from production:** The fallback IS the system's intelligence about recovery. Returning "Tool failed" is useless. Returning "Tool failed because the query was malformed; try rephrasing the question" gives the agent something to work with.

### 3.7 Common Failure Modes

| Failure | Symptom | Mitigation |
|---|---|---|
| Infinite tool loop | Same `search` query every step | Hash recent calls, refuse duplicates |
| Tool error cascade | One 500 leads to model panic and 6 retries | Surface errors as structured content |
| Context overflow | Step 12 exceeds 200K tokens | Summarize older tool results into scratchpad |
| Wrong tool picked | Model calls `search` when it should use `calculator` | Tighten tool descriptions, add few-shot examples |
| Hallucinated tool name | Tool not in registry | Validate name; return "no such tool" as tool_result |
| Premature final answer | Stops without checking | Add system prompt: "Verify with at least one tool" |
| Hallucinated observations | Model writes fake `Observation:` | Always use `stop=["Observation:"]` |

### 3.8 Context Management

```python
class ContextManager:
    """Keep context under control as the loop progresses."""
    
    def compress(self, messages: list[dict]) -> list[dict]:
        """Summarize older tool results to free context window."""
        if count_tokens(messages) < 100_000:
            return messages
        
        # Keep system prompt + last 4 exchanges
        system = messages[0]
        recent = messages[-8:]
        middle = messages[1:-8]
        
        summary = llm.summarize(middle)
        return [system, {"role": "system", "content": f"Summary of earlier work: {summary}"}] + recent
```

---

## 4. LLM Provider Abstraction Patterns

### 4.1 Architecture

```
Application Code
      │
      │ (OpenAI-compatible API)
      ▼
  LiteLLM Proxy / Provider Pool
      │
  ┌───┴──────────────────────────────────┐
  │   Router: Load Balance + Fallback    │
  └───┬──────────┬──────────┬────────────┘
      │          │          │
  Anthropic   OpenAI    Ollama (local)
  Claude     GPT-4o     Llama 3.1
```

### 4.2 Provider Configuration

```yaml
# LiteLLM proxy config
model_list:
  - model_name: flagship-primary
    litellm_params:
      model: openai/gpt-4o
      api_key: os.environ/OPENAI_API_KEY
      weight: 60
      tpm: 200000
      rpm: 500

  - model_name: flagship-secondary
    litellm_params:
      model: anthropic/claude-sonnet-4-6
      api_key: os.environ/ANTHROPIC_API_KEY
      weight: 40
      tpm: 150000
      rpm: 400

  - model_name: cost-fallback
    litellm_params:
      model: ollama/llama3.1:8b
      api_base: http://localhost:11434
      api_key: ollama

router_settings:
  routing_strategy: usage-based-routing
  num_retries: 2
  fallbacks:
    - flagship-primary: [flagship-secondary, cost-fallback]
  provider_budget_config:
    openai:
      budget_limit: 200.0
      time_period: 1d
    anthropic:
      budget_limit: 100.0
      time_period: 1d
```

### 4.3 Routing Strategies

| Strategy | How it works | Best when |
|---|---|---|
| **Weighted pick** | Random selection weighted by provider capacity | Balanced load across providers |
| **Rate-limit aware** | Tracks RPM/TPM per provider, routes to least-loaded | Providers have strict rate limits |
| **Least busy** | Routes to provider with fewest in-flight requests | Variable latency |
| **Latency based** | Routes to provider with lowest recent p99 | Performance-critical |
| **Cost based** | Routes to cheapest provider that meets quality bar | Cost optimization |
| **Usage-based** (default) | Combines rate limits, latency, cost | Production default |

### 4.4 OpenAI-Compatible Interface Pattern

```python
from openai import OpenAI

class LLMProvider:
    """Unified interface across OpenAI, Anthropic, Ollama, OpenRouter."""
    
    def __init__(self, config: ProviderConfig):
        self.client = OpenAI(
            base_url=config.base_url,
            api_key=config.api_key,
        )
        self.model = config.model
    
    async def generate(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return LLMResponse(
            content=response.choices[0].message.content,
            tool_calls=response.choices[0].message.tool_calls,
            usage=response.usage,
            stop_reason=response.choices[0].finish_reason,
        )
```

### 4.5 Fallback Chain Pattern

```python
class ProviderPool:
    """Ordered fallback chain with health tracking."""
    
    def __init__(self, providers: list[LLMProvider]):
        self.providers = providers
        self.health = {p: True for p in providers}
    
    async def generate(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        for provider in self.providers:
            if not self.health[provider]:
                continue
            try:
                response = await provider.generate(messages, tools, timeout=30)
                return response
            except (TimeoutError, ServiceUnavailableError, RateLimitError) as e:
                self.health[provider] = False
                asyncio.get_event_loop().call_later(60, lambda: self._mark_healthy(provider))
                continue
        raise AllProvidersFailedError("All providers exhausted")
```

### 4.6 OpenRouter-Specific Considerations

OpenRouter provides unified access to 200+ models through an OpenAI-compatible API:

```python
# OpenRouter: access multiple providers through one API
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ["OPENROUTER_API_KEY"],
)

# Model naming: provider/model-name
response = client.chat.completions.create(
    model="anthropic/claude-sonnet-4-6",
    messages=[...],
)
```

**Advantages:**
- Single API key for all providers
- Automatic failover between providers
- Cost comparison across models
- No vendor lock-in

**Disadvantages:**
- Additional latency (~50–200ms)
- Another dependency to monitor
- Rate limits apply to OpenRouter's infrastructure

### 4.7 Ollama for Local Development

```python
# Ollama: OpenAI-compatible local models
client = OpenAI(
    base_url="http://localhost:11434/v1",
    api_key="ollama",  # Required but unused
)

response = client.chat.completions.create(
    model="llama3.1:8b",
    messages=[...],
)
```

**Best for:** Development, testing, offline operation, cost-free inference, privacy-sensitive workloads.

### 4.8 Semantic Caching

```python
class SemanticCache:
    """Cache responses by semantic similarity to avoid redundant LLM calls."""
    
    def __init__(self, threshold: float = 0.92):
        self.threshold = threshold
        self.cache = {}  # embedding -> response
    
    async def lookup(self, messages: list[dict]) -> LLMResponse | None:
        query_text = messages[-1]["content"]
        query_embedding = await embed(query_text)
        
        for cached_embedding, response in self.cache.items():
            similarity = cosine_similarity(query_embedding, cached_embedding)
            if similarity >= self.threshold:
                return response
        return None
```

---

## 5. Agent Session Management and Fork Patterns

### 5.1 LangGraph Checkpointing Model

LangGraph's checkpointing system provides the foundation for durable agent sessions:

```
Thread (thread_id) = Conversation or workflow execution
  ├── Checkpoint 0: Initial State
  ├── Checkpoint 1: After Agent Node
  ├── Checkpoint 2: After Tool Node
  ├── Checkpoint 3: After Synthesis Node
  └── ...
```

Each checkpoint contains:
- **State values** — The full state dictionary (messages, tool results, custom data)
- **Metadata** — Timestamp, step number, parent checkpoint ID
- **Version info** — For state schema migrations

### 5.2 Session Lifecycle

```python
from langgraph.checkpoint.postgres import AsyncPostgresSaver

# Setup (idempotent)
pool = AsyncConnectionPool(conninfo=DSN, min_size=2, max_size=20, kwargs={"autocommit": True})
saver = AsyncPostgresSaver(pool)
await saver.setup()  # Creates checkpoints, checkpoint_blobs, checkpoint_writes tables

# Compile once, cache for process lifetime
graph = builder.compile(checkpointer=saver)

# Run with thread_id
config = {"configurable": {"thread_id": "session-123"}}
result = await graph.ainvoke({"messages": [HumanMessage(content="Research topic")]}, config)

# Resume after crash — pass None as input
result = await graph.ainvoke(None, config)
```

### 5.3 Fork Pattern

```python
# Get checkpoint history
history = [c async for c in graph.aget_state_history(config)]

# Fork from an earlier checkpoint
fork_point = history[3]  # 4 steps ago
fork_config = {
    "configurable": {
        "thread_id": "session-123-fork",
        "checkpoint_id": fork_point.config["configurable"]["checkpoint_id"],
    }
}

# Continue from the fork with different input
await graph.ainvoke(
    {"messages": [HumanMessage(content="Try the alternative approach")]},
    fork_config,
)
```

**Use cases for forking:**
- **A/B testing** — Try different strategies from the same point
- **Debugging** — Reproduce and fix issues without losing previous work
- **Human-in-the-loop** — Pause, review, edit state, resume
- **Counterfactual evals** — "What if the agent had done X instead?"

### 5.4 State Schema Design

```python
from typing import TypedDict, Annotated
from langgraph.graph.message import add_messages

class AgentState(TypedDict):
    # Reducer functions control how fields merge across nodes
    messages: Annotated[list, add_messages]  # Appended, not replaced
    question: str                             # Replaced
    context: list[str]                        # Replaced
    plan: list[str]                           # Replaced
    status: Literal["draft", "researching", "complete", "failed"]
    retry_count: int                          # Replaced
    final_answer: Optional[str]               # Replaced
    error: Optional[str]                      # Replaced
```

**Critical rules:**
1. Keep state small and serializable — it's written to storage after every step
2. Never mutate state in place; return an update
3. Use `add_messages` reducer for message history (appends, doesn't replace)
4. Keep large blobs (files, audio) by reference, not by value

### 5.5 Human-in-the-Loop Pattern

```python
from langgraph.types import interrupt

# Pause before critical actions
graph = builder.compile(
    checkpointer=saver,
    interrupt_before=["send_refund", "delete_file"],
)

# 1. Agent runs to the interrupt point
config = {"configurable": {"thread_id": "session-2841"}}
state = await graph.ainvoke({"messages": [HumanMessage(content="Refund order #99")]}, config)

# 2. Human reviews and approves (can be 30 seconds or 3 days later)
await graph.aupdate_state(config, {"approved_by": "ops_user_17"})

# 3. Resume from the same checkpoint
final = await graph.ainvoke(None, config)
```

### 5.6 Production Checkpoint Management

```sql
-- Table sizes to monitor
SELECT thread_id, COUNT(*) as checkpoints 
FROM checkpoints 
GROUP BY thread_id 
ORDER BY checkpoints DESC 
LIMIT 20;

-- Archive idle threads (30+ days)
SELECT cron.schedule(
    'archive-old-threads',
    '0 3 * * *',  -- Daily at 3am
    $$INSERT INTO checkpoints_archive SELECT * FROM checkpoints 
      WHERE thread_id IN (
        SELECT thread_id FROM checkpoints 
        GROUP BY thread_id 
        HAVING MAX(created_at) < NOW() - INTERVAL '30 days'
      ) $$
);
```

### 5.7 Checkpoint Retention Policy

| Policy | Recommendation |
|---|---|
| Checkpoints per thread | Cap at 2,000; archive older to cold storage |
| Event retention | Keep full ledger 90 days for audits |
| PII | Mask fields in state before checkpointing |
| Orphan threads | Reap after 30 days idle via cron |
| Backups | Non-negotiable — lost checkpoint table = lost product |

### 5.8 Multi-Agent Orchestration Patterns

| Pattern | How it works | Best when |
|---|---|---|
| **Sequential (Handoff)** | Linear pipeline, each agent processes output of previous | Deterministic workflows |
| **Supervisor** | Central manager routes to specialists, feedback loop | Dynamic planning, quality gates |
| **Concurrent** | Multiple agents work in parallel, results aggregated | Brainstorming, ensemble reasoning |
| **Group Chat** | Agents in managed conversation, manager coordinates | Debate, consensus-building |
| **Magentic** | Manager builds task ledger, adapts in real time | Open-ended complex problems |

### 5.9 Agent-as-Tool Pattern

```python
# Manager agent calls specialists as tools
researcher = Agent(
    name="researcher",
    instructions="You are a research specialist. Find and synthesize information.",
    tools=[web_search, arxiv_search],
)

reviewer = Agent(
    name="reviewer",
    instructions="You are a quality reviewer. Critique and improve outputs.",
)

# Agents as tools — manager keeps control
manager = Agent(
    name="manager",
    instructions="You coordinate research and review.",
    tools=[researcher.as_tool(), reviewer.as_tool()],
)
```

---

## 6. Consolidated Recommendations for AgentHarness

### 6.1 Priority Matrix

| Priority | Recommendation | Impact | Effort |
|---|---|---|---|
| **P0** | Use HNSW index with `m=16, ef_construction=128` for all embedding columns | High | Low |
| **P0** | Set `maintenance_work_mem = 8GB` in PostgreSQL config | High | Low |
| **P0** | Enable `iterative_scan` for all filtered vector queries | High | Low |
| **P0** | Implement per-task retry budget with token cap | High | Medium |
| **P0** | Implement no-progress detector (hash last 3 tool calls) | High | Low |
| **P1** | Use MessagePack for inter-agent message serialization | Medium | Low |
| **P1** | Use JSONB for database-persisted context (queryable metadata) | Medium | Low |
| **P1** | Implement circuit breaker with 3 arms (provider, tool, cost) | Medium | Medium |
| **P1** | Set up LiteLLM proxy for multi-provider routing | Medium | Medium |
| **P1** | Implement checkpoint-based session persistence | High | High |
| **P2** | Add halfvec quantization for 10M+ vector scale | Medium | Low |
| **P2** | Implement hybrid search (vector + BM25 with RRF) | Medium | Medium |
| **P2** | Add semantic caching layer | Medium | Medium |
| **P2** | Implement fork pattern for debugging and A/B testing | Medium | Medium |
| **P3** | Add binary quantization (BQ) for 50M+ vector scale | Low | Medium |
| **P3** | Implement TOON format for LLM-context serialization | Low | Medium |
| **P3** | Add pgvectorscale (StreamingDiskANN) for disk-based ANN | Low | High |

### 6.2 Architecture Diagram

```
┌─────────────────────────────────────────────────────────┐
│                    AgentHarness Runtime                  │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  ┌──────────────┐    ┌──────────────┐    ┌───────────┐ │
│  │  Agent Loop  │───▶│  Tool Executor│───▶│  Tools    │ │
│  │  (ReAct)     │    │  (with retry) │    │  Registry │ │
│  └──────┬───────┘    └──────────────┘    └───────────┘ │
│         │                                                │
│         ▼                                                │
│  ┌──────────────┐    ┌──────────────┐                   │
│  │  LLM Gateway │───▶│  Provider Pool│                   │
│  │  (LiteLLM)   │    │  (fallback)  │                   │
│  └──────────────┘    └──────┬───────┘                   │
│         │                   │                            │
│         ▼                   ▼                            │
│  ┌──────────────┐    ┌──────────────┐                   │
│  │  OpenAI      │    │  Anthropic   │    ┌───────────┐ │
│  │  Ollama      │    │  OpenRouter  │    │  Local    │ │
│  └──────────────┘    └──────────────┘    └───────────┘ │
│                                                          │
├─────────────────────────────────────────────────────────┤
│                    Persistence Layer                       │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │  PostgreSQL + pgvector                            │   │
│  │  ├── chunks (HNSW index, cosine ops)             │   │
│  │  ├── sessions (checkpoint state)                  │   │
│  │  ├── messages (JSONB, MessagePack for transport)  │   │
│  │  └── memories (hybrid search: vector + BM25)      │   │
│  └──────────────────────────────────────────────────┘   │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

### 6.3 Key Files to Create/Modify

| Component | File | Pattern |
|---|---|---|
| Vector index | `db/migrations/001_hnsw_index.sql` | HNSW with tuned params |
| Serializer | `core/serialization.py` | Dual-format (MessagePack + JSON) |
| Agent loop | `core/agent_loop.py` | ReAct with budgets + no-progress |
| Retry policy | `core/retry.py` | Exponential backoff with full jitter |
| Circuit breaker | `core/circuit_breaker.py` | Three-arm breaker |
| Provider pool | `core/llm_gateway.py` | LiteLLM-style routing |
| Session manager | `core/session.py` | Checkpoint-based persistence |
| Context manager | `core/context.py` | Compression + summarization |

---

## References

### pgvector & Vector Search
1. [pgvector HNSW vs IVFFlat: How to Choose and Benchmark](https://markaicode.com/benchmarks/postgresql-pgvector-benchmark)
2. [Building a Production RAG System with pgvector](https://markaicode.com/pgvector-rag-production)
3. [Scaling PostgreSQL with pgvector for RAG](https://ribbsaetersystems.com/blog/scaling-postgres-pgvector-rag)
4. [pgvector Deep Dive — HNSW, IVFFlat, and Tuning](https://blog.rajpoot.dev/posts/postgresql/pgvector-deep-dive-hnsw-tuning)
5. [The Complete Guide to pgvector Tuning](https://tomodahinata.com/en/blog/pgvector-index-tuning-hnsw-ivfflat-quantization-iterative-scan-guide)
6. [pgvector Architecture: Scaling PostgreSQL for Vector Search at 100K QPS](https://markaicode.com/architecture/pgvector-system-design-architecture-973)
7. [Scale pgvector with binary quantization on Amazon Aurora PostgreSQL](https://aws.amazon.com/blogs/database/scale-pgvector-with-binary-quantization-on-amazon-aurora-postgresql/)
8. [Scalar and binary quantization for pgvector](https://jkatz.github.io/post/postgres/pgvector-scalar-binary-quantization/)
9. [pgvector at Scale: When Postgres Is Enough](https://turion.ai/blog/pgvector-at-scale-when-postgres-is-enough/)
10. [pgvector in Production: A 2026 Reality Check](https://selfhost.dev/blog/pgvector-in-production-2026-critiques-failures/)

### Serialization Formats
11. [MessagePack: It's like JSON, but fast and small](https://msgpack.org/)
12. [MessagePack vs JSON Comparison](https://github.com/dchenk/messagepack-vs-json)
13. [Tokenization Comparison: Token Usage Across CSV, JSON, YAML](https://medium.com/another-integration-blog/tokenization-comparison-token-usage-across-csv-json-yaml-and-toon-for-llm-interactions-3a2df3956587)
14. [TOON vs JSON: Why AI Agents Need Token-Optimized Data Formats](https://jduncan.io/blog/2025-11-11-toon-vs-json-agent-optimized-data/)
15. [Rethinking Serialization for LLMs: TOON vs JSON vs Protobuf](https://www.linkedin.com/posts/samuel-kenea_aiengineering-llmcosts-promptengineering-activity-7422088152003043328-7aCF)

### Agent Loop & ReAct
16. [Fault Tolerance in LangGraph: Retries, Timeouts and Error Handling](https://www.langchain.com/blog/fault-tolerance-in-langgraph)
17. [The Agentic Loop: ReAct, Plan-Execute & Reflection](https://prakashkagitha.github.io/llm-stack-book/08-agents-harness/02-agentic-loop.html)
18. [Production AI Agent Patterns: Retry, Idempotency, and Circuit Breakers](https://amtocsoft.blogspot.com/2026/04/production-ai-agent-patterns-retry.html)
19. [Building a ReAct Agent Loop From Scratch](https://folarin.dev/blog/building-a-react-agent-loop-from-scratch)
20. [Tool Failure Fallbacks in ReAct](https://theneuralbase.com/react-prompting/learn/advanced/tool-failure-fallbacks/)
21. [The Agent Loop: ReAct and Its Descendants](https://jatinbansal.com/ai-engineering/agent-loop/)
22. [Build Production-Ready LLM Agents: ReAct Pattern](https://python.elitedev.in/large_language_model/build-production-ready-llm-agents-react-pattern-with-custom-tools-and-python-integration-guide-4d6e569d/)

### LLM Provider Abstraction
23. [LiteLLM Router - Load Balancing](https://docs.litellm.ai/docs/routing)
24. [LLM Gateway in Production: Multi-Provider Routing + Fallbacks](https://devopsboys.com/blog/llm-gateway-litellm-multi-provider-routing-production-2026)
25. [Running LiteLLM as a Proxy in Front of Multiple Model Providers](https://medium.com/data-science-collective/running-litellm-as-a-proxy-in-front-of-multiple-llm-providers-528f74ebb30b)
26. [Implementing Resilience Patterns with Amazon Bedrock and LLM Gateway](https://aws.amazon.com/blogs/machine-learning/implementing-resilience-patterns-with-amazon-bedrock-and-llm-gateway/)
27. [Ollama OpenAI Compatibility](https://ollama.com/blog/openai-compatibility)
28. [Open Source LLM Platforms in 2026](https://medium.com/codex/open-source-llm-platforms-in-2026-ollama-openrouter-groq-nvidia-nim-which-one-should-you-use-2f11c7ba60bc)

### Session Management & Orchestration
29. [Checkpointing Agent State with LangGraph](https://blog.redlinesoft.net/posts/checkpointing-agent-state-langgraph)
30. [Checkpointing LangGraph State: Snapshots, Restores, and Forks](https://blog.redlinesoft.net/posts/checkpointing-langgraph-state-snapshots-restore)
31. [LangGraph: An Agent as a State Machine](https://multigrid.ai/learn/langgraph-guide)
32. [How to Persist LangGraph State Across Sessions](https://n4n.ai/blog/how-to-persist-langgraph-state-across-sessions)
33. [LangGraph Checkpointers in Production](https://callsphere.ai/blog/langgraph-checkpointer-durable-resumable-agents)
34. [Event-Sourced Durable Agent Execution](https://dailyaiworld.com/public/workflow/event-sourced-durable-agent-execution-checkpointing-time)
35. [OpenAI Agents SDK: Multi-Agent Orchestration](https://github.com/openai/openai-agents-python/blob/main/docs/multi_agent.md)
36. [Azure AI Agent Orchestration Patterns](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/ai-agent-design-patterns)
37. [Semantic Kernel: Multi-agent Orchestration](https://devblogs.microsoft.com/agent-framework/semantic-kernel-multi-agent-orchestration)
38. [Multi-Agent Orchestration Patterns with LangGraph](https://github.com/tigerjibo/multi-agent-orchestration-patterns)
39. [State Management Patterns for AI Agents](https://github.com/ombharatiya/ai-system-design-guide/blob/main/08-memory-and-state/06-state-management-patterns.md)

---

*Report compiled: 2026-10-04*
*Target codebase: AgentHarness (multi-agent AI orchestration framework with PostgreSQL+pgvector)*
