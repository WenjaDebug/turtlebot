"""Tests for the portfolio backtest and the performance metrics."""

from __future__ import annotations

import importlib
import math
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import numpy as np
import pandas as pd
import pytest

from tests.factories import ohlcv
from turtlebot.analytics.metrics import (
    cagr,
    compute_metrics,
    daily_returns,
    drawdown_series,
    max_drawdown,
    sharpe_ratio,
    sortino_ratio,
    trade_stats,
)
from turtlebot.backtest.engine import BacktestConfig, run_backtest
from turtlebot.config import Settings
from turtlebot.domain.models import Block
from turtlebot.exceptions import EmptyMarketDataError

LEGACY = Path(__file__).resolve().parents[2] / "legacy"
NO_COSTS = BacktestConfig(slippage_bps=0.0, commission=False)


@pytest.fixture
def legacy_research() -> Iterator[ModuleType]:
    sys.path[:0] = [str(LEGACY), str(LEGACY / "research")]
    try:
        yield importlib.import_module("backtest_turtle_filters")
    finally:
        del sys.path[:2]


def trend_then_drop(
    *, base: float = 50.0, jump: float = 5.0, rise: int = 8, drop_to: float = 40.0, n: int = 40
) -> pd.DataFrame:
    """Flat, breakout with volume, a few rising days, then a fall through the stop."""
    closes = [base] * (n - 1) + [base + jump]
    closes += [base + jump + i for i in range(1, rise + 1)]
    closes += [drop_to] * 5
    volumes = [1e6] * (n - 1) + [3e6] + [1e6] * (rise + 5)
    return ohlcv(closes, volumes=volumes, end="2026-09-30")


def settings() -> Settings:
    return Settings(_env_file=None)


class TestBacktest:
    def test_single_trade_matches_legacy_stop_logic(self, legacy_research: ModuleType) -> None:
        df = trend_then_drop()
        result = run_backtest({"AAA": df}, settings(), NO_COSTS)
        assert len(result.trades) == 1
        trade = result.trades[0]

        o, h, low, c = (df[k].to_numpy(float) for k in ("Open", "High", "Low", "Close"))
        low_incl = df["Low"].rolling(10).min().to_numpy()
        low_wide = df["Low"].rolling(10).min().shift(1).to_numpy()
        i = 39  # signal bar
        j, price, still_open = legacy_research.stop_exit(o, h, low, c, low_wide, i + 1, low_incl[i])
        assert not still_open
        assert trade.entry_price == pytest.approx(o[i + 1])
        assert trade.exit_price == pytest.approx(price, abs=0.005)
        assert trade.exit_date == df.index[j].date()

    def test_stop_trails_up_and_gap_exits_at_open(self) -> None:
        trade = run_backtest({"AAA": trend_then_drop()}, settings(), NO_COSTS).trades[0]
        assert trade.exit_reason == "stop_gap"  # opens at 40 far below the trailed stop
        assert trade.exit_price == 40.0
        assert trade.pnl == pytest.approx(trade.qty * (40.0 - trade.entry_price))

    def test_slots_and_block2_filter(self) -> None:
        cfg = settings()
        cfg.block1.max_positions = 1
        prices = {
            "STRONG": trend_then_drop(jump=10.0),  # ~ +19 %
            "MID": trend_then_drop(jump=6.0),  # ~ +11 %
            "WEAK": trend_then_drop(jump=1.0),  # ~ +1 %, below the block-2 filter
        }
        trades = {t.ticker: t.block for t in run_backtest(prices, cfg, NO_COSTS).trades}
        assert trades == {"STRONG": Block.ONE, "MID": Block.TWO}

    def test_close_fill_mode_enters_on_signal_day(self) -> None:
        df = trend_then_drop()
        cfg = BacktestConfig(entry_fill="close", slippage_bps=0, commission=False)
        trade = run_backtest({"AAA": df}, settings(), cfg).trades[0]
        assert trade.entry_date == df.index[39].date()
        assert trade.entry_price == 55.0

    def test_equity_accounting_is_consistent(self) -> None:
        prices = {"A": trend_then_drop(), "B": trend_then_drop(jump=8)}
        cfg = BacktestConfig(initial_capital=50_000)
        result = run_backtest(prices, settings(), cfg)
        assert result.equity.iloc[-1] == pytest.approx(50_000 + sum(t.pnl for t in result.trades))
        assert result.metrics.trades.trades == len(result.trades)
        frame = result.trades_frame()
        assert set(frame["ticker"]) == {"A", "B"}

    def test_costs_reduce_pnl(self) -> None:
        df = {"A": trend_then_drop()}
        free = run_backtest(df, settings(), NO_COSTS).trades[0].pnl
        costly = run_backtest(df, settings(), BacktestConfig(slippage_bps=20)).trades[0].pnl
        assert costly < free

    def test_insufficient_cash_skips_entry(self) -> None:
        cfg = BacktestConfig(initial_capital=1_000, slippage_bps=0, commission=False)
        s = settings()
        s.risk.position_pct = 0.9
        assert run_backtest({"A": trend_then_drop()}, s, cfg).trades
        # Sized at the signal close (55) with the whole equity; the next open (56) no longer fits.
        s.risk.position_pct = 1.0
        assert run_backtest({"A": trend_then_drop()}, s, cfg).trades == []
        s.risk.position_pct = 0.001  # budget below one share
        assert run_backtest({"A": trend_then_drop()}, s, cfg).trades == []

    def test_open_position_is_closed_at_the_end(self) -> None:
        closes = [50.0] * 39 + [55.0] + [56.0 + i for i in range(5)]
        vols = [1e6] * 39 + [3e6] + [1e6] * 5
        trade = run_backtest({"A": ohlcv(closes, volumes=vols)}, settings(), NO_COSTS).trades[0]
        assert trade.exit_reason == "end"
        assert trade.exit_price == 60.0

    def test_no_usable_history_raises(self) -> None:
        with pytest.raises(EmptyMarketDataError):
            run_backtest({"A": ohlcv([1.0] * 5)}, settings())


