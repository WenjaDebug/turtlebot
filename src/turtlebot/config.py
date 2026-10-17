"""Typed configuration loaded from environment variables and ``.env``.

All tunables of the bot live here; no other module hard-codes thresholds, slot counts or
capital allocation. Values are read from environment variables prefixed with ``TURTLE_``;
nested groups use a double underscore, e.g. ``TURTLE_RISK__POSITION_PCT=0.015``.

The defaults reproduce the production parameters of the legacy bot exactly
(``legacy/live_trade_turtle.py``), so switching to the new engine does not change the strategy.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from turtlebot.domain.models import Block


class RankBy(StrEnum):
    """Column used to rank candidates when there are more signals than free slots."""

    BREAKOUT_STRENGTH = "breakout_strength"
    VOLUME_RATIO = "volume_ratio"


class StrategySettings(BaseModel):
    """Donchian/Turtle breakout parameters (identical for both blocks)."""

    entry_period: int = Field(20, ge=2, description="Donchian high lookback for entries (days).")
    exit_period: int = Field(10, ge=2, description="Donchian low lookback for the trailing stop.")
    volume_period: int = Field(20, ge=2, description="Lookback of the average volume.")
    volume_factor: float = Field(1.5, gt=0, description="Required volume / average volume.")
    rank_by: RankBy = RankBy.BREAKOUT_STRENGTH
    history_days: int = Field(75, ge=30, description="Calendar days of history per scan.")
    intraday_entries: bool = Field(
        False,
        description="Legacy TEST_MODE: buy intraday with a DAY market order instead of MOC.",
    )


class BlockSettings(BaseModel):
    """One independent slot pool fed from the same scan."""

    block: Block
    enabled: bool = True
    max_positions: int = Field(20, ge=0)
    min_strength: float | None = Field(
        None, description="Minimum breakout strength (close / donchian high - 1); None = no filter."
    )
    order_ref: str = Field(min_length=1, max_length=20)


class Block1Settings(BlockSettings):
    """Block 1: the base strategy without additional filter."""

    block: Block = Block.ONE
    order_ref: str = Field("TURTLE", min_length=1, max_length=20)


class Block2Settings(BlockSettings):
    """Block 2: same strategy, only signals with breakout strength >= 10 %."""

    block: Block = Block.TWO
    min_strength: float | None = 0.10
    order_ref: str = Field("TURTLE2", min_length=1, max_length=20)


class RiskSettings(BaseModel):
    """Position sizing, universe filters and kill switches."""

    position_pct: float = Field(0.015, gt=0, le=1, description="Share of net liquidation.")
    min_cushion: float = Field(0.25, ge=0, le=1, description="Excess liquidity after the order.")
    max_new_entries_per_day: int = Field(10, ge=0, description="Per block and day.")
    daily_loss_limit_pct: float = Field(0.02, gt=0, le=1)
    min_market_cap: float = Field(500e6, ge=0)
    min_dollar_volume: float = Field(5e6, ge=0, description="20d average of close x volume.")
    min_price: float = Field(1.0, ge=0)


class ScheduleSettings(BaseModel):
    """Wall-clock schedule in exchange time."""

    timezone: str = "America/New_York"
    universe_time: dt.time = dt.time(8, 0)
    reconcile_time: dt.time = dt.time(9, 0)
    scan_times: list[dt.time] = Field(default_factory=lambda: [dt.time(15, 45)])
    scan_grace_min: int = Field(360, ge=0, description="Catch-up window for a missed scan.")
    session_open: dt.time = dt.time(9, 30)
    session_close: dt.time = dt.time(16, 0)
    moc_cutoff: dt.time = dt.time(15, 50)
    job_timeout_min: int = Field(45, ge=1)
    job_retry_min: int = Field(10, ge=1)
    job_max_attempts: int = Field(3, ge=1)
    loop_interval_s: float = Field(30.0, gt=0)
    fill_wait_s: float = Field(30.0, ge=0)
    account_snapshot_min: float = Field(15.0, gt=0, description="Equity curve sampling.")

    @property
    def tz(self) -> ZoneInfo:
        """Exchange timezone as a :class:`~zoneinfo.ZoneInfo`."""
        return ZoneInfo(self.timezone)

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (KeyError, ValueError) as exc:  # ZoneInfoNotFoundError is a KeyError
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value


class BrokerSettings(BaseModel):
    """Connection to TWS / IB Gateway."""

    host: str = "127.0.0.1"
    port: int = Field(7497, description="7497 = TWS paper. Other ports are refused.")
    client_id: int = 11
    dry_run_client_ids: list[int] = Field(default_factory=lambda: list(range(21, 30)))
    connect_timeout_s: float = Field(20.0, gt=0)
    paper_port: int = 7497
    paper_account_prefix: str = "D"
    position_sync_timeout_s: float = Field(10.0, gt=0)
    reconnect_initial_delay_s: float = Field(5.0, gt=0)
    reconnect_max_delay_s: float = Field(300.0, gt=0)
    reconnect_backoff: float = Field(2.0, ge=1)
    disconnect_alert_after_min: float = Field(5.0, ge=0)
    disconnect_alert_repeat_min: float = Field(30.0, gt=0)

    @model_validator(mode="after")
    def _paper_only(self) -> BrokerSettings:
        if self.port != self.paper_port:
            msg = f"Only the paper port {self.paper_port} is allowed, got {self.port}"
            raise ValueError(msg)
        if self.client_id in self.dry_run_client_ids:
            raise ValueError("client_id must not be one of the dry-run client ids")
        return self


class ReconciliationSettings(BaseModel):
    """Safety thresholds for comparing the local state with the broker.

    These guard against the 12.10.2026 incident, where an incomplete broker snapshot made the
    bot believe all positions were closed and cancel their stop-loss orders.
    """

    close_confirmation_cycles: int = Field(
        2,
        ge=1,
        description="Consecutive reconciliations a position must be missing before it is "
        "considered closed (unless a SELL execution proves the exit).",
    )
    mass_disappearance_fraction: float = Field(
        0.25,
        gt=0,
        le=1,
        description="If at least this share of tracked positions vanishes at once without "
        "executions, the snapshot is treated as suspect and nothing is closed or cancelled.",
    )
    mass_disappearance_min_count: int = Field(2, ge=1)
    orphan_confirmation_cycles: int = Field(2, ge=1)


class PathSettings(BaseModel):
    """Filesystem locations (relative paths are resolved against the working directory)."""

    data_dir: Path = Path("var")
    cache_dir: Path = Path(".cache")

    @property
    def db_path(self) -> Path:
        """SQLite state store."""
        return self.data_dir / "turtlebot.db"

    @property
    def log_dir(self) -> Path:
        """Directory for structured log files."""
        return self.data_dir / "log"

    @property
    def lock_dir(self) -> Path:
        """Per-client-id lock files."""
        return self.data_dir / "locks"


class DashboardSettings(BaseModel):
    """FastAPI dashboard server."""

    host: str = "127.0.0.1"
    port: int = Field(8000, ge=1, le=65535)
    cors_origins: list[str] = Field(default_factory=list)
    push_interval_s: float = Field(1.0, gt=0, description="DB change polling for the WebSocket.")
    price_refresh_s: float = Field(60.0, gt=0, description="Quote cache TTL.")
    chat_enabled: bool = True
    chat_timeout_s: float = Field(180.0, gt=0)
    chat_command: str = "claude"


class LogSettings(BaseModel):
    """Logging configuration."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    json_console: bool = False
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 10


