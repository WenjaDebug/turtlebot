"""Turn a universe of daily bars into ranked breakout candidates.

Extracted from ``scan()`` in the legacy bot; the decision logic is unchanged, but it is now a pure
function of its inputs (prices, time, settings) and therefore unit-testable.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

import numpy as np
import pandas as pd

from turtlebot.config import ScheduleSettings, StrategySettings
from turtlebot.domain.models import Candidate, EntryMode
from turtlebot.exceptions import EmptyMarketDataError
from turtlebot.strategy.signals import OHLCV, turtle_breakout

log = logging.getLogger(__name__)

# Typical cumulative share of daily volume for US equities (U-shape with closing auction).
# Used to extrapolate the partial intraday volume to a full day.
INTRADAY_VOLUME_PROFILE: Final[dict[dt.time, float]] = {
    dt.time(9, 30): 0.00,
    dt.time(10, 0): 0.12,
    dt.time(10, 30): 0.19,
    dt.time(11, 0): 0.25,
    dt.time(11, 30): 0.30,
    dt.time(12, 0): 0.35,
    dt.time(12, 30): 0.39,
    dt.time(13, 0): 0.43,
    dt.time(13, 30): 0.47,
    dt.time(14, 0): 0.52,
    dt.time(14, 30): 0.57,
    dt.time(15, 0): 0.63,
    dt.time(15, 30): 0.72,
    dt.time(16, 0): 1.00,
}
MIN_VOLUME_FRACTION: Final = 0.05


def _minutes(t: dt.time) -> float:
    return t.hour * 60 + t.minute + t.second / 60


def volume_fraction(now: dt.datetime) -> float:
    """Typical share of the daily volume traded until ``now`` (linear between the anchors).

    Args:
        now: Current exchange time.

    Returns:
        Fraction in ``[0.05, 1.0]``.
    """
    xs = [_minutes(t) for t in INTRADAY_VOLUME_PROFILE]
    ys = list(INTRADAY_VOLUME_PROFILE.values())
    fraction = float(np.interp(_minutes(now.time()), xs, ys))
    return min(max(fraction, MIN_VOLUME_FRACTION), 1.0)


def in_session(now: dt.datetime, schedule: ScheduleSettings) -> bool:
    """Whether ``now`` lies within regular trading hours."""
    return schedule.session_open <= now.time() < schedule.session_close


def entry_mode(
    now: dt.datetime, schedule: ScheduleSettings, strategy: StrategySettings
) -> EntryMode:
    """Choose how entries of a scan at ``now`` are executed.

    After the close the entry is a market-on-open order for the next morning. During the
    session it is a market-on-close order, unless the MOC acceptance cutoff has passed, in which
    case it falls back to market-on-open as well.
    """
    if not in_session(now, schedule):
        return EntryMode.OPEN
    if strategy.intraday_entries:
        return EntryMode.DAY
    if now.time() < schedule.moc_cutoff:
        return EntryMode.MOC
    log.warning(
        "Scan too close to the close for a new MOC order (cutoff %s ET) -> market-on-open tomorrow",
        schedule.moc_cutoff.strftime("%H:%M"),
    )
    return EntryMode.OPEN


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Outcome of one scan.

    Attributes:
        candidates: Signals sorted by breakout strength, strongest first.
        ref_date: Date of the most common last bar (the scan's reference day).
        n_with_data: Tickers with at least one bar.
        n_stale: Tickers whose last bar is older than ``ref_date``.
        n_invalid: Tickers skipped because of malformed data.
        skipped_reason: Set if the whole scan was skipped (e.g. data not current).
    """

    candidates: list[Candidate] = field(default_factory=list)
    ref_date: dt.date | None = None
    n_with_data: int = 0
    n_stale: int = 0
    n_invalid: int = 0
    skipped_reason: str | None = None


def _valid_frame(df: pd.DataFrame) -> bool:
    return not df.empty and all(c in df.columns for c in OHLCV)


def find_candidates(
    prices: Mapping[str, pd.DataFrame],
    *,
    now: dt.datetime,
    mode: EntryMode,
    session_active: bool,
    strategy: StrategySettings,
) -> ScanResult:
    """Evaluate the breakout signal on the last bar of every ticker.

    Args:
        prices: Daily OHLCV per ticker. During the session the last bar is today's partial bar.
        now: Scan time (exchange timezone).
        mode: Entry mode, determines whether today's low is part of the initial stop.
        session_active: Whether the scan runs during regular trading hours.
        strategy: Strategy parameters.

    Returns:
        The scan result.

    Raises:
        EmptyMarketDataError: If ``prices`` contains no usable frame at all.
    """
    frames = {t: df for t, df in prices.items() if _valid_frame(df)}
    n_invalid = len(prices) - len(frames)
    if not frames:
        raise EmptyMarketDataError(f"no usable price data ({len(prices)} tickers requested)")

    last_dates = Counter(df.index[-1].date() for df in frames.values())
    top = max(last_dates.values())
    ref_date = min(d for d, n in last_dates.items() if n == top)  # == pd.Series.mode()[0]
    if session_active and ref_date != now.date():
        return ScanResult(
            ref_date=ref_date,
            n_with_data=len(frames),
            n_invalid=n_invalid,
            skipped_reason=f"price data ends on {ref_date}, not today",
        )
    fraction = volume_fraction(now) if session_active else 1.0

    candidates: list[Candidate] = []
    stale = 0
    for ticker, raw in frames.items():
        if raw.index[-1].date() != ref_date:
            stale += 1
            continue
        # Copy as float so the extrapolation of today's partial volume also works on int64 data.
        df = raw.astype({"Volume": float})
        volume = df["Volume"].to_numpy(copy=True)
        volume[-1] /= fraction
        df["Volume"] = volume
        sig = turtle_breakout(
            df,
            entry_period=strategy.entry_period,
            exit_period=strategy.exit_period,
            volume_period=strategy.volume_period,
            volume_factor=strategy.volume_factor,
            rank_by=strategy.rank_by.value,
        ).iloc[-1]
        if not bool(sig["entry"]):
            continue
        # MOC/OPEN: today's low is (nearly) final -> part of the stop. DAY: the running bar is
        # incomplete -> Donchian low without today.
        stop = (
            float(df["Low"].tail(strategy.exit_period).min())
            if mode is not EntryMode.DAY
            else float(sig["donchian_low"])
        )
        price = float(df["Close"].iloc[-1])
        if not (math.isfinite(stop) and math.isfinite(price)):
            log.warning("%s: signal ignored, non-finite price/stop", ticker)
            continue
        candidates.append(
            Candidate(
                ticker=ticker,
                price=price,
                stop=stop,
                strength=float(sig["breakout_strength"]),
                volume_ratio=float(sig["volume_ratio"]),
            )
        )
    candidates.sort(key=lambda c: -c.strength)
    return ScanResult(
        candidates=candidates,
        ref_date=ref_date,
        n_with_data=len(frames),
        n_stale=stale,
        n_invalid=n_invalid,
    )