class TestMetrics:
    def test_drawdown(self) -> None:
        equity = pd.Series([100.0, 120.0, 90.0, 130.0])
        assert drawdown_series(equity).tolist() == pytest.approx([0, 0, -0.25, 0])
        assert max_drawdown(equity) == pytest.approx(-0.25)
        assert max_drawdown(pd.Series(dtype=float)) == 0.0

    def test_sharpe_and_sortino(self) -> None:
        rets = pd.Series([0.01, -0.005, 0.02, 0.0, 0.004])
        expected = rets.mean() / rets.std(ddof=1) * math.sqrt(252)
        assert sharpe_ratio(rets) == pytest.approx(expected)
        assert sortino_ratio(rets) is not None
        assert sharpe_ratio(pd.Series([0.01])) is None
        assert sharpe_ratio(pd.Series([0.01, 0.01])) is None
        assert sortino_ratio(pd.Series([0.01, 0.02])) is None

    def test_daily_returns_resample_intraday_snapshots(self) -> None:
        idx = pd.to_datetime(["2026-10-12 10:00", "2026-10-12 16:00", "2026-10-13 16:00"])
        rets = daily_returns(pd.Series([100.0, 110.0, 121.0], index=idx))
        assert rets.tolist() == pytest.approx([0.1])

    def test_cagr(self) -> None:
        idx = pd.to_datetime(["2025-01-01", "2026-01-01"])
        assert cagr(pd.Series([100.0, 110.0], index=idx)) == pytest.approx(0.1, abs=1e-3)
        assert cagr(pd.Series([100.0], index=idx[:1])) is None

    def test_trade_stats(self) -> None:
        stats = trade_stats([100.0, -50.0, 200.0, -50.0, float("nan")])
        assert stats.trades == 4
        assert stats.win_rate == 0.5
        assert stats.profit_factor == pytest.approx(3.0)
        assert stats.expectancy == pytest.approx(50.0)
        assert trade_stats([]).win_rate is None
        assert trade_stats([10.0]).profit_factor is None

    def test_compute_metrics(self) -> None:
        idx = pd.bdate_range("2026-01-01", periods=60)
        equity = pd.Series(100 * np.cumprod(1 + np.full(60, 0.001)), index=idx)
        m = compute_metrics(equity, [1.0, -1.0])
        assert m.total_return == pytest.approx(1.001**59 - 1)
        assert m.max_drawdown == 0.0
        assert m.as_dict()["trades"]["trades"] == 2  # type: ignore[index]
