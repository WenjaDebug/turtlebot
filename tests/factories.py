"""Test data builders shared by the test-suite."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from zoneinfo import ZoneInfo

import numpy as np
import numpy.typing as npt
import pandas as pd

from turtlebot.domain.models import (
    Block,
    BrokerOrder,
    BrokerPosition,
    BrokerSnapshot,
    OrderAction,
    Position,
    PositionStatus,
)

NY = ZoneInfo("America/New_York")


def ny(
    year: int = 2026, month: int = 10, day: int = 14, hour: int = 15, minute: int = 45
) -> dt.datetime:
    """Timezone-aware timestamp in New York."""
    return dt.datetime(year, month, day, hour, minute, tzinfo=NY)


def ohlcv(
    closes: npt.ArrayLike,
    *,
    volumes: npt.ArrayLike | None = None,
    end: str = "2026-10-14",
    spread: float = 0.5,
) -> pd.DataFrame:
    """Daily OHLCV frame ending on ``end`` (business days) built from closing prices."""
    closes_arr = np.asarray(closes, dtype=float)
    index = pd.bdate_range(end=end, periods=len(closes_arr))
    vols = np.asarray(volumes if volumes is not None else [1_000_000] * len(closes_arr), float)
    return pd.DataFrame(
        {
            "Open": closes_arr,
            "High": closes_arr + spread,
            "Low": closes_arr - spread,
            "Close": closes_arr,
            "Volume": vols,
        },
        index=index,
    )


def breakout_frame(
    *,
    n: int = 40,
    base: float = 50.0,
    jump: float = 5.0,
    vol_mult: float = 3.0,
    end: str = "2026-10-14",
) -> pd.DataFrame:
    """Flat series followed by a breakout bar with elevated volume on the last day."""
    closes = [base] * (n - 1) + [base + jump]
    volumes = [1_000_000.0] * (n - 1) + [1_000_000.0 * vol_mult]
    return ohlcv(closes, volumes=volumes, end=end)


def position(
    symbol: str,
    *,
    qty: float = 100,
    stop: float = 90.0,
    status: PositionStatus = PositionStatus.OPEN,
    block: Block = Block.ONE,
    entry_price: float | None = 100.0,
    stop_order_id: int | None = None,
    parent_order_id: int | None = None,
    missing_cycles: int = 0,
    pid: int | None = None,
) -> Position:
    """Local position."""
    return Position(
        symbol=symbol,
        block=block,
        status=status,
        qty=qty,
        stop=stop,
        signal_price=entry_price or 100.0,
        entry_price=entry_price if status is PositionStatus.OPEN else None,
        entry_date=dt.date(2026, 10, 12) if status is PositionStatus.OPEN else None,
        parent_order_id=parent_order_id,
        stop_order_id=stop_order_id,
        missing_cycles=missing_cycles,
        id=pid,
    )


def stop_order(
    symbol: str, order_id: int, *, qty: float = 100, stop: float = 90.0, ref: str = "TURTLE"
) -> BrokerOrder:
    """Working protective stop."""
    return BrokerOrder(
        order_id=order_id,
        symbol=symbol,
        action=OrderAction.SELL,
        order_type="STP",
        qty=qty,
        order_ref=ref,
        aux_price=stop,
    )


def entry_order(
    symbol: str, order_id: int, *, qty: float = 100, ref: str = "TURTLE", filled: float = 0
) -> BrokerOrder:
    """Working entry order."""
    return BrokerOrder(
        order_id=order_id,
        symbol=symbol,
        action=OrderAction.BUY,
        order_type="MOC",
        qty=qty,
        order_ref=ref,
        filled=filled,
    )


def snapshot(
    positions: dict[str, float] | None = None,
    orders: Sequence[BrokerOrder] = (),
    *,
    synced: bool = True,
    avg_cost: float = 100.0,
) -> BrokerSnapshot:
    """Broker snapshot from ``{symbol: qty}``."""
    return BrokerSnapshot(
        positions={
            s: BrokerPosition(symbol=s, qty=q, avg_cost=avg_cost)
            for s, q in (positions or {}).items()
        },
        orders=list(orders),
        positions_synced=synced,
        taken_at=ny(),
    )
