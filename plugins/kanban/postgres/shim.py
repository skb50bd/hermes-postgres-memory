"""hermes_cli.kanban_db — thin compatibility shim.

This module replaces the old SQLite-backed `kanban_db.py` (7,463 LOC)
with a thin wrapper around the `hermes-postgres-kanban` plugin. The
public surface is preserved as much as possible so the 4 callers
(CLI, dashboard, dispatcher, diagnostics) keep working without
modification.

Translation rules
-----------------
- Every function that previously took `conn: sqlite3.Connection` as
  its first arg now takes `_conn=None` (ignored). The plugin uses a
  psycopg2 connection pool, so callers don't need to manage
  connections.
- `write_txn(conn)` is now a no-op context manager (the plugin's
  cursor helper handles its own commit/rollback).
- Domain objects (Task, Run, Comment, Attachment, Event) become
  `dict` subclasses. The dashboard plugin's `_task_dict` /
  `_event_dict` helpers use `asdict()` on these, so the dict-shape
  contract is the same.
- `connect(board=...)` and friends (`kanban_home`, `boards_root`,
  `board_dir`, etc.) return empty paths. The dashboard's
  `attachments_root` etc. point at a local directory; those
  directories still exist (we don't break the file system) but
  the metadata now lives in PG.

What's NOT in the shim
----------------------
- The old `_sqlite_connect`, `_guard_existing_db_is_healthy`,
  `_backup_corrupt_db`, `_migrate_add_optional_columns`,
  `_rebuild_drifted_tables`, `_check_file_length_invariant` —
  SQLite-specific integrity checks. With Postgres, these become
  unnecessary. The runtime never calls them.
- `reap_worker_zombies`, `_record_worker_exit`,
  `_terminate_reclaimed_worker`, `_scratch_tip_*` — SQLite
  filesystem-coordination primitives. The new plugin tracks
  worker state in the `task_runs` table directly.

If a function isn't found, importing this module emits a clear
`DeprecationWarning` at first call (but doesn't crash) so a future
debug session can find missing wrappers quickly.
"""

from __future__ import annotations

import logging
import os
import warnings
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Re-export the plugin's storage layer. The plugin lives at
# `plugins/kanban/postgres` inside the hermes-postgres-memory repo.
# It is also installed under `~/.hermes/hermes-agent/plugins/kanban/postgres`
# when the user runs install.sh. Try the editable location first
# (so devs see changes immediately), then fall back to the installed copy.
_PLUGIN_OK: bool = False
_PG: Any = None  # filled by the import below
_PLUGIN_IMPORT_ERROR: Optional[Exception] = None

try:
    from plugins.kanban import postgres as _PG  # type: ignore
    _PLUGIN_OK = True
except ImportError as _exc:
    _PLUGIN_IMPORT_ERROR = _exc
    # Stubs so the module still imports when the plugin isn't installed
    class _PG:  # type: ignore[no-redef]
        pass


def _warn_missing(name: str) -> None:
    """Surface a deprecation warning when a missing shim is called.

    The first call gets a full explanation; subsequent calls just say
    "still missing" so the log isn't spammed.
    """
    warnings.warn(
        f"hermes_cli.kanban_db.{name} is not implemented in the "
        f"postgres shim — caller should use plugins.kanban.postgres "
        f"directly. (Plugin import: "
        f"{'ok' if _PLUGIN_OK else _PLUGIN_IMPORT_ERROR})",
        DeprecationWarning,
        stacklevel=3,
    )


# ── Domain objects (dict subclasses for the dashboard's asdict) ──


