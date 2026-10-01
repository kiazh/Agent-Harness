# AgentHarness Microservices Architecture Proposal

**Date:** 2026-09-30
**Author:** Microservices Advocate
**Status:** Proposal

---

## Executive Summary

AgentHarness is currently a monolithic CLI application where the ReAct agent loop, context management, tool execution, session management, and CLI interface all run in a single Python process with a single asyncpg connection pool. While this works for a single-user prototype, it has hard architectural ceilings that prevent scaling beyond one concurrent user.

This proposal decomposes the monolith into **five independently deployable microservices**:

1. **Agent Service** — runs the ReAct loop
2. **Context Service** — manages context chunks and embeddings
3. **Tool Service** — executes tools in sandboxed environments
4. **Session Service** — manages agent sessions and state
5. **Gateway Service** — CLI/API gateway for user interaction

Each service owns its data, exposes a gRPC/REST API, and can be scaled independently.

---

## Why the Monolith Won't Scale

### 1. Single Connection Pool — Hard Ceiling at 10 Concurrent Operations

```python
# ah/db/connection.py:22-27
self._pool = await asyncpg.create_pool(
    self.dsn,
    min_size=2,
    max_size=10,        # ← HARD CEILING
    command_timeout=30,
)
```

A single ReAct iteration performs 3-4 DB calls (session get, context add, context fetch, session update). With 3 concurrent users, the pool is saturated. At 5 users, `acquire()` blocks for 30 seconds then raises `PoolTimeoutError`. The agent loop has no retry logic — it crashes.

**Microservices fix:** Each service owns its own connection pool. The Context Service can have a pool of 50+ connections since it handles the highest query volume. The Session Service needs only 5-10. No contention.

### 2. Blocking I/O Freezes the Entire Process

```python
# ah/tools/terminal.py:87
result = subprocess.run(args, shell=False, capture_output=True, text=True, timeout=timeout)
```

`subprocess.run()` is synchronous. A 30-second `terminal` call blocks the entire asyncio event loop. All other agent sessions, all DB connections, all LLM calls freeze. The `web_search` and `web_extract` tools use synchronous `httpx.get()` — same problem.

**Microservices fix:** The Tool Service runs tools in isolated worker processes. A blocking tool only affects that worker. The Agent Service's event loop stays responsive.

### 3. No Horizontal Scaling — Single Process, Single Instance

```python
# ah/cli.py:30-32
def _run(coro):
    return asyncio.run(coro)
```

The entire application is a Typer CLI that runs `asyncio.run()` per command. There is no HTTP server, no message queue consumer. You cannot deploy this as a service. You cannot have a web UI, a Slack bot, and a CLI all talking to the same agent. You cannot run multiple agent workers against the same database.

**Microservices fix:** The Gateway Service exposes HTTP/gRPC endpoints. The Agent Service runs as a long-running async process that can be replicated behind a load balancer. A message queue (Redis/RabbitMQ) distributes work across Agent Service instances.

### 4. Unbounded Context Growth — No Eviction, No Partitioning

```python
# ah/core/context.py:67-94
async def add_chunk(self, session_id, agent_id, chunk_type, payload, token_count=0, embedding=None):
    # INSERT — no check on total session size, no eviction
```

A session that runs for 100 iterations accumulates 100+ context chunks. At 10,000 sessions, that's 1GB in `context_chunks` with no archival. The `messages` list within a turn grows to 60+ entries, potentially exceeding the LLM's context window. The HNSW index on `embedding` becomes a write bottleneck at 100+ inserts/second.

**Microservices fix:** The Context Service implements sliding-window eviction, time-based partitioning, and summarization. It can use a dedicated vector database (Qdrant, Weaviate) instead of pgvector if the embedding workload grows.

### 5. No Isolation — One Bug Takes Down Everything

The `ReActAgent.run()` method is a 130-line God Object that does everything: LLM calls, tool execution, context storage, session updates, error handling. A memory leak in tool execution eventually OOMs the entire process. An infinite loop in the agent loop blocks all users. There is no way to restart just the tool execution component.

**Microservices fix:** Each service runs in its own process/container. If the Tool Service crashes, the Agent Service detects the failure and returns an error to the user. The Session and Context Services remain unaffected.

### 6. No Multi-Tenancy — All Users Share One Database

