-- 000_schema.sql
--
-- Create the agent_memory and agent_memory_settings tables for a fresh
-- install. Run this once before configuring Hermes Agent to use the
-- postgres memory provider.
--
-- Requires the `vector` extension. Adjust the password / role if
-- you're not using the default `hermes` user.
--
-- Idempotent: every CREATE has IF NOT EXISTS.

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
    content_vector vector(1024),
    source_session uuid,
    confidence smallint DEFAULT 80,
    is_active boolean DEFAULT true,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now(),
    expires_at timestamptz,
    tags text[] DEFAULT '{}',
    metadata jsonb DEFAULT '{}'
);

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

CREATE TABLE IF NOT EXISTS agent_memory_settings (
    key text PRIMARY KEY,
    value jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Default: live vector column is the only column (fresh install).
-- The plugin reads this on init; flip to 'v2' if you migrate from a
-- legacy 1536-dim schema with a sidecar column.
INSERT INTO agent_memory_settings (key, value)
VALUES ('live_vector_column', 'v2'::jsonb)
ON CONFLICT (key) DO NOTHING;