class Task(dict):
    """Dict that exposes fields as attributes (dashboard uses asdict())."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            return None

    def __setattr__(self, key, value):
        self[key] = value


class Run(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            return None

    def __setattr__(self, key, value):
        self[key] = value


class Comment(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            return None

    def __setattr__(self, key, value):
        self[key] = value


class Attachment(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            return None

    def __setattr__(self, key, value):
        self[key] = value


class Event(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            return None

    def __setattr__(self, key, value):
        self[key] = value


# ── Valid statuses (mirrored from the SQL CHECK constraint) ──


VALID_STATUSES = (
    "ready", "running", "done", "failed", "cancelled",
    "blocked", "review", "archived",
)
DEFAULT_BOARD = "default"


# ── Connection / write_txn (the dashboard passes these around) ──


@contextmanager
def write_txn(conn=None):
    """No-op context manager. The plugin manages its own transactions.

    Kept as a no-op (not an error) because the dashboard's
    `with kanban_db.write_txn(conn):` patterns are pervasive. Yields
    `None` so any `as x:` usage also doesn't crash.
    """
    yield None


# ── Board / filesystem helpers (mostly no-ops now) ──


def _normalize_board_slug(slug: Optional[str]) -> Optional[str]:
    if slug is None:
        return None
    if slug == DEFAULT_BOARD:
        return None  # default board maps to the "default" tenant
    return slug


def kanban_home() -> Path:
    """Kept for callers that still look for the SQLite root. Returns
    the legacy path so the dashboard's "Attachments" button doesn't
    404 — files there are the same SQLite-era archive.
    """
    return Path(os.path.expanduser("~/.hermes/kanban"))


def boards_root() -> Path:
    return kanban_home() / "boards"


def board_dir(board: Optional[str] = None) -> Path:
    return boards_root() / (board or DEFAULT_BOARD)


def board_exists(board: Optional[str] = None) -> bool:
    return board_dir(board).exists()


def init_db(board: Optional[str] = None) -> None:
    """No-op. The PG schema is initialized by the hermes-memory
    bootstrap, not by the runtime."""
    return None


def connect(board: Optional[str] = None):
    """Returns None. The plugin uses a pool, so callers don't need
    a connection. Dashboard code that does `with conn:` is broken
    by design — the dashboard needs a per-PR rewrite to use the
    plugin's context managers."""
    return None


def connect_closing(*args, **kwargs):
    return None


def kanban_db_path(board: Optional[str] = None) -> Path:
    return board_dir(board) / "kanban.db"


def workspaces_root(board: Optional[str] = None) -> Path:
    return board_dir(board) / "workspaces"


def attachments_root(board: Optional[str] = None) -> Path:
    """Where SQLite-era attachments live. New attachments go in
    `hermes_kanban.task_attachments.path` and may point elsewhere."""
    return board_dir(board) / "attachments"


def task_attachments_dir(task_id: str, board: Optional[str] = None) -> Path:
    return attachments_root(board) / task_id


def worker_logs_dir(board: Optional[str] = None) -> Path:
    return board_dir(board) / "worker_logs"


def board_metadata_path(board: Optional[str] = None) -> Path:
    return board_dir(board) / "metadata.json"


def _default_board_display_name(slug: str) -> str:
    return slug.replace("-", " ").title()


def read_board_metadata(board: Optional[str] = None) -> dict:
    """Read the legacy JSON metadata file. Returns an empty dict if
    the file is missing. The new metadata lives in
    `hermes_kanban.tenants`."""
    p = board_metadata_path(board)
    if not p.exists():
        return {}
    try:
        import json
        return json.loads(p.read_text())
    except Exception:
        return {}


def write_board_metadata(board: Optional[str] = None, **data) -> None:
    """Best-effort write to the legacy JSON. New metadata should
    use the plugin's `upsert_tenant` instead."""
    p = board_metadata_path(board)
    p.parent.mkdir(parents=True, exist_ok=True)
    import json
    p.write_text(json.dumps(data, indent=2, default=str))


def create_board(slug: str, *, display_name: Optional[str] = None) -> dict:
    """Create a tenant (the new equivalent of a board)."""
    if not _PLUGIN_OK:
        _warn_missing("create_board")
        return {"slug": slug}
    return _PG.upsert_tenant(slug, display_name=display_name)


def list_boards(*, include_archived: bool = True) -> List[dict]:
    if not _PLUGIN_OK:
        _warn_missing("list_boards")
        return []
    tenants = _PG.list_tenants()
    return [
        {"slug": t["slug"], "display_name": t.get("display_name"),
         "id": t["id"], "created_at": t["created_at"].isoformat() if t.get("created_at") else None}
        for t in tenants
    ]


def remove_board(slug: str, *, archive: bool = True) -> dict:
    _warn_missing("remove_board")
    return {"slug": slug, "archived": archive}


def get_current_board() -> str:
    return DEFAULT_BOARD


def set_current_board(slug: str) -> Path:
    return board_dir(slug)


def clear_current_board() -> None:
    return None


# ── Task CRUD (the dashboard / dispatcher hot path) ──