All sessions share the same `sessions` table with no `organization_id` or `user_id`. No row-level security. No quota enforcement. One user can create 10,000 sessions and consume all DB resources.

**Microservices fix:** The Gateway Service handles authentication and injects tenant context. Each service enforces tenant isolation at the query level. The Session Service can shard by tenant.

---

## Proposed Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        CLI / Web UI / API                       │
└──────────────────────────────┬──────────────────────────────────┘
                               │ HTTP/gRPC
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Gateway Service                             │
│  • Authentication & authorization                               │
│  • Rate limiting & quotas                                      │
│  • Request routing                                              │
│  • Streaming (SSE/WebSocket)                                    │
│  • Multi-tenant context injection                               │
└──────┬──────────────┬──────────────┬──────────────┬────────────┘
       │              │              │              │
       ▼              ▼              ▼              ▼
┌─────────────┐ ┌───────────┐ ┌───────────┐ ┌──────────────┐
│   Agent     │ │  Context  │ │   Tool    │ │   Session    │
│  Service    │ │  Service  │ │  Service  │ │   Service    │
│             │ │           │ │           │ │              │
│ • ReAct     │ │ • Chunks  │ │ • Execute │ │ • CRUD       │
│   loop      │ │ • Embed   │ │ • Sandbox │ │ • State      │
│ • LLM calls │ │ • Search  │ │ • Validate│ │ • Goal       │
│ • Retry     │ │ • Summarize│ │ • Timeout│ │ • Archive    │
│ • Streaming │ │ • Evict   │ │ • Audit   │ │ • List       │
└──────┬──────┘ └─────┬─────┘ └─────┬─────┘ └──────┬───────┘
       │              │              │              │
       ▼              ▼              ▼              ▼
┌─────────────────────────────────────────────────────────────────┐
│              Message Queue (Redis / RabbitMQ)                    │
│  • Agent task distribution                                       │
│  • Tool execution requests                                       │
│  • Inter-service events                                          │
└─────────────────────────────────────────────────────────────────┘
       │              │              │              │
       ▼              ▼              ▼              ▼
