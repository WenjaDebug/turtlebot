"""Yahoo Finance / Wikipedia / nasdaqtrader.com implementation of the market data provider.

Port of ``legacy/data_sources.py`` (same sources, same filters, same cache layout), with logging
instead of ``print()`` and explicit error types. Excluded from coverage: it only talks to the
network; the pure helpers live in :mod:`turtlebot.data.provider`.
"""

from __future__ import annotations

import datetime as dt
import gc
import io
import json
import logging
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Final

import pandas as pd
import requests
import yfinance as yf

from turtlebot.exceptions import MarketDataError

log = logging.getLogger(__name__)

HEADERS: Final = {"User-Agent": "Mozilla/5.0 (turtlebot research)"}
LIVE_WIKI: Final = {
    "S&P 500": ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "Symbol"),
    "S&P 400": ("https://en.wikipedia.org/wiki/List_of_S%26P_400_companies", "Symbol"),
    "S&P 600": ("https://en.wikipedia.org/wiki/List_of_S%26P_600_companies", "Symbol"),
}
NASDAQ_LISTED: Final = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
NON_COMMON: Final = r"\b(?:Warrants?|Units?|Rights?|Preferred|Notes|Debentures)\b"
SPAC: Final = (
    r"acquisitions?\d*\b(?:\s+[\w&.]+){0,3}?\s+(?:Corp(?:oration)?|Company|Co|Inc|Ltd|Limited)\b"
    r"|\bacquisitions?\s+[IVX]+\b|\bblank check\b"
)


class YahooMarketData:
    """Market data from public sources with a per-day file cache.

    Args:
        cache_dir: Cache root (shared with the legacy scripts: ``.cache``).
        threads: Use yfinance's threaded downloader (needs a high open-file limit).
        batch_size: Tickers per download batch.
    """

    def __init__(self, cache_dir: Path, *, threads: bool = True, batch_size: int = 50) -> None:
        self.cache_dir = cache_dir
        self.threads = threads
        self.batch_size = batch_size

    # ------------------------------------------------------------------ universe
    def _wiki_constituents(self) -> pd.DataFrame:
        frames = []
        for index, (url, column) in LIVE_WIKI.items():
            html = requests.get(url, headers=HEADERS, timeout=20).text
            table = next(
                t for t in pd.read_html(io.StringIO(html), flavor="lxml") if column in t.columns
            )
            frames.append(
                pd.DataFrame({"ticker": table[column].astype(str).str.strip(), "index": index})
            )
        return pd.concat(frames)

    def _nasdaq_listed(self) -> pd.DataFrame:
        text = requests.get(NASDAQ_LISTED, headers=HEADERS, timeout=30).text
        table = pd.read_csv(io.StringIO(text), sep="|")
        table = table[~table["Symbol"].astype(str).str.startswith("File Creation Time")]
        names = table["Security Name"].astype(str)
        common = (
            (table["Test Issue"] == "N")
            & (table["ETF"] == "N")
            & (table["Financial Status"] == "N")
            & table["Symbol"].astype(str).str.fullmatch(r"[A-Z]+")
            & ~names.str.contains(NON_COMMON, case=False, regex=True)
        )
        spacs = common & names.str.contains(SPAC, case=False, regex=True)
        log.info("Nasdaq list: %d SPACs excluded", int(spacs.sum()))
        common &= ~spacs
        return pd.DataFrame({"ticker": table.loc[common, "Symbol"], "index": "Nasdaq Composite"})

    def universe(self, as_of: dt.date) -> list[str]:
        """S&P 500 + 400 + 600 + Nasdaq Composite common stocks (cached per day)."""
        path = self.cache_dir / "live" / f"universe_{as_of:%Y%m%d}.csv"
        if path.exists():
            return pd.read_csv(path)["ticker"].astype(str).tolist()
        try:
            frames = [self._wiki_constituents(), self._nasdaq_listed()]
        except (requests.RequestException, ValueError, StopIteration) as exc:
            raise MarketDataError(f"universe download failed: {exc}") from exc
        universe = (
            pd.concat(frames)
            .assign(ticker=lambda d: d["ticker"].str.replace(".", "-", regex=False))
            .groupby("ticker", as_index=False)["index"]
            .agg(" + ".join)
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        universe.to_csv(path, index=False)
        return [str(t) for t in universe["ticker"]]

    # ------------------------------------------------------------------ prices
    def _download(
        self, tickers: list[str], start: dt.date, end: dt.date
    ) -> Iterator[tuple[str, pd.DataFrame]]:
        loaded = 0
        for i in range(0, len(tickers), self.batch_size):
            batch = tickers[i : i + self.batch_size]
            try:
                raw = yf.download(
                    batch,
                    start=start,
                    end=end,
                    interval="1d",
                    auto_adjust=False,
                    group_by="ticker",
                    threads=self.threads,
                    progress=False,
                    timeout=30,
                )
            except Exception as exc:
                log.warning("Batch %d-%d failed: %s", i, i + len(batch), exc)
                continue
            if raw is None or raw.empty:
                continue
            available = set(raw.columns.get_level_values(0))
            for ticker in batch:
                if ticker not in available:
                    continue
                df = raw[ticker][["Open", "High", "Low", "Close", "Volume"]].dropna(how="all")
                if not df.empty:
                    df.index = pd.DatetimeIndex(df.index).tz_localize(None)
                    loaded += 1
                    yield ticker, df
            del raw
            gc.collect()  # yfinance keeps file handles until garbage collection
            log.info("Prices: %d/%d loaded", min(i + self.batch_size, len(tickers)), len(tickers))
            time.sleep(2)
        missing = len(tickers) - loaded
        if tickers and missing > 0.1 * len(tickers):
            log.warning(
                "%d of %d tickers without prices - download problem?", missing, len(tickers)
            )

    def daily_bars(
        self, tickers: Sequence[str], start: dt.date, end: dt.date, *, use_cache: bool = True
    ) -> dict[str, pd.DataFrame]:
        """Daily OHLCV, cached per ticker and date range (``use_cache=False`` downloads fresh)."""
        if not use_cache:
            return dict(self._download(list(tickers), start, end))
        folder = self.cache_dir / f"prices_{start:%Y%m%d}_{end:%Y%m%d}"
        folder.mkdir(parents=True, exist_ok=True)
        missing = [t for t in tickers if not (folder / f"{t}.pkl").exists()]
        for ticker, df in self._download(missing, start, end):
            df.to_pickle(folder / f"{ticker}.pkl")
        return {
            t: pd.read_pickle(folder / f"{t}.pkl")  # noqa: S301 - our own cache files
            for t in tickers
            if (folder / f"{t}.pkl").exists()
        }

    # ------------------------------------------------------------------ fundamentals
    def market_caps(self, tickers: Sequence[str], as_of: dt.date) -> dict[str, float | None]:
        """Market cap snapshot (yfinance ``.info``), cached per day."""
        path = self.cache_dir / f"market_caps_{as_of:%Y%m%d}.json"
        caps: dict[str, float | None] = json.loads(path.read_text()) if path.exists() else {}
        todo = [t for t in tickers if t not in caps]
        for n, ticker in enumerate(todo, 1):
            try:
                cap = yf.Ticker(ticker).info.get("marketCap")
                caps[ticker] = float(cap) if cap is not None else None
            except Exception as exc:
                log.debug("Market cap %s failed: %s", ticker, type(exc).__name__)
                caps[ticker] = None
            if n % 50 == 0 or n == len(todo):
                log.info("Market caps: %d/%d loaded", n, len(todo))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(caps))
            time.sleep(0.3)
        return {t: caps.get(t) for t in tickers}
