"""Quality checks of the lows that the trailing stops are computed from.

The strategy is unchanged: a stop is the lowest low of the exit period. These checks only decide
which data source provides that low and raise warnings a human should look at:

* ``low_source_mismatch`` - Yahoo and TWS disagree by more than the tolerance; the TWS low
  (the broker's own record of the trades) is used.
* ``low_unverified`` - TWS bars are unavailable; with ``require_second_source`` the stop is not
  trailed in this run (it keeps its last level, the conservative choice).
* ``low_outlier`` - the low of the window lies far (``outlier_atr_multiple`` ATRs) below the
  surrounding closes. Warning only: if both sources agree, the low is real.
* ``possible_takeover`` - a jump on heavy volume followed by sessions in a very narrow range,
  the typical pattern of a pending cash takeover (e.g. BMBL, 13.10.2026). The trailing stop
  then stays far below the price for up to ``exit_period`` sessions. Warning only.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

import pandas as pd

from turtlebot.config import DataQualitySettings
from turtlebot.data.provider import recent_lows
from turtlebot.domain.models import DailyBar

VOLUME_WINDOW = 20
LOW_CHECK_KEY = "low_check"  # meta key: latest findings per symbol (JSON)


class LowIssueKind(StrEnum):
    """Kinds of data quality findings."""

    SOURCE_MISMATCH = "low_source_mismatch"
    UNVERIFIED = "low_unverified"
    OUTLIER = "low_outlier"
    TAKEOVER = "possible_takeover"


@dataclass(frozen=True, slots=True)
class LowIssue:
    """One finding for one symbol."""

    kind: LowIssueKind
    symbol: str
    message: str


@dataclass(frozen=True, slots=True)
class LowsCheck:
    """Validated lows plus findings.

    Attributes:
        lows: Low per symbol to trail stops to; symbols without a usable low are omitted.
        issues: Findings in symbol order.
    """

    lows: dict[str, float] = field(default_factory=dict)
    issues: list[LowIssue] = field(default_factory=list)


def bars_frame(bars: Sequence[DailyBar]) -> pd.DataFrame:
    """Daily bars as an OHLCV frame indexed by date (the layout of the Yahoo provider)."""
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


def _completed(frame: pd.DataFrame, before: dt.date) -> pd.DataFrame:
    return frame[pd.DatetimeIndex(frame.index).date < before]


def check_lows(
    symbols: Sequence[str],
    primary: Mapping[str, pd.DataFrame],
    secondary: Mapping[str, Sequence[DailyBar]] | None,
    *,
    period: int,
    before: dt.date,
    settings: DataQualitySettings,
) -> LowsCheck:
    """Choose the low per symbol and collect findings.

    Args:
        symbols: Symbols whose stops need a low.
        primary: Yahoo daily bars (with enough history for the ATR and volume checks).
        secondary: TWS daily bars per symbol, ``None`` if the cross-check is disabled.
        period: Exit period (number of completed sessions).
        before: Only sessions strictly before this date count.
        settings: Thresholds.

    Returns:
        The lows to use and the findings.
    """
    result = LowsCheck()
    for symbol in sorted(symbols):
        frame = primary.get(symbol)
        first = _low(frame, period, before)
        tws_frame = bars_frame(secondary.get(symbol, [])) if secondary is not None else None
        second = _low(tws_frame, period, before) if tws_frame is not None else None

        chosen, source = first, frame
        if secondary is None:
            pass
        elif first is not None and second is not None:
            if abs(first - second) > settings.low_tolerance * second:
                chosen, source = second, tws_frame
                result.issues.append(
                    LowIssue(
                        LowIssueKind.SOURCE_MISMATCH,
                        symbol,
                        f"{period}-day low Yahoo {first:g} vs TWS {second:g} "
                        f"({first / second - 1:+.1%}); stop uses the TWS low",
                    )
                )
        elif second is not None:
            chosen, source = second, tws_frame
            result.issues.append(
                LowIssue(
                    LowIssueKind.SOURCE_MISMATCH,
                    symbol,
                    f"no Yahoo bars; stop uses the TWS {period}-day low {second:g}",
                )
            )
        elif first is not None:
            trailed = not settings.require_second_source
            result.issues.append(
                LowIssue(
                    LowIssueKind.UNVERIFIED,
                    symbol,
                    f"TWS bars unavailable, Yahoo low {first:g} not verified; "
                    + ("stop uses it anyway" if trailed else "stop not trailed this run"),
                )
            )
            if not trailed:
                chosen = None

        if chosen is not None:
            result.lows[symbol] = round(chosen, 4)
        if source is not None and chosen is not None:
            outlier = outlier_message(_completed(source, before), period, settings)
            if outlier:
                confirmed = second is not None and first is not None
                result.issues.append(
                    LowIssue(
                        LowIssueKind.OUTLIER,
                        symbol,
                        outlier + ("; confirmed by Yahoo and TWS" if confirmed else ""),
                    )
                )
        pattern_frame = frame if frame is not None and not frame.empty else tws_frame
        if pattern_frame is not None:
            takeover = takeover_message(_completed(pattern_frame, before), period, settings)
            if takeover:
                result.issues.append(LowIssue(LowIssueKind.TAKEOVER, symbol, takeover))
    return result


def _low(frame: pd.DataFrame | None, period: int, before: dt.date) -> float | None:
    if frame is None or frame.empty:
        return None
    return recent_lows({"x": frame}, period, before=before).get("x")


def outlier_message(frame: pd.DataFrame, period: int, settings: DataQualitySettings) -> str | None:
    """Describe the window's low if it lies far below the surrounding closes, else ``None``."""
    window = frame.tail(period)
    if window.empty or len(frame) <= settings.atr_period:
        return None
    pos = len(frame) - len(window) + int(window["Low"].to_numpy().argmin())
    low = float(frame["Low"].iloc[pos])
    neighbours = [float(frame["Close"].iloc[i]) for i in (pos - 1, pos + 1) if 0 <= i < len(frame)]
    neighbours.append(float(frame["Close"].iloc[pos]))
    atr = _atr(frame.iloc[:pos], settings.atr_period)
    if atr is None or atr <= 0:
        return None
    depth = (min(neighbours) - low) / atr
    if depth < settings.outlier_atr_multiple:
        return None
    day = pd.Timestamp(frame.index[pos]).date()
    return f"low {low:g} on {day:%d.%m.} is {depth:.1f} ATR below the surrounding closes"


