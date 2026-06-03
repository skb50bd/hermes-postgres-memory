# Changelog

## 1.4.0 (2026-06-03)

### Major changes
- **`minimax` embedding provider** (new). 1536-dim default now points
  at MiniMax's `embo-01` model via the OpenAI-compatible endpoint at
  `https://api.minimax.io/v1/embeddings`. Auth: `MINIMAX_API_KEY`. Same
  HTTP contract as `kimi` — `POST /v1/embeddings` with `Authorization:
  Bearer $MINIMAX_API_KEY` and `{"model": "embo-01", "input": "..."}`,
  response is `{"data": [{"embedding": [...]}]}`. The
  `_embed_openai_compat` helper handles the wire format, so this is a
  ~20-line change in `embedder.py` plus provider dispatch.
- **The 1536-dim SQL registry row is now `minimax/embo-01`** (was
  `kimi/text-embedding-3-small`, which never actually worked — Kimi
  always returns 1024-dim regardless of the requested model name, so
  the 1536-dim column was previously being filled with 1024-dim
  vectors in 1536 slots. The plugin's dim check catches this on embed,
  but it meant a real 1536-dim flow wasn't possible). The 1536 column
  is now genuinely populated with real 1536-dim vectors via the
  MiniMax endpoint.
- **CLI fallback for missing registry rows is dim-aware.** `hermes
  postgres-memory model-set --dim <dim>` no longer hardcodes the
  kimi/bge_m3_embed + KIMI_API_KEY fallback when inserting a missing
  row — it picks the right (provider, model, api_key_env) tuple per
  dim, aligned with `sql/000_schema.sql`.

### Embedder
- New `_embed_live` branch for `provider == "minimax"`, routing to
  the OpenAI-compatible helper with `default_base="https://api.minimax.io/v1"`.
- `_resolve_api_key(dim, "minimax")` recognizes `MINIMAX_API_KEY` in
  the same priority chain as `KIMI_API_KEY` / `OLLAMA_API_KEY` (per-dim
  explicit > shared `HERMES_EMBED_API_KEY` > provider-specific).
- `_default_model_config_for_dim(1536)` now returns `minimax` / `embo-01`
  by default; the 1024 and 768 defaults are unchanged.
- The plugin's `_read_model_config_for_dim` (which reads the SQL
  registry) now also looks up `MINIMAX_API_KEY` when the registered
  provider is `minimax`. Verified end-to-end against the live DB.

### SQL
- `sql/000_schema.sql` and `migrations/001_add_per_dim_columns.sql`:
  1536 registry row updated to `(1536, 'minimax', 'embo-01',
  'MINIMAX_API_KEY')`. The misleading "kimi returns 1024-dim
  regardless of model" caveat is removed.

### Plugin
- `pg_model_set` tool description: 1536 now reads "embo-01 (MiniMax)"
  instead of "OpenAI text-embedding-3-small".
- `cli.py` example commands reference `minimax/embo-01` for 1536.
- `embedder.py` docstring: provider list now includes `minimax` and
  documents its endpoint.

### Tests
- 5 new tests for the `minimax` provider:
  - `test_minimax_default_per_dim_config` — default config returns
    `minimax`/`embo-01`/`MINIMAX_API_KEY`
  - `test_minimax_api_key_resolution_chain` — per-dim > shared >
    provider-specific, with `KIMI_API_KEY` not leaking into the
    minimax provider
  - `test_minimax_live_call_hits_api_minimax_io` — POST goes to
    `https://api.minimax.io/v1/embeddings` with the right auth + body
  - `test_minimax_uses_configured_base_url_override` — `HERMES_EMBED_BASE_URL_1536`
    overrides the default
  - `test_minimax_dim_mismatch_surfaces_as_error` — wrong-dim
    response from the provider surfaces as `EmbeddingError` when
    `fail_open=False`
- Existing `test_default_per_dim_models` updated: 1536 row now
  expects `minimax`/`embo-01` (and the fixture clears/reads
  `MINIMAX_API_KEY`).
- **35/35 pass** (was 30).

### Live verification
- Updated the live `agent_memory_models` row for dim 1536 from
  `kimi/text-embedding-3-small` → `minimax/embo-01`. Verified
  end-to-end: `_read_model_config_for_dim(1536)` against the live
  database returns
  `{'dim': 1536, 'provider': 'minimax', 'model': 'embo-01',
    'api_key': 'sk-cp-...5bHI', 'base_url': ''}` — the real
  `MINIMAX_API_KEY` from the user's `.env` is plumbed through.

### Docs
- `README.md`, `SKILL.md`, `onboarding-checklist.md`,
  `embedding-provider-landscape.md` all updated to reflect
  `minimax/embo-01` as the 1536-dim default. The misleading
  "kimi returns 1024-dim regardless of model name" note is gone.

## 1.3.1 (2026-06-03)

