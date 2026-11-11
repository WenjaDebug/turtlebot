"""Engine tests: scan -> entries, order events, schedule and connection handling."""

from __future__ import annotations

import datetime as dt
import logging
import threading
from collections.abc import Sequence
from pathlib import Path

import pandas as pd
import pytest

from tests.conftest import MutableClock, RecordingNotifier
from tests.factories import NY, breakout_frame, ohlcv, position
from tests.fakes import FakeBroker, frame_bars
from turtlebot.broker.gateway import DryRunGateway
from turtlebot.config import Settings
from turtlebot.domain.models import (
    AccountSnapshot,
    Block,
    EntryMode,
    OrderAction,
    OrderEvent,
    OrderStatus,
    PositionStatus,
)
from turtlebot.engine.engine import HEARTBEAT_KEY, TradingEngine
from turtlebot.engine.jobs import run_with_watchdog
from turtlebot.exceptions import JobTimeoutError, MarketDataError, NotPaperAccountError
from turtlebot.persistence.store import StateStore
from turtlebot.reconciliation.service import LAST_STOP_UPDATE_KEY


class FakeMarketData:
    """Market data double serving fixed frames."""

    def __init__(self, frames: dict[str, pd.DataFrame] | None = None) -> None:
        self.frames = frames or {}
        self.caps: dict[str, float | None] = {}
        self.fail = False
        self.calls = 0

    def daily_bars(
        self, tickers: Sequence[str], start: dt.date, end: dt.date, *, use_cache: bool = True
    ) -> dict[str, pd.DataFrame]:
        self.calls += 1
        if self.fail:
            raise MarketDataError("network down")
        return {t: self.frames[t] for t in tickers if t in self.frames}

    def universe(self, as_of: dt.date) -> list[str]:
        return sorted(self.frames)

    def market_caps(self, tickers: Sequence[str], as_of: dt.date) -> dict[str, float | None]:
        return {t: self.caps.get(t, 1e10) for t in tickers}


@pytest.fixture
def engine_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        paths={"data_dir": tmp_path / "var", "cache_dir": tmp_path / "cache"},  # type: ignore[arg-type]
    )


@pytest.fixture
def market() -> FakeMarketData:
    # Strengths: STRONG ~ +18 %, MID ~ +10.9 %, WEAK ~ +1 %; FLAT has no signal.
    return FakeMarketData(
        {
            "STRONG": breakout_frame(jump=9.5),
            "MID": breakout_frame(jump=6.0),
            "WEAK": breakout_frame(jump=1.0),
            "FLAT": ohlcv([50.0] * 40),
        }
    )


@pytest.fixture
def engine(
    engine_settings: Settings,
    broker: FakeBroker,
    store: StateStore,
    market: FakeMarketData,
    notifier: RecordingNotifier,
    clock: MutableClock,
) -> TradingEngine:
    clock.now = dt.datetime(2026, 10, 14, 15, 45, tzinfo=NY)
    sleeps: list[float] = []
    eng = TradingEngine(
        engine_settings, broker, store, market, notifier, clock, sleep=sleeps.append
    )
    broker.set_order_event_handler(eng.on_order_event)
    broker.bar_source = lambda symbol: frame_bars(market.frames.get(symbol))
    return eng


def active(store: StateStore) -> dict[str, PositionStatus]:
    return {p.symbol: p.status for p in store.active_positions()}


