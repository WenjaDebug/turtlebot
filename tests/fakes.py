"""In-memory broker used instead of TWS in every test."""

from __future__ import annotations

import datetime as dt
import itertools
from collections.abc import Sequence
from dataclasses import dataclass, field

from tests.factories import NY
from turtlebot.broker.gateway import OrderEventHandler
from turtlebot.domain.models import (
    AccountSnapshot,
    BrokerOrder,
    BrokerPosition,
    BrokerSnapshot,
    EntryMode,
    EntryTicket,
    Fill,
    OrderAction,
    OrderEvent,
    OrderStatus,
)
from turtlebot.exceptions import BrokerConnectionError, OrderRejectedError


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
    margin_excess: float | None = 90_000.0
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
            orders=list(self.orders.values()),
            positions_synced=self.synced,
            taken_at=dt.datetime(2026, 10, 14, 9, 0, tzinfo=NY),
        )

    def recent_sell_fills(self, symbols: Sequence[str]) -> dict[str, Fill]:
        result: dict[str, Fill] = {}
        for fill in self.fills:
            if fill.symbol in symbols and fill.action is OrderAction.SELL:
                result[fill.symbol] = fill
        return result

    def margin_excess_after(self, symbol: str, qty: float) -> float | None:
        return self.margin_excess

    def place_entry(
        self, symbol: str, qty: float, mode: EntryMode, stop_price: float, order_ref: str
    ) -> EntryTicket:
        self._require_connection()
        if symbol.startswith("BAD"):
            raise OrderRejectedError(f"{symbol}: contract not found")
        parent = next(self._ids)
        stop = next(self._ids)
        self.orders[parent] = BrokerOrder(
            parent, symbol, OrderAction.BUY, mode.value.upper(), qty, order_ref
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
        )
        self.calls.append(("place_entry", symbol))
        self.entries.append((symbol, qty, mode, stop_price, order_ref))
        return EntryTicket(parent, stop)

    def place_stop(self, symbol: str, qty: float, stop_price: float, order_ref: str) -> int:
        self._require_connection()
        oid = next(self._ids)
        self.orders[oid] = BrokerOrder(
            oid, symbol, OrderAction.SELL, "STP", qty, order_ref, aux_price=stop_price
        )
        self.calls.append(("place_stop", (symbol, qty, stop_price)))
        return oid

    def modify_stop(self, order: BrokerOrder, qty: float, stop_price: float) -> None:
        self._require_connection()
        self.calls.append(("modify_stop", (order.order_id, qty, stop_price)))
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
        )

    def cancel_order(self, order: BrokerOrder) -> None:
        self._require_connection()
        self.calls.append(("cancel", order.order_id))
        self.orders.pop(order.order_id, None)

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

    def add_stop(self, symbol: str, qty: float, price: float, ref: str = "TURTLE") -> int:
        oid = next(self._ids)
        self.orders[oid] = BrokerOrder(
            oid, symbol, OrderAction.SELL, "STP", qty, ref, aux_price=price
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
