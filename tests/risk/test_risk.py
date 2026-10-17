"""Tests for position sizing and kill switches."""

from __future__ import annotations

import math

import pytest

from turtlebot.config import RiskSettings
from turtlebot.domain.models import AccountSnapshot, Block
from turtlebot.risk.limits import (
    daily_loss_breached,
    entry_allowed,
    fallback_excess_after,
    margin_cushion_ok,
)
from turtlebot.risk.sizing import ibkr_commission, position_quantity

RISK = RiskSettings()


def account(netliq: float = 100_000.0, daily_pnl: float | None = 0.0) -> AccountSnapshot:
    return AccountSnapshot(
        net_liquidation=netliq,
        currency="CHF",
        available_funds=50_000.0,
        daily_pnl=daily_pnl,
        usd_per_base=1.25,
    )


class TestPositionQuantity:
    def test_matches_legacy_formula(self) -> None:
        # legacy: floor(netliq * POSITION_PCT * usd_per_base / price)
        assert position_quantity(100_000, 1.25, 0.015, 28.31) == math.floor(1875 / 28.31)

    def test_budget_below_one_share(self) -> None:
        assert position_quantity(1_000, 1.0, 0.015, 100.0) == 0

    @pytest.mark.parametrize(("netliq", "price"), [(0, 10), (-5, 10), (1e5, 0), (1e5, math.nan)])
    def test_invalid_inputs_give_zero(self, netliq: float, price: float) -> None:
        assert position_quantity(netliq, 1.0, 0.015, price) == 0


@pytest.mark.parametrize(
    ("shares", "price", "expected"),
    [(10, 50.0, 1.0), (1000, 50.0, 5.0), (100, 0.5, 1.0), (10_000, 0.05, 5.0)],
)
def test_commission(shares: float, price: float, expected: float) -> None:
    assert ibkr_commission(shares, price) == pytest.approx(expected)


class TestKillSwitch:
    def test_daily_loss_limit(self) -> None:
        assert daily_loss_breached(-2_000, 100_000, 0.02)
        assert not daily_loss_breached(-1_999, 100_000, 0.02)

    @pytest.mark.parametrize("pnl", [None, math.nan])
    def test_unknown_pnl_never_trips(self, pnl: float | None) -> None:
        assert not daily_loss_breached(pnl, 100_000, 0.02)

    def test_entry_allowed(self) -> None:
        assert entry_allowed(account(), 0, Block.ONE, RISK).allowed

    def test_entry_denied_by_loss(self) -> None:
        decision = entry_allowed(account(daily_pnl=-5_000), 0, Block.ONE, RISK)
        assert not decision.allowed
        assert decision.reason is not None
        assert "daily P&L" in decision.reason

    def test_entry_denied_by_daily_limit(self) -> None:
        decision = entry_allowed(account(), 10, Block.TWO, RISK)
        assert not decision.allowed
        assert decision.reason is not None
        assert "block 2" in decision.reason


class TestMargin:
    def test_cushion(self) -> None:
        assert margin_cushion_ok(25_000, 100_000, 0.25)
        assert not margin_cushion_ok(24_999, 100_000, 0.25)

    def test_fallback_excess(self) -> None:
        assert fallback_excess_after(50_000, 100, 25.0, 1.25) == pytest.approx(48_000)
        assert fallback_excess_after(None, 1, 10.0, 1.0) == pytest.approx(-10.0)
