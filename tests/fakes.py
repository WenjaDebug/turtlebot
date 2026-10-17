"""In-memory broker used instead of TWS in every test."""

from __future__ import annotations

import datetime as dt
import itertools
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace

import pandas as pd

from tests.factories import NY
from turtlebot.broker.gateway import OrderEventHandler
from turtlebot.domain.models import (
    AccountSnapshot,
    BrokerOrder,
    BrokerPosition,
    BrokerSnapshot,
    DailyBar,
    EntryMode,
    EntryTicket,
    Fill,
    OrderAction,
    OrderEvent,
    OrderStatus,
)
from turtlebot.exceptions import BrokerConnectionError, OrderRejectedError


def frame_bars(frame: pd.DataFrame | None) -> list[DailyBar]:
    """Daily bars of an OHLCV frame (a broker that agrees with the market data)."""
    if frame is None:
        return []
    cols = {c: frame[c].to_numpy(dtype=float) for c in ("Open", "High", "Low", "Close", "Volume")}
    return [
        DailyBar(
            ts.date(),
            cols["Open"][i],
            cols["High"][i],
            cols["Low"][i],
            cols["Close"][i],
            cols["Volume"][i],
        )
        for i, ts in enumerate(pd.DatetimeIndex(frame.index))
    ]


