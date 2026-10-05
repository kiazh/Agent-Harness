-- AgentHarness PostgreSQL Schema
-- Core tables (sessions, context) plus memory, jobs, usage, audit, and identity.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- required by the gin_trgm_ops index below

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
        'user_message', 'assistant_message', 'document', 'compression_summary'
    )),
    payload_msgpack BYTEA NOT NULL,
    embedding vector(1536),
    search_text TEXT,
    token_count INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    accessed_at TIMESTAMPTZ
);

-- Migration for existing databases: CREATE TABLE IF NOT EXISTS does not update
-- the CHECK constraint, so re-create it to allow 'compression_summary'.
ALTER TABLE context_chunks DROP CONSTRAINT IF EXISTS context_chunks_chunk_type_check;
ALTER TABLE context_chunks ADD CONSTRAINT context_chunks_chunk_type_check CHECK (chunk_type IN (
    'tool_call', 'result', 'memory', 'heartbeat', 'system', 'user', 'assistant',
    'user_message', 'assistant_message', 'document', 'compression_summary'
));

-- Composite index for get_chunks query: WHERE session_id = $1 [AND chunk_type = $2] ORDER BY created_at DESC
CREATE INDEX IF NOT EXISTS idx_context_chunks_session_type_created ON context_chunks(session_id, chunk_type, created_at DESC);
-- Keep the simple session+created index for queries without chunk_type filter
CREATE INDEX IF NOT EXISTS idx_context_chunks_session ON context_chunks(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_context_chunks_session_eviction
    ON context_chunks(session_id,
        (COALESCE(created_at, '0001-01-01 00:00:00+00'::timestamptz)), id);
CREATE INDEX IF NOT EXISTS idx_context_chunks_embedding ON context_chunks
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- ─── Context Archive (reversible eviction) ─────────────────────────────────

CREATE TABLE IF NOT EXISTS context_archive (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    chunk_id UUID NOT NULL,
    payload_msgpack BYTEA NOT NULL,
    embedding vector(1536),
    archived_at TIMESTAMPTZ DEFAULT NOW(),
    archive_reason TEXT,
    resurrection_count INT DEFAULT 0,
    last_resurrected TIMESTAMPTZ
);

ALTER TABLE context_archive ADD COLUMN IF NOT EXISTS agent_id TEXT;
ALTER TABLE context_archive ADD COLUMN IF NOT EXISTS chunk_type TEXT;
ALTER TABLE context_archive ADD COLUMN IF NOT EXISTS token_count INT NOT NULL DEFAULT 0;
ALTER TABLE context_archive ADD COLUMN IF NOT EXISTS original_created_at TIMESTAMPTZ;
ALTER TABLE context_archive ADD COLUMN IF NOT EXISTS search_text TEXT;

-- Older installations created this table without the session foreign key.
-- Make migration idempotent: only delete if orphan rows exist, and log the count.
DO $$
DECLARE
    deleted_count INT;
BEGIN
    -- Count orphan rows before deletion
    SELECT COUNT(*) INTO deleted_count
    FROM context_archive a
    WHERE NOT EXISTS (SELECT 1 FROM sessions s WHERE s.id = a.session_id);

    IF deleted_count > 0 THEN
        RAISE NOTICE 'context_archive migration: deleting % orphan rows', deleted_count;
        DELETE FROM context_archive a WHERE NOT EXISTS (
            SELECT 1 FROM sessions s WHERE s.id = a.session_id
        );
    END IF;
END $$;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'context_archive'::regclass
          AND confrelid = 'sessions'::regclass AND contype = 'f'
    ) THEN
        ALTER TABLE context_archive ADD CONSTRAINT context_archive_session_fk
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_context_archive_session ON context_archive(session_id);
CREATE INDEX IF NOT EXISTS idx_context_archive_fts ON context_archive
    USING GIN (to_tsvector('english', COALESCE(search_text, '')));
CREATE INDEX IF NOT EXISTS idx_context_archive_embedding ON context_archive
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
    last_accessed TIMESTAMPTZ,
    quarantined BOOLEAN NOT NULL DEFAULT FALSE
);