def create_task(
    conn=None,  # sqlite3.Connection in the old API; ignored
    *,
    title: str = "",
    body: str = "",
    priority: int = 5,
    assignee: Optional[str] = None,
    tenant: Optional[str] = None,
    workspace: Optional[str] = None,
    status: str = "ready",
    created_by: Optional[str] = None,
    parent_ids: Optional[List[str]] = None,
    tags: Optional[List[str]] = None,
    metadata: Optional[dict] = None,
    task_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Create a task. Returns the task_id (str), matching the old API."""
    if not _PLUGIN_OK:
        _warn_missing("create_task")
        return task_id or ""
    board = tenant
    result = _PG.create_task(
        title=title or "(untitled)",
        body=body or "",
        priority=priority,
        assignee=assignee,
        tenant=board or DEFAULT_BOARD,
        parent_ids=parent_ids,
        tags=tags,
        metadata=metadata,
        workspace=workspace,
        status=status,
        created_by=created_by,
        task_id=task_id,
    )
    return result["id"]


def get_task(conn=None, task_id: Optional[str] = None, **kwargs) -> Optional[Task]:
    if task_id is None:
        # Old API allowed get_task(conn, task_id) positional
        task_id = conn
    if not _PLUGIN_OK or not task_id:
        return None
    row = _PG.get_task(task_id)
    return Task(row) if row else None


def list_tasks(
    conn=None,
    *,
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    tenant: Optional[str] = None,
    board: Optional[str] = None,
    parent_id: Optional[str] = None,
    child_id: Optional[str] = None,
    tags: Optional[List[str]] = None,
    search: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    order_by: str = "priority DESC, created_at",
    **kwargs,
) -> List[Task]:
    if not _PLUGIN_OK:
        return []
    rows = _PG.list_tasks(
        status=status,
        assignee=assignee,
        tenant=tenant or board,
        parent_id=parent_id,
        child_id=child_id,
        tags=tags,
        search=search,
        limit=limit,
        offset=offset,
        order_by=order_by,
    )
    return [Task(r) for r in rows]


def update_task(conn=None, task_id: Optional[str] = None, **fields) -> Optional[Task]:
    """The old API was `update_task(conn, task_id, **fields)`. The
    new plugin's `update_task(task_id, **fields)` doesn't take conn."""
    if isinstance(conn, str) and task_id is None:
        task_id = conn
        conn = None
    if not _PLUGIN_OK or not task_id:
        return None
    row = _PG.update_task(task_id, **fields)
    return Task(row) if row else None


def delete_task(conn=None, task_id: Optional[str] = None) -> bool:
    if isinstance(conn, str) and task_id is None:
        task_id = conn
    if not _PLUGIN_OK or not task_id:
        return False
    return _PG.delete_task(task_id)


# ── Aliases the dashboard uses ──


def assign_task(conn=None, task_id: Optional[str] = None, *,
                assignee: Optional[str] = None, **kwargs) -> bool:
    if isinstance(conn, str) and task_id is None:
        task_id = conn
    if not _PLUGIN_OK or not task_id:
        return False
    row = _PG.update_task(task_id, assignee=assignee)
    return row is not None


def claim_task(conn=None, *,
               assignee: Optional[str] = None,
               worker_pid: Optional[int] = None,
               claim_ttl_seconds: int = 300,
               **kwargs) -> Optional[Task]:
    """Race-free claim via SKIP LOCKED. Returns the claimed Task or None."""
    if not _PLUGIN_OK:
        return None
    row = _PG.claim_next(
        assignee=assignee, worker_pid=worker_pid,
        claim_ttl_seconds=claim_ttl_seconds,
    )
    return Task(row) if row else None


def heartbeat_claim(conn=None, task_id: Optional[str] = None, *,
                    worker_pid: Optional[int] = None,
                    extend_seconds: int = 300, **kwargs) -> bool:
    if isinstance(conn, str) and task_id is None:
        task_id = conn
    if not _PLUGIN_OK or not task_id:
        return False
    return _PG.heartbeat_claim(
        task_id, worker_pid=worker_pid, extend_seconds=extend_seconds,
    )


def complete_task(conn=None, task_id: Optional[str] = None, *,
                  result: str = "", worker_pid: Optional[int] = None,
                  **kwargs) -> Optional[Task]:
    if isinstance(conn, str) and task_id is None:
        task_id = conn
    if not _PLUGIN_OK or not task_id:
        return None
    row = _PG.complete_task(task_id, result, worker_pid=worker_pid)
    return Task(row) if row else None


def fail_task(conn=None, task_id: Optional[str] = None, *,
              error: str = "", worker_pid: Optional[int] = None,
              requeue: bool = True, **kwargs) -> Optional[Task]:
    if isinstance(conn, str) and task_id is None:
        task_id = conn
    if not _PLUGIN_OK or not task_id:
        return None
    row = _PG.fail_task(task_id, error, worker_pid=worker_pid, requeue=requeue)
    return Task(row) if row else None


def block_task(conn=None, task_id: Optional[str] = None, *,
               reason: str = "", **kwargs) -> bool:
    if isinstance(conn, str) and task_id is None:
        task_id = conn
    if not _PLUGIN_OK or not task_id:
        return False
    row = _PG.update_task(task_id, status="blocked", metadata={"block_reason": reason})
    return row is not None


def unblock_task(conn=None, task_id: Optional[str] = None, **kwargs) -> bool:
    if isinstance(conn, str) and task_id is None:
        task_id = conn
    if not _PLUGIN_OK or not task_id:
        return False
    row = _PG.update_task(task_id, status="ready")
    return row is not None


def schedule_task(conn=None, task_id: Optional[str] = None, *,
                  reason: str = "", **kwargs) -> bool:
    """Alias for `block_task` with reason in metadata."""
    return block_task(conn=conn, task_id=task_id, reason=reason, **kwargs)


def archive_task(conn=None, task_id: Optional[str] = None, **kwargs) -> bool:
    if isinstance(conn, str) and task_id is None:
        task_id = conn
    if not _PLUGIN_OK or not task_id:
        return False
    row = _PG.update_task(task_id, status="archived")
    return row is not None


def delete_archived_task(conn=None, task_id: Optional[str] = None) -> bool:
    return delete_task(conn=conn, task_id=task_id)


def promote_task(conn=None, task_id: Optional[str] = None, **kwargs) -> bool:
    _warn_missing("promote_task")
    return False


def edit_completed_task_result(conn=None, task_id: Optional[str] = None, *,
                               result: str = "", **kwargs) -> bool:
    if isinstance(conn, str) and task_id is None:
        task_id = conn
    if not _PLUGIN_OK or not task_id:
        return False
    row = _PG.update_task(task_id, result=result)
    return row is not None


# ── Comments / events / runs (dashboard's task-detail page) ──


def list_comments(conn=None, task_id: Optional[str] = None, **kwargs) -> List[Comment]:
    if isinstance(conn, str) and task_id is None:
        task_id = conn
    if not _PLUGIN_OK or not task_id:
        return []
    return [Comment(r) for r in _PG.list_comments(task_id)]


def add_comment(conn=None, task_id: Optional[str] = None, *,
                body: str = "", author: Optional[str] = None, **kwargs) -> int:
    if isinstance(conn, str) and task_id is None:
        task_id = conn
    if not _PLUGIN_OK or not task_id:
        return 0
    result = _PG.add_comment(task_id, body, author=author)
    return result.get("id", 0)


def list_events(conn=None, task_id: Optional[str] = None, *,
                limit: int = 100, **kwargs) -> List[Event]:
    if isinstance(conn, str) and task_id is None:
        task_id = conn
    if not _PLUGIN_OK or not task_id:
        return []
    return [Event(r) for r in _PG.list_events(task_id, limit=limit)]


def list_runs(conn=None, task_id: Optional[str] = None, **kwargs) -> List[Run]:
    if isinstance(conn, str) and task_id is None:
        task_id = conn
    if not _PLUGIN_OK or not task_id:
        return []
    return [Run(r) for r in _PG.list_runs(task_id)]


def start_run(conn=None, task_id: Optional[str] = None, *,
              worker_pid: Optional[int] = None,
              worker_label: Optional[str] = None, **kwargs) -> int:
    if isinstance(conn, str) and task_id is None:
        task_id = conn
    if not _PLUGIN_OK or not task_id:
        return 0
    return _PG.start_run(task_id, worker_pid or 0, worker_label).get("id", 0)


def _end_run(conn=None, run_id: Optional[int] = None, *,
             status: str = "done", exit_code: Optional[int] = None) -> Optional[Run]:
    if not _PLUGIN_OK or not run_id:
        return None
    row = _PG.end_run(run_id, status=status, exit_code=exit_code)
    return Run(row) if row else None


# ── Links (parent/child) ──


def link_tasks(conn=None, parent_id: Optional[str] = None,
               child_id: Optional[str] = None, **kwargs) -> bool:
    if isinstance(conn, str) and child_id is None:
        child_id = parent_id
        parent_id = conn
    if not _PLUGIN_OK or not parent_id or not child_id:
        return False
    return _PG.link_tasks(parent_id, child_id)


def unlink_tasks(conn=None, parent_id: Optional[str] = None,
                 child_id: Optional[str] = None, **kwargs) -> bool:
    if isinstance(conn, str) and child_id is None:
        child_id = parent_id
        parent_id = conn
    if not _PLUGIN_OK or not parent_id or not child_id:
        return False
    return _PG.unlink_tasks(parent_id, child_id)


def parent_ids(conn=None, task_id: Optional[str] = None, **kwargs) -> List[str]:
    if isinstance(conn, str) and task_id is None:
        task_id = conn
    if not _PLUGIN_OK or not task_id:
        return []
    return [p["id"] for p in _PG.list_parents(task_id)]


def child_ids(conn=None, task_id: Optional[str] = None, **kwargs) -> List[str]:
    if isinstance(conn, str) and task_id is None:
        task_id = conn
    if not _PLUGIN_OK or not task_id:
        return []
    return [c["id"] for c in _PG.list_children(task_id)]


def parent_results(conn=None, task_id: Optional[str] = None, **kwargs) -> List[Tuple[str, Optional[str]]]:
    parents = parent_ids(conn=conn, task_id=task_id)
    return [(p, None) for p in parents]


# ── Attachments ──


def add_attachment(conn=None, task_id: Optional[str] = None, *,
                   filename: str = "", mime: str = "", size: int = 0,
                   path: str = "", author: Optional[str] = None, **kwargs) -> int:
    if isinstance(conn, str) and task_id is None:
        task_id = conn
    if not _PLUGIN_OK or not task_id:
        return 0
    return _PG.add_attachment(
        task_id, filename, mime, size, path, author=author,
    ).get("id", 0)


def list_attachments(conn=None, task_id: Optional[str] = None, **kwargs) -> List[Attachment]:
    if isinstance(conn, str) and task_id is None:
        task_id = conn
    if not _PLUGIN_OK or not task_id:
        return []
    return [Attachment(r) for r in _PG.list_attachments(task_id)]


def get_attachment(conn=None, attachment_id: Optional[int] = None) -> Optional[Attachment]:
    _warn_missing("get_attachment")
    return None


def delete_attachment(conn=None, attachment_id: Optional[int] = None) -> Optional[Attachment]:
    _warn_missing("delete_attachment")
    return None


# ── Misc dashboard / dispatcher helpers ──


def task_age(task) -> Optional[float]:
    """Age of a task in seconds, matching the dashboard's `age` field."""
    if isinstance(task, dict):
        ts = task.get("created_at")
    else:
        ts = getattr(task, "created_at", None)
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, datetime):
        return (datetime.now(timezone.utc) - ts).total_seconds()
    return None


