"""Broker abstraction.

The engine, the reconciliation and the tests only depend on the :class:`BrokerGateway` protocol.
:mod:`turtlebot.broker.ibkr` implements it with ``ib_async``; :class:`DryRunGateway` wraps any
gateway and turns all order-changing calls into log lines.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable, Sequence
from typing import Protocol, runtime_checkable

from turtlebot.domain.models import (
    AccountSnapshot,
    BrokerOrder,
    BrokerSnapshot,
    EntryMode,
    EntryTicket,
    Fill,
    OrderEvent,
)
from turtlebot.logging_setup import log_order_event

log = logging.getLogger(__name__)

type OrderEventHandler = Callable[[OrderEvent], None]


@runtime_checkable
class BrokerGateway(Protocol):
    """Operations the bot needs from a broker."""

    def connect(self) -> None:
        """Connect and verify the account is a paper account.

        Raises:
            BrokerConnectionError: Connection failed (transient).
            NotPaperAccountError: The account is not a paper account (fatal).
        """

    def disconnect(self) -> None:
        """Close the connection (idempotent)."""

    def is_connected(self) -> bool:
        """Whether the API connection is up."""

    def sleep(self, seconds: float) -> None:
        """Wait while processing broker events."""

    def set_order_event_handler(self, handler: OrderEventHandler) -> None:
        """Register the callback for status updates of the bot's own orders."""

    def account(self) -> AccountSnapshot:
        """Current account values.

        Raises:
            BrokerDataUnavailableError: If net liquidation is not available.
        """

    def snapshot(self) -> BrokerSnapshot:
        """Positions and the bot's working orders, with a sync flag."""

    def recent_sell_fills(self, symbols: Sequence[str]) -> dict[str, Fill]:
        """Latest SELL execution per symbol (symbols without executions are omitted)."""

    def margin_excess_after(self, symbol: str, qty: float) -> float | None:
        """Excess liquidity after a hypothetical buy (TWS what-if); ``None`` if unavailable."""

    def place_entry(
        self, symbol: str, qty: float, mode: EntryMode, stop_price: float, order_ref: str
    ) -> EntryTicket:
        """Send an entry order with an attached GTC stop.

        Raises:
            OrderRejectedError: If the contract is unknown or the order is rejected.
        """

    def place_stop(self, symbol: str, qty: float, stop_price: float, order_ref: str) -> int | None:
        """Place a standalone GTC stop and return its order id."""

    def modify_stop(self, order: BrokerOrder, qty: float, stop_price: float) -> None:
        """Change quantity/price of a working stop.

        Raises:
            OrderRejectedError: If TWS rejects the modification (e.g. the id is dead).
        """

    def cancel_order(self, order: BrokerOrder) -> None:
        """Cancel a working order."""


class DryRunGateway:
    """Read-through gateway that never changes anything at the broker.

    All reads are delegated to ``inner``; every write is logged and answered with a synthetic
    negative order id, so the engine's bookkeeping can run unchanged.
    """

    def __init__(self, inner: BrokerGateway) -> None:
        self.inner = inner
        self._ids = itertools.count(-1, -1)

    def connect(self) -> None:  # noqa: D102 - see protocol
        self.inner.connect()

    def disconnect(self) -> None:  # noqa: D102
        self.inner.disconnect()

    def is_connected(self) -> bool:  # noqa: D102
        return self.inner.is_connected()

    def sleep(self, seconds: float) -> None:  # noqa: D102
        self.inner.sleep(seconds)

    def set_order_event_handler(self, handler: OrderEventHandler) -> None:  # noqa: D102
        self.inner.set_order_event_handler(handler)

    def account(self) -> AccountSnapshot:  # noqa: D102
        return self.inner.account()

    def snapshot(self) -> BrokerSnapshot:  # noqa: D102
        return self.inner.snapshot()

    def recent_sell_fills(self, symbols: Sequence[str]) -> dict[str, Fill]:  # noqa: D102
        return self.inner.recent_sell_fills(symbols)

    def margin_excess_after(self, symbol: str, qty: float) -> float | None:  # noqa: D102
        return self.inner.margin_excess_after(symbol, qty)

    def place_entry(  # noqa: D102
        self, symbol: str, qty: float, mode: EntryMode, stop_price: float, order_ref: str
    ) -> EntryTicket:
        log_order_event(
            "dryrun.entry",
            f"DRY-RUN: would BUY {qty:g} {symbol} ({mode.value}) with stop {stop_price}",
            symbol=symbol,
            qty=qty,
            stop=stop_price,
            order_ref=order_ref,
        )
        return EntryTicket(parent_order_id=None, stop_order_id=None)

    def place_stop(  # noqa: D102
        self, symbol: str, qty: float, stop_price: float, order_ref: str
    ) -> int:
        log_order_event(
            "dryrun.stop",
            f"DRY-RUN: would place stop SELL {qty:g} {symbol} @ {stop_price}",
            symbol=symbol,
            qty=qty,
            stop=stop_price,
            order_ref=order_ref,
        )
        return next(self._ids)

    def modify_stop(self, order: BrokerOrder, qty: float, stop_price: float) -> None:  # noqa: D102
        log_order_event(
            "dryrun.modify",
            f"DRY-RUN: would modify stop #{order.order_id} {order.symbol} "
            f"-> {qty:g} @ {stop_price}",
            symbol=order.symbol,
            order_id=order.order_id,
        )

    def cancel_order(self, order: BrokerOrder) -> None:  # noqa: D102
        log_order_event(
            "dryrun.cancel",
            f"DRY-RUN: would cancel #{order.order_id} {order.symbol} {order.order_type}",
            symbol=order.symbol,
            order_id=order.order_id,
        )