ALTER TABLE memories ADD COLUMN IF NOT EXISTS quarantined BOOLEAN NOT NULL DEFAULT FALSE;

-- Index for agent+category filtering
CREATE INDEX IF NOT EXISTS idx_memories_agent_category ON memories(agent_id, category);
-- Composite index for MemoryStore.search(): WHERE agent_id = $1 AND quarantined = FALSE ORDER BY importance DESC, created_at DESC
CREATE INDEX IF NOT EXISTS idx_memories_agent_importance_created ON memories(agent_id, importance DESC, created_at DESC);
-- Partial index: every search filters quarantined = FALSE
CREATE INDEX IF NOT EXISTS idx_memories_quarantined_false ON memories(quarantined) WHERE quarantined = FALSE;
-- HNSW index for vector similarity search
CREATE INDEX IF NOT EXISTS idx_memories_embedding ON memories
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
-- Index for importance-based eviction queries
CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance ASC);
-- Index for session-based cleanup
CREATE INDEX IF NOT EXISTS idx_memories_session ON memories(session_id);

-- GIN trigram index for ILIKE pattern matching (fixes full table scan)
CREATE INDEX IF NOT EXISTS idx_memories_content_trgm ON memories
    USING GIN (content gin_trgm_ops);

-- Full-text search index for BM25 hybrid search
CREATE INDEX IF NOT EXISTS idx_context_chunks_fts ON context_chunks
    USING GIN (to_tsvector('english', search_text));

-- ─── Pending Memories (Approval Gate) ───────────────────────────────────────

CREATE TABLE IF NOT EXISTS pending_memories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_id UUID REFERENCES memories(id) ON DELETE SET NULL,
    content TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN (
        'preference', 'decision', 'fact', 'event', 'transient'
    )),
    importance FLOAT NOT NULL DEFAULT 0.5 CHECK (importance >= 0.0 AND importance <= 1.0),
    agent_id TEXT NOT NULL DEFAULT 'harness',
    session_id UUID REFERENCES sessions(id) ON DELETE SET NULL,
    redactions TEXT[] NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    explicitly_important BOOLEAN NOT NULL DEFAULT FALSE,
    base_strength FLOAT NOT NULL DEFAULT 1.0,
    embedding vector(1536),
    created_at TIMESTAMPTZ DEFAULT now(),
    reviewed_at TIMESTAMPTZ,
    review_note TEXT
);

CREATE INDEX IF NOT EXISTS idx_pending_memories_status ON pending_memories(status);
CREATE INDEX IF NOT EXISTS idx_pending_memories_agent ON pending_memories(agent_id);
CREATE INDEX IF NOT EXISTS idx_pending_memories_session ON pending_memories(session_id);
CREATE INDEX IF NOT EXISTS idx_pending_memories_created ON pending_memories(created_at DESC);
-- Composite index for list_pending: WHERE status = $1 [AND agent_id = $2] ORDER BY created_at DESC
CREATE INDEX IF NOT EXISTS idx_pending_memories_status_agent_created ON pending_memories(status, agent_id, created_at DESC);

-- ─── User Profiles ──────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS user_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL DEFAULT '',
    preferences JSONB NOT NULL DEFAULT '{}',
    interaction_count INT NOT NULL DEFAULT 0,
    topics JSONB NOT NULL DEFAULT '{}',
    last_topics JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_user_profiles_user_id ON user_profiles(user_id);
CREATE INDEX IF NOT EXISTS idx_user_profiles_updated ON user_profiles(updated_at DESC);

-- ─── Agents (Phase 5: multi-agent) ──────────────────────────────────────────

CREATE TABLE IF NOT EXISTS agents (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    system_prompt TEXT NOT NULL DEFAULT '',
    tools JSONB NOT NULL DEFAULT '[]',       -- allowed tool names ([] = all)
    model TEXT,
    provider TEXT,
    max_iterations INT NOT NULL DEFAULT 10,
    source TEXT NOT NULL DEFAULT 'db',        -- 'db' or 'builtin'
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Messages passed between agents during orchestration.
CREATE TABLE IF NOT EXISTS agent_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID REFERENCES sessions(id) ON DELETE CASCADE,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    task TEXT NOT NULL,
    response TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'complete', 'error', 'cancelled')),
    tokens_used INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    completed_at TIMESTAMPTZ
);

