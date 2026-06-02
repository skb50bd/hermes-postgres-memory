-- 003_migrate_legacy_content_vector.sql
--
-- Copy data from the legacy `content_vector` column (whatever its dim
-- is) into the matching per-dim column (vector_768 / vector_1024 /
-- vector_1536). The legacy column is left intact for now — you can
-- drop it later with 004_drop_legacy_column.sql.
--
-- Auto-detect the legacy column's dim from pg_attribute and copy
-- only rows that have a non-null, non-zero legacy vector.
--
-- This is a one-time data migration. It does NOT rebuild the HNSW
-- index — that's already done in 002_hnsw_per_dim.sql on the new
-- per-dim columns. After this migration, the new per-dim columns
-- will start populating and the HNSW index will fill up.
--
-- Idempotent: ON CONFLICT-free, just updates rows. Re-running is
-- safe (it overwrites with the same data).
--
-- IMPORTANT: This file is plain SQL, no transaction wrapper. The
-- statements are individual UPDATEs, not a single bulk copy, so
-- partial failures are recoverable. For very large tables, batch
-- this with a LIMIT/OFFSET loop in the application layer.

DO $$
DECLARE
    legacy_dim int;
    target_col text;
    rows_copied int;
BEGIN
    -- Detect the legacy column's dim.
    SELECT atttypmod INTO legacy_dim
    FROM pg_attribute
    WHERE attrelid = 'agent_memory'::regclass
      AND attname = 'content_vector';

    IF legacy_dim IS NULL OR legacy_dim <= 0 THEN
        RAISE NOTICE 'No content_vector column (or it has no dim); skipping migration.';
        RETURN;
    END IF;

    -- Pick the right target column.
    IF legacy_dim = 768 THEN
        target_col := 'vector_768';
    ELSIF legacy_dim = 1024 THEN
        target_col := 'vector_1024';
    ELSIF legacy_dim = 1536 THEN
        target_col := 'vector_1536';
    ELSE
        RAISE NOTICE 'content_vector is at %, which the plugin does not natively support (only 768/1024/1536). Skipping auto-migration. Run a custom copy.', legacy_dim;
        RETURN;
    END IF;

    RAISE NOTICE 'Migrating content_vector (dim=%) → %', legacy_dim, target_col;

    -- Copy only rows where the source is non-null and the target
    -- is currently null (don't overwrite newer per-dim embeddings).
    EXECUTE format(
        'UPDATE agent_memory
            SET %I = content_vector
          WHERE content_vector IS NOT NULL
            AND %I IS NULL',
        target_col, target_col
    );
    GET DIAGNOSTICS rows_copied = ROW_COUNT;
    RAISE NOTICE 'Copied % rows from content_vector → %', rows_copied, target_col;

    -- If the user has a non-default-dim legacy column, suggest
    -- switching default_dim. For example, if legacy was 1536 and
    -- the user's current default is 1024, they may want to switch.
    IF legacy_dim != (
        SELECT (value #>> '{}')::int
        FROM agent_memory_settings
        WHERE key = 'default_dim'
    ) THEN
        RAISE NOTICE 'Legacy dim (%) does not match current default_dim. Run `hermes postgres-memory model-set --dim %` if you want new writes to also use this dim.', legacy_dim, legacy_dim;
    END IF;
END
$$;
