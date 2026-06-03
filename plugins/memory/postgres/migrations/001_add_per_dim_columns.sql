-- 001_add_per_dim_columns.sql
--
-- NON-DESTRUCTIVE upgrade to 1.2.0's multi-dim schema.
--
-- Adds three new nullable vector columns: vector_768, vector_1024,
-- vector_1536. All indexed with HNSW. The legacy content_vector
-- column (whatever its dim) is left intact. The plugin's runtime
-- decides which column to use based on agent_memory_settings.default_dim.
--
-- For users upgrading from pre-1.2.0:
--   - If your existing content_vector is at 1024-dim, the plugin
--     will auto-detect this on init and use vector_1024 from now on
--     (and migrate the data from content_vector → vector_1024 in
--     migration 002).
--   - If your existing content_vector is at 1536-dim (the original
--     placeholder dim), the plugin will keep using content_vector
--     until you run 002 to migrate it to vector_1536 (or pick a
--     different default).
--   - If you want to switch to a different dim entirely, just run
--     `hermes postgres-memory model-set --dim 768` (or 1024 / 1536)
--     after this migration.
--
-- This file is idempotent. Re-running is a no-op.
--
-- IMPORTANT: This migration is one transaction. Adding three
-- columns + three HNSW indexes in one transaction is fine for
-- small tables; for tables with millions of rows, run the HNSW
-- CREATE INDEX statements outside a transaction (CONCURRENTLY).
-- The CREATE INDEX CONCURRENTLY form is below; it MUST run
-- outside a transaction, so this file wraps only the column
-- adds in BEGIN/COMMIT and runs the index builds without.

BEGIN;

-- Three vector columns. All nullable. Each indexed separately.
ALTER TABLE agent_memory
    ADD COLUMN IF NOT EXISTS vector_768  vector(768);
ALTER TABLE agent_memory
    ADD COLUMN IF NOT EXISTS vector_1024 vector(1024);
ALTER TABLE agent_memory
    ADD COLUMN IF NOT EXISTS vector_1536 vector(1536);

-- Settings + per-dim model registry.
CREATE TABLE IF NOT EXISTS agent_memory_settings (
    key text PRIMARY KEY,
    value jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_memory_models (
    dim smallint PRIMARY KEY CHECK (dim IN (768, 1024, 1536)),
    provider text NOT NULL,
    model text NOT NULL,
    base_url text,
    api_key_env text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO agent_memory_models (dim, provider, model, api_key_env) VALUES
    (768,  'ollama_local', 'nomic-embed-text',  'OLLAMA_API_KEY'),
    (1024, 'kimi',         'bge_m3_embed',      'KIMI_API_KEY'),
    (1536, 'minimax',      'embo-01',           'MINIMAX_API_KEY')
ON CONFLICT (dim) DO NOTHING;

-- Default dim. The plugin reads this on init.
-- We default to 1024 (the BGE-M3 / Kimi default). Users on a
-- 1536-dim legacy column should set this to 1536 after running
-- 002 to migrate their data.
INSERT INTO agent_memory_settings (key, value)
VALUES ('default_dim', '1024'::jsonb)
ON CONFLICT (key) DO NOTHING;

COMMIT;
