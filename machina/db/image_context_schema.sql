-- Latest image context for follow-up turns, independent of text
-- compaction and tool churn. MessagePack stores decoded image bytes:
-- 20 MiB plus 8 KiB bounded UTF-8 metadata. Applied to fresh installs via
-- initialize_schema and to existing installations via
-- initialize_runtime_schema (schema.sql alone never runs there).
CREATE TABLE IF NOT EXISTS session_image_context (
    session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    images_msgpack BYTEA NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, agent_id)
);

-- CREATE TABLE IF NOT EXISTS cannot update a previously installed bound.
ALTER TABLE session_image_context
    DROP CONSTRAINT IF EXISTS session_image_context_images_msgpack_check;
ALTER TABLE session_image_context
    ADD CONSTRAINT session_image_context_images_msgpack_check
    CHECK (octet_length(images_msgpack) <= 20979712);