class TestScanAndEntries:
    def test_entries_are_sent_as_moc_brackets_and_tracked(
        self, engine: TradingEngine, broker: FakeBroker, store: StateStore
    ) -> None:
        engine.scan(["STRONG", "MID", "WEAK", "FLAT"])
        assert [e[0] for e in broker.entries] == ["STRONG", "MID", "WEAK"]
        _, qty, mode, stop, ref = broker.entries[0]
        assert mode is EntryMode.MOC
        assert stop == 49.5
        assert ref == "TURTLE"
        assert qty == 1_500 // 59.5  # 1.5 % of 100k at 59.50
        assert set(active(store).values()) == {PositionStatus.PENDING}
        assert len(store.signals()) == 3
        assert store.entries_today(dt.date(2026, 10, 14), Block.ONE) == 3

    def test_block2_takes_only_strong_signals_not_held_by_block1(
        self, engine: TradingEngine, engine_settings: Settings, store: StateStore
    ) -> None:
        engine_settings.block1.max_positions = 1
        engine.scan(["STRONG", "MID", "WEAK"])
        blocks = {p.symbol: p.block for p in store.active_positions()}
        assert blocks == {"STRONG": Block.ONE, "MID": Block.TWO}

    def test_disabled_block2_takes_nothing(
        self, engine: TradingEngine, engine_settings: Settings, store: StateStore
    ) -> None:
        engine_settings.block1.max_positions = 1
        engine_settings.block2.enabled = False
        engine.scan(["STRONG", "MID", "WEAK"])
        assert set(active(store)) == {"STRONG"}

    def test_after_close_entries_are_market_on_open(
        self, engine: TradingEngine, broker: FakeBroker, clock: MutableClock
    ) -> None:
        clock.now = dt.datetime(2026, 10, 14, 16, 30, tzinfo=NY)
        engine.scan(["STRONG"])
        assert broker.calls[-1][0] in {"place_entry", "place_stop"}
        assert broker.entries[0][2] is EntryMode.OPEN

    def test_daily_loss_kill_switch(self, engine: TradingEngine, broker: FakeBroker) -> None:
        broker.account_snapshot = AccountSnapshot(100_000, "USD", 80_000, -2_500, 1.0)
        engine.scan(["STRONG", "MID"])
        assert broker.entries == []

    def test_daily_entry_limit_per_block(
        self, engine: TradingEngine, engine_settings: Settings, store: StateStore
    ) -> None:
        engine_settings.risk.max_new_entries_per_day = 1
        engine.scan(["STRONG", "MID", "WEAK"])
        blocks = sorted(p.block for p in store.active_positions())
        assert blocks == [Block.ONE, Block.TWO]

    def test_margin_cushion_blocks_entry(
        self, engine: TradingEngine, broker: FakeBroker, store: StateStore
    ) -> None:
        broker.margin_excess = 10_000.0  # < 25 % of 100k
        engine.scan(["STRONG"])
        assert store.active_positions() == []
        assert store.events(kinds=["SKIP"])[0].details["reason"] == "margin cushion too small"

    def test_margin_fallback_uses_available_funds(
        self, engine: TradingEngine, broker: FakeBroker, store: StateStore
    ) -> None:
        broker.margin_excess = None
        broker.account_snapshot = AccountSnapshot(100_000, "USD", 20_000, 0.0, 1.0)
        engine.scan(["STRONG"])
        assert store.active_positions() == []

    def test_unknown_contract_is_skipped(
        self, engine: TradingEngine, market: FakeMarketData, store: StateStore
    ) -> None:
        market.frames["BADX"] = breakout_frame(jump=20)
        engine.scan(["BADX", "STRONG"])
        assert set(active(store)) == {"STRONG"}
        assert store.events(kinds=["SKIP"])[0].symbol == "BADX"

    def test_held_and_traded_tickers_are_not_bought_again(
        self, engine: TradingEngine, broker: FakeBroker, store: StateStore
    ) -> None:
        broker.hold("STRONG", 10)
        broker.add_stop("STRONG", 10, 40.0)
        store.insert_position(position("STRONG", qty=10, stop=40.0))
        store.mark_traded(dt.date(2026, 10, 14), "MID")
        engine.scan(["STRONG", "MID", "WEAK"])
        assert [e[0] for e in broker.entries] == ["WEAK"]

    def test_empty_market_data_does_not_crash(
        self, engine: TradingEngine, broker: FakeBroker
    ) -> None:
        engine.scan(["UNKNOWN"])
        assert broker.calls == []

    def test_stale_data_during_session_skips_scan(
        self, engine: TradingEngine, market: FakeMarketData, broker: FakeBroker
    ) -> None:
        market.frames = {"OLD": breakout_frame(end="2026-10-13")}
        engine.scan(["OLD"])
        assert broker.entries == []

    def test_dry_run_sends_nothing_and_tracks_nothing(
        self,
        engine_settings: Settings,
        broker: FakeBroker,
        store: StateStore,
        market: FakeMarketData,
        notifier: RecordingNotifier,
        clock: MutableClock,
    ) -> None:
        clock.now = dt.datetime(2026, 10, 14, 15, 45, tzinfo=NY)
        eng = TradingEngine(
            engine_settings, DryRunGateway(broker), store, market, notifier, clock, dry_run=True
        )
        eng.scan(["STRONG", "MID"])
        assert broker.orders == {}
        assert store.active_positions() == []
        # STRONG and MID go to block 1; block 2 sees both as traded today.
        assert len(store.events(kinds=["ORDER"])) == 2


