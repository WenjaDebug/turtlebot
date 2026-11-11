"""Trading engine: schedule, scan, entries, order events and connection supervision.

Behaviour is identical to the legacy bot (``legacy/live_trade_turtle.py``), restructured around
injected collaborators (broker gateway, state store, market data, clock, notifier) so that every
path is testable with fakes:

* 08:00 ET  build the universe (constituents -> liquidity -> market cap)
* 09:00 ET  reconcile and trail stops up to the 10-day low
* 15:45 ET  scan; entries as market-on-close (or market-on-open after the MOC cutoff / close)

Each entry is a bracket: parent order plus an attached GTC stop at the 10-day low.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field

from turtlebot.broker.gateway import BrokerGateway
from turtlebot.broker.retry import Backoff
from turtlebot.config import BlockSettings, Settings
from turtlebot.data.provider import MarketDataProvider
from turtlebot.data.quality import LOW_CHECK_KEY, LowIssueKind, LowsCheck, check_lows
from turtlebot.domain.models import (
    Block,
    Candidate,
    DailyBar,
    EntryMode,
    OrderAction,
    OrderEvent,
    OrderStatus,
    Position,
    PositionStatus,
)
from turtlebot.engine.jobs import JobRunner, run_with_watchdog
from turtlebot.exceptions import (
    BrokerConnectionError,
    BrokerDataUnavailableError,
    EmptyMarketDataError,
    JobTimeoutError,
    NotPaperAccountError,
    OrderRejectedError,
    TurtleBotError,
)
from turtlebot.logging_setup import log_order_event
from turtlebot.notify import Notifier
from turtlebot.persistence.store import StateStore
from turtlebot.reconciliation.service import LAST_STOP_UPDATE_KEY, ReconciliationService
from turtlebot.risk.limits import (
    entry_allowed,
    fallback_excess_after,
    margin_cushion_ok,
)
from turtlebot.risk.sizing import position_quantity
from turtlebot.strategy.allocation import plan_block_entries
from turtlebot.strategy.scanner import entry_mode, find_candidates, in_session
from turtlebot.timeutil import Clock, at, is_trading_day, previous_trading_day
from turtlebot.universe import UniverseBuilder

log = logging.getLogger(__name__)

HEARTBEAT_KEY = "engine_heartbeat"
MODE_KEY = "engine_mode"
NOTIFY_MAX_LINES = 4
ERROR_ALERT_INTERVAL_S = 3600.0
DISCONNECT_LOG_INTERVAL_S = 900.0  # one outage WARNING per 15 min


@dataclass(slots=True)
class _ConnectionState:
    down_since: dt.datetime | None = None
    last_alert: dt.datetime | None = None
    delays: Iterator[float] | None = None  # backoff delays while disconnected
    last_log: dt.datetime | None = None  # last WARNING about the outage (throttled)


@dataclass(slots=True)
class _Fills:
    lines: list[str] = field(default_factory=list)


class TradingEngine:
    """Orchestrates the strategy against a broker.

    Args:
        settings: Application settings.
        gateway: Broker adapter (wrap it in :class:`DryRunGateway` for dry runs).
        store: State store (use a separate file for dry runs).
        market_data: Price/universe provider.
        notifier: Alert channel.
        clock: Time source in exchange time.
        dry_run: Dry runs do not create positions for entries (legacy behaviour).
        sleep: Wall-clock sleep used while disconnected (injected for tests).
    """

    def __init__(
        self,
        settings: Settings,
        gateway: BrokerGateway,
        store: StateStore,
        market_data: MarketDataProvider,
        notifier: Notifier,
        clock: Clock,
        *,
        dry_run: bool = False,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.gateway = gateway
        self.store = store
        self.market_data = market_data
        self.notifier = notifier
        self.clock = clock
        self.dry_run = dry_run
        self._sleep = sleep
        self.universe = UniverseBuilder(
            market_data, settings.risk, settings.strategy, settings.paths.cache_dir
        )
        self.jobs = JobRunner(store, settings.schedule, notifier, clock)
        self.reconciler = ReconciliationService(
            gateway, store, self.load_lows, settings, notifier, clock
        )
        self._eligible: list[str] | None = None
        self._fills = _Fills()
        self._conn = _ConnectionState()
        self._reconcile_requested = False
        self._last_account_snapshot: dt.datetime | None = None
        self._last_error_alert: float | None = None
        self.backoff = Backoff(
            settings.broker.reconnect_initial_delay_s,
            settings.broker.reconnect_max_delay_s,
            settings.broker.reconnect_backoff,
        )

    # ================================================================== lifecycle
    @property
    def tz(self) -> dt.tzinfo:
        """Exchange timezone."""
        return self.settings.schedule.tz

    def start(self) -> None:
        """Connect, subscribe to order events, reconcile and catch up a missed stop update."""
        self.gateway.set_order_event_handler(self.on_order_event)
        self.gateway.connect()
        self.store.set_meta(MODE_KEY, "dry-run" if self.dry_run else "paper")
        self._log_configuration()
        self.reconcile()
        self.catch_up_stop_update()

    def run_once(self) -> None:
        """Start, run one scan immediately and return (``--once``)."""
        self.start()
        self.scan(self.eligible_universe())
        self.flush_fill_notifications()

    def run_forever(self, should_stop: Callable[[], bool] = lambda: False) -> None:
        """Main loop until ``should_stop()`` returns true (or Ctrl+C)."""
        self.start()
        while not should_stop():
            self.step()

    def step(self) -> None:
        """One loop iteration; never raises except for fatal safety errors."""
        try:
            if not self.gateway.is_connected():
                self._sleep(self._handle_disconnect())
                return
            self.tick()
            self.gateway.sleep(self.settings.schedule.loop_interval_s)
        except NotPaperAccountError:
            raise
        except Exception as exc:
            log.exception("Error in main loop - continuing in 60 s")
            now = time.monotonic()
            if (
                self._last_error_alert is None
                or now - self._last_error_alert > ERROR_ALERT_INTERVAL_S
            ):
                self.notifier.notify(
                    "Turtle: error in main loop",
                    f"{type(exc).__name__}: {exc}"[:150] + " - bot keeps running",
                )
                self._last_error_alert = now
            self._sleep(60)

    def tick(self) -> None:
        """Run whatever is due now (connected case)."""
        now = self.clock()
        self.store.set_meta(HEARTBEAT_KEY, now.isoformat(timespec="seconds"))
        self._maybe_snapshot_account(now)
        if self._reconcile_requested:
            self._reconcile_requested = False
            self.reconcile()
        schedule = self.settings.schedule
        if is_trading_day(now.date()):
            if (
                self.jobs.is_due("universe", at(now.date(), schedule.universe_time, schedule.tz))
                and now.time() < schedule.session_close
            ):
                self.jobs.run("universe", self._universe_job)
            if self.jobs.is_due("reconcile", at(now.date(), schedule.reconcile_time, schedule.tz)):
                self.jobs.run("reconcile", lambda: self.reconcile(update_stops=True))
            for scan_time in schedule.scan_times:
                name = f"scan {scan_time:%H:%M}"
                when = at(now.date(), scan_time, schedule.tz)
                if self.jobs.is_due(name, when, schedule.scan_grace_min):
                    self.jobs.run(name, lambda: self.scan(self.eligible_universe()))
        self.flush_fill_notifications()

    # ================================================================== connection
    def _handle_disconnect(self) -> float:
        """Reconnect with exponential backoff; alert after a while. Returns the wait time."""
        now = self.clock()
        broker = self.settings.broker
        state = self._conn
        if state.down_since is None:
            state.down_since = now
            state.delays = self.backoff.delays()
        down_min = (now - state.down_since).total_seconds() / 60
        if down_min >= broker.disconnect_alert_after_min and (
            state.last_alert is None
            or (now - state.last_alert).total_seconds() > broker.disconnect_alert_repeat_min * 60
        ):
            self.notifier.notify(
                "Turtle: no connection to TWS",
                f"Disconnected for {down_min:.0f} min - please check/re-login TWS. "
                "Open positions stay protected by their stop orders at IBKR.",
            )
            state.last_alert = now
        try:
            self.gateway.connect()
        except NotPaperAccountError:
            raise
        except (BrokerConnectionError, OSError) as exc:
            delay = next(state.delays) if state.delays is not None else self.backoff.initial
            loud = (
                state.last_log is None
                or (now - state.last_log).total_seconds() >= DISCONNECT_LOG_INTERVAL_S
            )
            log.log(
                logging.WARNING if loud else logging.DEBUG,
                "TWS unreachable for %.0f min (%s), retrying in %.0f s",
                down_min,
                exc,
                delay,
            )
            if loud:
                state.last_log = now
            return float(delay)
        log.info("Connection restored after %.0f min", down_min)
        if down_min >= broker.disconnect_alert_after_min:
            self.notifier.notify(
                "Turtle: connection restored", f"Reconnected after {down_min:.0f} min"
            )
        self._conn = _ConnectionState()
        self.reconcile()
        self.catch_up_stop_update()
        return 0.0

    # ================================================================== reconciliation
    def reconcile(self, *, update_stops: bool = False) -> None:
        """Reconcile the state store with the broker (see :mod:`turtlebot.reconciliation`)."""
        self.reconciler.run(update_stops=update_stops)

    def last_due_stop_update(self) -> dt.datetime:
        """Time of the latest 09:00 stop update that should have happened by now."""
        now = self.clock()
        schedule = self.settings.schedule
        day = now.date()
        if not (is_trading_day(day) and now >= at(day, schedule.reconcile_time, schedule.tz)):
            day = previous_trading_day(day)
        return at(day, schedule.reconcile_time, schedule.tz)

    def catch_up_stop_update(self) -> None:
        """Run a missed 09:00 stop update now (crash, laptop asleep, hanging job)."""
        due = self.last_due_stop_update()
        last = self.store.get_meta(LAST_STOP_UPDATE_KEY)
        if last and dt.datetime.fromisoformat(last) >= due:
            return
        log.info("Stop update of %s was missed (last: %s) - catching up", due, last or "never")
        self.reconcile(update_stops=True)
        if due.date() == self.clock().date():
            self.jobs.mark_done("reconcile")

    def load_lows(self, symbols: Sequence[str]) -> dict[str, float]:
        """Lowest low of the last ``exit_period`` completed sessions per symbol.

        Yahoo lows are cross-checked against TWS bars (see :mod:`turtlebot.data.quality`); the
        findings are logged, alerted and kept for the dashboard.
        """
        today = self.clock().date()
        start = today - dt.timedelta(days=self.settings.strategy.history_days)
        prices = run_with_watchdog(
            lambda: self.market_data.daily_bars(
                list(symbols), start, today + dt.timedelta(days=1), use_cache=False
            ),
            timeout_s=self.settings.schedule.job_timeout_min * 60,
            pump=self._pump,
            name="recent_lows",
        )
        quality = self.settings.data_quality
        period = self.settings.strategy.exit_period
        check = check_lows(
            symbols,
            prices,
            self._broker_bars(symbols, period) if quality.cross_check_lows else None,
            period=period,
            before=today,
            settings=quality,
        )
        self._record_low_check(symbols, check)
        return check.lows

    def _broker_bars(self, symbols: Sequence[str], period: int) -> dict[str, list[DailyBar]]:
        bars: dict[str, list[DailyBar]] = {}
        for symbol in symbols:
            try:
                # A few extra sessions: today's forming bar is dropped by the check.
                bars[symbol] = self.gateway.daily_bars(symbol, period + 3)
            except TurtleBotError as exc:
                log.warning("%s: TWS daily bars unavailable (%s)", symbol, exc)
        return bars

    def _record_low_check(self, symbols: Sequence[str], check: LowsCheck) -> None:
        """Log the findings and keep the latest per symbol for the dashboard."""
        now = self.clock().isoformat(timespec="seconds")
        for issue in check.issues:
            log.warning(
                "Data check %s %s: %s",
                issue.kind.value,
                issue.symbol,
                issue.message,
                extra={"ctx": {"event": "data.check", "kind": issue.kind.value}},
            )
        try:
            latest = json.loads(self.store.get_meta(LOW_CHECK_KEY) or "{}")
        except json.JSONDecodeError:
            latest = {}
        for symbol in symbols:
            latest[symbol] = {
                "ts": now,
                "issues": [
                    {"kind": i.kind.value, "message": i.message}
                    for i in check.issues
                    if i.symbol == symbol
                ],
            }
        self.store.set_meta(LOW_CHECK_KEY, json.dumps(latest, sort_keys=True))
        data_problems = [
            i
            for i in check.issues
            if i.kind in (LowIssueKind.SOURCE_MISMATCH, LowIssueKind.UNVERIFIED)
        ]
        if data_problems:
            self.notifier.notify(
                "Turtle: price data check",
                "; ".join(f"{i.symbol}: {i.message}" for i in data_problems)[:200],
            )

    # ================================================================== universe & scan
    def _pump(self, seconds: float) -> None:
        if self.gateway.is_connected():
            self.gateway.sleep(seconds)
        else:
            self._sleep(seconds)

    def _universe_job(self) -> None:
        self._eligible = self.eligible_universe()

    def eligible_universe(self) -> list[str]:
        """Today's universe; falls back to the previous one if the build hangs."""
        day = self.clock().date()
        if self._eligible is not None and self.universe.cached(day) is not None:
            return self._eligible
        timeout = self.settings.schedule.job_timeout_min * 60
        try:
            eligible, summary = run_with_watchdog(
                lambda: self.universe.build(day),
                timeout_s=timeout,
                pump=self._pump,
                name="universe",
            )
        except JobTimeoutError as exc:
            previous = self.universe.previous(day)
            if previous is None:
                raise
            saved, eligible = previous
            log.warning("%s -> using the universe of %s (%d tickers)", exc, saved, len(eligible))
            self._event("UNIVERSE", details={"fallback": saved.isoformat(), "size": len(eligible)})
            self.notifier.notify("Turtle: universe build aborted", f"Using the universe of {saved}")
            return eligible
        self._event("UNIVERSE", details={"size": summary.eligible, "universe": summary.universe})
        self._eligible = eligible
        return eligible

    def scan(self, eligible: Sequence[str]) -> None:
        """Scan the universe and fill free slots of both blocks."""
        now = self.clock()
        s = self.settings
        session = in_session(now, s.schedule)
        mode = entry_mode(now, s.schedule, s.strategy)
        log.info(
            "Scan of %d tickers (lookback %d, volume > %.1fx, mode %s)",
            len(eligible),
            s.strategy.entry_period,
            s.strategy.volume_factor,
            mode.value,
        )
        start = now.date() - dt.timedelta(days=s.strategy.history_days)
        prices = run_with_watchdog(
            lambda: self.market_data.daily_bars(
                list(eligible), start, now.date() + dt.timedelta(days=1), use_cache=False
            ),
            timeout_s=s.schedule.job_timeout_min * 60,
            pump=self._pump,
            name="scan_prices",
        )
        try:
            result = find_candidates(
                prices, now=now, mode=mode, session_active=session, strategy=s.strategy
            )
        except EmptyMarketDataError:
            log.warning("Scan: no price data received")
            return
        if result.skipped_reason:
            log.warning("Scan skipped: %s", result.skipped_reason)
            return
        log.info(
            "Scan (%s): %d with data, %d stale, %d invalid, %d signals",
            result.ref_date,
            result.n_with_data,
            result.n_stale,
            result.n_invalid,
            len(result.candidates),
        )
        if result.ref_date is not None:
            self.store.record_signals(result.ref_date, result.candidates, now)
        for c in result.candidates:
            self._event(
                "SIGNAL",
                symbol=c.ticker,
                price=c.price,
                stop=c.stop,
                strength=c.strength,
                details={"volume_ratio": round(c.volume_ratio, 2)},
            )

        # Reconcile before allocating so slots freed by stop-outs are refilled in this scan
        # (incident 13.10.2026).
        self.reconcile()
        for block in s.blocks:
            if block.enabled:
                self.allocate(result.candidates, mode, block)

        if mode is EntryMode.DAY:  # intraday fills arrive within seconds
            waited = 0.0
            while waited < s.schedule.fill_wait_s and any(
                p.status is PositionStatus.PENDING for p in self.store.active_positions()
            ):
                self.gateway.sleep(1)
                waited += 1
        self.reconcile()
        self.flush_fill_notifications()

    def allocate(
        self, candidates: Sequence[Candidate], mode: EntryMode, block: BlockSettings
    ) -> None:
        """Fill a block's free slots with the strongest eligible candidates."""
        today = self.clock().date()
        plan = plan_block_entries(
            candidates,
            block,
            self.store.active_positions(),
            self.store.traded_today(today),
            self.settings.strategy.rank_by,
        )
        log.info(
            "Block %d: %d free slots, %d candidates, chosen: %s",
            block.block,
            plan.free_slots,
            plan.pool_size,
            [c.ticker for c in plan.chosen] or "-",
        )
        for candidate in plan.chosen:
            account = self.gateway.account()
            decision = entry_allowed(
                account,
                self.store.entries_today(today, block.block),
                block.block,
                self.settings.risk,
            )
            if not decision.allowed:
                log.warning("Kill switch: %s", decision.reason)
                break
            self.enter(candidate, mode, block)

    def enter(self, candidate: Candidate, mode: EntryMode, block: BlockSettings) -> bool:
        """Send one entry bracket. Returns ``True`` if an order was sent."""
        ticker, price, stop = candidate.ticker, candidate.price, round(candidate.stop, 2)
        if not stop < price:
            log.info("%s: skipped, stop %s not below price %s", ticker, stop, price)
            return False
        try:
            account = self.gateway.account()
        except BrokerDataUnavailableError as exc:
            log.warning("%s: account value unavailable (%s) - entry rejected", ticker, exc)
            return False
        qty = position_quantity(
            account.net_liquidation, account.usd_per_base, self.settings.risk.position_pct, price
        )
        if qty < 1:
            return False

        try:
            excess = self.gateway.margin_excess_after(ticker, qty)
        except OrderRejectedError as exc:
            self._event("SKIP", symbol=ticker, block=block.block, details={"reason": str(exc)})
            return False
        if excess is None:
            log.warning("%s: what-if unavailable, falling back to AvailableFunds", ticker)
            excess = fallback_excess_after(
                account.available_funds, qty, price, account.usd_per_base
            )
        if not margin_cushion_ok(excess, account.net_liquidation, self.settings.risk.min_cushion):
            log.warning("MARGIN: %s rejected, excess after %.0f", ticker, excess)
            self._event(
                "SKIP",
                symbol=ticker,
                block=block.block,
                qty=qty,
                price=price,
                stop=stop,
                strength=candidate.strength,
                details={"reason": "margin cushion too small"},
            )
            return False

        try:
            ticket = self.gateway.place_entry(ticker, qty, mode, stop, block.order_ref)
        except OrderRejectedError as exc:
            self._event("SKIP", symbol=ticker, block=block.block, details={"reason": str(exc)})
            return False
        log_order_event(
            "order.entry_sent",
            f"Block {int(block.block)}: BUY {qty} {ticker} ({mode.value}) ~ {qty * price:,.0f} $"
            f" | stop {stop} | strength {candidate.strength:+.2%}",
            symbol=ticker,
            block=int(block.block),
            qty=qty,
            price=price,
            stop=stop,
            parent_id=ticket.parent_order_id,
            stop_id=ticket.stop_order_id,
            dry_run=self.dry_run,
        )
        now = self.clock()
        with self.store.transaction():
            self._event(
                "ORDER",
                symbol=ticker,
                block=block.block,
                qty=qty,
                price=price,
                stop=stop,
                strength=candidate.strength,
                order_id=ticket.parent_order_id,
                details={
                    "mode": mode.value,
                    "volume_ratio": round(candidate.volume_ratio, 2),
                    "dry_run": self.dry_run,
                },
            )
            if not self.dry_run:
                self.store.insert_position(
                    Position(
                        symbol=ticker,
                        block=block.block,
                        status=PositionStatus.PENDING,
                        qty=qty,
                        stop=stop,
                        signal_price=price,
                        parent_order_id=ticket.parent_order_id,
                        stop_order_id=ticket.stop_order_id,
                        signal_time=now,
                    )
                )
            self.store.increment_entries(now.date(), block.block)
            self.store.mark_traded(now.date(), ticker)
        return True

    # ================================================================== order events
    def on_order_event(self, event: OrderEvent) -> None:
        """Update the state store from a broker order status event (idempotent)."""
        if event.order_ref not in self.settings.order_refs:
            return
        log_order_event(
            f"order.{event.status.value.lower()}",
            f"{event.action} {event.symbol} {event.order_type} #{event.order_id}: "
            f"{event.status.value} filled {event.filled:g} @ {event.avg_fill_price:.2f}",
            symbol=event.symbol,
            order_id=event.order_id,
            status=event.status.value,
            filled=event.filled,
            remaining=event.remaining,
            price=event.avg_fill_price,
            tws_message=event.message,
        )
        try:
            if event.action is OrderAction.BUY:
                self._on_buy_event(event)
            else:
                self._on_sell_event(event)
        except TurtleBotError:
            log.exception("%s: order event could not be applied, reconciling", event.symbol)
            self._reconcile_requested = True

    def _on_buy_event(self, event: OrderEvent) -> None:
        pos = self.store.active_position(event.symbol)
        if pos is None:
            return
        now = self.clock()
        if event.status is OrderStatus.FILLED:
            if pos.status is PositionStatus.OPEN and pos.entry_price == event.avg_fill_price:
                return  # duplicate status update
            slip = event.avg_fill_price / pos.signal_price - 1 if pos.signal_price else 0.0
            self.store.update_position(
                pos.evolve(
                    status=PositionStatus.OPEN,
                    qty=event.filled,
                    entry_price=event.avg_fill_price,
                    entry_date=now.date(),
                    missing_cycles=0,
                )
            )
            self._event(
                "FILL",
                symbol=event.symbol,
                block=pos.block,
                qty=event.filled,
                price=event.avg_fill_price,
                stop=pos.stop,
                order_id=event.order_id,
                details={"slippage": round(slip, 4)},
            )
            self._fills.lines.append(
                f"Buy {event.filled:g} {event.symbol} @ {event.avg_fill_price:.2f}"
            )
        elif event.status is OrderStatus.PARTIALLY_FILLED:
            self._event(
                "PARTIAL_FILL",
                symbol=event.symbol,
                block=pos.block,
                qty=event.filled,
                price=event.avg_fill_price,
                order_id=event.order_id,
                details={"remaining": event.remaining},
            )
        elif event.status in (OrderStatus.CANCELLED, OrderStatus.INACTIVE):
            if pos.status is not PositionStatus.PENDING:
                return
            if event.filled > 0:
                # Partially filled, rest cancelled: the shares are real and must stay tracked.
                self.store.update_position(
                    pos.evolve(
                        status=PositionStatus.OPEN,
                        qty=event.filled,
                        entry_price=event.avg_fill_price,
                        entry_date=now.date(),
                    )
                )
                self._event(
                    "FILL",
                    symbol=event.symbol,
                    block=pos.block,
                    qty=event.filled,
                    price=event.avg_fill_price,
                    order_id=event.order_id,
                    details={"partial": True, "rest": event.status.value},
                )
                self._reconcile_requested = True  # resize the attached stop
            else:
                self.store.close_position(
                    pos, reason=f"entry {event.status.value}", exit_price=None, closed_at=now
                )
                self._event(
                    "CANCEL",
                    symbol=event.symbol,
                    block=pos.block,
                    order_id=event.order_id,
                    details={"reason": f"entry {event.status.value}", "message": event.message},
                )

    def _on_sell_event(self, event: OrderEvent) -> None:
        pos = self.store.active_position(event.symbol)
        now = self.clock()
        if event.status is OrderStatus.FILLED:
            if pos is None:
                return  # already closed (duplicate status update)
            ret = event.avg_fill_price / pos.entry_price - 1 if pos.entry_price else None
            if event.filled >= pos.qty:
                self.store.close_position(
                    pos,
                    reason=f"{event.order_type} filled",
                    exit_price=event.avg_fill_price,
                    closed_at=now,
                )
            else:
                self.store.update_position(pos.evolve(qty=pos.qty - event.filled))
                self._reconcile_requested = True
            self.store.mark_traded(now.date(), event.symbol)
            self._event(
                "EXIT",
                symbol=event.symbol,
                block=pos.block,
                qty=event.filled,
                price=event.avg_fill_price,
                order_id=event.order_id,
                details={
                    "order_type": event.order_type,
                    "return": round(ret, 4) if ret is not None else None,
                },
            )
            self._fills.lines.append(
                f"Sell {event.filled:g} {event.symbol} @ {event.avg_fill_price:.2f}"
            )
        elif event.status is OrderStatus.PARTIALLY_FILLED and pos is not None:
            self._event(
                "PARTIAL_EXIT",
                symbol=event.symbol,
                block=pos.block,
                qty=event.filled,
                price=event.avg_fill_price,
                order_id=event.order_id,
            )
        elif event.status in (OrderStatus.CANCELLED, OrderStatus.INACTIVE) and pos is not None:
            # A protective stop disappeared while the position is open: re-protect quickly.
            log.warning(
                "%s: stop #%d %s - position may be unprotected",
                event.symbol,
                event.order_id,
                event.status.value,
            )
            self._event(
                "STOP_LOST",
                symbol=event.symbol,
                block=pos.block,
                order_id=event.order_id,
                details={"status": event.status.value, "message": event.message},
            )
            self._reconcile_requested = True

    # ================================================================== helpers
    def flush_fill_notifications(self) -> None:
        """One notification for all executions since the last one."""
        lines = self._fills.lines
        if not lines:
            return
        message = " · ".join(lines[:NOTIFY_MAX_LINES])
        if len(lines) > NOTIFY_MAX_LINES:
            message += f" · +{len(lines) - NOTIFY_MAX_LINES} more"
        self.notifier.notify(f"Turtle: {len(lines)} order(s) executed", message)
        self._fills.lines = []

    def _maybe_snapshot_account(self, now: dt.datetime) -> None:
        interval = dt.timedelta(minutes=self.settings.schedule.account_snapshot_min)
        if self._last_account_snapshot is not None and now - self._last_account_snapshot < interval:
            return
        try:
            self.store.record_account(self.gateway.account(), now)
        except BrokerDataUnavailableError as exc:
            log.debug("Account snapshot skipped: %s", exc)
            return
        self._last_account_snapshot = now

    def _event(
        self,
        kind: str,
        *,
        symbol: str | None = None,
        block: Block | None = None,
        qty: float | None = None,
        price: float | None = None,
        stop: float | None = None,
        strength: float | None = None,
        order_id: int | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        self.store.record_event(
            kind,
            ts=self.clock(),
            symbol=symbol,
            block=block,
            qty=qty,
            price=price,
            stop=stop,
            strength=strength,
            order_id=order_id,
            details=details,
        )

    def _log_configuration(self) -> None:
        s = self.settings
        log.info(
            "Configuration: lookback %d, volume > %.1fx, scans %s ET, %.1f%% per position, "
            "max %d new entries per day and block%s",
            s.strategy.entry_period,
            s.strategy.volume_factor,
            [t.strftime("%H:%M") for t in s.schedule.scan_times],
            s.risk.position_pct * 100,
            s.risk.max_new_entries_per_day,
            " | DRY-RUN: no orders are sent" if self.dry_run else "",
        )
        for b in s.blocks:
            log.info(
                "Block %d: %s, max %d slots, min strength %s, order ref %s",
                b.block,
                "enabled" if b.enabled else "disabled (existing positions still managed)",
                b.max_positions,
                f"{b.min_strength:.0%}" if b.min_strength is not None else "-",
                b.order_ref,
            )