@dataclass
class FakeBroker:
    """Broker double with knobs for failure scenarios.

    Attributes:
        hide_positions: Simulate the 12.10. glitch - the position list comes back empty while
            orders are still reported.
        hide_symbols: Simulate a partially synced position list.
        synced: Value of ``positions_synced`` in snapshots.
        reject_modify: Order ids whose modification TWS rejects (dead ids).
        connect_failures: Number of upcoming ``connect()`` calls that fail.
        client_id: The bot's API client id. Orders of another client id behave like TWS treats
            them: modifications are rejected with error 103, cancellations have no effect.
        uncancellable: Own order ids whose cancellation is never confirmed.
        report_client_ids: If false, snapshots omit the order owner (``client_id=None``), as if
            TWS did not tell - only the executor's cancel-and-replace safeguard then helps.
    """

    positions: dict[str, BrokerPosition] = field(default_factory=dict)
    orders: dict[int, BrokerOrder] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    account_snapshot: AccountSnapshot = field(
        default_factory=lambda: AccountSnapshot(
            net_liquidation=100_000.0,
            currency="USD",
            available_funds=80_000.0,
            daily_pnl=0.0,
            usd_per_base=1.0,
        )
    )
    connected: bool = True
    synced: bool = True
    hide_positions: bool = False
    hide_symbols: set[str] = field(default_factory=set)
    reject_modify: set[int] = field(default_factory=set)
    connect_failures: int = 0
    client_id: int = 11
    uncancellable: set[int] = field(default_factory=set)
    report_client_ids: bool = True
    margin_excess: float | None = 90_000.0
    bars: dict[str, list[DailyBar]] = field(default_factory=dict)
    bar_source: Callable[[str], list[DailyBar]] | None = None
    calls: list[tuple[str, object]] = field(default_factory=list)
    entries: list[tuple[str, float, EntryMode, float, str]] = field(default_factory=list)
    slept: float = 0.0
    _handler: OrderEventHandler | None = None
    _ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1000))

    # ------------------------------------------------------------------ protocol
    def connect(self) -> None:
        self.calls.append(("connect", None))
        if self.connect_failures > 0:
            self.connect_failures -= 1
            raise BrokerConnectionError("TWS not reachable")
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected

    def sleep(self, seconds: float) -> None:
        self.slept += seconds

    def set_order_event_handler(self, handler: OrderEventHandler) -> None:
        self._handler = handler

    def _require_connection(self) -> None:
        if not self.connected:
            raise BrokerConnectionError("not connected")

    def account(self) -> AccountSnapshot:
        self._require_connection()
        return self.account_snapshot

    def snapshot(self) -> BrokerSnapshot:
        self._require_connection()
        visible = (
            {}
            if self.hide_positions
            else {s: p for s, p in self.positions.items() if s not in self.hide_symbols}
        )
        return BrokerSnapshot(
            positions=dict(visible),
            orders=[
                o if self.report_client_ids else replace(o, client_id=None)
                for o in self.orders.values()
            ],
            positions_synced=self.synced,
            taken_at=dt.datetime(2026, 10, 14, 9, 0, tzinfo=NY),
            client_id=self.client_id,
        )

    def recent_sell_fills(self, symbols: Sequence[str]) -> dict[str, Fill]:
        result: dict[str, Fill] = {}
        for fill in self.fills:
            if fill.symbol in symbols and fill.action is OrderAction.SELL:
                result[fill.symbol] = fill
        return result

    def margin_excess_after(self, symbol: str, qty: float) -> float | None:
        return self.margin_excess

    def daily_bars(self, symbol: str, sessions: int) -> list[DailyBar]:
        self.calls.append(("daily_bars", symbol))
        if symbol not in self.bars and self.bar_source is not None:
            return self.bar_source(symbol)[-sessions:]
        return self.bars.get(symbol, [])[-sessions:]

    def place_entry(
        self, symbol: str, qty: float, mode: EntryMode, stop_price: float, order_ref: str
    ) -> EntryTicket:
        self._require_connection()
        if symbol.startswith("BAD"):
            raise OrderRejectedError(f"{symbol}: contract not found")
        parent = next(self._ids)
        stop = next(self._ids)
        self.orders[parent] = BrokerOrder(
            parent,
            symbol,
            OrderAction.BUY,
            mode.value.upper(),
            qty,
            order_ref,
            client_id=self.client_id,
        )
        self.orders[stop] = BrokerOrder(
            stop,
            symbol,
            OrderAction.SELL,
            "STP",
            qty,
            order_ref,
            aux_price=stop_price,
            parent_id=parent,
            client_id=self.client_id,
        )
        self.calls.append(("place_entry", symbol))
        self.entries.append((symbol, qty, mode, stop_price, order_ref))
        return EntryTicket(parent, stop)

    def place_stop(self, symbol: str, qty: float, stop_price: float, order_ref: str) -> int:
        self._require_connection()
        oid = next(self._ids)
        self.orders[oid] = BrokerOrder(
            oid,
            symbol,
            OrderAction.SELL,
            "STP",
            qty,
            order_ref,
            aux_price=stop_price,
            client_id=self.client_id,
        )
        self.calls.append(("place_stop", (symbol, qty, stop_price)))
        return oid

    def _foreign(self, order_id: int) -> bool:
        order = self.orders.get(order_id)
        return order is not None and order.client_id not in (None, self.client_id)

    def modify_stop(self, order: BrokerOrder, qty: float, stop_price: float) -> None:
        self._require_connection()
        self.calls.append(("modify_stop", (order.order_id, qty, stop_price)))
        if self._foreign(order.order_id):
            # Like TWS: the id belongs to another client -> error 103, original order unchanged.
            raise OrderRejectedError("103: duplicate order id", order_id=order.order_id, code=103)
        if order.order_id in self.reject_modify or order.order_id not in self.orders:
            self.orders.pop(order.order_id, None)
            raise OrderRejectedError("10148: order cannot be modified", order_id=order.order_id)
        old = self.orders[order.order_id]
        self.orders[order.order_id] = BrokerOrder(
            old.order_id,
            old.symbol,
            old.action,
            old.order_type,
            qty,
            old.order_ref,
            aux_price=stop_price,
            parent_id=old.parent_id,
            client_id=old.client_id,
        )

    def cancel_order(self, order: BrokerOrder) -> None:
        self._require_connection()
        self.calls.append(("cancel", order.order_id))
        if self._foreign(order.order_id) or order.order_id in self.uncancellable:
            return  # TWS ignores it (not the owner) / never confirms it
        self.orders.pop(order.order_id, None)

    def confirm_cancelled(self, order: BrokerOrder, timeout_s: float) -> bool:
        if order.order_id in self.orders:
            self.slept += timeout_s
            return False
        return True

    # ------------------------------------------------------------------ simulation helpers
    @property
    def cancels(self) -> list[object]:
        return [arg for name, arg in self.calls if name == "cancel"]

    def stops(self, symbol: str | None = None) -> list[BrokerOrder]:
        return [
            o for o in self.orders.values() if o.is_stop and (symbol is None or o.symbol == symbol)
        ]

    def hold(self, symbol: str, qty: float, avg_cost: float = 100.0) -> None:
        self.positions[symbol] = BrokerPosition(symbol, qty, avg_cost)

    def add_stop(
        self,
        symbol: str,
        qty: float,
        price: float,
        ref: str = "TURTLE",
        *,
        client_id: int | None = None,
        order_id: int | None = None,
    ) -> int:
        """Working stop; a ``client_id`` other than the bot's makes it a foreign order."""
        oid = order_id if order_id is not None else next(self._ids)
        self.orders[oid] = BrokerOrder(
            oid,
            symbol,
            OrderAction.SELL,
            "STP",
            qty,
            ref,
            aux_price=price,
            client_id=self.client_id if client_id is None else client_id,
        )
        return oid

    def _emit(self, order: BrokerOrder, status: OrderStatus, filled: float, price: float) -> None:
        if self._handler is not None:
            self._handler(
                OrderEvent(
                    order_id=order.order_id,
                    symbol=order.symbol,
                    action=order.action,
                    order_type=order.order_type,
                    order_ref=order.order_ref,
                    status=status,
                    filled=filled,
                    remaining=order.qty - filled,
                    avg_fill_price=price,
                )
            )

    def fill_entry(self, symbol: str, price: float, qty: float | None = None) -> None:
        """Fill (or partially fill) the working BUY order of ``symbol``."""
        parent = next(o for o in self.orders.values() if o.symbol == symbol and o.action == "BUY")
        filled = parent.qty if qty is None else qty
        self.hold(symbol, filled, price)
        if filled >= parent.qty:
            del self.orders[parent.order_id]
            self._emit(parent, OrderStatus.FILLED, filled, price)
        else:
            self._emit(parent, OrderStatus.PARTIALLY_FILLED, filled, price)

    def cancel_entry(self, symbol: str, filled: float = 0.0, price: float = 0.0) -> None:
        parent = next(o for o in self.orders.values() if o.symbol == symbol and o.action == "BUY")
        del self.orders[parent.order_id]
        self._emit(parent, OrderStatus.CANCELLED, filled, price)

    def trigger_stop(self, symbol: str, price: float, qty: float | None = None) -> None:
        """Execute the stop of ``symbol`` (fully or partially)."""
        stop = next(o for o in self.stops(symbol))
        held = self.positions[symbol].qty
        sold = stop.qty if qty is None else qty
        self.fills.append(
            Fill(symbol, OrderAction.SELL, sold, price, dt.datetime(2026, 10, 14, 10, tzinfo=NY))
        )
        if sold >= held:
            del self.positions[symbol]
        else:
            self.hold(symbol, held - sold, self.positions[symbol].avg_cost)
        if sold >= stop.qty:
            del self.orders[stop.order_id]
            self._emit(stop, OrderStatus.FILLED, sold, price)
        else:
            self._emit(stop, OrderStatus.PARTIALLY_FILLED, sold, price)
