-- AgentHarness PostgreSQL Schema
-- Minimal schema: sessions + context_chunks only

CREATE EXTENSION IF NOT EXISTS vector;

-- ─── Sessions ───────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT,
    agent_id TEXT NOT NULL DEFAULT 'harness',
    state_msgpack BYTEA,
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'idle', 'archived')),
    goal TEXT,
    model TEXT,
    provider TEXT,
    context_budget INT DEFAULT 8000,
    created_at TIMESTAMPTZ DEFAULT now(),
    last_activity TIMESTAMPTZ DEFAULT now()
);

-- Composite index for list_sessions query: WHERE status = $1 ORDER BY last_activity DESC
CREATE INDEX IF NOT EXISTS idx_sessions_status_last_activity ON sessions(status, last_activity DESC);
-- Index for get_last_active query: WHERE status = 'active' ORDER BY last_activity DESC
CREATE INDEX IF NOT EXISTS idx_sessions_active_last_activity ON sessions(status, last_activity DESC) WHERE status = 'active';
-- Keep the simple status index for other status-only lookups
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);

-- ─── Context Chunks ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS context_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    chunk_type TEXT NOT NULL CHECK (chunk_type IN (
        'tool_call', 'result', 'memory', 'heartbeat', 'system', 'user', 'assistant',
        'user_message', 'assistant_message', 'document'
    )),
    payload_msgpack BYTEA NOT NULL,
    embedding vector(1536),
    search_text TEXT,
    token_count INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    accessed_at TIMESTAMPTZ
);

-- Composite index for get_chunks query: WHERE session_id = $1 [AND chunk_type = $2] ORDER BY created_at DESC
CREATE INDEX IF NOT EXISTS idx_context_chunks_session_type_created ON context_chunks(session_id, chunk_type, created_at DESC);
-- Keep the simple session+created index for queries without chunk_type filter
CREATE INDEX IF NOT EXISTS idx_context_chunks_session ON context_chunks(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_context_chunks_embedding ON context_chunks
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- ─── Long-Term Memories ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS memories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID REFERENCES sessions(id) ON DELETE SET NULL,
    agent_id TEXT NOT NULL,
    content TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN (
        'preference', 'decision', 'fact', 'event', 'transient'
    )),
    importance FLOAT NOT NULL DEFAULT 0.5 CHECK (importance >= 0.0 AND importance <= 1.0),
    base_strength FLOAT NOT NULL DEFAULT 1.0,
    access_count INT NOT NULL DEFAULT 0,
    embedding vector(1536),
    explicitly_important BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT now(),
    last_accessed TIMESTAMPTZ
);

-- Index for agent+category filtering
CREATE INDEX IF NOT EXISTS idx_memories_agent_category ON memories(agent_id, category);
-- HNSW index for vector similarity search
CREATE INDEX IF NOT EXISTS idx_memories_embedding ON memories
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
-- Index for importance-based eviction queries
CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance ASC);
-- Index for session-based cleanup
CREATE INDEX IF NOT EXISTS idx_memories_session ON memories(session_id);

-- Full-text search index for BM25 hybrid search
CREATE INDEX IF NOT EXISTS idx_context_chunks_fts ON context_chunks
    USING GIN (to_tsvector('english', search_text));
