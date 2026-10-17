"""Kill switches and margin checks.

All checks are pure functions over numbers, so every branch is unit-tested; the engine only
gathers the inputs from the broker.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from turtlebot.config import RiskSettings
from turtlebot.domain.models import AccountSnapshot, Block

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """Result of a risk check."""

    allowed: bool
    reason: str | None = None

    @classmethod
    def ok(cls) -> RiskDecision:
        """Allowed."""
        return cls(True)

    @classmethod
    def deny(cls, reason: str) -> RiskDecision:
        """Denied with a reason."""
        return cls(False, reason)


def daily_loss_breached(daily_pnl: float | None, net_liquidation: float, limit_pct: float) -> bool:
    """Whether today's loss reached ``limit_pct`` of the account (unknown P&L never trips)."""
    if daily_pnl is None or math.isnan(daily_pnl) or net_liquidation <= 0:
        return False
    return daily_pnl <= -limit_pct * net_liquidation


def entry_allowed(
    account: AccountSnapshot, entries_today: int, block: Block, risk: RiskSettings
) -> RiskDecision:
    """Kill switch: daily loss limit (whole account) and daily entry limit (per block)."""
    if daily_loss_breached(account.daily_pnl, account.net_liquidation, risk.daily_loss_limit_pct):
        return RiskDecision.deny(
            f"daily P&L {account.daily_pnl:,.0f} <= -{risk.daily_loss_limit_pct:.0%} of account"
        )
    if entries_today >= risk.max_new_entries_per_day:
        return RiskDecision.deny(
            f"block {int(block)}: daily limit of {risk.max_new_entries_per_day} entries reached"
        )
    return RiskDecision.ok()


def fallback_excess_after(
    available_funds: float | None, qty: float, price: float, usd_per_base: float
) -> float:
    """Approximate excess liquidity after a buy when TWS' what-if check is unavailable."""
    return (available_funds or 0.0) - qty * price / usd_per_base


def margin_cushion_ok(excess_after: float, net_liquidation: float, min_cushion: float) -> bool:
    """Whether at least ``min_cushion`` of the account remains as excess liquidity."""
    return excess_after >= min_cushion * net_liquidation
