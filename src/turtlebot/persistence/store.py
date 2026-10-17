"""SQLite-backed state store: the bot's local source of truth.

Positions, order events, signals, day counters, job runs and reconciliation results all live in
one SQLite file (WAL mode, so the dashboard can read while the bot writes). Unlike the legacy JSON
file, which was rewritten as a whole every cycle, every change is an explicit, transactional
update and the history is never overwritten.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from turtlebot.domain.models import AccountSnapshot, Block, Candidate, Position, PositionStatus
from turtlebot.exceptions import PersistenceError
from turtlebot.persistence.schema import MIGRATIONS, SCHEMA_VERSION

log = logging.getLogger(__name__)

_POSITION_COLUMNS = (
    "symbol, block, status, qty, stop, signal_price, entry_price, entry_date, parent_order_id, "
    "stop_order_id, signal_time, missing_cycles, exit_price, closed_at, close_reason"
)


def _iso(value: dt.datetime | dt.date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass(frozen=True, slots=True)
class EventRecord:
    """One row of the event journal."""

    id: int
    ts: dt.datetime
    kind: str
    symbol: str | None
    block: int | None
    qty: float | None
    price: float | None
    stop: float | None
    strength: float | None
    order_id: int | None
    details: dict[str, Any]


@dataclass(frozen=True, slots=True)
class JobRecord:
    """Status of a scheduled job on one day."""

    status: str
    attempts: int
    last_error: str | None


@dataclass(frozen=True, slots=True)
class DiscrepancyRecord:
    """A persisted reconciliation discrepancy."""

    kind: str
    severity: str
    symbol: str | None
    local: str | None
    broker: str | None
    resolution: str | None


@dataclass(frozen=True, slots=True)
class ReconciliationRecord:
    """A persisted reconciliation run."""

    id: int
    ts: dt.datetime
    outcome: str
    update_stops: bool
    n_local: int
    n_broker: int
    n_actions: int
    message: str | None
    discrepancies: list[DiscrepancyRecord]


class StateStore:
    """Transactional access to the SQLite state database.

    The store is safe to share between threads; writes are serialised by a lock. Every public
    write method runs in its own transaction unless called inside :meth:`transaction`.

    Args:
        path: Database file, or ``":memory:"`` for tests.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        except sqlite3.Error as exc:
            raise PersistenceError(f"cannot open state database {self.path}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._depth = 0
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        try:
            self._migrate()
        except PersistenceError:
            self._conn.close()
            raise

    # ------------------------------------------------------------------ infrastructure
    def _migrate(self) -> None:
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise PersistenceError(
                f"database schema v{version} is newer than this code (v{SCHEMA_VERSION})"
            )
        for target, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            log.info("Migrating state database to schema v%d", target)
            try:
                self._conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {target};\nCOMMIT;")
            except sqlite3.Error as exc:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise PersistenceError(f"migration to v{target} failed: {exc}") from exc

    def close(self) -> None:
        """Close the connection."""
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Group several writes atomically (re-entrant).

        Yields:
            The underlying connection.

        Raises:
            PersistenceError: If SQLite reports an error; the transaction is rolled back.
        """
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self._conn
            except sqlite3.Error as exc:
                if outer:
                    self._conn.execute("ROLLBACK")
                raise PersistenceError(str(exc)) from exc
            except BaseException:
                if outer:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                if outer:
                    self._conn.execute("COMMIT")
            finally:
                self._depth -= 1

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            try:
                return self._conn.execute(sql, params).fetchall()
            except sqlite3.Error as exc:
                raise PersistenceError(str(exc)) from exc

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str) -> str | None:
        """Read a key/value entry."""
        rows = self._query("SELECT value FROM meta WHERE key = ?", (key,))
        return str(rows[0]["value"]) if rows else None

    def set_meta(self, key: str, value: str) -> None:
        """Write a key/value entry."""
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # ------------------------------------------------------------------ positions
    @staticmethod
    def _row_to_position(row: sqlite3.Row) -> Position:
        return Position(
            id=int(row["id"]),
            symbol=row["symbol"],
            block=Block(row["block"]),
            status=PositionStatus(row["status"]),
            qty=float(row["qty"]),
            stop=float(row["stop"]),
            signal_price=float(row["signal_price"]),
            entry_price=row["entry_price"],
            entry_date=dt.date.fromisoformat(row["entry_date"]) if row["entry_date"] else None,
            parent_order_id=row["parent_order_id"],
            stop_order_id=row["stop_order_id"],
            signal_time=(
                dt.datetime.fromisoformat(row["signal_time"]) if row["signal_time"] else None
            ),
            missing_cycles=int(row["missing_cycles"]),
            exit_price=row["exit_price"],
            closed_at=dt.datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None,
            close_reason=row["close_reason"],
        )

    @staticmethod
    def _position_values(p: Position) -> tuple[Any, ...]:
        return (
            p.symbol,
            int(p.block),
            p.status.value,
            p.qty,
            p.stop,
            p.signal_price,
            p.entry_price,
            _iso(p.entry_date),
            p.parent_order_id,
            p.stop_order_id,
            _iso(p.signal_time),
            p.missing_cycles,
            p.exit_price,
            _iso(p.closed_at),
            p.close_reason,
        )

    def active_positions(self, block: Block | None = None) -> list[Position]:
        """Pending and open positions, optionally of one block."""
        sql = "SELECT * FROM positions WHERE status IN ('pending', 'open')"
        params: tuple[Any, ...] = ()
        if block is not None:
            sql += " AND block = ?"
            params = (int(block),)
        return [self._row_to_position(r) for r in self._query(sql + " ORDER BY symbol", params)]

    def active_position(self, symbol: str) -> Position | None:
        """The active position of ``symbol``, if any."""
        rows = self._query(
            "SELECT * FROM positions WHERE symbol = ? AND status IN ('pending', 'open')", (symbol,)
        )
        return self._row_to_position(rows[0]) if rows else None

    def closed_positions(self, limit: int | None = None) -> list[Position]:
        """Closed positions, most recent first."""
        sql = "SELECT * FROM positions WHERE status = 'closed' ORDER BY closed_at DESC, id DESC"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return [self._row_to_position(r) for r in self._query(sql)]

    def insert_position(self, position: Position) -> Position:
        """Insert a new position and return it with its id.

        Raises:
            PersistenceError: If the symbol already has an active position.
        """
        now = _iso(_utcnow())
        placeholders = ", ".join("?" * 17)
        with self.transaction() as conn:
            cur = conn.execute(
                f"INSERT INTO positions ({_POSITION_COLUMNS}, created_at, updated_at) "  # noqa: S608
                f"VALUES ({placeholders})",
                (*self._position_values(position), now, now),
            )
        return position.evolve(id=cur.lastrowid)

    def update_position(self, position: Position) -> None:
        """Persist all fields of an existing position.

        Raises:
            PersistenceError: If the position has no id or does not exist.
        """
        if position.id is None:
            raise PersistenceError(f"{position.symbol}: cannot update a position without id")
        assignments = ", ".join(f"{c.strip()} = ?" for c in _POSITION_COLUMNS.split(","))
        with self.transaction() as conn:
            cur = conn.execute(
                f"UPDATE positions SET {assignments}, updated_at = ? WHERE id = ?",  # noqa: S608
                (*self._position_values(position), _iso(_utcnow()), position.id),
            )
            if cur.rowcount != 1:
                raise PersistenceError(f"position id {position.id} not found")

    def close_position(
        self,
        position: Position,
        *,
        reason: str,
        exit_price: float | None,
        closed_at: dt.datetime,
    ) -> Position:
        """Mark a position as closed and return the updated object."""
        closed = position.evolve(
            status=PositionStatus.CLOSED,
            exit_price=exit_price,
            closed_at=closed_at,
            close_reason=reason,
            missing_cycles=0,
        )
        self.update_position(closed)
        return closed

    # ------------------------------------------------------------------ events
    def record_event(
        self,
        kind: str,
        *,
        ts: dt.datetime,
        symbol: str | None = None,
        block: Block | int | None = None,
        qty: float | None = None,
        price: float | None = None,
        stop: float | None = None,
        strength: float | None = None,
        order_id: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> int:
        """Append an event to the journal and return its id."""
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO events (ts, kind, symbol, block, qty, price, stop, strength, "
                "order_id, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _iso(ts),
                    kind,
                    symbol,
                    int(block) if block is not None else None,
                    qty,
                    price,
                    stop,
                    strength,
                    order_id,
                    json.dumps(dict(details), default=str) if details else None,
                ),
            )
        return int(cur.lastrowid or 0)

    def events(
        self, *, limit: int = 100, kinds: Sequence[str] | None = None, after_id: int = 0
    ) -> list[EventRecord]:
        """Most recent events (newest first)."""
        sql = "SELECT * FROM events WHERE id > ?"
        params: list[Any] = [after_id]
        if kinds:
            sql += f" AND kind IN ({', '.join('?' * len(kinds))})"
            params.extend(kinds)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [
            EventRecord(
                id=r["id"],
                ts=dt.datetime.fromisoformat(r["ts"]),
                kind=r["kind"],
                symbol=r["symbol"],
                block=r["block"],
                qty=r["qty"],
                price=r["price"],
                stop=r["stop"],
                strength=r["strength"],
                order_id=r["order_id"],
                details=json.loads(r["details"]) if r["details"] else {},
            )
            for r in self._query(sql, params)
        ]

    # ------------------------------------------------------------------ signals
    def record_signals(
        self, scan_date: dt.date, candidates: Sequence[Candidate], ts: dt.datetime
    ) -> None:
        """Store all signals of a scan."""
        with self.transaction() as conn:
            conn.executemany(
                "INSERT INTO signals (ts, scan_date, symbol, price, stop, strength, volume_ratio) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        _iso(ts),
                        scan_date.isoformat(),
                        c.ticker,
                        c.price,
                        c.stop,
                        c.strength,
                        c.volume_ratio,
                    )
                    for c in candidates
                ],
            )

    def signals(self, scan_date: dt.date | None = None) -> list[dict[str, Any]]:
        """Signals of one scan date (default: the latest one)."""
        if scan_date is None:
            rows = self._query("SELECT MAX(scan_date) AS d FROM signals")
            if not rows or rows[0]["d"] is None:
                return []
            day = str(rows[0]["d"])
        else:
            day = scan_date.isoformat()
        rows = self._query(
            "SELECT * FROM signals WHERE scan_date = ? ORDER BY strength DESC, id", (day,)
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ day counters
    def entries_today(self, day: dt.date, block: Block) -> int:
        """Entries sent by ``block`` on ``day``."""
        rows = self._query(
            "SELECT entries FROM day_entries WHERE day = ? AND block = ?",
            (day.isoformat(), int(block)),
        )
        return int(rows[0]["entries"]) if rows else 0

    def increment_entries(self, day: dt.date, block: Block) -> None:
        """Count one more entry for ``block`` on ``day``."""
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO day_entries(day, block, entries) VALUES (?, ?, 1) "
                "ON CONFLICT(day, block) DO UPDATE SET entries = entries + 1",
                (day.isoformat(), int(block)),
            )

    def traded_today(self, day: dt.date) -> set[str]:
        """Symbols bought or sold on ``day`` (both blocks)."""
        rows = self._query("SELECT symbol FROM day_traded WHERE day = ?", (day.isoformat(),))
        return {str(r["symbol"]) for r in rows}

    def mark_traded(self, day: dt.date, symbol: str) -> None:
        """Remember that ``symbol`` was traded on ``day``."""
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO day_traded(day, symbol) VALUES (?, ?)",
                (day.isoformat(), symbol),
            )

    # ------------------------------------------------------------------ jobs
    def job(self, day: dt.date, name: str) -> JobRecord | None:
        """Status of job ``name`` on ``day``."""
        rows = self._query(
            "SELECT status, attempts, last_error FROM jobs WHERE day = ? AND name = ?",
            (day.isoformat(), name),
        )
        if not rows:
            return None
        r = rows[0]
        return JobRecord(
            status=r["status"], attempts=int(r["attempts"]), last_error=r["last_error"]
        )

    def set_job(
        self, day: dt.date, name: str, status: str, *, attempts: int = 0, error: str | None = None
    ) -> None:
        """Record the status of a job run."""
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO jobs(day, name, status, attempts, last_error, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(day, name) DO UPDATE SET "
                "status = excluded.status, attempts = excluded.attempts, "
                "last_error = excluded.last_error, updated_at = excluded.updated_at",
                (day.isoformat(), name, status, attempts, error, _iso(_utcnow())),
            )

    # ------------------------------------------------------------------ reconciliation
    def record_reconciliation(
        self,
        *,
        ts: dt.datetime,
        outcome: str,
        update_stops: bool,
        n_local: int,
        n_broker: int,
        n_actions: int,
        message: str | None,
        discrepancies: Sequence[DiscrepancyRecord],
    ) -> int:
        """Persist a reconciliation run and its discrepancies."""
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO reconciliations (ts, outcome, update_stops, n_local, n_broker, "
                "n_actions, message) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_iso(ts), outcome, int(update_stops), n_local, n_broker, n_actions, message),
            )
            run_id = int(cur.lastrowid or 0)
            conn.executemany(
                "INSERT INTO discrepancies (run_id, kind, severity, symbol, local, broker, "
                "resolution) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (run_id, d.kind, d.severity, d.symbol, d.local, d.broker, d.resolution)
                    for d in discrepancies
                ],
            )
        return run_id

    def reconciliations(self, limit: int = 20) -> list[ReconciliationRecord]:
        """Most recent reconciliation runs with their discrepancies."""
        runs = self._query("SELECT * FROM reconciliations ORDER BY id DESC LIMIT ?", (limit,))
        result = []
        for r in runs:
            discs = self._query(
                "SELECT kind, severity, symbol, local, broker, resolution FROM discrepancies "
                "WHERE run_id = ? ORDER BY id",
                (r["id"],),
            )
            result.append(
                ReconciliationRecord(
                    id=r["id"],
                    ts=dt.datetime.fromisoformat(r["ts"]),
                    outcome=r["outcome"],
                    update_stops=bool(r["update_stops"]),
                    n_local=r["n_local"],
                    n_broker=r["n_broker"],
                    n_actions=r["n_actions"],
                    message=r["message"],
                    discrepancies=[DiscrepancyRecord(**dict(d)) for d in discs],
                )
            )
        return result

    def orphan_cycles(self) -> dict[int, int]:
        """Observation counters of orphaned orders (order id -> consecutive cycles)."""
        return {
            int(r["order_id"]): int(r["cycles"])
            for r in self._query("SELECT order_id, cycles FROM orphan_orders")
        }

    def replace_orphan_cycles(
        self, cycles: Mapping[int, int], symbols: Mapping[int, str], ts: dt.datetime
    ) -> None:
        """Replace the orphan counters with the latest observation."""
        with self.transaction() as conn:
            known = {
                int(r["order_id"]): str(r["first_seen"])
                for r in conn.execute("SELECT order_id, first_seen FROM orphan_orders")
            }
            conn.execute("DELETE FROM orphan_orders")
            conn.executemany(
                "INSERT INTO orphan_orders(order_id, symbol, cycles, first_seen) VALUES (?,?,?,?)",
                [
                    (oid, symbols.get(oid, "?"), n, known.get(oid, _iso(ts)))
                    for oid, n in cycles.items()
                ],
            )

    # ------------------------------------------------------------------ account
    def record_account(self, snapshot: AccountSnapshot, ts: dt.datetime) -> None:
        """Store an account snapshot (one row per timestamp)."""
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO account_snapshots (ts, net_liquidation, currency, "
                "usd_per_base, daily_pnl, available_funds) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    _iso(ts),
                    snapshot.net_liquidation,
                    snapshot.currency,
                    snapshot.usd_per_base,
                    snapshot.daily_pnl,
                    snapshot.available_funds,
                ),
            )

    def account_history(self) -> list[tuple[dt.datetime, AccountSnapshot]]:
        """All account snapshots in chronological order."""
        return [
            (
                dt.datetime.fromisoformat(r["ts"]),
                AccountSnapshot(
                    net_liquidation=r["net_liquidation"],
                    currency=r["currency"],
                    available_funds=r["available_funds"],
                    daily_pnl=r["daily_pnl"],
                    usd_per_base=r["usd_per_base"],
                ),
            )
            for r in self._query("SELECT * FROM account_snapshots ORDER BY ts")
        ]

    def data_version(self) -> int:
        """Monotonic change marker (largest event id + reconciliation/account counts).

        Used by the dashboard to push updates only when something changed.
        """
        row = self._query(
            "SELECT (SELECT IFNULL(MAX(id), 0) FROM events) * 1000003 "
            "+ (SELECT COUNT(*) FROM account_snapshots) * 31 "
            "+ (SELECT IFNULL(MAX(id), 0) FROM reconciliations) AS v"
        )[0]
        return int(row["v"])
