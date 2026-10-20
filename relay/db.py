"""SQLite schema and transaction helpers.

Migration discipline (section 11): never edit an applied migration; append a
new entry to MIGRATIONS instead.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

MIGRATIONS: list[str] = [
    # 0001 — v2 core schema
    """
    CREATE TABLE accounts (
        id TEXT PRIMARY KEY,
        handle TEXT NOT NULL UNIQUE,
        balance_milli INTEGER NOT NULL,
        reserved_milli INTEGER NOT NULL DEFAULT 0 CHECK (reserved_milli >= 0),
        credit_milli INTEGER NOT NULL,
        created_at INTEGER NOT NULL
    );

    CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES accounts(id),
        created_at INTEGER NOT NULL
    );

    CREATE TABLE pair_codes (
        code_hash TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES accounts(id),
        expires_at INTEGER NOT NULL,
        used_at INTEGER
    );

    CREATE TABLE devices (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES accounts(id),
        token_hash TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        client_version TEXT,
        protocol_version INTEGER,
        capabilities_json TEXT NOT NULL DEFAULT '{}',
        ready INTEGER NOT NULL DEFAULT 0,
        last_seen_at INTEGER,
        revoked_at INTEGER,
        created_at INTEGER NOT NULL
    );

    CREATE TABLE tasks (
        id TEXT PRIMARY KEY,
        publisher_id TEXT NOT NULL REFERENCES accounts(id),
        parent_id TEXT REFERENCES tasks(id),
        category TEXT NOT NULL,
        title TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        spec_json TEXT NOT NULL,
        spec_hash TEXT NOT NULL,
        adapter TEXT NOT NULL,
        model TEXT NOT NULL,
        budget_milli INTEGER NOT NULL CHECK (budget_milli > 0),
        status TEXT NOT NULL CHECK (status IN ('open','claimed','stalled','done','failed','cancelled')),
        fencing_counter INTEGER NOT NULL DEFAULT 0,
        active_run_id TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE INDEX tasks_status_created ON tasks(status, created_at DESC);
    CREATE INDEX tasks_publisher ON tasks(publisher_id, created_at DESC);

    CREATE TABLE runs (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        device_id TEXT NOT NULL REFERENCES devices(id),
        worker_id TEXT NOT NULL REFERENCES accounts(id),
        attempt INTEGER NOT NULL,
        fencing_token INTEGER NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('assigned','running','uploading','validating','settled','failed','expired')),
        outcome TEXT CHECK (outcome IN ('succeeded','failed')),
        lease_until INTEGER NOT NULL,
        lease_hard_limit INTEGER NOT NULL,
        stop_requested INTEGER NOT NULL DEFAULT 0,
        receipt_hash TEXT,
        output_text TEXT,
        error_code TEXT,
        usage_status TEXT CHECK (usage_status IN ('known','unknown')),
        input_tokens INTEGER,
        output_tokens INTEGER,
        fee_milli INTEGER,
        created_at INTEGER NOT NULL,
        started_at INTEGER,
        finished_at INTEGER,
        UNIQUE (task_id, attempt)
    );
    -- At most one active run per task, and one active run per device.
    CREATE UNIQUE INDEX runs_one_active_per_task ON runs(task_id)
        WHERE state IN ('assigned','running','uploading','validating');
    CREATE UNIQUE INDEX runs_one_active_per_device ON runs(device_id)
        WHERE state IN ('assigned','running','uploading','validating');

    CREATE TABLE usage_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL REFERENCES runs(id),
        provider TEXT NOT NULL,
        unit TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        source TEXT NOT NULL,
        known INTEGER NOT NULL
    );

    CREATE TABLE validation_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL REFERENCES runs(id),
        validator_id TEXT NOT NULL,
        validator_version TEXT NOT NULL,
        passed INTEGER NOT NULL,
        report_json TEXT NOT NULL,
        validation_milli INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL
    );

    -- One settlement per task and per run; the PK/UNIQUE are the idempotency guard.
    CREATE TABLE settlements (
        run_id TEXT PRIMARY KEY REFERENCES runs(id),
        task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
        fee_milli INTEGER NOT NULL CHECK (fee_milli >= 0),
        created_at INTEGER NOT NULL
    );

    CREATE TABLE ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT REFERENCES tasks(id),
        run_id TEXT REFERENCES runs(id),
        account_id TEXT NOT NULL REFERENCES accounts(id),
        amount_milli INTEGER NOT NULL CHECK (amount_milli <> 0),
        kind TEXT NOT NULL CHECK (kind IN ('settle_debit','settle_credit','reversal')),
        reverses_id INTEGER REFERENCES ledger(id),
        created_at INTEGER NOT NULL
    );
    CREATE INDEX ledger_account ON ledger(account_id, created_at DESC);

    CREATE TABLE late_receipts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL REFERENCES runs(id),
        fencing_token INTEGER,
        receipt_json TEXT NOT NULL,
        reason TEXT NOT NULL,
        received_at INTEGER NOT NULL
    );

    CREATE TABLE idempotency (
        scope TEXT NOT NULL,
        principal TEXT NOT NULL,
        key TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        response_json TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        PRIMARY KEY (scope, principal, key)
    );

    CREATE TABLE profiles (
        account_id TEXT PRIMARY KEY REFERENCES accounts(id),
        display_name TEXT NOT NULL DEFAULT '',
        bio TEXT NOT NULL DEFAULT '',
        work_title TEXT NOT NULL DEFAULT '',
        work_url TEXT NOT NULL DEFAULT '',
        public INTEGER NOT NULL DEFAULT 0,
        version INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL
    );

    CREATE TABLE profile_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id TEXT NOT NULL REFERENCES accounts(id),
        version INTEGER NOT NULL,
        public INTEGER NOT NULL,
        snapshot_json TEXT NOT NULL,
        created_at INTEGER NOT NULL
    );

    CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        actor TEXT NOT NULL,
        entity TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        data_json TEXT NOT NULL DEFAULT '{}',
        created_at INTEGER NOT NULL
    );
    CREATE INDEX events_entity ON events(entity, entity_id);
    """,
    # 0002 — pilot: GitHub identity, invites, session expiry, reports, showcase suspension
    """
    ALTER TABLE accounts ADD COLUMN github_id INTEGER;
    CREATE UNIQUE INDEX accounts_github ON accounts(github_id) WHERE github_id IS NOT NULL;
    ALTER TABLE sessions ADD COLUMN expires_at INTEGER;

    CREATE TABLE invites (
        code_hash TEXT PRIMARY KEY,
        note TEXT NOT NULL DEFAULT '',
        max_uses INTEGER NOT NULL CHECK (max_uses > 0),
        uses INTEGER NOT NULL DEFAULT 0,
        expires_at INTEGER,
        created_at INTEGER NOT NULL
    );

    CREATE TABLE reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reporter_id TEXT NOT NULL REFERENCES accounts(id),
        target_account_id TEXT NOT NULL REFERENCES accounts(id),
        reason TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        resolved_at INTEGER,
        resolution TEXT
    );

    ALTER TABLE profiles ADD COLUMN showcase_suspended INTEGER NOT NULL DEFAULT 0;
    """,
]


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)")
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    for i, sql in enumerate(MIGRATIONS, start=1):
        if i in applied:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in _split(sql):
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, strftime('%s','now'))", (i,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _split(sql: str) -> list[str]:
    # Strip line comments first so a ';' inside a comment never splits a statement.
    lines = [ln.split("--", 1)[0] for ln in sql.splitlines()]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Write transaction. BEGIN IMMEDIATE takes the write lock up front so
    read-check-write sequences cannot interleave between requests."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
