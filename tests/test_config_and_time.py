"""Tests for configuration and the trading calendar."""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from turtlebot.config import Block2Settings, BrokerSettings, ScheduleSettings, Settings
from turtlebot.domain.models import Block
from turtlebot.timeutil import is_trading_day, nyse_holidays, previous_trading_day


def test_defaults_reproduce_legacy_production_parameters() -> None:
    s = Settings(_env_file=None)
    assert (s.strategy.entry_period, s.strategy.exit_period) == (20, 10)
    assert (s.strategy.volume_period, s.strategy.volume_factor) == (20, 1.5)
    assert s.strategy.rank_by == "breakout_strength"
    assert s.risk.position_pct == 0.015
    assert s.risk.max_new_entries_per_day == 10
    assert s.risk.daily_loss_limit_pct == 0.02
    assert s.risk.min_cushion == 0.25
    assert s.block1.max_positions == 20
    assert s.block1.min_strength is None
    assert s.block2.max_positions == 20
    assert s.block2.min_strength == 0.10
    assert s.order_refs == {"TURTLE": Block.ONE, "TURTLE2": Block.TWO}
    assert s.schedule.scan_times == [dt.time(15, 45)]
    assert s.trading_enabled is False


def test_nested_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TURTLE_BLOCK2__ENABLED", "false")
    monkeypatch.setenv("TURTLE_RISK__MAX_NEW_ENTRIES_PER_DAY", "20")
    monkeypatch.setenv("TURTLE_SCHEDULE__SCAN_TIMES", '["15:30", "15:45"]')
    s = Settings(_env_file=None)
    assert s.block2.enabled is False
    assert s.block2.order_ref == "TURTLE2"  # partial override keeps the other defaults
    assert s.risk.max_new_entries_per_day == 20
    assert s.schedule.scan_times == [dt.time(15, 30), dt.time(15, 45)]


def test_live_port_is_refused() -> None:
    with pytest.raises(ValidationError, match="paper port"):
        BrokerSettings(port=7496)


def test_client_id_must_differ_from_dry_run_ids() -> None:
    with pytest.raises(ValidationError):
        BrokerSettings(client_id=21)


def test_invalid_timezone() -> None:
    with pytest.raises(ValidationError):
        ScheduleSettings(timezone="Mars/Olympus")


def test_duplicate_order_refs_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(block2=Block2Settings(order_ref="TURTLE"), _env_file=None)


def test_block_lookup() -> None:
    s = Settings(_env_file=None)
    assert s.block(Block.TWO).order_ref == "TURTLE2"


def test_nyse_holidays_2026() -> None:
    assert nyse_holidays(2026) == {
        dt.date(2026, 1, 1),
        dt.date(2026, 1, 19),
        dt.date(2026, 2, 16),
        dt.date(2026, 4, 3),  # Good Friday
        dt.date(2026, 5, 25),
        dt.date(2026, 6, 19),
        dt.date(2026, 7, 3),  # July 4th is a Saturday
        dt.date(2026, 9, 7),
        dt.date(2026, 11, 26),
        dt.date(2026, 12, 25),
    }


def test_saturday_new_year_is_not_observed() -> None:
    assert dt.date(2021, 12, 31) not in nyse_holidays(2021)
    assert dt.date(2022, 1, 1) not in nyse_holidays(2022)


def test_trading_days() -> None:
    assert is_trading_day(dt.date(2026, 10, 16))  # Friday
    assert not is_trading_day(dt.date(2026, 10, 17))  # Saturday
    assert not is_trading_day(dt.date(2026, 11, 26))  # Thanksgiving
    assert previous_trading_day(dt.date(2026, 10, 19)) == dt.date(2026, 10, 16)
    assert previous_trading_day(dt.date(2026, 11, 27)) == dt.date(2026, 11, 25)


def test_env_example_documents_the_defaults() -> None:
    """.env.example must stay in sync with the code defaults."""
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    example = Settings(_env_file=root / ".env.example")
    assert example.model_dump() == Settings(_env_file=None).model_dump()
