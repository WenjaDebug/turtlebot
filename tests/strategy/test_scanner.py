"""Tests for the scan: candidate extraction, entry mode and volume extrapolation."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from tests.factories import breakout_frame, ny, ohlcv
from turtlebot.config import ScheduleSettings, StrategySettings
from turtlebot.domain.models import EntryMode
from turtlebot.exceptions import EmptyMarketDataError
from turtlebot.strategy.scanner import (
    INTRADAY_VOLUME_PROFILE,
    ScanResult,
    entry_mode,
    find_candidates,
    in_session,
    volume_fraction,
)

SCHEDULE = ScheduleSettings()
STRATEGY = StrategySettings()


class TestVolumeFraction:
    def test_anchor_points(self) -> None:
        assert volume_fraction(ny(hour=10, minute=0)) == pytest.approx(0.12)
        assert volume_fraction(ny(hour=15, minute=30)) == pytest.approx(0.72)

    def test_linear_between_anchors(self) -> None:
        assert volume_fraction(ny(hour=15, minute=45)) == pytest.approx((0.72 + 1.0) / 2)

    def test_clamped_at_open(self) -> None:
        assert volume_fraction(ny(hour=9, minute=31)) == pytest.approx(0.05)

    @pytest.mark.parametrize("minute_of_day", range(9 * 60 + 31, 16 * 60, 7))
    def test_matches_legacy_pandas_interpolation(self, minute_of_day: int) -> None:
        """Legacy implementation: pandas reindex + interpolate("index") over the profile."""
        anchors = [(t.hour * 60 + t.minute) - 570 for t in INTRADAY_VOLUME_PROFILE]
        elapsed = minute_of_day - 570
        legacy = float(
            pd.Series(list(INTRADAY_VOLUME_PROFILE.values()), index=anchors)
            .reindex(sorted(set(anchors) | {elapsed}))
            .interpolate("index")
            .loc[elapsed]
        )
        now = ny(hour=minute_of_day // 60, minute=minute_of_day % 60)
        assert volume_fraction(now) == pytest.approx(min(max(legacy, 0.05), 1.0))


class TestEntryMode:
    def test_after_close_is_market_on_open(self) -> None:
        assert entry_mode(ny(hour=16, minute=30), SCHEDULE, STRATEGY) is EntryMode.OPEN

    def test_before_cutoff_is_moc(self) -> None:
        assert entry_mode(ny(hour=15, minute=45), SCHEDULE, STRATEGY) is EntryMode.MOC

    def test_after_cutoff_falls_back_to_open(self, caplog: pytest.LogCaptureFixture) -> None:
        assert entry_mode(ny(hour=15, minute=55), SCHEDULE, STRATEGY) is EntryMode.OPEN
        assert "MOC" in caplog.text

    def test_intraday_mode(self) -> None:
        strategy = StrategySettings(intraday_entries=True)
        assert entry_mode(ny(hour=11, minute=0), SCHEDULE, strategy) is EntryMode.DAY

    def test_session_bounds(self) -> None:
        assert in_session(ny(hour=9, minute=30), SCHEDULE)
        assert not in_session(ny(hour=16, minute=0), SCHEDULE)


class TestFindCandidates:
    def scan(
        self,
        prices: dict[str, pd.DataFrame],
        *,
        now: dt.datetime | None = None,
        mode: EntryMode = EntryMode.OPEN,
        session: bool = False,
    ) -> ScanResult:
        return find_candidates(
            prices,
            now=now or ny(hour=16, minute=30),
            mode=mode,
            session_active=session,
            strategy=STRATEGY,
        )

    def test_finds_breakout_and_sets_stop_to_lowest_low_incl_today(self) -> None:
        result = self.scan({"AAA": breakout_frame(), "BBB": ohlcv([50.0] * 40)})
        assert [c.ticker for c in result.candidates] == ["AAA"]
        cand = result.candidates[0]
        assert cand.price == 55.0
        assert cand.stop == pytest.approx(49.5)  # min low of last 10 bars incl. today
        assert result.ref_date == dt.date(2026, 10, 14)
        assert result.n_with_data == 2

    def test_day_mode_uses_donchian_low_without_today(self) -> None:
        df = breakout_frame()
        df.loc[df.index[-1], "Low"] = 10.0  # today's (incomplete) low must be ignored
        result = self.scan(
            {"AAA": df}, now=ny(hour=15, minute=59), mode=EntryMode.DAY, session=True
        )
        assert result.candidates[0].stop == pytest.approx(49.5)

    def test_candidates_sorted_by_strength(self) -> None:
        prices = {"WEAK": breakout_frame(jump=1.0), "STRONG": breakout_frame(jump=10.0)}
        assert [c.ticker for c in self.scan(prices).candidates] == ["STRONG", "WEAK"]

    def test_stale_tickers_are_skipped(self) -> None:
        prices = {
            "A": breakout_frame(),
            "B": ohlcv([50.0] * 40),
            "OLD": breakout_frame(end="2026-10-13"),
        }
        result = self.scan(prices)
        assert result.n_stale == 1
        assert [c.ticker for c in result.candidates] == ["A"]

    def test_in_session_scan_requires_todays_bar(self) -> None:
        result = self.scan(
            {"A": breakout_frame(end="2026-10-13")},
            now=ny(day=14, hour=15, minute=45),
            mode=EntryMode.MOC,
            session=True,
        )
        assert result.skipped_reason is not None
        assert result.candidates == []

    def test_intraday_volume_is_extrapolated(self) -> None:
        # 1.2x volume at 12:00 (35 % of the day) extrapolates to ~3.4x -> breakout confirmed.
        df = breakout_frame(vol_mult=1.2)
        assert self.scan({"A": df}).candidates == []
        result = self.scan({"A": df}, now=ny(hour=12, minute=0), mode=EntryMode.MOC, session=True)
        assert [c.ticker for c in result.candidates] == ["A"]
        assert result.candidates[0].volume_ratio == pytest.approx(1.2 / 0.35)

    def test_integer_volume_does_not_break_extrapolation(self) -> None:
        df = breakout_frame(vol_mult=1.2).astype({"Volume": np.int64})
        result = self.scan({"A": df}, now=ny(hour=12, minute=0), mode=EntryMode.MOC, session=True)
        assert len(result.candidates) == 1
        assert df["Volume"].dtype == np.int64  # caller's frame untouched

    def test_empty_market_data_raises(self) -> None:
        with pytest.raises(EmptyMarketDataError):
            self.scan({})

    def test_malformed_frames_are_counted_not_fatal(self) -> None:
        prices = {
            "GOOD": breakout_frame(),
            "EMPTY": ohlcv([]),
            "NOCOLS": pd.DataFrame({"Close": [1.0]}, index=pd.to_datetime(["2026-10-14"])),
        }
        result = self.scan(prices)
        assert result.n_invalid == 2
        assert len(result.candidates) == 1

    def test_only_malformed_frames_raise(self) -> None:
        with pytest.raises(EmptyMarketDataError):
            self.scan({"EMPTY": ohlcv([])})

    def test_nan_low_skips_candidate(self) -> None:
        df = breakout_frame()
        df["Low"] = np.nan
        assert self.scan({"A": df}).candidates == []
