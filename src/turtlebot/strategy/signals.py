"""Turtle/Donchian breakout signal.

Pure pandas, no broker or backtest dependency; the same function drives the backtest and the
live scan. The logic is a 1:1 port of ``legacy/strategies/signals.py::turtle_breakout`` and must
not be changed without an explicit decision (see the regression test
``tests/strategy/test_signals.py::test_matches_legacy_implementation``).
"""

from __future__ import annotations

from typing import Final

import pandas as pd

OHLCV: Final = ("Open", "High", "Low", "Close", "Volume")


def standardize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Bring ``ib_async`` bar data (``util.df(bars)``) into the expected format.

    Args:
        df: Frame with lower-case columns ``date/open/high/low/close/volume``.

    Returns:
        Frame indexed by date with the columns :data:`OHLCV`, sorted ascending.
    """
    df = df.rename(columns=str.capitalize)
    if "Date" in df.columns:
        df = df.set_index(pd.to_datetime(df.pop("Date")))
    return df[list(OHLCV)].sort_index()


def turtle_breakout(
    df: pd.DataFrame,
    entry_period: int = 20,
    exit_period: int = 10,
    volume_period: int = 20,
    volume_factor: float = 1.5,
    rank_by: str = "volume_ratio",
) -> pd.DataFrame:
    """Donchian breakout with volume confirmation.

    Entry: close above the highest high of the previous ``entry_period`` days (excluding today)
    **and** volume above ``volume_factor`` times the average of the previous ``volume_period``
    days. Exit: close below the lowest low of the previous ``exit_period`` days. A breakout is
    only valid on the signal day.

    Args:
        df: Daily OHLCV, ascending by date.
        entry_period: Donchian high lookback.
        exit_period: Donchian low lookback.
        volume_period: Volume average lookback.
        volume_factor: Required volume multiple.
        rank_by: Column copied into ``score`` for ranking (``volume_ratio`` or
            ``breakout_strength``).

    Returns:
        Frame with one row per day and the columns ``entry``, ``exit``, ``valid``,
        ``donchian_high``, ``donchian_low``, ``volume_ratio``, ``breakout_strength`` and ``score``.
    """
    donchian_high = df["High"].rolling(entry_period).max().shift(1)
    donchian_low = df["Low"].rolling(exit_period).min().shift(1)
    volume_ratio = df["Volume"] / df["Volume"].rolling(volume_period).mean().shift(1)
    breakout_strength = df["Close"] / donchian_high - 1
    entry = (df["Close"] > donchian_high) & (volume_ratio > volume_factor)
    result = pd.DataFrame(
        {
            "entry": entry,
            "exit": df["Close"] < donchian_low,
            "valid": entry,
            "donchian_high": donchian_high,
            "donchian_low": donchian_low,
            "volume_ratio": volume_ratio,
            "breakout_strength": breakout_strength,
        },
        index=df.index,
    )
    result["score"] = result[rank_by]
    return result
