---
name: hermes-postgres-kanban
description: "Use the hermes-postgres-kanban plugin for kanban operations. Replaces the SQLite boards at ~/.hermes/kanban/boards/*/kanban.db with a Postgres-backed hermes_kanban schema. Race-free claim, FTS, hierarchical tenants, NOTIFY events for live dashboards."
version: 0.1.0
---

# hermes-postgres-kanban

The PostgreSQL-backed kanban storage for Hermes Agent. Replaces the
old SQLite boards (`~/.hermes/kanban/boards/*/kanban.db`) with a
`hermes_kanban` schema in the same profile database that holds
agent_memory, hermes_wiki, hermes_journal, hermes_skills, and
hermes_metrics.

## When to use this skill

Use this skill when:
- The user asks to **add a kanban task** (or list, claim, complete, fail, comment on, link, unlink, archive, delete).
- A **dispatcher / worker / agent** is claiming tasks — always use the SKIP LOCKED claim path, not the old `claim_lock` column.
- The user asks to **migrate from SQLite to Postgres** — `hermes postgres-kanban migrate`.
- The user asks for **task stats** — `hermes postgres-kanban status`.
- The dashboard plugin needs to broadcast task events — the plugin fires `NOTIFY hermes_kanban_event` on every write.

## When NOT to use this skill

- **For memory / journal / skills / wiki / metrics** operations, use their respective plugins (or call `plugins.<name>.postgres` directly).
- **For atomic file storage** (PDFs, images, large blobs) — use `hermes_memory.attachments` or a real object store; the kanban only stores the metadata.

## Architecture

```
~/.hermes/kanban/boards/<slug>/kanban.db   ← OLD, SQLite, deprecated
                ↓ migrated by
plugins/kanban/postgres/migrate.py         ← run once: `hermes postgres-kanban migrate`
                ↓
hermes_kanban.tasks (PG)                   ← canonical store, per-profile DB
hermes_kanban.task_runs, task_events, task_links, task_comments,
task_attachments, tags, task_tags, notify_subs, tenants
```

The runtime can use either:
- **The thin shim** at `hermes_cli.kanban_db.py` (drop-in for old SQLite callers)
- **The plugin directly** at `plugins.kanban.postgres` (cleaner, new code should use this)

## Public API (canonical — use this for new code)

```python
from plugins.kanban.postgres import (
    # CRUD
    create_task, get_task, list_tasks, update_task, delete_task,
    list_tenants, upsert_tenant,

    # Claim lifecycle (SKIP LOCKED)
    claim_next, heartbeat_claim, complete_task, fail_task,

    # Comments / events / runs
    list_comments, add_comment, list_events, list_runs,
    start_run, end_run,

    # Links
    link_tasks, unlink_tasks, list_parents, list_children,

    # Attachments (metadata only)
    add_attachment, list_attachments,

    # Notify
    subscribe, unsubscribe,

    # House-keeping
    stats, recompute_ready,
)
```

## CLI

```bash
hermes postgres-kanban status     # show provider status as JSON
hermes postgres-kanban preflight  # schema/readiness checks
hermes postgres-kanban migrate    # SQLite → PG (idempotent)
```

## Race-free claim pattern

```python
task = claim_next(assignee="worker-1", worker_pid=os.getpid())
if task is None:
    return  # no work
try:
    result = do_work(task)
    complete_task(task["id"], result, worker_pid=os.getpid())
except Exception as exc:
    fail_task(task["id"], str(exc), worker_pid=os.getpid(), requeue=True)
```

`SKIP LOCKED` means multiple workers can call `claim_next()` concurrently
and never double-claim — each one gets a different task (or None if
nothing's ready).

## Tenants (replaces boards)

A "board" in the old SQLite world is now a row in `hermes_kanban.tenants`.
The first task to reference a new tenant auto-creates the row. The
dashboard groups tasks by tenant.

## NOTIFY for live dashboards

Every write fires `NOTIFY hermes_kanban_event '<json>'` so dashboard
WebSockets can broadcast without polling. Channel name is
`hermes_kanban_event`; payload is `{"task_id": ..., "kind": ..., "actor": ...}`.

## Configuration

- `PG_MEM_DB_CONN_STR` — same DSN the memory plugin uses (kanban reuses it)
- `HERMES_KANBAN_SCHEMA` — schema name override (default `hermes_kanban`)
- `HERMES_POSTGRES_POOL_MIN` / `HERMES_POSTGRES_POOL_MAX` — pool sizing

## Migration from SQLite

```bash
# 1. Make sure hermes_template is in your PG (already done by hermes-memory)
docker exec <pg-container> /usr/local/bin/hermes-init.sh

# 2. Set PG_MEM_DB_CONN_STR in ~/.hermes/.env
echo 'PG_MEM_DB_CONN_STR=postgresql://...' >> ~/.hermes/.env

# 3. Dry-run first
hermes postgres-kanban migrate --dry-run

# 4. Real run (idempotent; archives SQLite to kanban.db.migrated.<ts>)
hermes postgres-kanban migrate

# 5. Verify
hermes postgres-kanban status
hermes postgres-kanban preflight
```

The migration is keyed on the original SQLite task IDs
(`t_<26-char base32>`), so re-running it is a no-op.

## Known limits (v0.1.0)

- The dashboard plugin (`plugins/kanban/dashboard/plugin_api.py`) still
  reads from the SQLite file paths for attachments; future version
  will route through `list_attachments(task_id)` and the new
  `task_attachments.path` column.
- `_check_file_length_invariant` and the SQLite-specific integrity
  checks are gone (PG handles integrity).
- The `reap_worker_zombies` and `_scratch_tip_*` filesystem-coordination
  primitives are no-ops; the new `task_runs` table records worker
  state directly.
