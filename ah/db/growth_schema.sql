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

-- Bounded runtime self-improvement, separate from model weights and tool authority.
CREATE TABLE IF NOT EXISTS rsi_profiles (
    agent_id TEXT PRIMARY KEY REFERENCES agent_growth(agent_id) ON DELETE CASCADE,
    revision BIGINT NOT NULL DEFAULT 0,
    stable_revision BIGINT NOT NULL DEFAULT 0,
    trial_revision BIGINT,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE IF NOT EXISTS rsi_revisions (
    agent_id TEXT NOT NULL REFERENCES agent_growth(agent_id) ON DELETE CASCADE,
    revision BIGINT NOT NULL,
    notes JSONB NOT NULL,
    policy JSONB NOT NULL,
    reason TEXT NOT NULL,
    policy_episodes JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL CHECK (status IN ('stable', 'trial', 'rolled_back', 'superseded')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, revision)
);
CREATE TABLE IF NOT EXISTS rsi_episodes (
    id UUID PRIMARY KEY REFERENCES growth_episodes(id) ON DELETE CASCADE,
    revision BIGINT NOT NULL,
    domain TEXT NOT NULL,
    strategy TEXT NOT NULL,
    skills JSONB NOT NULL DEFAULT '[]'::jsonb,
    tools JSONB NOT NULL DEFAULT '[]'::jsonb,
    tokens BIGINT NOT NULL DEFAULT 0,
    iterations INTEGER NOT NULL DEFAULT 0,
    automatic_reward DOUBLE PRECISION CHECK (automatic_reward BETWEEN -1 AND 1),
    policy_delta JSONB NOT NULL DEFAULT '{}'::jsonb
);
ALTER TABLE rsi_revisions ADD COLUMN IF NOT EXISTS policy_episodes JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE rsi_episodes ADD COLUMN IF NOT EXISTS policy_delta JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE rsi_episodes ADD COLUMN IF NOT EXISTS policy_active BOOLEAN NOT NULL DEFAULT true;
