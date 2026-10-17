"""Event-driven daily portfolio backtest of the two-block turtle strategy.

The backtest reuses the live code paths wherever possible:

* signals: :func:`turtlebot.strategy.signals.turtle_breakout`
* slot allocation incl. block-2 filter and "traded today" exclusion:
  :func:`turtlebot.strategy.allocation.plan_block_entries`
* sizing and costs: :mod:`turtlebot.risk.sizing`

Execution model (same as ``legacy/research/backtest_turtle_filters.py``):

* signal at the close of day *t*, entry at the open of *t+1* (or at the close of *t* with
  ``entry_fill="close"``, mirroring the live MOC orders);
* initial stop = lowest low of the last ``exit_period`` days including the signal day;
* every morning the stop is raised (never lowered) to the lowest low of the ``exit_period``
  days before the current one;
* exit at the open on a gap below the stop, otherwise at the stop when the low touches it.

Known limitation: the universe is whatever ``prices`` contains (survivorship bias if it is
today's constituent list).
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import numpy.typing as npt
import pandas as pd

from turtlebot.analytics.metrics import PerformanceMetrics, compute_metrics
from turtlebot.config import Settings
from turtlebot.domain.models import Block, Candidate, Position, PositionStatus
from turtlebot.exceptions import EmptyMarketDataError
from turtlebot.risk.sizing import ibkr_commission, position_quantity
from turtlebot.strategy.allocation import plan_block_entries
from turtlebot.strategy.signals import turtle_breakout


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """Simulation settings that are not part of the live configuration.

    Attributes:
        initial_capital: Starting equity in USD.
        entry_fill: ``"next_open"`` (research default) or ``"close"`` (live MOC).
        slippage_bps: Adverse slippage per fill in basis points.
        commission: Apply IBKR fixed commissions.
    """

    initial_capital: float = 100_000.0
    entry_fill: Literal["next_open", "close"] = "next_open"
    slippage_bps: float = 5.0
    commission: bool = True


@dataclass(frozen=True, slots=True)
class Trade:
    """A simulated round trip."""

    ticker: str
    block: Block
    qty: int
    entry_date: dt.date
    entry_price: float
    exit_date: dt.date
    exit_price: float
    exit_reason: str
    pnl: float
    strength: float

    @property
    def ret(self) -> float:
        """Return of the trade before costs relative to the entry price."""
        return self.exit_price / self.entry_price - 1


@dataclass(slots=True)
class _Open:
    ticker: str
    block: Block
    qty: int
    entry_date: dt.date
    entry_price: float
    stop: float
    strength: float
    entry_cost: float


@dataclass(slots=True)
class BacktestResult:
    """Equity curve, trades and metrics of a run."""

    equity: pd.Series
    trades: list[Trade]
    metrics: PerformanceMetrics
    block_metrics: dict[Block, PerformanceMetrics] = field(default_factory=dict)

    def trades_frame(self) -> pd.DataFrame:
        """Trades as a DataFrame."""
        return pd.DataFrame(
            [
                {
                    "ticker": t.ticker,
                    "block": int(t.block),
                    "qty": t.qty,
                    "entry_date": t.entry_date,
                    "entry_price": t.entry_price,
                    "exit_date": t.exit_date,
                    "exit_price": t.exit_price,
                    "exit_reason": t.exit_reason,
                    "pnl": t.pnl,
                    "return": t.ret,
                    "strength": t.strength,
                }
                for t in self.trades
            ]
        )


type _Matrix = npt.NDArray[np.float64]


@dataclass(slots=True)
class _Panel:
    """Prices and precomputed signal columns as ``dates x tickers`` NumPy matrices."""

    dates: list[dt.date]
    tickers: list[str]
    open: _Matrix
    low: _Matrix
    close: _Matrix
    entry: npt.NDArray[np.bool_]
    strength: _Matrix
    volume_ratio: _Matrix
    low_incl: _Matrix  # lowest low incl. today (initial stop)
    low_prev: _Matrix  # lowest low of the days before (morning trailing)


def _panel(prices: Mapping[str, pd.DataFrame], settings: Settings) -> _Panel:
    s = settings.strategy
    keys = ("open", "low", "close", "entry", "strength", "vr", "low_incl", "low_prev")
    cols: dict[str, dict[str, pd.Series]] = {k: {} for k in keys}
    for ticker, raw in prices.items():
        df = raw.dropna(subset=["Open", "High", "Low", "Close"])
        df = df[df["Volume"] > 0]
        if len(df) <= s.entry_period + 1:
            continue
        sig = turtle_breakout(
            df, s.entry_period, s.exit_period, s.volume_period, s.volume_factor, s.rank_by.value
        )
        cols["open"][ticker] = df["Open"]
        cols["low"][ticker] = df["Low"]
        cols["close"][ticker] = df["Close"]
        cols["entry"][ticker] = sig["entry"].astype(float)
        cols["strength"][ticker] = sig["breakout_strength"]
        cols["vr"][ticker] = sig["volume_ratio"]
        cols["low_incl"][ticker] = df["Low"].rolling(s.exit_period).min()
        cols["low_prev"][ticker] = df["Low"].rolling(s.exit_period).min().shift(1)
    if not cols["close"]:
        raise EmptyMarketDataError("no ticker with enough history for the backtest")

    close = pd.DataFrame(cols["close"]).sort_index()
    tickers = [str(t) for t in close.columns]

    def matrix(key: str) -> _Matrix:
        frame = pd.DataFrame(cols[key]).reindex(index=close.index, columns=tickers)
        return frame.to_numpy(dtype=np.float64, na_value=np.nan)

    return _Panel(
        dates=[ts.date() for ts in pd.DatetimeIndex(close.index)],
        tickers=tickers,
        open=matrix("open"),
        low=matrix("low"),
        close=matrix("close"),
        entry=np.nan_to_num(matrix("entry"), nan=0.0) > 0,
        strength=matrix("strength"),
        volume_ratio=matrix("vr"),
        low_incl=matrix("low_incl"),
        low_prev=matrix("low_prev"),
    )


class _Simulation:
    def __init__(
        self, prices: Mapping[str, pd.DataFrame], settings: Settings, cfg: BacktestConfig
    ) -> None:
        self.s = settings
        self.cfg = cfg
        self.p = _panel(prices, settings)
        self.col = {t: j for j, t in enumerate(self.p.tickers)}
        self.cash = cfg.initial_capital
        self.open: dict[str, _Open] = {}
        self.trades: list[Trade] = []
        self.pending: list[tuple[Candidate, Block, int]] = []
        self.equity: dict[dt.date, float] = {}

    def _fee(self, qty: int, price: float) -> float:
        return ibkr_commission(qty, price) if self.cfg.commission else 0.0

    def _slip(self, price: float, side: int) -> float:
        return price * (1 + side * self.cfg.slippage_bps / 10_000)

    def _buy(self, cand: Candidate, block: Block, qty: int, price: float, day: dt.date) -> None:
        fill = self._slip(price, +1)
        fee = self._fee(qty, fill)
        if qty < 1 or qty * fill + fee > self.cash:
            return
        self.cash -= qty * fill + fee
        self.open[cand.ticker] = _Open(
            cand.ticker, block, qty, day, fill, round(cand.stop, 2), cand.strength, fee
        )

    def _sell(self, pos: _Open, price: float, day: dt.date, reason: str) -> None:
        fill = self._slip(price, -1)
        fee = self._fee(pos.qty, fill)
        self.cash += pos.qty * fill - fee
        pnl = pos.qty * (fill - pos.entry_price) - fee - pos.entry_cost
        self.trades.append(
            Trade(
                pos.ticker,
                pos.block,
                pos.qty,
                pos.entry_date,
                pos.entry_price,
                day,
                fill,
                reason,
                pnl,
                pos.strength,
            )
        )
        del self.open[pos.ticker]

    def _mark(self, i: int) -> float:
        value = self.cash
        for pos in self.open.values():
            c = float(self.p.close[i, self.col[pos.ticker]])
            value += pos.qty * (c if math.isfinite(c) else pos.entry_price)
        return value

    def run(self) -> BacktestResult:
        p = self.p
        for i, day in enumerate(p.dates):
            traded: set[str] = set()

            # 1) entries from yesterday's signals at today's open
            for cand, block, qty in self.pending:
                o = float(p.open[i, self.col[cand.ticker]])
                if math.isfinite(o) and o > 0:
                    self._buy(cand, block, qty, o, day)
            self.pending = []

            # 2) morning stop trailing and stop executions
            for pos in list(self.open.values()):
                j = self.col[pos.ticker]
                o, lo = float(p.open[i, j]), float(p.low[i, j])
                if not (math.isfinite(o) and math.isfinite(lo)):
                    continue
                trail = float(p.low_prev[i, j])
                if day > pos.entry_date and math.isfinite(trail):
                    pos.stop = max(pos.stop, round(trail, 2))
                if o <= pos.stop:
                    self._sell(pos, o, day, "stop_gap")
                    traded.add(pos.ticker)
                elif lo <= pos.stop and not (
                    self.cfg.entry_fill == "close" and day == pos.entry_date
                ):
                    self._sell(pos, pos.stop, day, "stop")
                    traded.add(pos.ticker)

            # 3) signals at the close
            self._scan(i, self._mark(i), traded)
            self.equity[day] = self._mark(i)

        last = len(p.dates) - 1
        for pos in list(self.open.values()):
            c = float(p.close[last, self.col[pos.ticker]])
            self._sell(pos, c if math.isfinite(c) else pos.entry_price, p.dates[last], "end")
        self.equity[p.dates[last]] = self.cash
        return self._result()

    def _scan(self, i: int, equity: float, traded: set[str]) -> None:
        p = self.p
        candidates = []
        for j in np.flatnonzero(p.entry[i]):
            stop, close = float(p.low_incl[i, j]), float(p.close[i, j])
            if math.isfinite(stop) and math.isfinite(close) and stop < close:
                candidates.append(
                    Candidate(
                        p.tickers[j],
                        close,
                        stop,
                        float(p.strength[i, j]),
                        float(p.volume_ratio[i, j]),
                    )
                )
        if not candidates:
            return
        candidates.sort(key=lambda c: -c.strength)
        active = [
            Position(o.ticker, o.block, PositionStatus.OPEN, o.qty, o.stop, o.entry_price)
            for o in self.open.values()
        ] + [
            Position(c.ticker, b, PositionStatus.PENDING, q, c.stop, c.price)
            for c, b, q in self.pending
        ]
        for block in self.s.blocks:
            if not block.enabled:
                continue
            plan = plan_block_entries(candidates, block, active, traded, self.s.strategy.rank_by)
            for cand in plan.chosen[: self.s.risk.max_new_entries_per_day]:
                qty = position_quantity(equity, 1.0, self.s.risk.position_pct, cand.price)
                if qty < 1:
                    continue
                traded.add(cand.ticker)
                active.append(
                    Position(
                        cand.ticker, block.block, PositionStatus.PENDING, qty, cand.stop, cand.price
                    )
                )
                if self.cfg.entry_fill == "close":
                    self._buy(cand, block.block, qty, cand.price, self.p.dates[i])
                else:
                    self.pending.append((cand, block.block, qty))

    def _result(self) -> BacktestResult:
        index = pd.DatetimeIndex([pd.Timestamp(d) for d in self.equity])
        equity = pd.Series(list(self.equity.values()), index=index, dtype=float)
        metrics = compute_metrics(equity, [t.pnl for t in self.trades])
        per_block = {}
        for block in (Block.ONE, Block.TWO):
            realised: dict[dt.date, float] = dict.fromkeys(self.equity, 0.0)
            for t in self.trades:
                if t.block == block:
                    realised[t.exit_date] = realised.get(t.exit_date, 0.0) + t.pnl
            curve = (
                self.cfg.initial_capital + pd.Series(list(realised.values()), index=index).cumsum()
            )
            per_block[block] = compute_metrics(
                curve, [t.pnl for t in self.trades if t.block == block]
            )
        return BacktestResult(equity, self.trades, metrics, per_block)


def run_backtest(
    prices: Mapping[str, pd.DataFrame], settings: Settings, config: BacktestConfig | None = None
) -> BacktestResult:
    """Simulate the strategy on daily bars.

    Args:
        prices: Daily OHLCV per ticker.
        settings: Live settings (strategy, blocks, risk).
        config: Simulation parameters.

    Returns:
        The backtest result.

    Raises:
        EmptyMarketDataError: If no ticker has enough history.
    """
    return _Simulation(prices, settings, config or BacktestConfig()).run()