┌─────────────┐ ┌───────────┐ ┌───────────┐ ┌──────────────┐
│ PostgreSQL  │ │  Vector   │ │  Tool     │ │  Session     │
│ (sessions,  │ │  DB       │ │  Sandbox  │ │  State       │
│  messages)  │ │ (Qdrant/  │ │  (Docker/ │ │  (Redis/     │
│             │ │  pgvector)│ │  Firecracker│ │  PostgreSQL) │
└─────────────┘ └───────────┘ └───────────┘ └──────────────┘
```

---

## Service Specifications

### 1. Agent Service

**Responsibility:** Run the ReAct loop — Thought → Action → Observation.

**Why separate:** The agent loop is the most CPU-intensive and latency-sensitive component. It needs to scale independently based on LLM API concurrency limits. It is also the component most likely to change (new agent strategies, new LLM providers).

**API:**
```protobuf
service AgentService {
  rpc RunAgent(RunAgentRequest) returns (stream AgentEvent);
  rpc CancelAgent(CancelAgentRequest) returns (CancelAgentResponse);
  rpc GetAgentStatus(GetAgentStatusRequest) returns (AgentStatus);
}

message RunAgentRequest {
  string session_id = 1;
  string user_message = 2;
  string agent_id = 3;
  string model = 4;
  string provider = 5;
  int32 max_iterations = 6;
  int32 context_budget = 7;
}

message AgentEvent {
  oneof event {
    TextDelta text = 1;
    ToolCallStarted tool_call = 2;
    ToolResult tool_result = 3;
    TokenUsage token_usage = 4;
    AgentComplete done = 5;
    AgentError error = 6;
  }
}
```

**Implementation:**
- Extract `ReActAgent` from `ah/core/agent.py` into a standalone service
- Use `asyncio.TaskGroup` (Python 3.11+) for parallel tool execution
- Stream events via gRPC server-side streaming
- Implement circuit breaker for LLM provider failures
- Configurable concurrency: `AGENT_MAX_CONCURRENT_LOOPS`, `AGENT_LLM_TIMEOUT`

**Scaling:** Horizontal — run N instances behind a load balancer. The message queue distributes agent tasks. Each instance maintains its own LLM provider connection pool.

**Dependencies:** Context Service (gRPC), Tool Service (gRPC), Session Service (gRPC), LLM Provider (HTTP).

---

### 2. Context Service

**Responsibility:** Manage context chunks — storage, retrieval, embedding, summarization, eviction.

**Why separate:** Context management is the highest-volume data operation. Every ReAct iteration reads and writes context chunks. The embedding search (HNSW on pgvector) is computationally expensive. This service needs its own connection pool, caching layer, and potentially a dedicated vector database.

**API:**
```protobuf
service ContextService {
  rpc AddChunk(AddChunkRequest) returns (Chunk);
  rpc GetRecentContext(GetRecentContextRequest) returns (ContextList);
  rpc SearchByEmbedding(SearchByEmbeddingRequest) returns (SearchResults);
  rpc SummarizeSession(SummarizeSessionRequest) returns (Summary);
  rpc EvictOldChunks(EvictOldChunksRequest) returns (EvictionResult);
  rpc GetTokenUsage(GetTokenUsageRequest) returns (TokenUsage);
}

message AddChunkRequest {
  string session_id = 1;
  string agent_id = 2;
  string chunk_type = 3;
  bytes payload_msgpack = 4;
  int32 token_count = 5;
  repeated float embedding = 6;
}

message SearchByEmbeddingRequest {
  string session_id = 1;
  repeated float query_embedding = 2;
  int32 top_k = 3;
  float threshold = 4;
}
```

**Implementation:**
- Extract `ContextManager` and `PromptAssembler` from `ah/core/context.py`
- Add Redis caching layer for recent context (5-second TTL)
- Implement sliding-window eviction: when `token_count > context_budget`, summarize older chunks
- Add time-based partitioning to `context_chunks` table
- Support pluggable vector backends: pgvector (default), Qdrant, Weaviate
- Batch inserts using `executemany()` for tool call results

**Scaling:** Horizontal — partition by `session_id`. Read replicas for search queries. Redis cache reduces DB load by 80%.

**Dependencies:** PostgreSQL (or vector DB), Redis (caching).

---

### 3. Tool Service

**Responsibility:** Execute tools in sandboxed, isolated environments.

**Why separate:** Tool execution is the most dangerous component. Tools run shell commands, make HTTP requests, and read/write files. A bug in one tool (e.g., infinite loop, memory leak) should not crash the agent. Tools also have wildly different resource requirements — `web_search` needs network access, `terminal` needs a shell, `read_file` needs disk I/O.

**API:**
```protobuf
service ToolService {
  rpc ListTools(ListToolsRequest) returns (ToolList);
  rpc ExecuteTool(ExecuteToolRequest) returns (ToolResult);
  rpc ValidateTool(ValidateToolRequest) returns (ValidationResult);
  rpc GetToolSchema(GetToolSchemaRequest) returns (ToolSchema);
}

message ExecuteToolRequest {
  string tool_name = 1;
  map<string, string> arguments = 2;
  string session_id = 3;
  string agent_id = 4;
  int32 timeout_seconds = 5;
  SandboxConfig sandbox = 6;
}

message SandboxConfig {
  bool network_access = 1;
  bool filesystem_access = 2;
  repeated string allowed_paths = 3;
  repeated string allowed_commands = 4;
  int32 max_memory_mb = 5;
  int32 max_cpu_percent = 6;
}
```

**Implementation:**
- Extract tool registry from `ah/tools/` into a standalone service
- Run each tool execution in a subprocess with resource limits (memory, CPU, time)
- Use Docker containers or Firecracker microVMs for strong isolation
- Implement the existing security controls (SSRF protection, path traversal prevention, command allowlist) as middleware
- Add audit logging for every tool execution
- Replace `subprocess.run()` with `asyncio.create_subprocess_exec()` for async execution
- Replace `httpx.get()` with `httpx.AsyncClient` for web tools

**Scaling:** Horizontal — stateless workers that pull tool execution requests from the message queue. Each worker can run multiple tool executions concurrently (with semaphore-based limits).

**Dependencies:** Message Queue (for task distribution), Docker/Firecracker (for sandboxing).

---

### 4. Session Service

**Responsibility:** Manage agent sessions — creation, resumption, state, goals, archiving.

**Why separate:** Sessions are the unit of user interaction. They need to be durable, queryable, and support long-running conversations. The Session Service is the most stable component — it changes rarely and has the simplest data model. Separating it allows the Agent Service to scale without worrying about session persistence.

**API:**
```protobuf
service SessionService {
  rpc CreateSession(CreateSessionRequest) returns (Session);
  rpc GetSession(GetSessionRequest) returns (Session);
  rpc ListSessions(ListSessionsRequest) returns (SessionList);
  rpc UpdateState(UpdateStateRequest) returns (Session);
  rpc SetGoal(SetGoalRequest) returns (Session);
  rpc ArchiveSession(ArchiveSessionRequest) returns (Session);
  rpc DeleteSession(DeleteSessionRequest) returns (DeleteSessionResponse);
  rpc GetLastActive(GetLastActiveRequest) returns (Session);
}

message Session {
  string id = 1;
  string title = 2;
  string agent_id = 3;
  string status = 4;
  bytes state_msgpack = 5;
  string goal = 6;
  string model = 7;
  string provider = 8;
  int32 context_budget = 9;
  string created_at = 10;
  string last_activity = 11;
}
```

**Implementation:**
- Extract `SessionManager` from `ah/core/session.py`
- Use Redis for hot session cache (active sessions)
- Use PostgreSQL for persistent session storage
- Implement session archival: move sessions inactive for >30 days to cold storage
- Add `organization_id` and `user_id` for multi-tenancy
- Implement row-level security policies in PostgreSQL

**Scaling:** Vertical is sufficient for most deployments (sessions are low-volume). For large scale, shard by `organization_id`.

**Dependencies:** PostgreSQL, Redis (caching).

---

### 5. Gateway Service

**Responsibility:** User-facing interface — CLI, HTTP API, WebSocket, authentication, rate limiting.

**Why separate:** The Gateway is the only component that needs to know about user identity, authentication, and rate limiting. It translates user requests into service calls. It handles streaming responses (SSE/WebSocket) for real-time agent output. Separating it allows the backend services to be internal-only, reducing the attack surface.

**API:**
```protobuf
service GatewayService {
  // CLI / HTTP endpoints
  rpc Chat(ChatRequest) returns (stream ChatEvent);
  rpc Status(StatusRequest) returns (StatusResponse);
  rpc ListSessions(ListSessionsRequest) returns (SessionList);
  rpc InspectContext(InspectContextRequest) returns (ContextView);
  rpc ListSkills(ListSkillsRequest) returns (SkillList);
  rpc Doctor(DoctorRequest) returns (DoctorResponse);

  // WebSocket streaming
  rpc StreamChat(stream ChatRequest) returns (stream ChatEvent);
}

message ChatRequest {
  string message = 1;
  string session_id = 2;       // optional — resume existing
  bool continue_last = 3;      // continue most recent session
  string model = 4;
  string provider = 5;
  bool verbose = 6;
}

message ChatEvent {
  oneof event {
    TextDelta text = 1;
    ToolCallEvent tool_call = 2;
    ToolResultEvent tool_result = 3;
    TokenUsage token_usage = 4;
    ChatComplete done = 5;
    Error error = 6;
  }
}
```

**Implementation:**
- Extract CLI from `ah/cli.py` into the Gateway Service
- Add FastAPI/Starlette HTTP server with `/chat`, `/sessions`, `/stream` endpoints
- Implement JWT-based authentication
- Add rate limiting per user (e.g., 10 agent turns per minute)
- Stream agent output via Server-Sent Events (SSE) or WebSocket
- The CLI becomes a thin client that calls the Gateway via HTTP

**Scaling:** Horizontal — stateless, behind a load balancer. WebSocket connections can be sticky-session.

**Dependencies:** Agent Service (gRPC), Session Service (gRPC), Context Service (gRPC), Auth provider.

---

## Communication Patterns

### Synchronous (gRPC)
- Gateway → Agent: `RunAgent` (server-side streaming)
- Agent → Context: `GetRecentContext`, `AddChunk`
- Agent → Tool: `ExecuteTool`
- Agent → Session: `GetSession`, `UpdateState`

### Asynchronous (Message Queue)
- Agent publishes `tool.executed` event → Context Service consumes to update embeddings
- Agent publishes `session.activity` event → Session Service consumes to update `last_activity`
- Gateway publishes `user.message` event → Agent Service consumes to start a new agent turn

### Event-Driven
- `session.created` → Context Service initializes context budget
- `session.archived` → Context Service evicts all chunks
- `tool.failed` → Agent Service triggers retry or circuit breaker

---

## Data Ownership

| Service | Owns | Storage |
|---------|------|---------|
| Agent Service | Nothing (stateless) | — |
| Context Service | `context_chunks`, `memories` | PostgreSQL + pgvector, Redis (cache) |
| Tool Service | Tool definitions, execution logs | PostgreSQL (audit log) |
| Session Service | `sessions`, `agent_messages` | PostgreSQL, Redis (hot cache) |
| Gateway Service | Users, API keys, rate limits | PostgreSQL |

**Key principle:** Each service owns its data. No service directly accesses another service's database. All cross-service access goes through the API.

---

## Deployment Architecture

### Docker Compose (Development)
```yaml
services:
  gateway:
    build: ./services/gateway
    ports: ["8080:8080"]
    depends_on: [agent, session, context, tool]
  
  agent:
    build: ./services/agent
    environment:
      - CONTEXT_SERVICE_URL=context:50051
      - TOOL_SERVICE_URL=tool:50051
      - SESSION_SERVICE_URL=session:50051
  
  context:
    build: ./services/context
    environment:
      - DATABASE_URL=postgresql://postgres:5432/agentharness
      - REDIS_URL=redis://redis:6379
  
  tool:
    build: ./services/tool
    environment:
      - DOCKER_HOST=unix:///var/run/docker.sock
  
  session:
    build: ./services/session
    environment:
      - DATABASE_URL=postgresql://postgres:5432/agentharness
      - REDIS_URL=redis://redis:6379
  
  redis:
    image: redis:7-alpine
  
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      - POSTGRES_DB=agentharness
```

### Kubernetes (Production)
- Each service is a Deployment with its own HPA (Horizontal Pod Autoscaler)
- Gateway is exposed via Ingress
- Services communicate via ClusterIP Services
- PostgreSQL and Redis are managed services (RDS, Cloud SQL, ElastiCache)
- Tool Service pods run with `privileged: true` for Docker-in-Docker sandboxing

---

## Migration Path

### Phase 1: Extract Services (Weeks 1-4)
1. Define gRPC protobuf interfaces for all 5 services
2. Extract `ContextManager` into Context Service (lowest risk — no user-facing changes)
3. Extract `SessionManager` into Session Service
4. Extract tool registry into Tool Service
5. Extract `ReActAgent` into Agent Service
6. Build Gateway Service with HTTP API

### Phase 2: Decouple Data (Weeks 5-8)
1. Add Redis caching to Context and Session Services
2. Implement message queue for async communication
3. Add multi-tenancy columns to all tables
4. Implement row-level security in PostgreSQL

### Phase 3: Harden (Weeks 9-12)
1. Add Docker sandboxing to Tool Service
2. Implement circuit breakers and retry logic
3. Add Prometheus metrics and OpenTelemetry tracing
4. Load test with 100+ concurrent users

---

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Network latency between services | Use gRPC (HTTP/2 multiplexing), co-locate services in same cluster |
| Distributed transaction complexity | Use saga pattern — each service owns its data, compensate on failure |
| Operational complexity | Start with Docker Compose, migrate to Kubernetes only when needed |
| Data consistency | Event-driven eventual consistency for non-critical data (embeddings, audit logs) |
| Debugging difficulty | OpenTelemetry tracing across all services, correlation IDs |

---

## Conclusion

The monolithic architecture works for a single-user prototype but has hard ceilings: 10 DB connections, single-process execution, no horizontal scaling, no isolation, and no multi-tenancy. These are not tuning problems — they are architectural limitations that require decomposition.

The proposed microservices architecture addresses each limitation:

- **Agent Service** scales horizontally behind a load balancer
- **Context Service** owns the highest-volume data and can use dedicated vector DBs
- **Tool Service** isolates dangerous code in sandboxed workers
- **Session Service** provides stable, durable session storage
- **Gateway Service** handles auth, rate limiting, and streaming

Each service is independently deployable, scalable, and replaceable. The migration can be done incrementally — extract one service at a time, starting with the lowest-risk component (Context Service).

The cost is operational complexity: more moving parts, network latency, and distributed systems challenges. But the alternative — a monolith that crashes under 3 concurrent users — is not viable for production use.
