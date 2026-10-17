"""Market data abstraction and helpers that are independent of the data vendor."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Protocol

import pandas as pd


class MarketDataProvider(Protocol):
    """Source of daily bars, index constituents and market caps."""

    def daily_bars(
        self, tickers: Sequence[str], start: dt.date, end: dt.date, *, use_cache: bool = True
    ) -> dict[str, pd.DataFrame]:
        """Split-adjusted daily OHLCV per ticker for ``[start, end)``.

        Tickers without data are omitted. ``use_cache=False`` always downloads fresh data (needed
        during the session, when today's bar is still forming).
        """

    def universe(self, as_of: dt.date) -> list[str]:
        """Candidate tickers before liquidity and market-cap filters."""

    def market_caps(self, tickers: Sequence[str], as_of: dt.date) -> dict[str, float | None]:
        """Market capitalisation per ticker (``None`` if unknown)."""


def liquid_tickers(
    prices: Mapping[str, pd.DataFrame], min_dollar_volume: float, window: int = 20
) -> list[str]:
    """Tickers whose average daily dollar volume over ``window`` days exceeds the minimum."""
    return [
        t
        for t, df in prices.items()
        if len(df) >= window
        and float((df["Close"] * df["Volume"]).tail(window).mean()) > min_dollar_volume
    ]


def recent_lows(
    prices: Mapping[str, pd.DataFrame], period: int, *, before: dt.date | None = None
) -> dict[str, float]:
    """Lowest low of the last ``period`` bars per ticker.

    Args:
        prices: Daily bars.
        period: Number of bars.
        before: If given, only bars strictly before this date count (completed days).

    Returns:
        Mapping ticker -> low; tickers without usable bars are omitted.
    """
    lows: dict[str, float] = {}
    for ticker, df in prices.items():
        frame = df if before is None else df[pd.DatetimeIndex(df.index).date < before]
        if frame.empty or "Low" not in frame:
            continue
        low = float(frame["Low"].tail(period).min())
        if pd.notna(low):
            lows[ticker] = low
    return lows
