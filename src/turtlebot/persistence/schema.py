"""SQLite schema and migrations.

Migrations are append-only: each entry upgrades the schema by one version and is applied inside a
transaction. ``PRAGMA user_version`` stores the current version.
"""

from __future__ import annotations

from typing import Final

MIGRATIONS: Final[tuple[str, ...]] = (
    # v1 ---------------------------------------------------------------------------------------
    """
    CREATE TABLE meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE positions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol          TEXT    NOT NULL,
        block           INTEGER NOT NULL CHECK (block IN (1, 2)),
        status          TEXT    NOT NULL CHECK (status IN ('pending', 'open', 'closed')),
        qty             REAL    NOT NULL,
        stop            REAL    NOT NULL,
        signal_price    REAL    NOT NULL,
        entry_price     REAL,
        entry_date      TEXT,
        parent_order_id INTEGER,
        stop_order_id   INTEGER,
        signal_time     TEXT,
        missing_cycles  INTEGER NOT NULL DEFAULT 0,
        exit_price      REAL,
        closed_at       TEXT,
        close_reason    TEXT,
        created_at      TEXT    NOT NULL,
        updated_at      TEXT    NOT NULL
    );
    -- At most one active (pending/open) position per symbol across both blocks.
    CREATE UNIQUE INDEX ux_positions_active ON positions(symbol)
        WHERE status IN ('pending', 'open');
    CREATE INDEX ix_positions_status ON positions(status);

    CREATE TABLE events (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        ts       TEXT NOT NULL,
        kind     TEXT NOT NULL,
        symbol   TEXT,
        block    INTEGER,
        qty      REAL,
        price    REAL,
        stop     REAL,
        strength REAL,
        order_id INTEGER,
        details  TEXT
    );
    CREATE INDEX ix_events_ts ON events(ts);
    CREATE INDEX ix_events_kind ON events(kind);

    CREATE TABLE signals (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts           TEXT NOT NULL,
        scan_date    TEXT NOT NULL,
        symbol       TEXT NOT NULL,
        price        REAL NOT NULL,
        stop         REAL NOT NULL,
        strength     REAL NOT NULL,
        volume_ratio REAL NOT NULL
    );
    CREATE INDEX ix_signals_scan_date ON signals(scan_date);

    CREATE TABLE day_entries (
        day     TEXT    NOT NULL,
        block   INTEGER NOT NULL,
        entries INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (day, block)
    );

    CREATE TABLE day_traded (
        day    TEXT NOT NULL,
        symbol TEXT NOT NULL,
        PRIMARY KEY (day, symbol)
    );

    CREATE TABLE jobs (
        day        TEXT    NOT NULL,
        name       TEXT    NOT NULL,
        status     TEXT    NOT NULL CHECK (status IN ('done', 'failed', 'skipped')),
        attempts   INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        updated_at TEXT    NOT NULL,
        PRIMARY KEY (day, name)
    );

    CREATE TABLE reconciliations (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ts              TEXT    NOT NULL,
        outcome         TEXT    NOT NULL,
        update_stops    INTEGER NOT NULL,
        n_local         INTEGER NOT NULL,
        n_broker        INTEGER NOT NULL,
        n_actions       INTEGER NOT NULL,
        message         TEXT
    );

    CREATE TABLE discrepancies (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id     INTEGER NOT NULL REFERENCES reconciliations(id) ON DELETE CASCADE,
        kind       TEXT    NOT NULL,
        severity   TEXT    NOT NULL,
        symbol     TEXT,
        local      TEXT,
        broker     TEXT,
        resolution TEXT
    );
    CREATE INDEX ix_discrepancies_run ON discrepancies(run_id);

    CREATE TABLE orphan_orders (
        order_id   INTEGER PRIMARY KEY,
        symbol     TEXT    NOT NULL,
        cycles     INTEGER NOT NULL,
        first_seen TEXT    NOT NULL
    );

    CREATE TABLE account_snapshots (
        ts              TEXT PRIMARY KEY,
        net_liquidation REAL NOT NULL,
        currency        TEXT NOT NULL,
        usd_per_base    REAL NOT NULL,
        daily_pnl       REAL,
        available_funds REAL
    );
    """,
    # v2 ---------------------------------------------------------------------------------------
    # Stops imported from the legacy state file are unverified until compared with TWS. A v1
    # database was only ever filled by that import (no reconciliation ran on it), so its active
    # stops start unverified as well.
    """
    ALTER TABLE positions ADD COLUMN stop_confirmed INTEGER NOT NULL DEFAULT 1;
    UPDATE positions SET stop_confirmed = 0 WHERE status IN ('pending', 'open');
    """,
)

SCHEMA_VERSION: Final = len(MIGRATIONS)
