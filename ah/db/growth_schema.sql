-- Persistent learned context and bounded feedback trajectories. Core identity stays separate.
CREATE TABLE IF NOT EXISTS agent_growth (
    agent_id TEXT PRIMARY KEY,
    notes JSONB NOT NULL DEFAULT '{}'::jsonb,
    policy JSONB NOT NULL,
    interactions BIGINT NOT NULL DEFAULT 0,
    feedback_count BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS growth_episodes (
    id UUID PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agent_growth(agent_id) ON DELETE CASCADE,
    session_id UUID,
    action TEXT NOT NULL CHECK (action IN ('retrieve', 'noop')),
    features JSONB NOT NULL,
    probability DOUBLE PRECISION NOT NULL CHECK (probability > 0 AND probability <= 1),
    completed BOOLEAN NOT NULL DEFAULT false,
    reward DOUBLE PRECISION CHECK (reward >= -1 AND reward <= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS growth_episodes_owner_time ON growth_episodes(agent_id, created_at DESC);