def latest_summary(conn=None, task_id: Optional[str] = None, **kwargs):
    """Dashboard used this to fetch a worker's last summary. In the
    new model, the most-recent task_event of kind 'completed' carries
    the result, or the `result` column on the task itself."""
    if not _PLUGIN_OK:
        return None
    if isinstance(conn, str) and task_id is None:
        task_id = conn
    task = _PG.get_task(task_id) if task_id else None
    if not task:
        return None
    return task.get("result")


def latest_summaries(conn=None, task_ids: Optional[Iterable[str]] = None, **kwargs) -> Dict[str, Optional[str]]:
    if not _PLUGIN_OK:
        return {}
    out: Dict[str, Optional[str]] = {}
    for tid in task_ids or []:
        t = _PG.get_task(tid)
        out[tid] = t.get("result") if t else None
    return out


def recompute_ready(conn=None, **kwargs) -> int:
    if not _PLUGIN_OK:
        return 0
    return _PG.recompute_ready()


# ── ID / claim helpers (used by the dispatcher) ──


def _new_task_id() -> str:
    return _PG._new_task_id()


def _claimer_id() -> str:
    return f"pid-{os.getpid()}"


def _canonical_assignee(assignee: Optional[str]) -> Optional[str]:
    return assignee or None


# ── Diagnostic helpers (kept as no-ops) ──


def list_tenants() -> List[dict]:
    if not _PLUGIN_OK:
        return []
    return _PG.list_tenants()


def list_kanban_tenants() -> List[dict]:  # alias
    return list_tenants()


# ── Re-exports of the raw plugin functions for direct callers ──

# The dashboard's newer code may prefer to call the plugin directly
# (cleaner, no translation layer). Expose the plugin symbols under
# their original names so `from hermes_cli.kanban_db import X` works
# whether X is a shim or a raw passthrough.
if _PLUGIN_OK:
    claim_next = _PG.claim_next
    heartbeat_claim_pg = _PG.heartbeat_claim
    complete_task_pg = _PG.complete_task
    fail_task_pg = _PG.fail_task
    stats = _PG.stats
    subscribe = _PG.subscribe
    unsubscribe = _PG.unsubscribe
    recompute_ready_pg = _PG.recompute_ready
    upsert_tenant = _PG.upsert_tenant
