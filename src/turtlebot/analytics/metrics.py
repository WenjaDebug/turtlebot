"""Performance metrics shared by the backtest and the dashboard."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def drawdown_series(equity: pd.Series) -> pd.Series:
    """Relative distance from the running peak (``0`` at a new high, negative below it)."""
    if equity.empty:
        return equity.astype(float)
    peak = equity.cummax()
    return equity / peak - 1.0


def max_drawdown(equity: pd.Series) -> float:
    """Largest peak-to-trough decline as a negative fraction (``0.0`` for empty input)."""
    dd = drawdown_series(equity)
    return float(dd.min()) if not dd.empty else 0.0


def daily_returns(equity: pd.Series) -> pd.Series:
    """Daily simple returns of an equity series sampled at any frequency (last value per day)."""
    if equity.empty:
        return equity.astype(float)
    index = pd.DatetimeIndex(equity.index)
    daily = equity.groupby(index.normalize()).last()
    return daily.pct_change().dropna()


def sharpe_ratio(
    returns: pd.Series, periods: int = TRADING_DAYS, risk_free: float = 0.0
) -> float | None:
    """Annualised Sharpe ratio; ``None`` if fewer than two returns or zero volatility."""
    if len(returns) < 2:
        return None
    excess = returns - risk_free / periods
    std = float(excess.std(ddof=1))
    if std == 0 or math.isnan(std):
        return None
    return float(excess.mean()) / std * math.sqrt(periods)


def sortino_ratio(returns: pd.Series, periods: int = TRADING_DAYS) -> float | None:
    """Annualised Sortino ratio (downside deviation); ``None`` if undefined."""
    if len(returns) < 2:
        return None
    downside = np.minimum(returns.to_numpy(float), 0.0)
    dd = float(np.sqrt(np.mean(downside**2)))
    if dd == 0:
        return None
    return float(returns.mean()) / dd * math.sqrt(periods)


def cagr(equity: pd.Series) -> float | None:
    """Compound annual growth rate based on calendar time; ``None`` for < 1 day of data."""
    if len(equity) < 2 or float(equity.iloc[0]) <= 0:
        return None
    index = pd.DatetimeIndex(equity.index)
    years = (index[-1] - index[0]).days / 365.25
    if years <= 0:
        return None
    return float((float(equity.iloc[-1]) / float(equity.iloc[0])) ** (1 / years) - 1)


@dataclass(frozen=True, slots=True)
class TradeStats:
    """Statistics over closed trades."""

    trades: int
    win_rate: float | None
    profit_factor: float | None
    avg_win: float | None
    avg_loss: float | None
    expectancy: float | None


def trade_stats(pnls: Sequence[float]) -> TradeStats:
    """Win rate, profit factor and averages of closed-trade P&Ls."""
    values = [p for p in pnls if p is not None and math.isfinite(p)]
    if not values:
        return TradeStats(0, None, None, None, None, None)
    wins = [p for p in values if p > 0]
    losses = [p for p in values if p <= 0]
    gross_loss = -sum(losses)
    return TradeStats(
        trades=len(values),
        win_rate=len(wins) / len(values),
        profit_factor=sum(wins) / gross_loss if gross_loss > 0 else None,
        avg_win=sum(wins) / len(wins) if wins else None,
        avg_loss=sum(losses) / len(losses) if losses else None,
        expectancy=sum(values) / len(values),
    )


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    """Headline metrics of an equity curve plus its trades."""

    total_return: float | None
    cagr: float | None
    sharpe: float | None
    sortino: float | None
    max_drawdown: float
    volatility: float | None
    trades: TradeStats

    def as_dict(self) -> dict[str, object]:
        """Plain dict (JSON-serialisable)."""
        return asdict(self)


def compute_metrics(equity: pd.Series, trade_pnls: Sequence[float]) -> PerformanceMetrics:
    """All headline metrics for an equity series and the P&Ls of its closed trades."""
    rets = daily_returns(equity)
    total = (
        float(equity.iloc[-1]) / float(equity.iloc[0]) - 1
        if len(equity) >= 2 and float(equity.iloc[0]) > 0
        else None
    )
    vol = float(rets.std(ddof=1)) * math.sqrt(TRADING_DAYS) if len(rets) >= 2 else None
    return PerformanceMetrics(
        total_return=total,
        cagr=cagr(equity),
        sharpe=sharpe_ratio(rets),
        sortino=sortino_ratio(rets),
        max_drawdown=max_drawdown(equity),
        volatility=vol,
        trades=trade_stats(trade_pnls),
    )
