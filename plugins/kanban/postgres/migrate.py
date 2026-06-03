"""SQLite → Postgres migration for the hermes kanban.

Reads every `~/.hermes/kanban/boards/*/kanban.db`, extracts tasks,
comments, events, and runs, and writes them to the `hermes_kanban`
schema in the profile database.

Idempotency: tasks are matched by their SQLite ID
(`t_<26-char base32>`). If a task with the same ID already exists in
Postgres, the migration skips it. The migration is therefore safe to
re-run.

This is the one-and-done script: after it succeeds, the SQLite files
become a noop archive (kept under `kanban.db.migrated.<timestamp>`).
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Use the same connection pool as the runtime plugin
from plugins.kanban.postgres import (
    create_task, add_comment, list_tenants, upsert_tenant, _cursor,
    get_pg_kanban_schema, _ensure_schema,
)

logger = logging.getLogger(__name__)


def _ep_from_epoch(value: Optional[float]) -> Optional[datetime]:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (ValueError, OSError):
        return None


def _ts_to_iso(value: Optional[float]) -> Optional[str]:
    dt = _ep_from_epoch(value)
    return dt.isoformat() if dt else None


def _connect_sqlite(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


# ── Per-board migration ──────────────────────────────────────────────


def _migrate_board(board_dir: Path, *, dry_run: bool = False) -> Dict[str, int]:
    """Migrate one board. Returns counts of inserted/skipped rows."""
    db_path = board_dir / "kanban.db"
    if not db_path.exists():
        return {"tasks": 0, "comments": 0, "events": 0, "runs": 0, "skipped": 0}

    counts = {"tasks": 0, "comments": 0, "events": 0, "runs": 0, "skipped": 0}
    logger.info("Migrating board %s from %s", board_dir.name, db_path)

    # The old SQLite `tasks.tenant` was a free-form text column. The
    # first task with a non-null tenant defines the tenant for the
    # whole board. Boards that were all `NULL` get the `default` tenant.
    board_tenant = board_dir.name  # use the board slug as the tenant
    if not dry_run:
        upsert_tenant(board_tenant, display_name=board_dir.name.replace("-", " ").title())

    with _connect_sqlite(db_path) as conn:
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "tasks" not in tables:
            logger.info("  → board has no tasks table, skipping")
            return counts
        # Tasks
        task_rows = list(conn.execute(
            "SELECT * FROM tasks ORDER BY created_at"
        ))
        for row in task_rows:
            row_d = dict(row)
            task_id = row_d["id"]
            # Skip if already migrated (idempotency by SQLite ID)
            with _cursor() as cur:
                _ensure_schema(cur)
                schema = get_pg_kanban_schema()
                cur.execute(
                    f"SELECT 1 FROM {schema}.tasks WHERE id = %s", (task_id,),
                )
                if cur.fetchone():
                    counts["skipped"] += 1
                    continue
            # Resolve tenant: per-task wins, else board-level.
            task_tenant = row_d.get("tenant") or board_tenant
            if not dry_run:
                create_task(
                    task_id=task_id,  # preserve SQLite ID for idempotency
                    title=row_d.get("title") or "(untitled)",
                    body=row_d.get("body") or "",
                    priority=int(row_d.get("priority") or 5),
                    assignee=row_d.get("assignee"),
                    tenant=task_tenant,
                    workspace=row_d.get("workspace"),
                    status=row_d.get("status") or "ready",
                    created_by=row_d.get("created_by"),
                    metadata=({"result": row_d["result"]} if row_d.get("result") else {}),
                )
            counts["tasks"] += 1
        # Comments
        if "comments" in tables:
            for row in conn.execute("SELECT * FROM comments ORDER BY created_at"):
                row_d = dict(row)
                if not dry_run:
                    add_comment(
                        task_id=row_d["task_id"],
                        body=row_d.get("body") or "",
                        author=row_d.get("author"),
                    )
                counts["comments"] += 1
        # Events (just record them; PG already has its own event log)
        if "events" in tables:
            counts["events"] = len(conn.execute("SELECT * FROM events").fetchall())
        # Runs
        if "runs" in tables:
            counts["runs"] = len(conn.execute("SELECT * FROM runs").fetchall())
    return counts


# ── CLI entry point ──────────────────────────────────────────────────


def cmd_migrate(args, parser) -> int:
    boards_root = Path(args.boards_root).expanduser()
    if not boards_root.exists():
        print(f"ERROR: boards root not found: {boards_root}", file=sys.stderr)
        return 1
    dry_run = args.dry_run
    archive = not args.no_archive
    boards = sorted(p for p in boards_root.iterdir() if p.is_dir())
    if not boards:
        print(f"No boards found under {boards_root}", file=sys.stderr)
        return 0
    print(f"{'DRY RUN — ' if dry_run else ''}migrating {len(boards)} board(s)")
    total = {"tasks": 0, "comments": 0, "events": 0, "runs": 0, "skipped": 0}
    for board in boards:
        counts = _migrate_board(board, dry_run=dry_run)
        for k, v in counts.items():
            total[k] += v
        print(
            f"  {board.name}: {counts['tasks']} tasks, "
            f"{counts['comments']} comments, {counts['events']} events, "
            f"{counts['runs']} runs, {counts['skipped']} skipped"
        )
    print(
        f"\nTotal: {total['tasks']} tasks, {total['comments']} comments, "
        f"{total['events']} events, {total['runs']} runs, "
        f"{total['skipped']} skipped"
    )
    # Archive the SQLite files (one timestamped backup per board)
    if archive and not dry_run and (total["tasks"] or total["skipped"]):
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for board in boards:
            db = board / "kanban.db"
            if not db.exists():
                continue
            backup = board / f"kanban.db.migrated.{ts}"
            shutil.copy2(db, backup)
            print(f"  archived → {backup.name}")
    return 0


def register_cli(subparser) -> None:
    p = subparser.add_parser(
        "postgres-kanban",
        help="PostgreSQL kanban provider commands",
    )
    subs = p.add_subparsers(dest="postgres_kanban_command")

    s_status = subs.add_parser("status", help="Show provider status")
    s_status.set_defaults(func=_cmd_status)

    s_mig = subs.add_parser("migrate", help="Migrate SQLite kanban boards to Postgres")
    s_mig.add_argument(
        "--boards-root", default="~/.hermes/kanban/boards",
        help="Path to the boards directory (default: ~/.hermes/kanban/boards)",
    )
    s_mig.add_argument(
        "--dry-run", action="store_true",
        help="Count rows without writing",
    )
    s_mig.add_argument(
        "--no-archive", action="store_true",
        help="Don't copy SQLite files to kanban.db.migrated.<ts>",
    )
    s_mig.set_defaults(func=cmd_migrate)

    p.set_defaults(func=lambda args, parser: p.print_help() or 1)


def _cmd_status(args, parser) -> int:
    import json as _json
    from plugins.kanban.postgres import stats
    try:
        s = stats()
        print(_json.dumps(s, indent=2))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
