-- 001_add_v2_column.sql
--
-- NON-DESTRUCTIVE migration from a 1536-dim content_vector (OpenAI
-- text-embedding-3-small placeholder, never actually used) to a 1024-dim
-- BGE-M3 vector. This migration ADDS a sidecar column `content_vector_v2`
-- and leaves the original `content_vector` (1536) intact.
--
-- After this migration:
--   - Old 1536-dim column is still queryable (until 005_drop_v1_column.sql).
--   - New 1024-dim column is empty; backfill it with scripts/backfill_embeddings.py.
--   - Plugin reads from v2 once it sees any non-zero vector in v2 (i.e. the
--     backfill is partially or fully complete). It never blends v1 and v2
--     because they come from different embedding spaces (different models
--     with different dim). The two columns are mutually exclusive in
--     semantic terms: a row is "in v2" if v2 is non-null, "in v1" otherwise.
--
-- This file is idempotent: re-running is a no-op (the ADD COLUMN IF NOT
-- EXISTS is a no-op when the column already exists).
--
-- After this migration, ALSO create a one-row settings table that records
-- the current "live column." This lets the plugin determine runtime read
-- behavior without inspecting the column dim.

BEGIN;

-- 1) Add the v2 column. Nullable: we backfill in a separate pass.
ALTER TABLE agent_memory
    ADD COLUMN IF NOT EXISTS content_vector_v2 vector(1024);

-- 2) Settings table: one row keyed by `key` text, holds JSONB value.
--    This is a generic "plugin runtime config" table so future migrations
--    can record their own state without schema changes.
CREATE TABLE IF NOT EXISTS agent_memory_settings (
    key text PRIMARY KEY,
    value jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- 3) Record the live vector column. NULL = use v1; 'v2' = use v2.
--    We default to NULL (legacy) so a fresh upgrade doesn't surprise the
--    plugin by switching to an empty column before backfill runs.
INSERT INTO agent_memory_settings (key, value)
VALUES ('live_vector_column', 'v1'::jsonb)
ON CONFLICT (key) DO NOTHING;

COMMIT;
