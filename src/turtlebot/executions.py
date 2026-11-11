"""Backfill exit prices from an IBKR trade export.

TWS only reports executions of the current day (at most about a week back) through the API. When a
position disappears while the bot is not running, or the reconciliation can only prove the exit
after the executions have aged out, the position is closed with an unknown exit price
(``"not reported by broker for N runs"``). A trade export from the IBKR portal fills that gap.

Two CSV formats are accepted:

* **Flex Query** (Reports > Flex Queries > Trades, CSV): one header row, columns such as
  ``Symbol``, ``Buy/Sell``, ``Quantity``, ``TradePrice``, ``DateTime``.
* **Activity Statement** (Reports > Statements, CSV): many sections in one file; only the rows of
  the ``Trades`` section are read (``Trades,Header,...`` / ``Trades,Data,Order,...``).

Matching is deliberately conservative: a closed position is only updated if the SELL executions of
its symbol between its entry date and its close date add up to its quantity (within one share).
Everything else is reported and left untouched.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from turtlebot.domain.models import Position
from turtlebot.exceptions import TurtleBotError
from turtlebot.persistence.store import StateStore

log = logging.getLogger(__name__)

QTY_TOLERANCE = 1.0
# IBKR exports report trade times in US/Eastern unless configured otherwise.
EXPORT_TZ = ZoneInfo("America/New_York")
REASON_PREFIX = "exit price from trade export"

_SYMBOL = ("symbol",)
_SIDE = ("buy/sell", "side", "action")
_QTY = ("quantity", "qty", "shares")
_PRICE = ("tradeprice", "t. price", "price")
_TIME = ("datetime", "date/time", "tradedate", "trade date", "date")


class ExecutionImportError(TurtleBotError):
    """The trade export could not be read."""


@dataclass(frozen=True, slots=True)
class Execution:
    """One execution from the trade export (quantity always positive)."""

    symbol: str
    side: str  # "BUY" or "SELL"
    qty: float
    price: float
    time: dt.datetime


@dataclass(frozen=True, slots=True)
class BackfillResult:
    """Outcome for one closed position."""

    symbol: str
    position_id: int | None
    status: str  # "updated", "would_update", "qty_mismatch", "no_executions"
    detail: str
    exit_price: float | None = None


# ------------------------------------------------------------------------------ parsing
def _column(header: Sequence[str], aliases: Sequence[str]) -> int | None:
    normalized = [h.strip().lower() for h in header]
    for alias in aliases:
        if alias in normalized:
            return normalized.index(alias)
    return None


def _parse_time(raw: str) -> dt.datetime:
    text = raw.strip().replace(",", " ").replace(";", " ")
    for fmt in ("%Y%m%d %H%M%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d  %H:%M:%S", "%Y%m%d", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, fmt).replace(tzinfo=EXPORT_TZ)
        except ValueError:
            continue
    raise ExecutionImportError(f"unrecognised date/time {raw!r}")


def _parse_number(raw: str) -> float:
    try:
        return float(raw.replace(",", "").replace("'", "").strip())
    except ValueError as exc:
        raise ExecutionImportError(f"not a number: {raw!r}") from exc


def _rows_from_table(header: Sequence[str], rows: Iterable[Sequence[str]]) -> list[Execution]:
    i_sym = _column(header, _SYMBOL)
    i_qty = _column(header, _QTY)
    i_price = _column(header, _PRICE)
    i_time = _column(header, _TIME)
    i_side = _column(header, _SIDE)
    if i_sym is None or i_qty is None or i_price is None or i_time is None:
        raise ExecutionImportError(
            f"missing columns: need symbol, quantity, price and date/time (found {list(header)})"
        )
    result: list[Execution] = []
    for row in rows:
        if len(row) <= max(i_sym, i_qty, i_price, i_time) or not row[i_sym].strip():
            continue
        qty = _parse_number(row[i_qty])
        if qty == 0:
            continue
        if i_side is not None and row[i_side].strip():
            side = "SELL" if row[i_side].strip().upper().startswith("S") else "BUY"
        else:
            side = "SELL" if qty < 0 else "BUY"
        result.append(
            Execution(
                symbol=row[i_sym].strip().replace(" ", "-"),
                side=side,
                qty=abs(qty),
                price=_parse_number(row[i_price]),
                time=_parse_time(row[i_time]),
            )
        )
    return result


def parse_trade_export(text: str) -> list[Execution]:
    """Parse a Flex Query or Activity Statement CSV.

    Raises:
        ExecutionImportError: If no trade table can be found.
    """
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if any(cell.strip() for cell in r)]
    if not rows:
        raise ExecutionImportError("file is empty")
    # Activity Statement: "Trades,Header,..." followed by "Trades,Data,Order,...".
    statement_header = next(
        (r for r in rows if len(r) > 2 and r[0] == "Trades" and r[1] == "Header"), None
    )
    if statement_header is not None:
        data = [
            r
            for r in rows
            if len(r) > 2 and r[0] == "Trades" and r[1] == "Data" and r[2].strip() == "Order"
        ]
        return _rows_from_table(statement_header, data)
    return _rows_from_table(rows[0], rows[1:])


def load_trade_export(path: Path) -> list[Execution]:
    """Read and parse a trade export file."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ExecutionImportError(f"cannot read {path}: {exc}") from exc
    return parse_trade_export(text)