def _atr(frame: pd.DataFrame, period: int) -> float | None:
    if len(frame) < period + 1:
        return None
    prev_close = frame["Close"].shift(1)
    true_range = pd.concat(
        [
            frame["High"] - frame["Low"],
            (frame["High"] - prev_close).abs(),
            (frame["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    value = float(true_range.tail(period).mean())
    return value if pd.notna(value) else None


def takeover_message(frame: pd.DataFrame, period: int, settings: DataQualitySettings) -> str | None:
    """Describe a takeover-like pattern within the exit period, else ``None``.

    Pattern: a session gaining at least ``takeover_jump`` on ``takeover_volume_ratio`` times its
    average volume, followed by at least ``takeover_quiet_days`` sessions whose range stays below
    ``takeover_quiet_range`` (the price is pinned to the offer).
    """
    closes = frame["Close"].to_numpy(dtype=float)
    volumes = frame["Volume"].to_numpy(dtype=float)
    ranges = ((frame["High"] - frame["Low"]) / frame["Close"]).to_numpy(dtype=float)
    n = len(frame)
    for j in range(max(1, n - period), n - settings.takeover_quiet_days + 1):
        jump = closes[j] / closes[j - 1] - 1 if closes[j - 1] > 0 else 0.0
        if jump < settings.takeover_jump:
            continue
        base = volumes[max(0, j - VOLUME_WINDOW) : j]
        if base.size == 0 or base.mean() <= 0:
            continue
        ratio = volumes[j] / base.mean()
        if ratio < settings.takeover_volume_ratio:
            continue
        after = ranges[j + 1 :]
        if (
            len(after) >= settings.takeover_quiet_days
            and (after <= settings.takeover_quiet_range).all()
        ):
            day = pd.Timestamp(frame.index[j]).date()
            return (
                f"jump {jump:+.0%} on {day:%d.%m.} at {ratio:.0f}x volume, then {len(after)} "
                f"sessions within {after.max():.1%} range - possibly a pending takeover; the "
                f"trailing stop stays far below the price until the jump leaves the "
                f"{period}-day window"
            )
    return None