-- Migration for existing databases: CREATE TABLE IF NOT EXISTS does not update
-- the CHECK constraint, so re-create it to allow 'cancelled'.
ALTER TABLE agent_messages DROP CONSTRAINT IF EXISTS agent_messages_status_check;
ALTER TABLE agent_messages ADD CONSTRAINT agent_messages_status_check CHECK (status IN ('pending', 'complete', 'error', 'cancelled'));

CREATE INDEX IF NOT EXISTS idx_agent_messages_session ON agent_messages(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_messages_to ON agent_messages(to_agent, status);

-- No FK from agent_messages to agents(name): from_agent/to_agent also cover
-- built-in names ('harness', 'orchestrator', 'delegate-tool') and YAML-defined
-- agents that are not rows in the agents table.
ALTER TABLE agent_messages DROP CONSTRAINT IF EXISTS agent_messages_from_agent_fk;
ALTER TABLE agent_messages DROP CONSTRAINT IF EXISTS agent_messages_to_agent_fk;

-- ─── Scheduled Jobs (Phase 6a: scheduler) ───────────────────────────────────

-- Sanitized security and lifecycle events. The async writer never stores raw
-- credentials; audit_log redacts values before enqueueing them.
CREATE TABLE IF NOT EXISTS audit_events (
    id BIGSERIAL PRIMARY KEY,
    event TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_events_created ON audit_events(created_at DESC);

CREATE TABLE IF NOT EXISTS jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('heartbeat', 'interval', 'cron')),
    session_id UUID REFERENCES sessions(id) ON DELETE CASCADE,
    agent_name TEXT NOT NULL DEFAULT 'harness',
    model TEXT,
    provider TEXT,
    no_agent BOOLEAN NOT NULL DEFAULT FALSE,
    script_path TEXT,
    prompt TEXT NOT NULL,
    interval_seconds INT NOT NULL CHECK (interval_seconds >= 10),
    cron_expression TEXT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    status TEXT NOT NULL DEFAULT 'idle' CHECK (status IN ('idle', 'running', 'error')),
    last_run_at TIMESTAMPTZ,
    next_run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error TEXT,
    run_count INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cron_expression TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS model TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS provider TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS no_agent BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS script_path TEXT;
ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN ('heartbeat', 'interval', 'cron'));

-- The runner polls for due, enabled jobs by next_run_at.
CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs(next_run_at) WHERE enabled;
CREATE INDEX IF NOT EXISTS idx_jobs_session ON jobs(session_id);

-- ─── Persona Memories (Gap 3: dual-stream memory) ───────────────────────────

CREATE TABLE IF NOT EXISTS persona_memories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fact_id UUID REFERENCES memories(id) ON DELETE CASCADE,
    persona_id TEXT NOT NULL,
    interpretation TEXT NOT NULL,
    emotional_valence FLOAT NOT NULL DEFAULT 0.0 CHECK (emotional_valence >= -1.0 AND emotional_valence <= 1.0),
    emotional_arousal FLOAT NOT NULL DEFAULT 0.0 CHECK (emotional_arousal >= 0.0 AND emotional_arousal <= 1.0),
    confidence FLOAT NOT NULL DEFAULT 0.5 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_persona_memories_fact ON persona_memories(fact_id);