# ------------------------------------------------------------------------------ matching
def needs_exit_price(p: Position) -> bool:
    """Closed positions whose exit price is unknown or came from a single partial fill."""
    if p.closed_at is None:
        return False
    if p.exit_price is None:
        return True
    return (p.close_reason or "") == "exit execution found"


def match_exit(p: Position, executions: Sequence[Execution]) -> BackfillResult:
    """Exit price of ``p`` from the SELL executions between its entry and its close."""
    assert p.closed_at is not None
    start = p.entry_date or dt.date.min
    end = p.closed_at.date()
    sells = [
        e
        for e in executions
        if e.symbol == p.symbol and e.side == "SELL" and start <= e.time.date() <= end
    ]
    if not sells:
        return BackfillResult(p.symbol, p.id, "no_executions", f"no SELL between {start} and {end}")
    qty = sum(e.qty for e in sells)
    if abs(qty - p.qty) > QTY_TOLERANCE:
        return BackfillResult(
            p.symbol,
            p.id,
            "qty_mismatch",
            f"sold {qty:g} between {start} and {end}, position was {p.qty:g}",
        )
    price = sum(e.qty * e.price for e in sells) / qty
    last = max(e.time for e in sells)
    return BackfillResult(
        p.symbol,
        p.id,
        "would_update",
        f"{len(sells)} execution(s), {qty:g} shares, last {last:%Y-%m-%d}",
        exit_price=price,
    )


def backfill_exit_prices(
    store: StateStore,
    executions: Sequence[Execution],
    *,
    apply: bool,
    now: dt.datetime,
) -> list[BackfillResult]:
    """Fill in exit prices of closed positions from ``executions``.

    Args:
        store: State store.
        executions: Parsed trade export.
        apply: Write the prices (otherwise only report what would change).
        now: Timestamp for the journal event.
    """
    results: list[BackfillResult] = []
    for p in store.closed_positions():
        if not needs_exit_price(p):
            continue
        result = match_exit(p, executions)
        if result.status == "would_update" and apply:
            assert result.exit_price is not None
            previous = p.exit_price
            store.update_position(
                p.evolve(
                    exit_price=result.exit_price,
                    close_reason=f"{REASON_PREFIX} ({p.close_reason})",
                )
            )
            store.record_event(
                "EXIT_PRICE_BACKFILL",
                ts=now,
                symbol=p.symbol,
                block=p.block,
                qty=p.qty,
                price=result.exit_price,
                details={"previous": previous, "detail": result.detail},
            )
            log.info("%s: exit price %.4f (%s)", p.symbol, result.exit_price, result.detail)
            result = BackfillResult(
                p.symbol, p.id, "updated", result.detail, exit_price=result.exit_price
            )
        results.append(result)
    return results
