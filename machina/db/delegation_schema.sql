-- Additive, idempotent runtime upgrade for delegation authority and accounting.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS parent_session_id UUID REFERENCES sessions(id) ON DELETE SET NULL;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS delegation_authority JSONB NOT NULL DEFAULT '{}';
-- Immutable ancestry survives deleting an intermediate or root session. The
-- live parent FK is only navigation; these snapshots are budget authority.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS ancestor_session_ids UUID[] NOT NULL DEFAULT '{}';
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS ancestor_agent_ids TEXT[] NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);

-- Backfill existing linked delegations once. Preserve snapshots already made by
-- current code, including references to ancestors that have since been deleted.
WITH RECURSIVE ancestry AS (
    SELECT id AS child_id, parent_session_id AS parent_id, ARRAY[id] AS path,
           ARRAY[]::UUID[] AS session_ids, ARRAY[]::TEXT[] AS agent_ids
    FROM sessions WHERE parent_session_id IS NOT NULL
      AND cardinality(ancestor_session_ids) = 0
    UNION ALL
    SELECT ancestry.child_id, parent.parent_session_id, ancestry.path || parent.id,
           ancestry.session_ids || parent.id, ancestry.agent_ids || parent.agent_id
    FROM ancestry JOIN sessions parent ON parent.id = ancestry.parent_id
    WHERE NOT parent.id = ANY(ancestry.path)
), deepest AS (
    SELECT DISTINCT ON (child_id) child_id, session_ids, agent_ids
    FROM ancestry ORDER BY child_id, cardinality(session_ids) DESC
)
UPDATE sessions SET ancestor_session_ids = deepest.session_ids,
                    ancestor_agent_ids = deepest.agent_ids
FROM deepest WHERE sessions.id = deepest.child_id;

ALTER TABLE agent_messages DROP CONSTRAINT IF EXISTS agent_messages_status_check;
ALTER TABLE agent_messages ADD CONSTRAINT agent_messages_status_check
    CHECK (status IN ('pending', 'complete', 'error', 'cancelled', 'waiting_approval'));

-- Keep one attributed provider row; snapshot charged scopes so usage survives
-- deleting any session and totals never count a wrapper reservation twice.
ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS ancestor_session_ids UUID[] NOT NULL DEFAULT '{}';
ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS ancestor_agent_ids TEXT[] NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS idx_llm_usage_ancestor_sessions ON llm_usage USING GIN(ancestor_session_ids);
CREATE INDEX IF NOT EXISTS idx_llm_usage_ancestor_agents ON llm_usage USING GIN(ancestor_agent_ids);

-- Attribute historical calls when their linked session ancestry is available.
UPDATE llm_usage SET ancestor_session_ids = sessions.ancestor_session_ids,
                     ancestor_agent_ids = sessions.ancestor_agent_ids
FROM sessions WHERE llm_usage.session_id = sessions.id
    AND cardinality(llm_usage.ancestor_session_ids) = 0
    AND cardinality(sessions.ancestor_session_ids) > 0;
