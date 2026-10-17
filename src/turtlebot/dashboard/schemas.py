"""Response models of the dashboard API (the contract between backend and frontend)."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, Field


class ReconciliationSummary(BaseModel):
    """Latest reconciliation run."""

    ts: dt.datetime
    outcome: str
    discrepancies: int
    critical: int
    message: str | None


class StopAlert(BaseModel):
    """A stop order problem that needs the user's attention."""

    kind: Literal["foreign_client_order", "stop_price_mismatch", "stop_replace_failed"]
    symbol: str | None
    order_id: int | None
    client_id: int | None
    message: str
    ts: dt.datetime


class BotStatus(BaseModel):
    """Liveness of the trading engine."""

    mode: str | None = Field(description="'paper', 'dry-run' or null if the engine never ran")
    heartbeat: dt.datetime | None
    running: bool = Field(description="Heartbeat younger than a few loop intervals")
    trading_enabled: bool
    demo: bool
    last_reconciliation: ReconciliationSummary | None
    alerts: list[StopAlert] = Field(default_factory=list)


class AccountView(BaseModel):
    """Latest account snapshot."""

    ts: dt.datetime
    net_liquidation: float
    currency: str
    net_liquidation_usd: float
    daily_pnl: float | None


class TradeStatsView(BaseModel):
    """Closed-trade statistics."""

    trades: int
    win_rate: float | None
    profit_factor: float | None
    avg_win: float | None
    avg_loss: float | None
    expectancy: float | None


class MetricsView(BaseModel):
    """Risk and performance metrics."""

    total_return: float | None
    cagr: float | None
    sharpe: float | None
    sortino: float | None
    max_drawdown: float
    current_drawdown: float
    volatility: float | None
    trades: TradeStatsView
    exposure_usd: float
    open_risk_usd: float = Field(description="Sum of (price - stop) x qty over open positions")


class BlockView(BaseModel):
    """P&L breakdown of one strategy block."""

    block: int
    label: str
    enabled: bool
    min_strength: float | None
    open_positions: int
    pending_positions: int
    max_positions: int
    market_value: float
    unrealized_pnl: float
    realized_pnl: float
    closed_trades: int
    win_rate: float | None


class PositionView(BaseModel):
    """One active position, valued with the latest quote."""

    symbol: str
    block: int
    status: str
    qty: float
    entry_price: float | None
    entry_date: dt.date | None
    last_price: float | None
    quote_stale: bool
    stop: float
    market_value: float | None
    unrealized_pnl: float | None
    unrealized_pct: float | None
    stop_distance_pct: float | None = Field(description="(price - stop) / price")
    risk_to_stop: float | None
    days_held: int | None
    missing_cycles: int
    stop_confirmed: bool
    stop_alert: str | None = Field(None, description="Foreign stop owner or price mismatch")


class TradeView(BaseModel):
    """A closed trade."""

    symbol: str
    block: int
    qty: float
    entry_date: dt.date | None
    entry_price: float | None
    exit_price: float | None
    closed_at: dt.datetime | None
    pnl: float | None
    return_pct: float | None
    reason: str | None


class EquityPoint(BaseModel):
    """One point of the equity curve."""

    ts: dt.datetime
    equity: float
    drawdown: float


class EquityView(BaseModel):
    """Equity curve and its source."""

    source: Literal["account", "realized", "none"]
    currency: str
    drawdown_unit: Literal["fraction", "currency"]
    points: list[EquityPoint]


class EventView(BaseModel):
    """Journal entry."""

    id: int
    ts: dt.datetime
    kind: str
    symbol: str | None
    block: int | None
    qty: float | None
    price: float | None
    stop: float | None
    details: dict[str, object]


class SignalView(BaseModel):
    """A scan signal."""

    symbol: str
    scan_date: dt.date
    price: float
    stop: float
    strength: float
    volume_ratio: float


class Overview(BaseModel):
    """Everything the header and KPI strip need."""

    generated_at: dt.datetime
    status: BotStatus
    account: AccountView | None
    metrics: MetricsView
    blocks: list[BlockView]


class Snapshot(BaseModel):
    """Payload pushed over the WebSocket."""

    type: Literal["snapshot"] = "snapshot"
    data_version: int
    overview: Overview
    positions: list[PositionView]
    events: list[EventView]


class ChatRequest(BaseModel):
    """Chat question from the UI."""

    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = Field(None, max_length=100, pattern=r"^[A-Za-z0-9-]+$")


class ChatResponse(BaseModel):
    """Chat answer."""

    answer: str
    session_id: str | None