CREATE INDEX IF NOT EXISTS idx_persona_memories_persona ON persona_memories(persona_id);
CREATE INDEX IF NOT EXISTS idx_persona_memories_fact_persona_rank
    ON persona_memories(fact_id, persona_id, confidence DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_persona_memories_valence ON persona_memories(emotional_valence);

-- ─── LLM usage (Phase 7: durable accounting and budgets) ────────────────────

-- INTENTIONAL: session_id has NO foreign key to sessions(id). Usage rows must
-- survive session deletion so agent-wide budgets and operational totals do not
-- silently reset. No prompts, completions, or credentials live in this table.
-- Orphaned rows are acceptable: they represent real token consumption that
-- occurred during the session's lifetime.
CREATE TABLE IF NOT EXISTS llm_usage (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL,
    agent_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('reserved', 'complete', 'error')),
    reserved_tokens INT NOT NULL CHECK (reserved_tokens >= 0),
    accounted_tokens INT NOT NULL CHECK (accounted_tokens >= 0),
    prompt_tokens INT CHECK (prompt_tokens >= 0),
    completion_tokens INT CHECK (completion_tokens >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_session ON llm_usage(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_usage_agent ON llm_usage(agent_id, created_at DESC);
-- Composite index for UsageStore.summary(): WHERE session_id = $1 [FILTER status]
CREATE INDEX IF NOT EXISTS idx_llm_usage_session_status ON llm_usage(session_id, status);
-- Composite index for UsageStore.summary(): WHERE agent_id = $1 [FILTER status]
CREATE INDEX IF NOT EXISTS idx_llm_usage_agent_status ON llm_usage(agent_id, status);

-- ─── Agent Beliefs (Gap 2: Identity Model) ─────────────────────────────────

CREATE TABLE IF NOT EXISTS agent_beliefs (
    agent_id TEXT PRIMARY KEY,
    belief JSONB NOT NULL,
    version INT NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_beliefs_updated ON agent_beliefs(updated_at DESC);

CREATE TABLE IF NOT EXISTS agent_belief_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id TEXT NOT NULL,
    from_version INT NOT NULL,
    to_version INT NOT NULL,
    before_belief JSONB,
    proposed_belief JSONB NOT NULL,
    drift_score DOUBLE PRECISION NOT NULL,
    causal_memory_ids UUID[] NOT NULL DEFAULT '{}',
    contained BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_belief_history_agent
    ON agent_belief_history(agent_id, created_at DESC);

-- ─── Memory Provenance (Gap 2: Identity Propagation Defense) ───────────────

CREATE TABLE IF NOT EXISTS memory_provenance (
    memory_id UUID PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    source_agent TEXT NOT NULL,
    signature TEXT NOT NULL,
    parent_memory_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

DELETE FROM memory_provenance p WHERE NOT EXISTS (
    SELECT 1 FROM memories m WHERE m.id = p.memory_id
);
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'memory_provenance'::regclass
          AND confrelid = 'memories'::regclass AND contype = 'f'
    ) THEN
        ALTER TABLE memory_provenance ADD CONSTRAINT memory_provenance_memory_fk
            FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_memory_provenance_source ON memory_provenance(source_agent);
CREATE INDEX IF NOT EXISTS idx_memory_provenance_parent ON memory_provenance(parent_memory_id);

-- Cross-agent delivery receipts. A source can be delivered to each recipient
-- once; rejected and quarantined attempts remain visible for audit.
CREATE TABLE IF NOT EXISTS shared_memory_deliveries (
    source_memory_id UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    recipient_agent TEXT NOT NULL,
    target_memory_id UUID REFERENCES memories(id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK (status IN ('accepted', 'rejected', 'quarantined')),
    reason TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_memory_id, recipient_agent)
);
CREATE INDEX IF NOT EXISTS idx_shared_memory_deliveries_recipient
    ON shared_memory_deliveries(recipient_agent, created_at DESC);

-- Bounded post-turn learning. Only reviewed proposals can become skills.
-- Fingerprints avoid paying for and staging the same turn twice after retries.
CREATE TABLE IF NOT EXISTS learning_reviews (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    turn_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('reviewing', 'none', 'pending', 'approved', 'rejected', 'error')),
    name TEXT,
    description TEXT,
    triggers JSONB NOT NULL DEFAULT '[]',
    content TEXT,
    reason TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at TIMESTAMPTZ,
    UNIQUE (session_id, turn_hash)
);
CREATE INDEX IF NOT EXISTS idx_learning_reviews_agent
    ON learning_reviews(agent_id, created_at DESC);
