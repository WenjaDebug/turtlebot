"""One-off import of the legacy JSON state and CSV journal into the SQLite store.

* ``live_turtle_state.json`` -> active (pending/open) positions. The legacy state file is the
  authority for what is currently held, but **not** for stop prices: they are imported as
  unconfirmed and checked against the live TWS stop orders in the first reconciliation.
* ``live_turtle_journal.csv`` -> events (full history) and closed trades.

Closed trades are reconstructed from FILL/EXIT pairs. EXIT rows without price written by the
12.10.2026 reconcile bug are recognised as false alarms when the position was demonstrably still
alive afterwards (later STOP_NEW/STOP_UPDATE before a new FILL, or still in the state file).
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from turtlebot.domain.models import Block, Position, PositionStatus
from turtlebot.exceptions import StateError
from turtlebot.persistence.store import StateStore

log = logging.getLogger(__name__)

_BLOCK_RE = re.compile(r"block=(\d)")
_ALIVE_EVENTS = frozenset({"STOP_NEW", "STOP_UPDATE"})


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """Counts of imported objects."""

    active_positions: int
    events: int
    closed_trades: int
    false_exits_ignored: int


def _float(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _block(details: str, default: int = 1) -> Block:
    match = _BLOCK_RE.search(details or "")
    return Block(int(match.group(1)) if match else default)


def _date(value: object) -> dt.date | None:
    return dt.date.fromisoformat(str(value)[:10]) if value else None


@dataclass(slots=True)
class _OpenTrade:
    block: Block
    qty: float
    entry_price: float
    entry_date: dt.date
    stop: float | None


def _active_positions(state: dict[str, Any]) -> list[Position]:
    result = []
    for symbol, p in state.get("positions", {}).items():
        status = PositionStatus(p.get("status", "open"))
        if status is PositionStatus.CLOSED:
            continue
        signal_time = p.get("signal_time")
        result.append(
            Position(
                symbol=symbol,
                block=Block(int(p.get("block", 1))),
                status=status,
                qty=float(p["qty"]),
                stop=float(p["stop"]),
                signal_price=float(p.get("signal_price") or p.get("entry_price") or 0.0),
                entry_price=_float(str(p.get("entry_price", ""))),
                entry_date=_date(p.get("entry_date")),
                parent_order_id=p.get("parent_id"),
                stop_order_id=p.get("stop_id"),
                signal_time=dt.datetime.fromisoformat(signal_time) if signal_time else None,
                # The legacy bot stored the *intended* stop even when TWS rejected the
                # modification (13.-16.10.2026) - verify against TWS before trusting it.
                stop_confirmed=False,
            )
        )
    return result


def migrate_legacy(
    store: StateStore, state_file: Path, journal_file: Path, *, force: bool = False
) -> MigrationReport:
    """Import legacy files into an (empty) store.

    Args:
        store: Target store.
        state_file: ``live_turtle_state.json``.
        journal_file: ``live_turtle_journal.csv``.
        force: Import even if the store already contains positions or events.

    Returns:
        Import statistics.

    Raises:
        StateError: If the store is not empty and ``force`` is false, or a file is missing.
    """
    if not force and (store.active_positions() or store.events(limit=1)):
        raise StateError("target store is not empty; use force=True to import anyway")
    if not state_file.exists():
        raise StateError(f"legacy state file not found: {state_file}")
    state = json.loads(state_file.read_text())
    active = _active_positions(state)
    held = {p.symbol for p in active}
    rows: list[dict[str, str]] = []
    if journal_file.exists():
        with journal_file.open(newline="") as f:
            rows = [r for r in csv.DictReader(f) if r.get("event") and r["event"] != "event"]

    open_trades: dict[str, _OpenTrade] = {}
    pending_exit: dict[str, tuple[_OpenTrade, dict[str, str]]] = {}
    closed: list[Position] = []
    false_exits = 0

    def finalize(symbol: str) -> None:
        trade, row = pending_exit.pop(symbol)
        closed.append(
            Position(
                symbol=symbol,
                block=trade.block,
                status=PositionStatus.CLOSED,
                qty=trade.qty,
                stop=trade.stop or 0.0,
                signal_price=trade.entry_price,
                entry_price=trade.entry_price,
                entry_date=trade.entry_date,
                exit_price=_float(row.get("price")),
                closed_at=dt.datetime.fromisoformat(row["time"]),
                close_reason="legacy journal",
            )
        )

    with store.transaction():
        for row in rows:
            event, symbol = row["event"], row.get("symbol", "")
            ts = dt.datetime.fromisoformat(row["time"])
            details = row.get("details", "")
            store.record_event(
                event,
                ts=ts,
                symbol=symbol or None,
                block=_block(details) if symbol and event != "SIGNAL" else None,
                qty=_float(row.get("qty")),
                price=_float(row.get("price")),
                stop=_float(row.get("stop")),
                strength=_float(row.get("strength")),
                details={"legacy": details} if details else None,
            )
            if event == "FILL":
                if symbol in pending_exit:
                    finalize(symbol)
                price = _float(row.get("price"))
                qty = _float(row.get("qty"))
                if price is not None and qty is not None:
                    open_trades[symbol] = _OpenTrade(
                        _block(details), qty, price, ts.date(), _float(row.get("stop"))
                    )
            elif event == "EXIT" and symbol in open_trades:
                pending_exit[symbol] = (open_trades.pop(symbol), row)
            elif event == "EXIT" and symbol in pending_exit:
                pending_exit[symbol] = (pending_exit[symbol][0], row)  # the later exit counts
            elif event in _ALIVE_EVENTS and symbol in pending_exit:
                # Position was still managed after the "exit" -> the EXIT was a false alarm.
                trade, _ = pending_exit.pop(symbol)
                open_trades[symbol] = trade
                false_exits += 1

        for symbol in list(pending_exit):
            if symbol in held:
                pending_exit.pop(symbol)
                false_exits += 1
            else:
                finalize(symbol)

        for position in active:
            store.insert_position(position)
        for position in closed:
            store.insert_position(position)

    report = MigrationReport(len(active), len(rows), len(closed), false_exits)
    log.info("Legacy import: %s", report)
    return report
