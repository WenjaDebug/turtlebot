"""Broker-independent domain model.

These immutable value objects are the vocabulary shared by the strategy, the risk layer, the
persistence layer, the reconciliation and the broker adapter. None of them depends on
``ib_async``, which keeps every decision function testable without TWS.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from enum import IntEnum, StrEnum


class Block(IntEnum):
    """Independent slot pool of the strategy."""

    ONE = 1
    TWO = 2


class PositionStatus(StrEnum):
    """Lifecycle of a position tracked by the bot."""

    PENDING = "pending"  # entry order sent, not (fully) filled yet
    OPEN = "open"  # shares held, protected by a stop order
    CLOSED = "closed"  # exited (stop, manual) or entry cancelled


class OrderAction(StrEnum):
    """Order side."""

    BUY = "BUY"
    SELL = "SELL"


class EntryMode(StrEnum):
    """How an entry order is executed."""

    MOC = "moc"  # market-on-close, today's closing auction
    OPEN = "open"  # market-on-open, next morning (OPG)
    DAY = "day"  # immediate market order (legacy intraday test mode)


@dataclass(frozen=True, slots=True)
class Candidate:
    """A breakout signal that may become an entry.

    Attributes:
        ticker: Symbol in yfinance notation (``BRK-B``).
        price: Last close used for sizing.
        stop: Initial stop price (lowest low of the exit period).
        strength: Breakout strength, ``close / donchian_high - 1``.
        volume_ratio: Volume relative to its moving average.
    """

    ticker: str
    price: float
    stop: float
    strength: float
    volume_ratio: float


@dataclass(frozen=True, slots=True)
class Position:
    """A position as tracked in the local state store (the bot's source of truth).

    Attributes:
        symbol: Ticker in yfinance notation.
        block: Slot pool the position belongs to.
        status: Lifecycle status.
        qty: Number of shares (planned for pending, held for open).
        stop: Last known stop price.
        signal_price: Close of the signal day.
        entry_price: Average fill price, ``None`` while pending.
        entry_date: Exchange date of the fill.
        parent_order_id: Broker id of the entry order.
        stop_order_id: Broker id of the protective stop.
        missing_cycles: Consecutive reconciliations in which the broker did not report the
            position. Used to require confirmation before treating it as closed.
        stop_confirmed: ``False`` while ``stop`` is an unverified value (e.g. imported from the
            legacy state file) that has not yet been compared with the broker's stop order.
        id: Primary key in the store (``None`` before insertion).
    """

    symbol: str
    block: Block
    status: PositionStatus
    qty: float
    stop: float
    signal_price: float
    entry_price: float | None = None
    entry_date: dt.date | None = None
    parent_order_id: int | None = None
    stop_order_id: int | None = None
    signal_time: dt.datetime | None = None
    missing_cycles: int = 0
    stop_confirmed: bool = True
    exit_price: float | None = None
    closed_at: dt.datetime | None = None
    close_reason: str | None = None
    id: int | None = None

    @property
    def is_active(self) -> bool:
        """Whether the position still occupies a slot."""
        return self.status in (PositionStatus.PENDING, PositionStatus.OPEN)

    def evolve(self, **changes: object) -> Position:
        """Return a copy with ``changes`` applied (dataclass ``replace``)."""
        return replace(self, **changes)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    """A position as reported by the broker."""

    symbol: str
    qty: float
    avg_cost: float


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    """An open (working) order as reported by the broker.

    Attributes:
        client_id: API client id that placed the order. TWS only lets the owning client id
            modify or cancel an order; ``None`` if the broker did not report it.
        perm_id: Broker-wide permanent id (order ids are only unique per client id).
    """

    order_id: int
    symbol: str
    action: OrderAction
    order_type: str
    qty: float
    order_ref: str
    aux_price: float | None = None
    parent_id: int | None = None
    filled: float = 0.0
    client_id: int | None = None
    perm_id: int | None = None

    @property
    def is_stop(self) -> bool:
        """Whether this is a protective stop (``SELL STP``)."""
        return self.action is OrderAction.SELL and self.order_type == "STP"


@dataclass(frozen=True, slots=True)
class BrokerSnapshot:
    """Positions and the bot's own open orders at one point in time.

    Attributes:
        positions: Non-zero positions keyed by symbol.
        orders: Working orders of all API clients that carry one of the bot's order references,
            plus any other protective SELL stop on a held symbol.
        positions_synced: ``False`` if the broker has not confirmed the end of the position
            stream (e.g. right after a reconnect). Reconciliation never acts on such snapshots.
        taken_at: Timestamp of the snapshot.
        client_id: The bot's own API client id.
    """

    positions: dict[str, BrokerPosition]
    orders: list[BrokerOrder]
    positions_synced: bool
    taken_at: dt.datetime
    client_id: int | None = None

    def is_foreign(self, order: BrokerOrder) -> bool:
        """Whether ``order`` was placed by another API client id than the bot's.

        Only decidable if both ids are known; unknown ids are treated as own (the executor
        still refuses unconfirmed replacements, see the reconciliation service).
        """
        return (
            order.client_id is not None
            and self.client_id is not None
            and order.client_id != self.client_id
        )

    def orders_for(self, symbol: str) -> list[BrokerOrder]:
        """All working orders of one symbol."""
        return [o for o in self.orders if o.symbol == symbol]

    def stops_for(self, symbol: str) -> list[BrokerOrder]:
        """All working protective stops of one symbol."""
        return [o for o in self.orders if o.symbol == symbol and o.is_stop]

    def has_open_entry(self, symbol: str) -> bool:
        """Whether a BUY entry order for ``symbol`` is still working."""
        return any(o.symbol == symbol and o.action is OrderAction.BUY for o in self.orders)


@dataclass(frozen=True, slots=True)
class DailyBar:
    """One completed (or forming) daily session bar."""

    date: dt.date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class Fill:
    """A single execution."""

    symbol: str
    action: OrderAction
    qty: float
    price: float
    time: dt.datetime
    order_id: int | None = None


def combine_order_fills(fills: Sequence[Fill]) -> Fill | None:
    """Combine the partial executions of the most recent order into one fill.

    TWS reports every partial execution separately; a stop that sells 848 shares may arrive as
    several fills of e.g. 100 + 748 shares. Taking only the latest execution understates the
    quantity and gives a wrong exit price. This returns the fill of the most recent order with
    the summed quantity and the quantity-weighted average price.

    Args:
        fills: Executions of one symbol and side, in any order.

    Returns:
        ``None`` for an empty sequence, otherwise the combined fill of the latest order.
    """
    if not fills:
        return None
    latest = max(fills, key=lambda f: f.time)
    if latest.order_id is None:
        return latest
    same_order = [f for f in fills if f.order_id == latest.order_id]
    qty = sum(f.qty for f in same_order)
    if qty <= 0:
        return latest
    price = sum(f.qty * f.price for f in same_order) / qty
    return Fill(
        symbol=latest.symbol,
        action=latest.action,
        qty=qty,
        price=price,
        time=latest.time,
        order_id=latest.order_id,
    )


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Account values in the account's base currency.

    Attributes:
        net_liquidation: Net liquidation value.
        currency: Base currency, e.g. ``CHF``.
        available_funds: Available funds, if reported.
        daily_pnl: Today's P&L, ``None`` if unknown.
        usd_per_base: Conversion factor base currency -> USD.
    """

    net_liquidation: float
    currency: str
    available_funds: float | None
    daily_pnl: float | None
    usd_per_base: float

    @property
    def net_liquidation_usd(self) -> float:
        """Net liquidation converted to USD."""
        return self.net_liquidation * self.usd_per_base


class OrderStatus(StrEnum):
    """Normalised broker order status."""

    SUBMITTED = "Submitted"
    PARTIALLY_FILLED = "PartiallyFilled"
    FILLED = "Filled"
    CANCELLED = "Cancelled"
    INACTIVE = "Inactive"
    OTHER = "Other"


@dataclass(frozen=True, slots=True)
class OrderEvent:
    """Status update of one of the bot's orders, emitted by the broker adapter."""

    order_id: int
    symbol: str
    action: OrderAction
    order_type: str
    order_ref: str
    status: OrderStatus
    filled: float
    remaining: float
    avg_fill_price: float
    message: str = ""
    time: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))


@dataclass(frozen=True, slots=True)
class EntryTicket:
    """Broker ids of an entry bracket (parent + attached stop)."""

    parent_order_id: int | None
    stop_order_id: int | None
