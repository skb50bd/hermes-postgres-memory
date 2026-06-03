-- 000_schema.sql
--
-- Create the agent_memory and agent_memory_settings tables for a fresh
-- install of the postgres memory provider.
--
-- The plugin supports three embedding dimensions out of the box:
--   - 768  (nomic-embed-text / bge-small)
--   - 1024 (BGE-M3 / bge-m3 — Kimi default)
--   - 1536 (MiniMax embo-01, or OpenAI text-embedding-3-small with override)
--
-- All three vector columns are present, all nullable, all indexed.
-- A row can have any subset of them populated. The plugin writes to
-- whichever column matches the current default_dim, and reads from
-- it on search. To switch default dim, change the agent_memory_settings
-- row and (optionally) backfill the new column.
--
-- This file is idempotent (every CREATE has IF NOT EXISTS).
--
-- Requires the `vector` extension. Adjust role / database if you're
-- not using the default `hermes` user / `hermes` DB.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS memory_categories (
    id smallint PRIMARY KEY,
    name varchar(50) UNIQUE NOT NULL
);

INSERT INTO memory_categories (id, name) VALUES
    (1, 'user_preference'),
    (2, 'user_profile'),
    (3, 'environment'),
    (4, 'project_convention'),
    (5, 'tool_quirk'),
    (6, 'lesson_learned'),
    (7, 'workflow'),
    (8, 'fact')
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS agent_memory (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    category_id smallint REFERENCES memory_categories(id),
    target varchar(20) DEFAULT 'memory',
    content text NOT NULL,
    -- Per-dim vector columns. All nullable, all indexed.
    -- A row can have any subset populated; the plugin reads/writes
    -- the column matching the configured default_dim.
    vector_768   vector(768),
    vector_1024  vector(1024),
    vector_1536  vector(1536),
    -- Legacy column from pre-1.2.0 schemas. Nullable. Read by the
    -- plugin if present and matches the configured dim; otherwise
    -- ignored. See migrations/002_migrate_legacy_content_vector.sql
    -- to migrate data out of this column.
    content_vector vector,
    source_session uuid,
    confidence smallint DEFAULT 80,
    is_active boolean DEFAULT true,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now(),
    expires_at timestamptz,
    tags text[] DEFAULT '{}',
    metadata jsonb DEFAULT '{}'
);

-- HNSW index on each supported dim. CONCURRENTLY-safe; safe to run
-- on an empty column. Re-run if you want to change m / ef_construction.
CREATE INDEX IF NOT EXISTS idx_memory_vector_768_hnsw
    ON agent_memory USING hnsw (vector_768 vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_memory_vector_1024_hnsw
    ON agent_memory USING hnsw (vector_1024 vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_memory_vector_1536_hnsw
    ON agent_memory USING hnsw (vector_1536 vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
-- Legacy index, if migrating from pre-1.2.0.
CREATE INDEX IF NOT EXISTS idx_memory_vector_hnsw
    ON agent_memory USING hnsw (content_vector vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS idx_memory_fts
    ON agent_memory USING gin (to_tsvector('english', content));
CREATE INDEX IF NOT EXISTS idx_memory_target ON agent_memory (target);
CREATE INDEX IF NOT EXISTS idx_memory_category ON agent_memory (category_id);
CREATE INDEX IF NOT EXISTS idx_memory_active ON agent_memory (is_active) WHERE is_active = true;
CREATE INDEX IF NOT EXISTS idx_memory_tags ON agent_memory USING gin (tags);
CREATE INDEX IF NOT EXISTS idx_memory_metadata ON agent_memory USING gin (metadata jsonb_path_ops);
CREATE INDEX IF NOT EXISTS idx_memory_created ON agent_memory (created_at DESC);

-- Settings table: one row per config key. The plugin reads from
-- here on init. The CLI's `vector-column` and `model-set` commands
-- write to it.
CREATE TABLE IF NOT EXISTS agent_memory_settings (
    key text PRIMARY KEY,
    value jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Per-dim model registry. Tells the embedder which provider+model
-- to use for each dim. Override any of these via the CLI:
--   hermes postgres-memory model-set --dim 768 --provider ollama_local --model nomic-embed-text
CREATE TABLE IF NOT EXISTS agent_memory_models (
    dim smallint PRIMARY KEY CHECK (dim IN (768, 1024, 1536)),
    provider text NOT NULL,
    model text NOT NULL,
    base_url text,
    api_key_env text,    -- name of the env var holding the API key (e.g. KIMI_API_KEY, OLLAMA_API_KEY)
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO agent_memory_models (dim, provider, model, api_key_env) VALUES
    (768,  'ollama_local', 'nomic-embed-text', 'OLLAMA_API_KEY'),
    (1024, 'kimi',         'bge_m3_embed',     'KIMI_API_KEY'),
    (1536, 'minimax',      'embo-01',          'MINIMAX_API_KEY')
ON CONFLICT (dim) DO NOTHING;
-- Notes:
--   1536: default points at minimax/embo-01 (https://api.minimax.io/v1).
--         Set MINIMAX_API_KEY in ~/.hermes/.env to use the default.
--   If you have an OpenAI key and want their text-embedding-3-small
--   instead, run:
--     hermes postgres-memory model-set --dim 1536 --provider openai --model text-embedding-3-small --api-key-env OPENAI_API_KEY
--   (Requires the openai provider to be wired in — currently the
--   embedder supports kimi / minimax / ollama_local / ollama_cloud / noop.)

-- The default dim the plugin writes to and searches on. Switch
-- with `hermes postgres-memory model-set --dim <768|1024|1536>`.
INSERT INTO agent_memory_settings (key, value)
VALUES ('default_dim', '1024'::jsonb)
ON CONFLICT (key) DO NOTHING;
