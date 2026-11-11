"""Interactive Brokers adapter based on ``ib_async``.

Implements :class:`~turtlebot.broker.gateway.BrokerGateway`. All TWS specifics (contract
qualification, order construction, error codes, the FX conversion of the account currency)
live here; the rest of the bot only sees domain objects.

This module is excluded from unit-test coverage because it needs a running TWS. Its logic is
deliberately thin; every decision is made in the tested layers above it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
import time
from collections.abc import Sequence
from typing import Final

import yfinance as yf
from ib_async import (
    IB,
    ExecutionFilter,
    Forex,
    MarketOrder,
    Order,
    Stock,
    StopOrder,
    Trade,
)

from turtlebot.broker.gateway import OrderEventHandler
from turtlebot.config import BrokerSettings
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
    combine_order_fills,
)
from turtlebot.exceptions import (
    BrokerConnectionError,
    BrokerDataUnavailableError,
    NotPaperAccountError,
    OrderRejectedError,
)
from turtlebot.logging_setup import log_order_event

log = logging.getLogger(__name__)

# Informational TWS messages (market data farm connections etc.). 2103/2105/2157 report a farm
# going offline, which happens every night during the IBKR server reset and is not actionable.
INFO_CODES: Final = frozenset({1102, 2103, 2104, 2105, 2106, 2107, 2108, 2119, 2157, 2158, 10349})
# Historical data permission errors (e.g. no forex market data subscription for IDEALPRO).
NO_PERMISSION_CODES: Final = frozenset({162, 354, 10167})
# Order id no longer valid at TWS: 103 duplicate id, 10147/10148 cancel/modify of unknown order.
DEAD_ORDER_CODES: Final = frozenset({103, 10147, 10148})
# Connectivity between TWS and IB servers lost / restored.
HISTORICAL_TIMEOUT_S: Final = 20.0
CONNECTIVITY_LOST: Final = frozenset({1100})
CONNECTIVITY_RESTORED: Final = frozenset({1101, 1102})

_DONE_STATUSES: Final = frozenset({"Cancelled", "ApiCancelled", "Filled", "Inactive"})
_STATUS_MAP: Final = {
    "Filled": OrderStatus.FILLED,
    "Cancelled": OrderStatus.CANCELLED,
    "ApiCancelled": OrderStatus.CANCELLED,
    "Inactive": OrderStatus.INACTIVE,
    "Submitted": OrderStatus.SUBMITTED,
    "PreSubmitted": OrderStatus.SUBMITTED,
}


def _bar_date(value: object) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))


def to_ib(ticker: str) -> str:
    """Yfinance notation -> IB symbol (``BRK-B`` -> ``BRK B``)."""
    return ticker.replace("-", " ")


def from_ib(symbol: str) -> str:
    """IB symbol -> yfinance notation."""
    return symbol.replace(" ", "-")


class IBKRGateway:
    """Broker gateway for TWS / IB Gateway (paper accounts only).

    Args:
        settings: Connection settings.
        client_id: API client id (the live bot owns ``settings.client_id``; dry runs use another).
        order_refs: Order references that identify the bot's own orders.
    """

    def __init__(
        self,
        settings: BrokerSettings,
        client_id: int,
        order_refs: Sequence[str],
        *,
        readonly: bool = False,
        owner_client_id: int | None = None,
    ) -> None:
        self.settings = settings
        self.client_id = client_id
        # Client id whose orders count as the bot's own in snapshots. A dry run connects with a
        # spare id but must judge ownership like the live bot (its stops are not foreign).
        self.owner_client_id = client_id if owner_client_id is None else owner_client_id
        self.order_refs = frozenset(order_refs)
        self.readonly = readonly
        self.ib = IB()
        # ib_async re-subscribes the account summary on every TWS message 1102 without cancelling
        # the old subscription; after two reconnects TWS answers with error 322. The bot does not
        # need the summary, so this automatism is switched off.
        self.ib.errorEvent -= self.ib._onError
        self.ib.errorEvent += self._on_error
        self.ib.orderStatusEvent += self._on_order_status
        self.ib.disconnectedEvent += self._on_disconnected
        self._handler: OrderEventHandler | None = None
        self._dead_order_ids: set[int] = set()
        self._connectivity_ok = True
        self._fx_cache: tuple[str, float] | None = None  # (hour key, rate)
        self._fx_from_tws = True  # cleared after a forex market data permission error
        self._closing = False
        self.unexpected_disconnects = 0  # since the last connect()

    # ------------------------------------------------------------------ connection
    def connect(self) -> None:
        """Connect, verify the paper account and wait for the position stream."""
        s = self.settings
        if s.port != s.paper_port:
            raise NotPaperAccountError(f"port {s.port} is not the paper port {s.paper_port}")
        try:
            self.ib.connect(
                s.host,
                s.port,
                clientId=self.client_id,
                timeout=s.connect_timeout_s,
                readonly=self.readonly,
            )
        except (OSError, TimeoutError, ConnectionError) as exc:
            raise BrokerConnectionError(
                f"cannot connect to TWS at {s.host}:{s.port}: {exc}"
            ) from exc
        accounts = self.ib.managedAccounts()
        if not accounts or not all(a.startswith(s.paper_account_prefix) for a in accounts):
            self.ib.disconnect()
            raise NotPaperAccountError(
                f"connected accounts {accounts} are not paper accounts - refusing to trade"
            )
        self.ib.reqPnL(accounts[0])
        self._connectivity_ok = True
        self.unexpected_disconnects = 0
        log.info("Connected to paper account %s (client id %d)", accounts[0], self.client_id)

    def disconnect(self) -> None:  # noqa: D102 - see protocol
        if self.ib.isConnected():
            self._closing = True
            try:
                self.ib.disconnect()
            finally:
                self._closing = False

    def is_connected(self) -> bool:  # noqa: D102
        return bool(self.ib.isConnected())

    def sleep(self, seconds: float) -> None:  # noqa: D102
        try:
            self.ib.sleep(seconds)
        except asyncio.CancelledError:
            # ib_async cancels pending waits when TWS drops the connection (e.g. the nightly
            # reset). That is a lost connection, handled by the engine's reconnect logic - not
            # an error in the main loop.
            if self.ib.isConnected():
                raise
            log.info("Wait interrupted by TWS disconnect")

    def set_order_event_handler(self, handler: OrderEventHandler) -> None:  # noqa: D102
        self._handler = handler

    def _account_id(self) -> str:
        accounts = self.ib.managedAccounts()
        if not accounts:
            raise BrokerConnectionError("no managed account (not connected?)")
        return accounts[0]

    # ------------------------------------------------------------------ events
    def _on_error(self, req_id: int, code: int, message: str, contract: object) -> None:
        level = logging.INFO if code in INFO_CODES else logging.WARNING
        log.log(level, "TWS %d (reqId %d): %s", code, req_id, message)
        if code in NO_PERMISSION_CODES and "IDEALPRO" in message:
            self._fx_from_tws = False
        if code in DEAD_ORDER_CODES and req_id > 0:
            self._dead_order_ids.add(req_id)
        if code in CONNECTIVITY_LOST:
            self._connectivity_ok = False
        elif code in CONNECTIVITY_RESTORED:
            self._connectivity_ok = True

    def _on_disconnected(self) -> None:
        # ib_async fires disconnectedEvent for our own disconnect() too; only an unrequested
        # disconnect is a lost connection.
        if self._closing:
            log.info("Disconnected from TWS")
            return
        self.unexpected_disconnects += 1
        log.warning("TWS connection lost")

    def _on_order_status(self, trade: Trade) -> None:
        order = trade.order
        if order.orderRef not in self.order_refs:
            return
        status = trade.orderStatus
        mapped = _STATUS_MAP.get(status.status, OrderStatus.OTHER)
        if mapped is OrderStatus.SUBMITTED and status.filled > 0:
            mapped = OrderStatus.PARTIALLY_FILLED
        messages = [e.message for e in trade.log if e.message]
        event = OrderEvent(
            order_id=order.orderId,
            symbol=from_ib(trade.contract.symbol),
            action=OrderAction(order.action),
            order_type=order.orderType,
            order_ref=order.orderRef,
            status=mapped,
            filled=float(status.filled),
            remaining=float(status.remaining),
            avg_fill_price=float(status.avgFillPrice),
            message=messages[-1] if messages else "",
        )
        if self._handler is not None:
            self._handler(event)

    # ------------------------------------------------------------------ reads
    def _account_value(self, tag: str) -> tuple[float | None, str | None]:
        for v in self.ib.accountValues(self._account_id()):
            if v.tag == tag and v.currency not in ("", "BASE"):
                return float(v.value), v.currency
        return None, None

    def _usd_per_base(self, base: str) -> float:
        """Conversion base currency -> USD (TWS forex midpoint, yfinance fallback, hourly cache)."""
        if base == "USD":
            return 1.0
        hour = dt.datetime.now(dt.UTC).strftime("%Y%m%d%H")
        if self._fx_cache and self._fx_cache[0] == hour:
            return self._fx_cache[1]
        rate = self._account_exchange_rate()
        if rate is None and self._fx_from_tws:
            rate = self._tws_forex_rate(base)
        if rate is None:
            try:
                history = yf.Ticker(f"USD{base}=X").history(period="5d")
                rate = 1.0 / float(history["Close"].iloc[-1])
            except Exception as exc:
                if self._fx_cache:
                    log.warning("FX unavailable (%s), reusing last rate", exc)
                    return self._fx_cache[1]
                raise BrokerDataUnavailableError(f"no FX rate USD{base}") from exc
        self._fx_cache = (hour, rate)
        return rate

    def _account_exchange_rate(self) -> float | None:
        """USD per base unit from the account's own ``ExchangeRate`` value (no market data needed).

        TWS reports ``ExchangeRate`` per currency as the factor that converts that currency into
        the account's base currency, so the USD row holds "base per USD".
        """
        for v in self.ib.accountValues(self._account_id()):
            if v.tag == "ExchangeRate" and v.currency == "USD":
                try:
                    base_per_usd = float(v.value)
                except ValueError:
                    return None
                return 1.0 / base_per_usd if base_per_usd > 0 else None
        return None

    def _tws_forex_rate(self, base: str) -> float | None:
        rate: float | None = None
        try:
            bars = self.ib.reqHistoricalData(
                Forex(f"USD{base}"),
                endDateTime="",
                durationStr="1 D",
                barSizeSetting="1 hour",
                whatToShow="MIDPOINT",
                useRTH=False,
            )
            if bars:
                rate = 1.0 / bars[-1].close  # USD<base> quotes base units per USD
        except (OSError, ValueError, TimeoutError) as exc:
            log.warning("Forex USD%s from TWS unavailable (%s)", base, exc)
        return rate

    def account(self) -> AccountSnapshot:  # noqa: D102
        netliq, currency = self._account_value("NetLiquidation")
        if netliq is None or currency is None:
            raise BrokerDataUnavailableError("NetLiquidation not available")
        available, _ = self._account_value("AvailableFunds")
        pnl = self.ib.pnl(self._account_id())
        daily = pnl[0].dailyPnL if pnl else None
        return AccountSnapshot(
            net_liquidation=netliq,
            currency=currency,
            available_funds=available,
            daily_pnl=None if daily is None or math.isnan(daily) else float(daily),
            usd_per_base=self._usd_per_base(currency),
        )

    def snapshot(self) -> BrokerSnapshot:
        """Positions (explicitly requested, blocking until ``positionEnd``) and own orders."""
        if not self.ib.isConnected():
            raise BrokerConnectionError("not connected")
        synced = self._connectivity_ok
        try:
            # reqPositions() blocks until TWS signals the end of the position stream; right after
            # a (re)connect ib.positions() can still be empty (incident 12.10.2026).
            raw_positions = self.ib.reqPositions()
        except (TimeoutError, OSError, ConnectionError) as exc:
            log.warning("Position request failed (%s) - snapshot marked unsynced", exc)
            raw_positions, synced = [], False
        account = self._account_id()
        positions = {
            from_ib(p.contract.symbol): BrokerPosition(
                symbol=from_ib(p.contract.symbol), qty=float(p.position), avg_cost=float(p.avgCost)
            )
            for p in raw_positions
            if p.account == account and p.position != 0
        }
        return BrokerSnapshot(
            positions=positions,
            orders=self._working_orders(set(positions)),
            positions_synced=synced,
            taken_at=dt.datetime.now(dt.UTC),
            client_id=self.owner_client_id,
        )

    def _working_orders(self, held: set[str]) -> list[BrokerOrder]:
        """Working orders of *all* API clients, requested fresh from TWS.

        ``reqOpenOrders()`` only returns the bot's own client id, so stops placed by another
        client (e.g. the 12.10. repair script, client id 50) would be invisible and the bot would
        place a second stop next to them. ``reqAllOpenOrders()`` is a one-off snapshot (not kept
        in sync), which is exactly what a reconciliation needs.
        """
        result = []
        for t in self.ib.reqAllOpenOrders():
            o = t.order
            if t.orderStatus.status in _DONE_STATUSES:
                continue
            ours = o.orderRef in self.order_refs
            protective = (
                o.action == "SELL" and o.orderType == "STP" and from_ib(t.contract.symbol) in held
            )
            if not (ours or protective):
                continue
            if o.clientId == self.client_id and o.orderId in self._dead_order_ids:
                continue  # order id reported invalid by TWS for our own client
            result.append(self._to_broker_order(t))
        return result

    @staticmethod
    def _to_broker_order(trade: Trade) -> BrokerOrder:
        o = trade.order
        aux = float(o.auxPrice) if o.orderType == "STP" and o.auxPrice is not None else None
        return BrokerOrder(
            order_id=o.orderId,
            symbol=from_ib(trade.contract.symbol),
            action=OrderAction(o.action),
            order_type=o.orderType,
            qty=float(o.totalQuantity),
            order_ref=o.orderRef,
            aux_price=aux,
            parent_id=o.parentId or None,
            filled=float(trade.orderStatus.filled),
            client_id=int(o.clientId),
            perm_id=int(o.permId) or None,
        )

    def recent_sell_fills(self, symbols: Sequence[str]) -> dict[str, Fill]:  # noqa: D102
        result: dict[str, Fill] = {}
        for symbol in symbols:
            try:
                fills = self.ib.reqExecutions(ExecutionFilter(symbol=to_ib(symbol), side="SELL"))
            except (OSError, TimeoutError) as exc:
                log.warning("%s: executions unavailable (%s)", symbol, exc)
                continue
            combined = combine_order_fills(
                [
                    Fill(
                        symbol=symbol,
                        action=OrderAction.SELL,
                        qty=float(f.execution.shares),
                        price=float(f.execution.price),
                        time=f.execution.time,
                        order_id=f.execution.orderId,
                    )
                    for f in fills
                ]
            )
            if combined is not None:
                result[symbol] = combined
        return result

    def margin_excess_after(self, symbol: str, qty: float) -> float | None:  # noqa: D102
        contract = self._qualify(symbol)
        # Explicit TIF, otherwise TWS discards the what-if order (error 10349).
        try:
            state = self.ib.whatIfOrder(contract, MarketOrder("BUY", qty, tif="DAY"))
            excess = float(state.equityWithLoanAfter) - float(state.initMarginAfter)
        except (ValueError, TypeError, AttributeError, OSError) as exc:
            log.warning("%s: what-if failed (%s)", symbol, exc)
            return None
        return None if excess > 1e300 else excess  # TWS reports "unset" as Double.MAX

    def daily_bars(self, symbol: str, sessions: int) -> list[DailyBar]:  # noqa: D102
        try:
            contract = self._qualify(symbol)
            # ADJUSTED_LAST matches Yahoo's split-adjusted bars; unadjusted TRADES would put the
            # pre-split lows of a recent split far above the price.
            bars = self.ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr=f"{sessions} D",
                barSizeSetting="1 day",
                whatToShow="ADJUSTED_LAST",
                useRTH=True,
                formatDate=1,
                timeout=HISTORICAL_TIMEOUT_S,
            )
        except (OrderRejectedError, ValueError, OSError, ConnectionError) as exc:
            log.warning("%s: TWS daily bars unavailable (%s)", symbol, exc)
            return []
        return [
            DailyBar(
                date=_bar_date(b.date),
                open=float(b.open),
                high=float(b.high),
                low=float(b.low),
                close=float(b.close),
                volume=float(b.volume),
            )
            for b in bars
        ]

    # ------------------------------------------------------------------ writes
    def _qualify(self, symbol: str) -> Stock:
        contract = Stock(to_ib(symbol), "SMART", "USD")
        try:
            qualified = self.ib.qualifyContracts(contract)
        except (ValueError, OSError) as exc:
            raise OrderRejectedError(f"{symbol}: contract ambiguous ({exc})") from exc
        if not qualified or qualified[0] is None:
            raise OrderRejectedError(f"{symbol}: contract not found")
        result = qualified[0]
        assert isinstance(result, Stock)
        return result

    def place_entry(  # noqa: D102
        self, symbol: str, qty: float, mode: EntryMode, stop_price: float, order_ref: str
    ) -> EntryTicket:
        contract = self._qualify(symbol)
        if mode is EntryMode.MOC:
            # eTradeOnly/firmQuoteOnly must be False explicitly for plain Order() objects.
            parent = Order(
                action="BUY",
                totalQuantity=qty,
                orderType="MOC",
                orderRef=order_ref,
                tif="DAY",
                transmit=False,
                eTradeOnly=False,
                firmQuoteOnly=False,
            )
        else:
            tif = "OPG" if mode is EntryMode.OPEN else "DAY"
            parent = MarketOrder("BUY", qty, orderRef=order_ref, tif=tif, transmit=False)
        parent.orderId = self.ib.client.getReqId()
        stop = StopOrder(
            "SELL",
            qty,
            stop_price,
            orderRef=order_ref,
            tif="GTC",
            parentId=parent.orderId,
            transmit=True,
            orderId=self.ib.client.getReqId(),
        )
        self.ib.placeOrder(contract, parent)
        self.ib.placeOrder(contract, stop)
        log_order_event(
            "order.entry",
            f"BUY {qty:g} {symbol} ({mode.value}) + stop {stop_price}",
            symbol=symbol,
            qty=qty,
            mode=mode.value,
            stop=stop_price,
            parent_id=parent.orderId,
            stop_id=stop.orderId,
            order_ref=order_ref,
        )
        return EntryTicket(parent_order_id=parent.orderId, stop_order_id=stop.orderId)

    def place_stop(  # noqa: D102
        self, symbol: str, qty: float, stop_price: float, order_ref: str
    ) -> int:
        contract = self._qualify(symbol)
        trade = self.ib.placeOrder(
            contract, StopOrder("SELL", qty, stop_price, orderRef=order_ref, tif="GTC")
        )
        order_id = int(trade.order.orderId)
        log_order_event(
            "order.stop_placed",
            f"stop SELL {qty:g} {symbol} @ {stop_price} GTC",
            symbol=symbol,
            qty=qty,
            stop=stop_price,
            order_id=order_id,
            order_ref=order_ref,
        )
        return order_id

    def _trade(self, order: BrokerOrder) -> Trade:
        if order.client_id is not None and order.client_id != self.client_id:
            raise OrderRejectedError(
                f"order #{order.order_id} belongs to client id {order.client_id}, "
                f"not {self.client_id} - TWS would reject any change",
                order_id=order.order_id,
            )
        for trade in self.ib.openTrades():
            if trade.order.orderId == order.order_id and trade.order.clientId == self.client_id:
                return trade
        raise OrderRejectedError(f"order #{order.order_id} is not open", order_id=order.order_id)

    def _still_working(self, order: BrokerOrder) -> bool:
        for t in self.ib.reqAllOpenOrders():
            if order.perm_id:
                same = t.order.permId == order.perm_id
            else:
                same = t.order.orderId == order.order_id and (
                    order.client_id is None or t.order.clientId == order.client_id
                )
            if same and t.orderStatus.status not in _DONE_STATUSES:
                return True
        return False

    def confirm_cancelled(self, order: BrokerOrder, timeout_s: float) -> bool:  # noqa: D102
        deadline = time.monotonic() + timeout_s
        while self._still_working(order):
            if time.monotonic() >= deadline:
                return False
            self.ib.sleep(1)
        return True

    def modify_stop(self, order: BrokerOrder, qty: float, stop_price: float) -> None:  # noqa: D102
        trade = self._trade(order)
        trade.order.totalQuantity = qty
        trade.order.auxPrice = stop_price
        self.ib.placeOrder(trade.contract, trade.order)
        self.ib.sleep(1)  # give TWS a moment to reject (error handler marks dead ids)
        if order.order_id in self._dead_order_ids:
            raise OrderRejectedError(
                f"TWS rejected modification of #{order.order_id}", order_id=order.order_id
            )
        log_order_event(
            "order.stop_modified",
            f"stop {order.symbol} #{order.order_id} -> {qty:g} @ {stop_price}",
            symbol=order.symbol,
            order_id=order.order_id,
            qty=qty,
            stop=stop_price,
        )

    def cancel_order(self, order: BrokerOrder) -> None:  # noqa: D102
        trade = self._trade(order)
        self.ib.cancelOrder(trade.order)
        log_order_event(
            "order.cancelled",
            f"cancel {order.symbol} {order.order_type} #{order.order_id}",
            symbol=order.symbol,
            order_id=order.order_id,
        )