### Fixed
- **`diagnose.sh` HERMES_HOME resolution**: the script now auto-resolves
  `HERMES_HOME` from the parent (`/home/u/.hermes`, which the agent
  runtime exports) to the checkout (`/home/u/.hermes/hermes-agent`).
  Detection uses the presence of `run_agent.py` or `AGENTS.md` at the
  resolved path. The script used to report a false-positive
  "not a hermes-agent checkout" failure when the env var pointed at the
  parent. Also accepts both the old flat `plugins/memory/` layout and
  the current nested `plugins/memory/postgres/` layout.
- **`bootstrap.sh` REPO_DIR math**: was resolving to
  `plugins/memory/` instead of the repo root (off by one `..` after
  the repo restructured to `plugins/memory/postgres/`). Fixed to
  `cd "$PLUGIN_DIR/../../.."` and annotated. Symptom was a silent
  failure to find `$REPO_DIR/install.sh` (the file existed; the path
  just didn't).
- **`install.sh` HERMES_HOME resolution**: now matches `diagnose.sh` —
  accepts the parent-dir export and walks down to the checkout. Same
  detection heuristic.
- **`uninstall.sh` HERMES_HOME + REPO_DIR**: same auto-resolve + same
  REPO_DIR math fix as the other two scripts, for consistency.
- **`diagnose.sh` next-steps hint**: the "ready to install" footer
  used to print a bare `./install.sh` (relative to cwd, so useless if
  the user ran diagnose from elsewhere). Now resolves to an absolute
  path: `$SCRIPT_DIR/../../../../install.sh` walked through `pwd -P`.
- **`bootstrap.sh` re-run hint**: the "if anything looks off, re-run"
  footer used to print `./diagnose.sh`. Now prints the absolute path
  via `$SCRIPT_DIR/diagnose.sh`.

### Live verification
- `HERMES_HOME=/home/u/.hermes` (parent): **17/17** ✓
- `HERMES_HOME=/home/u/.hermes/hermes-agent` (explicit): **17/17** ✓
- `HERMES_HOME` unset (default): **17/17** ✓
- `HERMES_HOME=/nonexistent`: **16/17** (fails the `HERMES_HOME exists`
  check correctly — no false positive).

## 1.3.0 (2026-06-XX)

### Major changes
- **One-shot installer**: `plugins/memory/postgres/scripts/bootstrap.sh`
  walks the entire first-time install end-to-end. Interactive by default,
  `--non-interactive` for scripted deploys. Handles: psql availability,
  psycopg2 install, hermes-agent checkout detection, superuser connection
  test, role + database + extension creation, schema install, .env
  patching, config.yaml patching, plugin + skill file copy, final
  preflight. Idempotent.
- **Preflight checker**: `plugins/memory/postgres/scripts/diagnose.sh`
  walks 16 prerequisites and prints a pass/fail table. Re-runnable.
  `--json` for automation. Used internally by `bootstrap.sh` and
  `install.sh` to refuse to proceed on a broken environment.
- **Clean uninstaller**: `plugins/memory/postgres/scripts/uninstall.sh`
  is the inverse of `bootstrap.sh`. Modes: `--plugin` (files only),
  `--db` (drop tables), `--all` (both), plus `--role` and `--database`.
  Asks before each destructive step.
- **First-class database bootstrap SQL**:
  `sql/000_create_database_and_role.sql` is the **only** file in the
  plugin that requires superuser privileges. Creates the role, the
  database, the `vector` extension, transfers ownership of the `public`
  schema to the new role. Accepts GUCs (`-v dbname=`, `-v rolename=`,
  `-v pw=`, `-v connlimit=`, `-v allow_weak_pw=`) so everything is
  customizable. Idempotent.
- **Onboarding-first skill**: `skills/devops/hermes-postgres-memory/SKILL.md`
  v1.6.0 — onboarding is now the first half of the skill, with a
  canonical 5-step flow, a pre-validation workflow, a "what to tell the
  user upfront" template, and explicit failure modes. Diagnostics
  moved to the second half.
- **Two new reference docs**:
  - `references/onboarding-checklist.md` — the canonical pre-flight
    checklist, with one-liner probes for every prerequisite
  - `references/database-bootstrap.md` — what the database needs, why
    each requirement exists, the password-piping caveat for `psql`,
    the GUCs the SQL script accepts
- **Bootstrap messages**: `bootstrap-message.txt` (full walkthrough
  for sending to another agent instance) and `bootstrap-message-short.txt`
  (the 5-command TL;DR).

### install.sh
- Now runs `diagnose.sh` as a preflight by default; refuses to install
  if any check fails (use `--yes` to override). Also supports
  `--diagnose` to run the preflight without installing.
- Print a clear "next steps" panel at the end pointing at
  `bootstrap.sh` for first-time installs.

### Live verification
- Run the new `diagnose.sh` against a freshly-1.2.0'd live DB:
  16/17 pass; the one failure (`public schema owner = pg_database_owner`)
  is a real pre-existing finding — the schema is unowned. The diagnose
  correctly reports it and tells the user the fix:
  `ALTER SCHEMA public OWNER TO hermes;`
  (run as superuser). Tracked as a follow-up.

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
