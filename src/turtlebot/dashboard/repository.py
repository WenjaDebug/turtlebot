"""Read model of the dashboard: turns the state store into API view models."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import pandas as pd

from turtlebot.analytics.metrics import compute_metrics, drawdown_series
from turtlebot.config import Settings
from turtlebot.dashboard.quotes import QuoteService
from turtlebot.dashboard.schemas import (
    AccountView,
    BlockView,
    BotStatus,
    EquityPoint,
    EquityView,
    EventView,
    MetricsView,
    Overview,
    PositionView,
    ReconciliationSummary,
    SignalView,
    Snapshot,
    TradeStatsView,
    TradeView,
)
from turtlebot.domain.models import Position, PositionStatus
from turtlebot.engine.engine import HEARTBEAT_KEY, MODE_KEY
from turtlebot.persistence.store import StateStore
from turtlebot.timeutil import Clock

BLOCK_LABELS = {1: "Core breakout", 2: "High-conviction filter"}
RUNNING_AFTER_LOOPS = 4  # heartbeat older than this many loop intervals => not running


def _trade_pnl(p: Position) -> float | None:
    if p.entry_price is None or p.exit_price is None:
        return None
    return (p.exit_price - p.entry_price) * p.qty


class DashboardRepository:
    """Builds view models from the state store and live quotes.

    Args:
        store: State store (the dashboard only reads from it).
        quotes: Price source for open positions.
        settings: Application settings.
        clock: Time source.
        demo: Whether the data is synthetic.
    """

    def __init__(
        self,
        store: StateStore,
        quotes: QuoteService,
        settings: Settings,
        clock: Clock,
        *,
        demo: bool = False,
    ) -> None:
        self.store = store
        self.quotes = quotes
        self.settings = settings
        self.clock = clock
        self.demo = demo

    # ------------------------------------------------------------------ positions
    def positions(self) -> list[PositionView]:
        """Active positions valued at the latest quote (entry price as fallback)."""
        active = self.store.active_positions()
        prices = self.quotes.quotes([p.symbol for p in active]) if active else {}
        today = self.clock().date()
        rows = []
        for p in active:
            quote = prices.get(p.symbol)
            price = quote if quote is not None else p.entry_price
            is_open = p.status is PositionStatus.OPEN
            value = price * p.qty if price is not None and is_open else None
            pnl = (
                (price - p.entry_price) * p.qty
                if price is not None and p.entry_price is not None and is_open
                else None
            )
            rows.append(
                PositionView(
                    symbol=p.symbol,
                    block=int(p.block),
                    status=p.status.value,
                    qty=p.qty,
                    entry_price=p.entry_price,
                    entry_date=p.entry_date,
                    last_price=price,
                    quote_stale=quote is None,
                    stop=p.stop,
                    market_value=value,
                    unrealized_pnl=pnl,
                    unrealized_pct=(
                        price / p.entry_price - 1
                        if price is not None and p.entry_price and is_open
                        else None
                    ),
                    stop_distance_pct=(price - p.stop) / price if price else None,
                    risk_to_stop=(
                        (price - p.stop) * p.qty if price is not None and is_open else None
                    ),
                    days_held=(today - p.entry_date).days if p.entry_date else None,
                    missing_cycles=p.missing_cycles,
                )
            )
        rows.sort(key=lambda r: (r.block, -(r.unrealized_pct or 0.0)))
        return rows

    # ------------------------------------------------------------------ trades & equity
    def trades(self, limit: int = 200) -> list[TradeView]:
        """Closed trades, newest first."""
        result = []
        for p in self.store.closed_positions(limit=limit):
            if p.entry_price is None:
                continue  # cancelled entries are not trades
            pnl = _trade_pnl(p)
            result.append(
                TradeView(
                    symbol=p.symbol,
                    block=int(p.block),
                    qty=p.qty,
                    entry_date=p.entry_date,
                    entry_price=p.entry_price,
                    exit_price=p.exit_price,
                    closed_at=p.closed_at,
                    pnl=pnl,
                    return_pct=(p.exit_price / p.entry_price - 1) if p.exit_price else None,
                    reason=p.close_reason,
                )
            )
        return result

    def equity_series(self) -> tuple[pd.Series, str, str]:
        """Equity curve, its source and currency.

        Prefers account snapshots (net liquidation); without them the curve is the cumulative
        realised P&L of closed trades.
        """
        history = self.store.account_history()
        if history:
            index = pd.DatetimeIndex([ts for ts, _ in history])
            values = [a.net_liquidation for _, a in history]
            return pd.Series(values, index=index, dtype=float), "account", history[-1][1].currency
        closed = [
            (p.closed_at, pnl)
            for p in reversed(self.store.closed_positions())
            if p.closed_at is not None and (pnl := _trade_pnl(p)) is not None
        ]
        if not closed:
            return pd.Series(dtype=float), "none", "USD"
        index = pd.DatetimeIndex([ts for ts, _ in closed])
        return pd.Series([v for _, v in closed], index=index).cumsum(), "realized", "USD"

    def equity(self) -> EquityView:
        """Equity curve with drawdown."""
        series, source, currency = self.equity_series()
        # Net liquidation has a level -> relative drawdown; a P&L curve does not -> absolute.
        relative = source == "account"
        dd = drawdown_series(series) if relative else self._pnl_drawdown(series)
        return EquityView(
            source=source,  # type: ignore[arg-type]
            currency=currency,
            drawdown_unit="fraction" if relative else "currency",
            points=[
                EquityPoint(ts=ts.to_pydatetime(), equity=float(v), drawdown=float(d))
                for ts, v, d in zip(series.index, series.to_numpy(), dd.to_numpy(), strict=True)
            ],
        )

    @staticmethod
    def _pnl_drawdown(pnl: pd.Series) -> pd.Series:
        """Drawdown of a P&L curve in currency units relative to its running peak."""
        return pnl - pnl.cummax().clip(lower=0.0)

    # ------------------------------------------------------------------ overview
    def _status(self) -> BotStatus:
        heartbeat_raw = self.store.get_meta(HEARTBEAT_KEY)
        heartbeat = dt.datetime.fromisoformat(heartbeat_raw) if heartbeat_raw else None
        max_age = dt.timedelta(seconds=self.settings.schedule.loop_interval_s * RUNNING_AFTER_LOOPS)
        runs = self.store.reconciliations(limit=1)
        last = None
        if runs:
            r = runs[0]
            last = ReconciliationSummary(
                ts=r.ts,
                outcome=r.outcome,
                discrepancies=len(r.discrepancies),
                critical=sum(d.severity == "critical" for d in r.discrepancies),
                message=r.message,
            )
        return BotStatus(
            mode=self.store.get_meta(MODE_KEY),
            heartbeat=heartbeat,
            running=heartbeat is not None and self.clock() - heartbeat < max_age,
            trading_enabled=self.settings.trading_enabled,
            demo=self.demo,
            last_reconciliation=last,
        )

    def _account(self) -> AccountView | None:
        history = self.store.account_history()
        if not history:
            return None
        ts, a = history[-1]
        return AccountView(
            ts=ts,
            net_liquidation=a.net_liquidation,
            currency=a.currency,
            net_liquidation_usd=a.net_liquidation_usd,
            daily_pnl=a.daily_pnl,
        )

    def _blocks(
        self, positions: Sequence[PositionView], trades: Sequence[TradeView]
    ) -> list[BlockView]:
        result = []
        for cfg in self.settings.blocks:
            b = int(cfg.block)
            mine = [p for p in positions if p.block == b]
            closed = [t for t in trades if t.block == b and t.pnl is not None]
            wins = sum(1 for t in closed if (t.pnl or 0) > 0)
            result.append(
                BlockView(
                    block=b,
                    label=BLOCK_LABELS.get(b, f"Block {b}"),
                    enabled=cfg.enabled,
                    min_strength=cfg.min_strength,
                    open_positions=sum(p.status == "open" for p in mine),
                    pending_positions=sum(p.status == "pending" for p in mine),
                    max_positions=cfg.max_positions,
                    market_value=sum(p.market_value or 0.0 for p in mine),
                    unrealized_pnl=sum(p.unrealized_pnl or 0.0 for p in mine),
                    realized_pnl=sum(t.pnl or 0.0 for t in closed),
                    closed_trades=len(closed),
                    win_rate=wins / len(closed) if closed else None,
                )
            )
        return result

    def _metrics(
        self, positions: Sequence[PositionView], trades: Sequence[TradeView]
    ) -> MetricsView:
        series, source, _ = self.equity_series()
        pnls = [t.pnl for t in trades if t.pnl is not None]
        if source == "account" and len(series) >= 2:
            m = compute_metrics(series, pnls)
            current_dd = float(drawdown_series(series).iloc[-1])
            total, cagr_, sharpe, sortino, mdd, vol = (
                m.total_return,
                m.cagr,
                m.sharpe,
                m.sortino,
                m.max_drawdown,
                m.volatility,
            )
            stats = m.trades
        else:
            m = compute_metrics(pd.Series(dtype=float), pnls)
            stats = m.trades
            total = cagr_ = sharpe = sortino = vol = None
            mdd = current_dd = 0.0
        return MetricsView(
            total_return=total,
            cagr=cagr_,
            sharpe=sharpe,
            sortino=sortino,
            max_drawdown=mdd,
            current_drawdown=current_dd,
            volatility=vol,
            trades=TradeStatsView(
                trades=stats.trades,
                win_rate=stats.win_rate,
                profit_factor=stats.profit_factor,
                avg_win=stats.avg_win,
                avg_loss=stats.avg_loss,
                expectancy=stats.expectancy,
            ),
            exposure_usd=sum(p.market_value or 0.0 for p in positions),
            open_risk_usd=sum(max(p.risk_to_stop or 0.0, 0.0) for p in positions),
        )

    def overview(self, positions: Sequence[PositionView] | None = None) -> Overview:
        """Header, KPI and block data."""
        positions = self.positions() if positions is None else positions
        trades = self.trades(limit=10_000)
        return Overview(
            generated_at=self.clock(),
            status=self._status(),
            account=self._account(),
            metrics=self._metrics(positions, trades),
            blocks=self._blocks(positions, trades),
        )

    # ------------------------------------------------------------------ journal
    def events(self, limit: int = 50, after_id: int = 0) -> list[EventView]:
        """Journal entries without the (numerous) raw signals."""
        kinds = [
            "ORDER",
            "FILL",
            "PARTIAL_FILL",
            "EXIT",
            "PARTIAL_EXIT",
            "CANCEL",
            "SKIP",
            "STOP_NEW",
            "STOP_UPDATE",
            "STOP_LOST",
            "ADOPT",
            "UNIVERSE",
        ]
        return [
            EventView(
                id=e.id,
                ts=e.ts,
                kind=e.kind,
                symbol=e.symbol,
                block=e.block,
                qty=e.qty,
                price=e.price,
                stop=e.stop,
                details=e.details,
            )
            for e in self.store.events(limit=limit, kinds=kinds, after_id=after_id)
        ]

    def signals(self) -> list[SignalView]:
        """Signals of the latest scan."""
        return [
            SignalView(
                symbol=s["symbol"],
                scan_date=dt.date.fromisoformat(s["scan_date"]),
                price=s["price"],
                stop=s["stop"],
                strength=s["strength"],
                volume_ratio=s["volume_ratio"],
            )
            for s in self.store.signals()
        ]

    def snapshot(self) -> Snapshot:
        """Everything pushed over the WebSocket."""
        positions = self.positions()
        return Snapshot(
            data_version=self.store.data_version(),
            overview=self.overview(positions),
            positions=positions,
            events=self.events(limit=40),
        )

    def chat_context(self) -> dict[str, object]:
        """Compact state summary for the chat assistant."""
        snap = self.snapshot()
        return {
            "overview": snap.overview.model_dump(mode="json"),
            "positions": [p.model_dump(mode="json") for p in snap.positions],
            "recent_events": [e.model_dump(mode="json") for e in snap.events[:15]],
            "latest_signals": [s.model_dump(mode="json") for s in self.signals()[:15]],
        }