class TestOrderEvents:
    def test_fill_opens_position_and_notifies(
        self,
        engine: TradingEngine,
        broker: FakeBroker,
        store: StateStore,
        notifier: RecordingNotifier,
    ) -> None:
        engine.scan(["STRONG"])
        broker.fill_entry("STRONG", 59.7)
        p = store.active_position("STRONG")
        assert p is not None
        assert (p.status, p.entry_price) == (PositionStatus.OPEN, 59.7)
        fill = store.events(kinds=["FILL"])[0]
        assert fill.details["slippage"] == pytest.approx(59.7 / 59.5 - 1, abs=1e-4)
        engine.flush_fill_notifications()
        assert "STRONG" in notifier.messages[-1][1]

    def test_duplicate_fill_events_are_idempotent(
        self, engine: TradingEngine, broker: FakeBroker, store: StateStore
    ) -> None:
        engine.scan(["STRONG"])
        broker.fill_entry("STRONG", 59.7)
        parent = store.events(kinds=["FILL"])[0]
        engine.on_order_event(
            OrderEvent(
                parent.order_id or 0,
                "STRONG",
                OrderAction.BUY,
                "MOC",
                "TURTLE",
                OrderStatus.FILLED,
                25,
                0,
                59.7,
            )
        )
        assert len(store.events(kinds=["FILL"])) == 1

    def test_partial_fill_then_cancel_keeps_the_shares(
        self, engine: TradingEngine, broker: FakeBroker, store: StateStore
    ) -> None:
        engine.scan(["STRONG"])
        broker.fill_entry("STRONG", 59.6, qty=10)
        assert store.events(kinds=["PARTIAL_FILL"])
        broker.cancel_entry("STRONG", filled=10, price=59.6)
        p = store.active_position("STRONG")
        assert p is not None
        assert (p.status, p.qty) == (PositionStatus.OPEN, 10)
        engine.tick()  # requested reconcile resizes the stop to 10 shares
        assert [s.qty for s in broker.stops("STRONG")] == [10]

    def test_cancelled_entry_without_fill_frees_the_slot(
        self, engine: TradingEngine, broker: FakeBroker, store: StateStore
    ) -> None:
        engine.scan(["STRONG"])
        broker.cancel_entry("STRONG")
        assert store.active_positions() == []
        assert store.events(kinds=["CANCEL"])

    def test_stop_fill_closes_position(
        self, engine: TradingEngine, broker: FakeBroker, store: StateStore
    ) -> None:
        engine.scan(["STRONG"])
        broker.fill_entry("STRONG", 59.5)
        broker.trigger_stop("STRONG", 49.4)
        assert store.active_positions() == []
        closed = store.closed_positions()[0]
        assert closed.exit_price == 49.4
        assert store.events(kinds=["EXIT"])[0].details["return"] == pytest.approx(
            49.4 / 59.5 - 1, abs=1e-4
        )

    def test_partial_stop_fill_reduces_quantity(
        self, engine: TradingEngine, broker: FakeBroker, store: StateStore
    ) -> None:
        engine.scan(["STRONG"])
        broker.fill_entry("STRONG", 59.5)
        engine.on_order_event(
            OrderEvent(
                1, "STRONG", OrderAction.SELL, "STP", "TURTLE", OrderStatus.FILLED, 5, 0, 49.0
            )
        )
        p = store.active_position("STRONG")
        assert p is not None
        assert p.qty == 25 - 5

    def test_lost_stop_is_replaced_on_next_tick(
        self, engine: TradingEngine, broker: FakeBroker, store: StateStore
    ) -> None:
        engine.scan(["STRONG"])
        broker.fill_entry("STRONG", 59.5)
        stop = broker.stops("STRONG")[0]
        del broker.orders[stop.order_id]
        broker._emit(stop, OrderStatus.CANCELLED, 0, 0)
        assert store.events(kinds=["STOP_LOST"])
        engine.tick()
        assert len(broker.stops("STRONG")) == 1

    def test_foreign_orders_are_ignored(self, engine: TradingEngine, store: StateStore) -> None:
        engine.on_order_event(
            OrderEvent(1, "SPY", OrderAction.BUY, "MKT", "MANUAL", OrderStatus.FILLED, 1, 0, 1.0)
        )
        assert store.events() == []


