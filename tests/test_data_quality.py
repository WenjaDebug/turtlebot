"""Cross-check of the lows used for trailing stops, outlier and takeover warnings."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd
import pytest

from tests.conftest import MutableClock, RecordingNotifier
from tests.factories import NY, ohlcv, position
from tests.fakes import FakeBroker, frame_bars
from turtlebot.config import DataQualitySettings, Settings
from turtlebot.dashboard.quotes import StaticQuotes
from turtlebot.dashboard.repository import DashboardRepository
from turtlebot.data.quality import LOW_CHECK_KEY, LowIssueKind, check_lows
from turtlebot.domain.models import DailyBar
from turtlebot.engine.engine import TradingEngine
from turtlebot.exceptions import BrokerConnectionError
from turtlebot.persistence.store import StateStore

TODAY = dt.date(2026, 10, 17)  # Saturday after the BMBL entry
CFG = DataQualitySettings()

# BMBL daily bars from TWS (TRADES, RTH), 21.09.-16.10.2026: (date, open, high, low, close, volume)
BMBL = [
    ("2026-09-21", 2.80, 2.89, 2.76, 2.85, 1_426_740),
    ("2026-09-22", 2.89, 2.92, 2.79, 2.83, 1_282_397),
    ("2026-09-23", 2.81, 2.81, 2.66, 2.68, 1_379_999),
    ("2026-09-24", 2.70, 2.78, 2.67, 2.68, 1_692_461),
    ("2026-09-25", 2.68, 2.75, 2.65, 2.66, 1_186_893),
    ("2026-09-28", 2.65, 2.66, 2.52, 2.53, 2_109_475),
    ("2026-09-29", 2.52, 2.58, 2.49, 2.57, 1_734_517),
    ("2026-09-30", 2.58, 2.71, 2.58, 2.63, 2_122_517),
    ("2026-10-01", 2.60, 2.63, 2.50, 2.55, 1_940_469),
    ("2026-10-02", 2.58, 2.58, 2.49, 2.54, 1_145_846),
    ("2026-10-05", 2.52, 2.57, 2.51, 2.55, 1_299_344),
    ("2026-10-06", 2.57, 2.58, 2.49, 2.51, 1_043_841),
    ("2026-10-07", 2.49, 2.52, 2.44, 2.50, 1_581_953),
    ("2026-10-08", 2.51, 2.57, 2.48, 2.56, 1_204_878),
    ("2026-10-09", 2.57, 2.59, 2.52, 2.54, 793_079),
    ("2026-10-12", 2.54, 3.98, 2.50, 3.80, 36_323_701),
    ("2026-10-13", 5.98, 5.98, 5.92, 5.95, 82_572_127),
    ("2026-10-14", 5.95, 5.98, 5.93, 5.97, 37_572_550),
    ("2026-10-15", 5.96, 5.97, 5.93, 5.93, 12_307_863),
    ("2026-10-16", 5.94, 5.95, 5.93, 5.94, 4_708_378),
]


def bmbl_bars() -> list[DailyBar]:
    return [DailyBar(dt.date.fromisoformat(d), o, h, lo, c, v) for d, o, h, lo, c, v in BMBL]


def bmbl_frame() -> pd.DataFrame:
    bars = bmbl_bars()
    return pd.DataFrame(
        {
            "Open": [b.open for b in bars],
            "High": [b.high for b in bars],
            "Low": [b.low for b in bars],
            "Close": [b.close for b in bars],
            "Volume": [b.volume for b in bars],
        },
        index=pd.DatetimeIndex([pd.Timestamp(b.date) for b in bars]),
    )


def trending(low_on_day: float | None = None) -> pd.DataFrame:
    """40 calm sessions up to 16.10. around 100; optionally one bad low tick on 12.10."""
    frame = ohlcv([100.0 + 0.1 * i for i in range(40)], end="2026-10-16")
    if low_on_day is not None:
        frame.loc[pd.Timestamp("2026-10-12"), "Low"] = low_on_day
    return frame


def kinds(check: object) -> list[LowIssueKind]:
    return [i.kind for i in check.issues]  # type: ignore[attr-defined]


class TestCrossCheck:
    def test_agreeing_sources_keep_the_yahoo_low_silently(self) -> None:
        yahoo = trending()
        check = check_lows(
            ["X"], {"X": yahoo}, {"X": frame_bars(yahoo)}, period=10, before=TODAY, settings=CFG
        )
        assert check.lows == {"X": pytest.approx(102.5)}
        assert check.issues == []

    def test_bad_yahoo_tick_is_replaced_by_the_tws_low(self) -> None:
        check = check_lows(
            ["X"],
            {"X": trending(low_on_day=10.0)},
            {"X": frame_bars(trending())},
            period=10,
            before=TODAY,
            settings=CFG,
        )
        assert check.lows == {"X": pytest.approx(102.5)}
        assert kinds(check) == [LowIssueKind.SOURCE_MISMATCH]
        assert "Yahoo 10 vs TWS 102.5" in check.issues[0].message

    def test_difference_within_tolerance_is_no_finding(self) -> None:
        tws = trending()
        tws["Low"] = tws["Low"] * 1.015  # e.g. dividend adjustment
        check = check_lows(
            ["X"], {"X": trending()}, {"X": frame_bars(tws)}, period=10, before=TODAY, settings=CFG
        )
        assert check.lows == {"X": pytest.approx(102.5)}
        assert check.issues == []

    def test_missing_tws_bars_mean_no_trailing(self) -> None:
        check = check_lows(["X"], {"X": trending()}, {}, period=10, before=TODAY, settings=CFG)
        assert check.lows == {}
        assert kinds(check) == [LowIssueKind.UNVERIFIED]
        assert "not trailed" in check.issues[0].message

    def test_missing_tws_bars_can_be_tolerated(self) -> None:
        lenient = DataQualitySettings(require_second_source=False)
        check = check_lows(["X"], {"X": trending()}, {}, period=10, before=TODAY, settings=lenient)
        assert check.lows == {"X": pytest.approx(102.5)}
        assert kinds(check) == [LowIssueKind.UNVERIFIED]

    def test_missing_yahoo_bars_fall_back_to_tws(self) -> None:
        check = check_lows(
            ["X"], {}, {"X": frame_bars(trending())}, period=10, before=TODAY, settings=CFG
        )
        assert check.lows == {"X": pytest.approx(102.5)}
        assert kinds(check) == [LowIssueKind.SOURCE_MISMATCH]

    def test_disabled_cross_check_uses_yahoo_only(self) -> None:
        check = check_lows(
            ["X"], {"X": trending(low_on_day=10.0)}, None, period=10, before=TODAY, settings=CFG
        )
        assert check.lows == {"X": 10.0}
        assert kinds(check) == [LowIssueKind.OUTLIER]

    def test_todays_forming_bar_does_not_count(self) -> None:
        frame = trending()
        tws = [*frame_bars(frame), DailyBar(TODAY, 104.0, 104.0, 50.0, 51.0, 1e6)]
        check = check_lows(["X"], {"X": frame}, {"X": tws}, period=10, before=TODAY, settings=CFG)
        assert check.lows == {"X": pytest.approx(102.5)}
        assert check.issues == []


class TestWarnings:
    def test_outlier_confirmed_by_both_sources_is_kept_and_reported(self) -> None:
        bad = trending(low_on_day=80.0)
        check = check_lows(
            ["X"], {"X": bad}, {"X": frame_bars(bad)}, period=10, before=TODAY, settings=CFG
        )
        assert check.lows == {"X": 80.0}
        assert kinds(check) == [LowIssueKind.OUTLIER]
        assert "12.10." in check.issues[0].message
        assert "confirmed by Yahoo and TWS" in check.issues[0].message

    def test_bmbl_low_is_real_and_the_takeover_pattern_is_flagged(self) -> None:
        """BMBL 2026-10: 2.44 on 07.10. is a genuine low; 13.10. cash offer at 6.05."""
        check = check_lows(
            ["BMBL"],
            {"BMBL": bmbl_frame()},
            {"BMBL": bmbl_bars()},
            period=10,
            before=TODAY,
            settings=CFG,
        )
        assert check.lows == {"BMBL": 2.44}  # the strategy's stop is unchanged
        assert kinds(check) == [LowIssueKind.TAKEOVER]
        message = check.issues[0].message
        assert "13.10." in message
        assert "possibly a pending takeover" in message

    def test_takeover_needs_the_quiet_sessions(self) -> None:
        frame = bmbl_frame().iloc[:-2]  # only 14.10. after the jump so far
        check = check_lows(["BMBL"], {"BMBL": frame}, None, period=10, before=TODAY, settings=CFG)
        assert LowIssueKind.TAKEOVER not in kinds(check)

    def test_ordinary_breakout_is_no_takeover(self) -> None:
        closes = [50.0] * 30 + [65.0, 66.5, 68.0, 66.0, 69.0]
        volumes = [1e6] * 30 + [8e6, 3e6, 3e6, 2e6, 2e6]
        frame = ohlcv(closes, volumes=volumes, end="2026-10-16")
        check = check_lows(["X"], {"X": frame}, None, period=10, before=TODAY, settings=CFG)
        assert LowIssueKind.TAKEOVER not in kinds(check)


# ---------------------------------------------------------------------------- engine
class _Market:
    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self.frames = frames

    def daily_bars(
        self, tickers: object, start: dt.date, end: dt.date, *, use_cache: bool = True
    ) -> dict[str, pd.DataFrame]:
        return {t: f for t, f in self.frames.items() if t in tickers}  # type: ignore[operator]

    def universe(self, as_of: dt.date) -> list[str]:
        return []

    def market_caps(self, tickers: object, as_of: dt.date) -> dict[str, float | None]:
        return {}


@pytest.fixture
def engine(
    tmp_path: Path,
    broker: FakeBroker,
    store: StateStore,
    notifier: RecordingNotifier,
    clock: MutableClock,
) -> TradingEngine:
    clock.now = dt.datetime(2026, 10, 17, 9, 0, tzinfo=NY)
    settings = Settings(
        _env_file=None,
        paths={"data_dir": tmp_path / "var", "cache_dir": tmp_path / "cache"},  # type: ignore[arg-type]
    )
    market = _Market({"X": trending(low_on_day=10.0), "BMBL": bmbl_frame()})
    return TradingEngine(settings, broker, store, market, notifier, clock)


class TestEngine:
    def test_load_lows_uses_tws_on_mismatch_and_alerts(
        self,
        engine: TradingEngine,
        broker: FakeBroker,
        store: StateStore,
        notifier: RecordingNotifier,
    ) -> None:
        broker.bars = {"X": frame_bars(trending()), "BMBL": bmbl_bars()}
        assert engine.load_lows(["X", "BMBL"]) == {"X": pytest.approx(102.5), "BMBL": 2.44}
        latest = json.loads(store.get_meta(LOW_CHECK_KEY) or "{}")
        assert [i["kind"] for i in latest["X"]["issues"]] == ["low_source_mismatch"]
        assert [i["kind"] for i in latest["BMBL"]["issues"]] == ["possible_takeover"]
        assert len(notifier.messages) == 1
        assert "X: 10-day low Yahoo 10 vs TWS 102.5" in notifier.messages[0][1]

    def test_broker_failure_leaves_the_stop_untrailed(
        self, engine: TradingEngine, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def down(symbol: str, sessions: int) -> list[DailyBar]:
            raise BrokerConnectionError("not connected")

        monkeypatch.setattr(broker, "daily_bars", down)
        assert engine.load_lows(["X"]) == {}

    def test_findings_are_replaced_per_checked_symbol(
        self, engine: TradingEngine, broker: FakeBroker, store: StateStore
    ) -> None:
        broker.bars = {"BMBL": bmbl_bars()}
        engine.load_lows(["X", "BMBL"])  # X unverified
        broker.bars["X"] = frame_bars(trending())
        engine.market_data.frames["X"] = trending()  # type: ignore[attr-defined]
        engine.load_lows(["X"])
        latest = json.loads(store.get_meta(LOW_CHECK_KEY) or "{}")
        assert latest["X"]["issues"] == []
        assert [i["kind"] for i in latest["BMBL"]["issues"]] == ["possible_takeover"]


# ---------------------------------------------------------------------------- dashboard
def repo(store: StateStore, settings: Settings, prices: dict[str, float]) -> DashboardRepository:
    return DashboardRepository(
        store, StaticQuotes(prices), settings, lambda: dt.datetime(2026, 10, 17, 9, 5, tzinfo=NY)
    )


class TestDashboard:
    def test_data_findings_and_wide_stop_are_warnings(
        self, store: StateStore, settings: Settings
    ) -> None:
        store.insert_position(position("BMBL", qty=3001, stop=2.44, entry_price=5.95))
        store.insert_position(position("SHOP", qty=114, stop=151.7, entry_price=157.84))
        store.set_meta(
            LOW_CHECK_KEY,
            json.dumps(
                {
                    "BMBL": {
                        "ts": "2026-10-17T09:00:00-04:00",
                        "issues": [{"kind": "possible_takeover", "message": "jump +57% ..."}],
                    },
                    "CLOSED": {
                        "ts": "2026-10-17T09:00:00-04:00",
                        "issues": [{"kind": "low_outlier", "message": "not held"}],
                    },
                }
            ),
        )
        r = repo(store, settings, {"BMBL": 5.94, "SHOP": 169.52})
        alerts = r.stop_alerts()
        assert [(a.kind, a.symbol, a.severity) for a in alerts] == [
            ("possible_takeover", "BMBL", "warn"),
            ("wide_stop", "BMBL", "warn"),
        ]
        assert "59% below the price 5.94" in alerts[1].message
        rows = {p.symbol: p for p in r.positions()}
        assert rows["BMBL"].warnings == ["possible takeover", "wide stop"]
        assert rows["BMBL"].stop_alert is None
        assert rows["SHOP"].warnings == []

    def test_old_findings_expire(self, store: StateStore, settings: Settings) -> None:
        store.insert_position(position("BMBL", qty=3001, stop=5.5, entry_price=5.95))
        store.set_meta(
            LOW_CHECK_KEY,
            json.dumps(
                {
                    "BMBL": {
                        "ts": "2026-10-10T09:00:00-04:00",
                        "issues": [{"kind": "possible_takeover", "message": "old"}],
                    }
                }
            ),
        )
        assert repo(store, settings, {"BMBL": 5.94}).stop_alerts() == []
