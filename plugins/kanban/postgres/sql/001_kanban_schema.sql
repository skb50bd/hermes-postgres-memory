-- 001_kanban_schema.sql
--
-- Create the hermes_kanban schema for the postgres-backed kanban
-- provider. Replaces the SQLite boards at
-- ~/.hermes/kanban/boards/*/kanban.db.
--
-- Design notes:
--  - Tenants are a first-class FK (replaces old tasks.tenant TEXT)
--  - Claim uses SELECT FOR UPDATE SKIP LOCKED (no claim_lock column
--    and no cron-based reaper; lock auto-releases on tx end)
--  - body_tsv is a generated tsvector column for FTS
--  - Every state-changing operation calls NOTIFY hermes_kanban_event
--    so the dashboard WebSocket can broadcast
--  - All 8 tables are created in this single file for atomicity; if
--    you need to migrate a partial install, drop the schema and
--    re-run.
--
-- This file is idempotent (every CREATE has IF NOT EXISTS).

CREATE SCHEMA IF NOT EXISTS hermes_kanban;

-- ── Tenants ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hermes_kanban.tenants (
    id           serial PRIMARY KEY,
    slug         varchar(64) UNIQUE NOT NULL,
    display_name varchar(200),
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- ── Tasks ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hermes_kanban.tasks (
    id                     varchar(40) PRIMARY KEY,
    title                  text NOT NULL,
    body                   text NOT NULL DEFAULT '',
    body_tsv               tsvector GENERATED ALWAYS AS
        (to_tsvector('english', coalesce(title, '') || ' ' || coalesce(body, ''))) STORED,
    status                 varchar(16) NOT NULL DEFAULT 'ready'
        CHECK (status IN ('ready','running','done','failed','cancelled','blocked','review','archived')),
    priority               smallint NOT NULL DEFAULT 5,
    assignee               varchar(100),
    worker_pid             integer,
    tenant_id               integer REFERENCES hermes_kanban.tenants(id) ON DELETE SET NULL,
    workspace               text,
    result                  text,
    metadata                jsonb NOT NULL DEFAULT '{}'::jsonb,
    tags                    text[] NOT NULL DEFAULT '{}',
    created_by              varchar(100),
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    started_at              timestamptz,
    completed_at            timestamptz,
    claim_expires_at        timestamptz,
    consecutive_failures    smallint NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS tasks_status_priority ON hermes_kanban.tasks (status, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS tasks_assignee ON hermes_kanban.tasks (assignee) WHERE assignee IS NOT NULL;
CREATE INDEX IF NOT EXISTS tasks_tenant ON hermes_kanban.tasks (tenant_id);
CREATE INDEX IF NOT EXISTS tasks_body_tsv ON hermes_kanban.tasks USING GIN (body_tsv);
CREATE INDEX IF NOT EXISTS tasks_updated_at ON hermes_kanban.tasks (updated_at DESC);

-- ── Runs ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hermes_kanban.task_runs (
    id            bigserial PRIMARY KEY,
    task_id       varchar(40) NOT NULL REFERENCES hermes_kanban.tasks(id) ON DELETE CASCADE,
    worker_pid    integer,
    worker_label  varchar(200),
    started_at    timestamptz NOT NULL DEFAULT now(),
    ended_at      timestamptz,
    status        varchar(16),
    exit_code     integer
);
CREATE INDEX IF NOT EXISTS task_runs_task ON hermes_kanban.task_runs (task_id, started_at DESC);

-- ── Events (append-only audit log) ──────────────────────────────────
CREATE TABLE IF NOT EXISTS hermes_kanban.task_events (
    id          bigserial PRIMARY KEY,
    task_id     varchar(40) NOT NULL REFERENCES hermes_kanban.tasks(id) ON DELETE CASCADE,
    kind        varchar(32) NOT NULL,
    actor       varchar(100),
    payload     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS task_events_task ON hermes_kanban.task_events (task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS task_events_kind ON hermes_kanban.task_events (kind, created_at DESC);

-- ── Links (parent ↔ child graph) ────────────────────────────────────
CREATE TABLE IF NOT EXISTS hermes_kanban.task_links (
    parent_id  varchar(40) NOT NULL REFERENCES hermes_kanban.tasks(id) ON DELETE CASCADE,
    child_id   varchar(40) NOT NULL REFERENCES hermes_kanban.tasks(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (parent_id, child_id),
    CHECK (parent_id <> child_id)
);
CREATE INDEX IF NOT EXISTS task_links_child ON hermes_kanban.task_links (child_id);

-- ── Comments ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hermes_kanban.task_comments (
    id          bigserial PRIMARY KEY,
    task_id     varchar(40) NOT NULL REFERENCES hermes_kanban.tasks(id) ON DELETE CASCADE,
    body        text NOT NULL,
    author      varchar(100),
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS task_comments_task ON hermes_kanban.task_comments (task_id, created_at);

-- ── Tags (lightweight, free-form) ────────────────────────────────────
CREATE TABLE IF NOT EXISTS hermes_kanban.tags (
    id   serial PRIMARY KEY,
    name varchar(64) UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS hermes_kanban.task_tags (
    task_id  varchar(40) NOT NULL REFERENCES hermes_kanban.tasks(id) ON DELETE CASCADE,
    tag_id   integer NOT NULL REFERENCES hermes_kanban.tags(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, tag_id)
);

-- ── Attachments (metadata; blob lives in hermes_memory.attachments) ─
CREATE TABLE IF NOT EXISTS hermes_kanban.task_attachments (
    id          bigserial PRIMARY KEY,
    task_id     varchar(40) NOT NULL REFERENCES hermes_kanban.tasks(id) ON DELETE CASCADE,
    filename    text NOT NULL,
    mime        varchar(200),
    size        bigint,
    path        text NOT NULL,
    author      varchar(100),
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS task_attachments_task ON hermes_kanban.task_attachments (task_id);

-- ── Notify subs (cross-process pub/sub fan-out) ────────────────────
CREATE TABLE IF NOT EXISTS hermes_kanban.notify_subs (
    task_id      varchar(40) NOT NULL REFERENCES hermes_kanban.tasks(id) ON DELETE CASCADE,
    channel      varchar(100) NOT NULL,
    filter_kind  varchar(32),
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, channel)
);

-- ── Convenience view: tasks + tenant slug ──────────────────────────
CREATE OR REPLACE VIEW hermes_kanban.v_board_tasks AS
SELECT
    t.*,
    ten.slug AS tenant_slug
FROM hermes_kanban.tasks t
LEFT JOIN hermes_kanban.tenants ten ON t.tenant_id = ten.id;

-- ── Seed the 'default' tenant so first-write doesn't trip FK ──────
INSERT INTO hermes_kanban.tenants (slug, display_name)
VALUES ('default', 'Default tenant')
ON CONFLICT (slug) DO NOTHING;