class Settings(BaseSettings):
    """Root settings object.

    Attributes:
        trading_enabled: Master switch. Unless true, ``turtlebot run`` refuses to start without
            ``--dry-run``. Keep it false while the bot is paused.
        anthropic_api_key: Optional; only needed if the chat should use the API instead of the
            local Claude Code CLI. Never logged.
    """

    model_config = SettingsConfigDict(
        env_prefix="TURTLE_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    trading_enabled: bool = False
    strategy: StrategySettings = Field(default_factory=StrategySettings)
    block1: Block1Settings = Field(default_factory=Block1Settings)
    block2: Block2Settings = Field(default_factory=Block2Settings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    broker: BrokerSettings = Field(default_factory=BrokerSettings)
    reconciliation: ReconciliationSettings = Field(default_factory=ReconciliationSettings)
    paths: PathSettings = Field(default_factory=PathSettings)
    dashboard: DashboardSettings = Field(default_factory=DashboardSettings)
    log: LogSettings = Field(default_factory=LogSettings)
    anthropic_api_key: SecretStr | None = None

    @model_validator(mode="after")
    def _unique_blocks(self) -> Settings:
        if self.block1.block == self.block2.block or self.block1.order_ref == self.block2.order_ref:
            raise ValueError("block ids and order refs must be unique")
        return self

    @property
    def blocks(self) -> list[BlockSettings]:
        """Both blocks in allocation order (block 1 chooses first)."""
        return [self.block1, self.block2]

    def block(self, block: Block) -> BlockSettings:
        """Settings of one block.

        Raises:
            KeyError: If the block is not configured.
        """
        for cfg in self.blocks:
            if cfg.block == block:
                return cfg
        raise KeyError(block)

    @property
    def order_refs(self) -> dict[str, Block]:
        """Mapping order reference -> block, used to recognise the bot's own orders."""
        return {b.order_ref: b.block for b in self.blocks}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (cached)."""
    return Settings()
