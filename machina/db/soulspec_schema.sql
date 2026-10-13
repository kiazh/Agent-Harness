-- Preserve authored persona structure without replacing existing agent definitions.
ALTER TABLE IF EXISTS agents ADD COLUMN IF NOT EXISTS soul_spec JSONB;
