"""Position sizing and transaction costs."""

from __future__ import annotations

import math


def position_quantity(
    net_liquidation: float, usd_per_base: float, position_pct: float, price: float
) -> int:
    """Whole shares for a new position worth ``position_pct`` of the account.

    Args:
        net_liquidation: Account value in base currency.
        usd_per_base: Conversion factor base currency -> USD.
        position_pct: Target share of the account per position (e.g. ``0.015``).
        price: Share price in USD.

    Returns:
        Number of shares (``0`` if the budget is below one share or inputs are invalid).
    """
    if price <= 0 or not math.isfinite(price) or net_liquidation <= 0:
        return 0
    budget_usd = net_liquidation * position_pct * usd_per_base
    return max(math.floor(budget_usd / price), 0)


def ibkr_commission(shares: float, price: float) -> float:
    """IBKR Fixed pricing for US stocks: 0.005 USD/share, min 1 USD, max 1 % of trade value."""
    return max(1.0, min(0.005 * shares, 0.01 * shares * price))
