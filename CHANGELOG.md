# Changelog

## 1.2.0 (2026-06-XX)

### Major changes
- **Multi-dim schema**: three vector columns (vector_768, vector_1024,
  vector_1536) all present, all nullable, all HNSW-indexed. Each row
  can have any subset populated. New writes go to the dim-matching
  column.
- **Runtime dim switching**: `hermes postgres-memory model-set --dim
  <768|1024|1536>` updates `agent_memory_settings.default_dim` and
  the embedder picks up the new config on the next call.
- **Per-dim model registry**: `agent_memory_models` table stores
  (dim, provider, model, api_key_env) per dim. Override any cell via
  the CLI.
- **Non-destructive migration**: sidecar columns (vector_*) are
  added, leaving any pre-existing `content_vector` intact. Old data
  can be migrated to the per-dim columns via migration 003, and the
  legacy column is dropped only on `finalize-cutover --yes`.

### Embedder
- New `_default_model_config_for_dim(dim)` factory and per-dim singleton
  registry (`get_embedder(dim)`).
- `SUPPORTED_DIMS = (768, 1024, 1536)`. Adding a new dim requires a
  code change + an ALTER TABLE migration + a registry row insertion.
- `_resolve_model_config(dim)` walks `sys.modules` to find the plugin's
  `_read_model_config_for_dim` (so tests can monkeypatch it regardless
  of import-name).
- `cache_dir` resolved from `HERMES_EMBED_CACHE_DIR_<dim>` / shared
  env var in the Embedder constructor (lets test fixtures and
  custom-cache-dir users skip the SQL registry).
- Per-dim embedder config: 768→ollama_local/nomic-embed-text,
  1024→kimi/bge_m3_embed, 1536→kimi/text-embedding-3-small.
- `reset_embedder(dim)` to drop a singleton (used by `model-set`).

### Plugin
- `_PostgresClient._default_dim` replaces `_live_column`. The plugin
  reads it from `agent_memory_settings.default_dim` at init.
- `add_memory()` writes to the dim-matching column
  (`_vector_column_for_dim(self._default_dim)`).
- `search_memories()` queries the dim-matching column. New `dim`
  parameter overrides the default for a single query.
- `update_memory()` re-embeds at the default dim and writes to the
  matching column.
- `count_by_dim()` returns `{768: N, 1024: N, 1536: N}` for status.
- `pg_status` now includes `default_dim`, `per_dim_embedded`, and
  per-dim embedder stats.
- New `pg_model_set` tool that mirrors the CLI.
- `add_memory` and `search_memories` raise `ValueError` for non-supported
  dims (no silent fallback to default).

### Migrations
- `000_grant_ddl_to_hermes.sql` — rename: was the old 000
- `001_add_per_dim_columns.sql` — adds vector_768, vector_1024,
  vector_1536 + agent_memory_settings + agent_memory_models. Wrapped
  in BEGIN/COMMIT (index build is separate).
- `002_hnsw_per_dim.sql` — builds HNSW on each per-dim column with
  CONCURRENTLY (no downtime).
- `003_migrate_legacy_content_vector.sql` — auto-detects legacy dim
  and copies data into the matching per-dim column.
- `004_drop_legacy_column.sql` — manual cutover, drops
  content_vector and its index. Irreversible.
- `sql/000_schema.sql` rewritten as the fresh-install superset.

### CLI
- `status` — now shows per-dim embeddings and per-dim embedder stats.
- `model-list` — print all (dim, provider, model, api_key_env) rows.
- `model-set --dim N --provider X --model Y` — switch and/or override.
- `backfill --dim N` — per-dim, parallel across dims.
- `preflight` — print ownership, settings, dim column presence, per-dim
  row counts, legacy column state.
- `finalize-cutover --yes` — drop legacy column (refuses if < 50% of
  rows have a populated per-dim column).
- `vector-column` — DEPRECATED in 1.2.0. Kept for backward compat;
  `--set v1` maps to `--dim 1536`, `--set v2` to `--dim 1024`.

### Tests
- 30 tests, all passing. Test cache uses tmp_path_factory so the
  user's real `~/.cache/hermes/embeddings/` is not affected.
- Embedder tests cover per-dim config, per-dim env overrides, API key
  resolution chain, fail-open, cache hit/miss, dim mismatch, per-dim
  singleton registry.
- Plugin tests cover per-dim column routing, placeholder/param drift
  guard, model-set tool, per-dim search override, fail-open behavior.

## 1.1.0 (2026-06-02)

### Added
- Pluggable embedder: `kimi` (default, free, BGE-M3 family), `ollama_local`,
  `ollama_cloud`, `noop`. Configured via `HERMES_EMBED_PROVIDER`.
- Real 1024-dim embeddings replacing the previous zero-vec placeholder.
- Hybrid search: FTS pre-filter + cosine re-rank on the live column.
- CLI subcommands: `status`, `vector-column`, `backfill`, `preflight`,
  `finalize-cutover`.
- Skill `hermes-memory-providers` v1.5.0.
- 24 tests covering embedder, integration, and pool hardening.

### Fixed
- Auto-detect the live column on init (`v1` legacy 1536, `v2` sidecar
  1024, `v2_named_v1` post-destructive-migration 1024).
- Search SQL no longer references `content_vector` from a CTE
  subquery — uses a JOIN against the base table.
- Embedder cache poisoning protection: zero-fallback vectors are never
  written to disk cache.
- Per-call fallback tracking in the embedder (was session-cumulative,
  caused `noop` provider to not cache its intentional zeros).
- `_PostgresClient` no longer serializes all DB ops behind `self._lock`.
