"""Latest prices for valuing open positions."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Protocol

import yfinance as yf

log = logging.getLogger(__name__)


class QuoteService(Protocol):
    """Latest trade prices per symbol."""

    def quotes(self, symbols: Sequence[str]) -> dict[str, float]:
        """Prices for the symbols that could be resolved (others are omitted)."""


class StaticQuotes:
    """Fixed prices (tests and demo mode)."""

    def __init__(self, prices: Mapping[str, float]) -> None:
        self.prices = dict(prices)

    def quotes(self, symbols: Sequence[str]) -> dict[str, float]:  # noqa: D102 - see protocol
        return {s: self.prices[s] for s in symbols if s in self.prices}


class YahooQuotes:
    """Delayed quotes from Yahoo Finance with a TTL cache.

    The dashboard is read-only and never queries TWS, so it cannot interfere with the bot's
    client id or market data lines.

    Args:
        ttl_s: Cache lifetime in seconds.
    """

    def __init__(self, ttl_s: float = 60.0) -> None:
        self.ttl_s = ttl_s
        self._cache: dict[str, tuple[float, float]] = {}  # symbol -> (fetched_at, price)
        self._lock = threading.Lock()

    def quotes(self, symbols: Sequence[str]) -> dict[str, float]:  # noqa: D102
        now = time.monotonic()
        with self._lock:
            missing = [
                s for s in symbols if s not in self._cache or now - self._cache[s][0] > self.ttl_s
            ]
        if missing:
            fresh = self._download(missing)
            with self._lock:
                for symbol, price in fresh.items():
                    self._cache[symbol] = (now, price)
        with self._lock:
            return {s: self._cache[s][1] for s in symbols if s in self._cache}

    @staticmethod
    def _download(symbols: Sequence[str]) -> dict[str, float]:
        try:
            data = yf.download(
                list(symbols), period="5d", progress=False, group_by="ticker", threads=True
            )
        except Exception as exc:  # noqa: BLE001 - network/vendor errors must not break the UI
            log.warning("Quote download failed: %s", exc)
            return {}
        prices: dict[str, float] = {}
        for symbol in symbols:
            try:
                series = data[symbol]["Close"] if len(symbols) > 1 else data["Close"]
                value = series.dropna().iloc[-1]
                prices[symbol] = float(value.iloc[0] if hasattr(value, "iloc") else value)
            except (KeyError, IndexError, TypeError, ValueError):
                continue
        return prices