class TestSchedule:
    def test_jobs_run_at_their_times(
        self, engine: TradingEngine, clock: MutableClock, store: StateStore, market: FakeMarketData
    ) -> None:
        day = dt.date(2026, 10, 14)
        clock.now = dt.datetime(2026, 10, 14, 7, 59, tzinfo=NY)
        engine.tick()
        assert store.job(day, "universe") is None
        clock.now = dt.datetime(2026, 10, 14, 8, 0, tzinfo=NY)
        engine.tick()
        assert store.job(day, "universe").status == "done"  # type: ignore[union-attr]
        clock.now = dt.datetime(2026, 10, 14, 9, 0, tzinfo=NY)
        engine.tick()
        assert store.job(day, "reconcile").status == "done"  # type: ignore[union-attr]
        assert store.get_meta(LAST_STOP_UPDATE_KEY) is not None
        clock.now = dt.datetime(2026, 10, 14, 15, 45, tzinfo=NY)
        engine.tick()
        assert store.job(day, "scan 15:45").status == "done"  # type: ignore[union-attr]
        assert store.get_meta(HEARTBEAT_KEY) == "2026-10-14T15:45:00-04:00"
        calls = market.calls
        engine.tick()  # nothing is repeated
        assert market.calls == calls

    def test_missed_scan_outside_grace_is_skipped(
        self, engine: TradingEngine, clock: MutableClock, store: StateStore, broker: FakeBroker
    ) -> None:
        clock.now = dt.datetime(2026, 10, 14, 21, 46, tzinfo=NY)  # 15:45 + 361 min
        engine.tick()
        assert store.job(dt.date(2026, 10, 14), "scan 15:45").status == "skipped"  # type: ignore[union-attr]

    def test_no_jobs_on_holidays(
        self, engine: TradingEngine, clock: MutableClock, store: StateStore
    ) -> None:
        clock.now = dt.datetime(2026, 11, 26, 15, 45, tzinfo=NY)  # Thanksgiving
        engine.tick()
        assert store.job(dt.date(2026, 11, 26), "scan 15:45") is None

    def test_failing_job_is_retried_then_given_up(
        self,
        engine: TradingEngine,
        clock: MutableClock,
        store: StateStore,
        market: FakeMarketData,
        notifier: RecordingNotifier,
    ) -> None:
        market.fail = True
        clock.now = dt.datetime(2026, 10, 14, 8, 0, tzinfo=NY)
        engine.tick()
        engine.tick()  # within retry delay: no new attempt
        assert market.calls == 1
        for _ in range(2):
            clock.advance(minutes=10)
            engine.tick()
        job = store.job(dt.date(2026, 10, 14), "universe")
        assert job is not None
        assert (job.status, job.attempts) == ("failed", 3)
        assert "failed for good" in notifier.messages[-1][0]
        clock.advance(minutes=30)
        engine.tick()
        assert market.calls == 3

    def test_account_snapshots_are_sampled(
        self, engine: TradingEngine, clock: MutableClock, store: StateStore
    ) -> None:
        clock.now = dt.datetime(2026, 10, 17, 12, 0, tzinfo=NY)  # Saturday: no jobs
        engine.tick()
        clock.advance(minutes=5)
        engine.tick()
        clock.advance(minutes=15)
        engine.tick()
        assert len(store.account_history()) == 2


