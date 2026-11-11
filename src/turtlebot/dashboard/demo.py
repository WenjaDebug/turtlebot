"""Synthetic demo data, so the dashboard can be explored without an account.

All numbers are generated from a seeded random walk; they are not real trading results.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd

from turtlebot.config import PhaseSettings
from turtlebot.domain.models import AccountSnapshot, Block, Candidate, Position, PositionStatus
from turtlebot.persistence.store import DiscrepancyRecord, StateStore
from turtlebot.timeutil import is_trading_day

NY_OFFSET = dt.timezone(dt.timedelta(hours=-4))
SYMBOLS = (
    "ARCX",
    "BLNK",
    "CRWL",
    "DYNA",
    "ELMO",
    "FJRD",
    "GLDN",
    "HRBR",
    "IONQX",
    "JUNO",
    "KLYP",
    "LUMN",
    "MRSH",
    "NOVA",
    "ORBT",
    "PYLN",
    "QRTZ",
    "RIVN",
    "SOLR",
    "TIDE",
    "UMBR",
    "VRTX",
    "WAVE",
    "XNTR",
    "YARD",
    "ZEPH",
)


def seed_demo_store(path: Path, *, seed: int = 7, today: dt.date | None = None) -> dict[str, float]:
    """(Re)create a demo database and return demo quotes for its open positions.

    Args:
        path: Database file; an existing file is replaced.
        seed: Random seed (deterministic output).
        today: Last day of the generated history.

    Returns:
        Mapping symbol -> current demo price.
    """
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    rng = np.random.default_rng(seed)
    today = today or dt.date.today()  # noqa: DTZ011 - demo only
    days = [today - dt.timedelta(days=i) for i in range(140, -1, -1)]
    days = [d for d in days if is_trading_day(d)]

    store = StateStore(path)
    equity = 100_000.0
    with store.transaction():
        for d in days:
            equity *= 1 + rng.normal(0.0009, 0.009)
            ts = dt.datetime.combine(d, dt.time(16, 5), tzinfo=NY_OFFSET)
            daily = equity * rng.normal(0.0009, 0.009)
            store.record_account(AccountSnapshot(equity, "USD", equity * 0.6, daily, 1.0), ts)

        quotes: dict[str, float] = {}
        symbols = list(SYMBOLS)
        rng.shuffle(symbols)
        for i, symbol in enumerate(symbols):
            block = Block.ONE if i % 3 else Block.TWO
            entry = float(rng.uniform(12, 240))
            entry_day = days[-int(rng.integers(2, 60))]
            qty = float(int(1_500 / entry))
            if i < 17:  # open positions
                last = entry * (1 + rng.normal(0.03, 0.09))
                stop = round(min(entry, last) * rng.uniform(0.84, 0.95), 2)
                store.insert_position(
                    Position(
                        symbol,
                        block,
                        PositionStatus.OPEN,
                        qty,
                        stop,
                        entry,
                        entry_price=round(entry, 2),
                        entry_date=entry_day,
                    )
                )
                quotes[symbol] = round(last, 2)
            else:  # closed trades
                exit_price = entry * (1 + rng.normal(0.02, 0.12))
                closed = dt.datetime.combine(
                    min(entry_day + dt.timedelta(days=int(rng.integers(3, 25))), today),
                    dt.time(10, 30),
                    tzinfo=NY_OFFSET,
                )
                store.insert_position(
                    Position(
                        symbol,
                        block,
                        PositionStatus.CLOSED,
                        qty,
                        round(exit_price, 2),
                        entry,
                        entry_price=round(entry, 2),
                        entry_date=entry_day,
                        exit_price=round(exit_price, 2),
                        closed_at=closed,
                        close_reason="STP filled",
                    )
                )
        # a few historic trades to make the statistics meaningful
        for k in range(24):
            entry = float(rng.uniform(20, 150))
            exit_price = entry * (1 + rng.normal(0.025, 0.11))
            d = days[int(rng.integers(5, len(days) - 5))]
            store.insert_position(
                Position(
                    f"H{k:02d}",
                    Block.ONE if k % 3 else Block.TWO,
                    PositionStatus.CLOSED,
                    float(int(1_500 / entry)),
                    round(exit_price, 2),
                    entry,
                    entry_price=round(entry, 2),
                    entry_date=d - dt.timedelta(days=12),
                    exit_price=round(exit_price, 2),
                    closed_at=dt.datetime.combine(d, dt.time(11), tzinfo=NY_OFFSET),
                    close_reason="STP filled",
                )
            )

        now = min(
            dt.datetime.combine(today, dt.time(15, 46), tzinfo=NY_OFFSET),
            dt.datetime.now(NY_OFFSET) - dt.timedelta(minutes=2),
        )
        signals = [
            Candidate(
                s,
                float(rng.uniform(20, 200)),
                0.0,
                float(rng.uniform(0.01, 0.22)),
                float(rng.uniform(1.6, 6)),
            )
            for s in ("ACME", "BOLT", "CRUX", "DELT", "EPIC", "FLUX")
        ]
        store.record_signals(today, signals, now)
        feed: list[tuple[str, str | None, dict[str, object]]] = [
            ("UNIVERSE", None, {"size": 1412, "universe": 4210}),
            ("STOP_UPDATE", symbols[0], {"change": "stop 41.2 -> 43.85"}),
            ("STOP_UPDATE", symbols[1], {"change": "stop 18.1 -> 18.9"}),
            ("ORDER", "BOLT", {"mode": "moc"}),
            ("FILL", "BOLT", {"slippage": 0.0012}),
            ("EXIT", symbols[18], {"order_type": "STP", "return": -0.061}),
        ]
        for n, (kind, feed_symbol, details) in enumerate(feed):
            store.record_event(
                kind,
                ts=now - dt.timedelta(minutes=50 - 8 * n),
                symbol=feed_symbol,
                block=Block.ONE,
                qty=40.0,
                price=52.3,
                details=details,
            )
        store.record_reconciliation(
            ts=now,
            outcome="ok",
            update_stops=True,
            n_local=17,
            n_broker=18,
            n_actions=1,
            message=None,
            discrepancies=[
                DiscrepancyRecord(
                    "untracked_foreign_position",
                    "info",
                    "SPY",
                    "not tracked",
                    "held 10, no bot order",
                    "ignored",
                )
            ],
        )
        store.set_meta("engine_mode", "demo")
        store.set_meta("engine_heartbeat", now.isoformat(timespec="seconds"))
    store.close()
    return quotes


def demo_benchmark(*, seed: int = 11, today: dt.date | None = None) -> pd.Series:
    """Synthetic daily closes of an index ETF covering the demo history."""
    rng = np.random.default_rng(seed)
    today = today or dt.date.today()  # noqa: DTZ011 - demo only
    days = [today - dt.timedelta(days=i) for i in range(150, -1, -1)]
    days = [d for d in days if is_trading_day(d)]
    closes = 560.0 * np.cumprod(1 + rng.normal(0.0004, 0.008, len(days)))
    return pd.Series(closes, index=pd.DatetimeIndex(days), dtype=float)


def demo_phases(today: dt.date | None = None) -> list[PhaseSettings]:
    """Milestones for the demo equity chart (relative to ``today``)."""
    today = today or dt.date.today()  # noqa: DTZ011 - demo only
    return [
        PhaseSettings(label="Test mode", start=today - dt.timedelta(days=140)),
        PhaseSettings(label="v1 · legacy bot", start=today - dt.timedelta(days=90)),
        PhaseSettings(label="v2 · engine + reconciliation", start=today - dt.timedelta(days=35)),
    ]
