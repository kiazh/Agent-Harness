-- Preserve authored persona structure without replacing existing agent definitions.
ALTER TABLE IF EXISTS agents ADD COLUMN IF NOT EXISTS soul_spec JSONB;
-- R4-025: max_iterations was dead configuration (never enforced). The CREATE
-- TABLE in schema.sql no longer defines it; this is the runtime-upgrade path
-- for pre-existing databases (applied by initialize_schema and
-- initialize_runtime_schema). Idempotent.
ALTER TABLE IF EXISTS agents DROP COLUMN IF EXISTS max_iterations;
