"""Tests for the legacy import."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from turtlebot.domain.models import Block, PositionStatus
from turtlebot.exceptions import StateError
from turtlebot.migration import migrate_legacy
from turtlebot.persistence.store import StateStore

HEADER = "time,event,symbol,qty,price,stop,strength,details\n"


@pytest.fixture
def legacy(tmp_path: Path) -> tuple[Path, Path]:
    state = {
        "positions": {
            "XP": {
                "status": "open",
                "qty": 638.0,
                "entry_price": 28.3,
                "stop": 20.57,
                "signal_price": 28.3,
                "entry_date": "2026-10-12",
                "block": 1,
                "stop_id": 77,
                "signal_time": "2026-10-09T15:45:00-04:00",
            },
            "NEW": {"status": "pending", "qty": 10, "stop": 9.0, "signal_price": 10.0, "block": 2},
        },
        "days": {},
    }
    journal = HEADER + "".join(
        [
            "2026-10-09T15:45:00-04:00,SIGNAL,XP,,28.2,20.5,0.12,vol_ratio=2.0\n",
            '2026-10-09T16:00:00-04:00,FILL,XP,638,28.3,20.57,,"slippage=0.001, block=1"\n',
            "2026-10-09T16:00:00-04:00,FILL,AAA,100,10.0,9.0,,block=1\n",
            "2026-10-09T16:00:00-04:00,FILL,BBB,50,20.0,18.0,,block=2\n",
            # 12.10. incident: false exits without price, later stop updates prove they are alive
            '2026-10-12T05:56:06-04:00,EXIT,XP,,,,,"beim Abgleich erkannt, block=1"\n',
            '2026-10-12T05:56:06-04:00,EXIT,AAA,,,,,"beim Abgleich erkannt, block=1"\n',
            "2026-10-13T09:00:00-04:00,STOP_UPDATE,AAA,100,,9.5,,block=1\n",
            '2026-10-14T10:00:00-04:00,EXIT,AAA,100,9.4,,,"STP, return=-6%, block=1"\n',
            "2026-10-14T11:00:00-04:00,EXIT,BBB,50,19.0,,,block=2\n",
        ]
    )
    (tmp_path / "state.json").write_text(json.dumps(state))
    (tmp_path / "journal.csv").write_text(journal)
    return tmp_path / "state.json", tmp_path / "journal.csv"


def test_import(store: StateStore, legacy: tuple[Path, Path]) -> None:
    report = migrate_legacy(store, *legacy)
    assert report.active_positions == 2
    assert report.events == 9
    assert report.false_exits_ignored == 2  # XP (still held) and AAA (stop update afterwards)
    active = {p.symbol: p for p in store.active_positions()}
    assert active["XP"].stop_order_id == 77
    assert active["NEW"].status is PositionStatus.PENDING
    assert active["NEW"].block is Block.TWO
    closed = {p.symbol: p for p in store.closed_positions()}
    assert set(closed) == {"AAA", "BBB"}
    assert closed["AAA"].exit_price == 9.4
    assert closed["BBB"].block is Block.TWO


def test_refuses_non_empty_store(store: StateStore, legacy: tuple[Path, Path]) -> None:
    migrate_legacy(store, *legacy)
    with pytest.raises(StateError):
        migrate_legacy(store, *legacy)


def test_missing_state_file(store: StateStore, tmp_path: Path) -> None:
    with pytest.raises(StateError):
        migrate_legacy(store, tmp_path / "nope.json", tmp_path / "nope.csv")


def test_journal_is_optional(store: StateStore, legacy: tuple[Path, Path], tmp_path: Path) -> None:
    report = migrate_legacy(store, legacy[0], tmp_path / "missing.csv")
    assert (report.active_positions, report.events) == (2, 0)
