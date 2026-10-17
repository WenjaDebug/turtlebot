"""Daily universe construction: constituents -> prices -> liquidity -> market cap."""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from turtlebot.config import RiskSettings, StrategySettings
from turtlebot.data.provider import MarketDataProvider, liquid_tickers

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UniverseSummary:
    """Funnel statistics of one universe build."""

    universe: int
    with_prices: int
    liquid: int
    market_cap_unknown: int
    eligible: int


class UniverseBuilder:
    """Builds and caches the list of tradable tickers for a day.

    Args:
        provider: Market data source.
        risk: Filter thresholds.
        strategy: Strategy settings (history length, volume window).
        cache_dir: Where ``eligible_YYYYMMDD.json`` files are stored.
    """

    def __init__(
        self,
        provider: MarketDataProvider,
        risk: RiskSettings,
        strategy: StrategySettings,
        cache_dir: Path,
    ) -> None:
        self.provider = provider
        self.risk = risk
        self.strategy = strategy
        self.dir = cache_dir / "live"

    def path(self, day: dt.date) -> Path:
        """Cache file of ``day``."""
        return self.dir / f"eligible_{day:%Y%m%d}.json"

    def cached(self, day: dt.date) -> list[str] | None:
        """Previously built universe of ``day``."""
        path = self.path(day)
        if not path.exists():
            return None
        return list(json.loads(path.read_text())["tickers"])

    def previous(self, day: dt.date) -> tuple[dt.date, list[str]] | None:
        """Most recent stored universe strictly before ``day``."""
        for path in sorted(self.dir.glob("eligible_*.json"), reverse=True):
            saved = dt.datetime.strptime(path.stem.split("_")[1], "%Y%m%d").date()  # noqa: DTZ007
            if saved < day:
                return saved, list(json.loads(path.read_text())["tickers"])
        return None

    def build(self, day: dt.date) -> tuple[list[str], UniverseSummary]:
        """Build (slow: downloads prices and market caps) and cache the eligible tickers."""
        cached = self.cached(day)
        if cached is not None:
            return cached, UniverseSummary(0, 0, 0, 0, len(cached))
        universe = self.provider.universe(day)
        start = day - dt.timedelta(days=self.strategy.history_days)
        prices = self.provider.daily_bars(universe, start, day)
        liquid = [
            t
            for t in liquid_tickers(
                prices, self.risk.min_dollar_volume, self.strategy.volume_period
            )
            if float(prices[t]["Close"].iloc[-1]) >= self.risk.min_price
        ]
        caps = self.provider.market_caps(liquid, day)
        eligible = sorted(t for t in liquid if (caps.get(t) or 0) > self.risk.min_market_cap)
        summary = UniverseSummary(
            universe=len(universe),
            with_prices=len(prices),
            liquid=len(liquid),
            market_cap_unknown=sum(1 for t in liquid if caps.get(t) is None),
            eligible=len(eligible),
        )
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path(day).write_text(json.dumps({"summary": asdict(summary), "tickers": eligible}))
        log.info("Universe built: %s", summary)
        return eligible, summary