class TestLifecycle:
    def test_start_reconciles_and_catches_up_missed_stop_update(
        self,
        engine: TradingEngine,
        broker: FakeBroker,
        store: StateStore,
        clock: MutableClock,
        market: FakeMarketData,
    ) -> None:
        clock.now = dt.datetime(2026, 10, 14, 10, 0, tzinfo=NY)
        market.frames["HELD"] = ohlcv([60.0] * 20, end="2026-10-13")
        broker.hold("HELD", 10)
        sid = broker.add_stop("HELD", 10, 50.0)
        store.insert_position(position("HELD", qty=10, stop=50.0, stop_order_id=sid))
        engine.start()
        assert broker.orders[sid].aux_price == 59.5  # trailed to the 10-day low
        assert store.job(dt.date(2026, 10, 14), "reconcile").status == "done"  # type: ignore[union-attr]

    def test_reconnect_with_backoff_alert_and_reconcile(
        self,
        engine: TradingEngine,
        broker: FakeBroker,
        clock: MutableClock,
        notifier: RecordingNotifier,
    ) -> None:
        waits: list[float] = []
        engine._sleep = waits.append
        broker.connected = False
        broker.connect_failures = 3
        for _ in range(4):
            engine.step()
            clock.advance(minutes=3)
        assert waits[:3] == [5.0, 10.0, 20.0]
        assert broker.connected
        titles = [t for t, _ in notifier.messages]
        assert "Turtle: no connection to TWS" in titles
        assert "Turtle: connection restored" in titles

    def test_long_outage_logs_one_warning_per_quarter_hour(
        self,
        engine: TradingEngine,
        broker: FakeBroker,
        clock: MutableClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Laptop closed overnight: TWS is gone for hours, the log must not fill up.
        engine._sleep = lambda _s: None
        broker.connected = False
        broker.connect_failures = 100
        with caplog.at_level(logging.DEBUG, logger="turtlebot.engine.engine"):
            for _ in range(10):  # 10 attempts over 30 minutes
                engine.step()
                clock.advance(minutes=3)
        warnings = [
            r for r in caplog.records if r.levelno == logging.WARNING and "unreachable" in r.msg
        ]
        assert len(warnings) == 2  # at 0 and at 15 minutes
        assert sum("unreachable" in r.msg for r in caplog.records) == 10

    def test_non_paper_account_is_fatal(self, engine: TradingEngine, broker: FakeBroker) -> None:
        broker.connected = False

        def refuse() -> None:
            raise NotPaperAccountError("live account")

        broker.connect = refuse  # type: ignore[method-assign]
        with pytest.raises(NotPaperAccountError):
            engine.step()

    def test_unexpected_error_in_loop_is_contained(
        self, engine: TradingEngine, notifier: RecordingNotifier
    ) -> None:
        def boom() -> None:
            raise RuntimeError("bug")

        engine.tick = boom  # type: ignore[method-assign]
        engine.step()
        engine.step()
        assert [t for t, _ in notifier.messages] == ["Turtle: error in main loop"]

    def test_run_once(self, engine: TradingEngine, broker: FakeBroker, store: StateStore) -> None:
        engine.run_once()
        assert {p.symbol for p in store.active_positions()} == {"STRONG", "MID", "WEAK"}

    def test_universe_filters_liquidity_price_and_market_cap(
        self, engine: TradingEngine, market: FakeMarketData
    ) -> None:
        market.frames["ILLIQ"] = breakout_frame(jump=9).assign(Volume=10.0)
        market.frames["PENNY"] = ohlcv([0.5] * 40, volumes=[1e9] * 40)
        market.frames["SMALL"] = breakout_frame(jump=9)
        market.caps["SMALL"] = 1e8
        assert engine.eligible_universe() == ["FLAT", "MID", "STRONG", "WEAK"]

    def test_universe_timeout_falls_back_to_previous(
        self, engine: TradingEngine, engine_settings: Settings
    ) -> None:
        path = engine.universe.path(dt.date(2026, 10, 13))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"tickers": ["OLD"]}')
        release = threading.Event()

        def hang(day: dt.date) -> tuple[list[str], object]:
            release.wait(5)
            return [], None

        engine.universe.build = hang  # type: ignore[assignment, method-assign]
        engine_settings.schedule.job_timeout_min = 0  # bypasses validation: immediate timeout
        try:
            assert engine.eligible_universe() == ["OLD"]
        finally:
            release.set()


def test_watchdog_returns_result_and_propagates_errors() -> None:
    pumped: list[float] = []
    assert run_with_watchdog(lambda: 42, timeout_s=5, pump=pumped.append) == 42

    def fail() -> int:
        raise ValueError("x")

    with pytest.raises(ValueError, match="x"):
        run_with_watchdog(fail, timeout_s=5, pump=pumped.append)


def test_watchdog_timeout() -> None:
    release = threading.Event()
    with pytest.raises(JobTimeoutError):
        run_with_watchdog(lambda: release.wait(5), timeout_s=0.05, pump=lambda s: None)
    release.set()
