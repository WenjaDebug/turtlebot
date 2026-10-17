"""Tests for the SQLite state store."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from tests.factories import NY, position
from turtlebot.domain.models import AccountSnapshot, Block, Candidate, PositionStatus
from turtlebot.exceptions import PersistenceError
from turtlebot.persistence.store import DiscrepancyRecord, StateStore

NOW = dt.datetime(2026, 10, 14, 9, 0, tzinfo=NY)
DAY = NOW.date()


def test_position_roundtrip(store: StateStore) -> None:
    saved = store.insert_position(position("AAPL", qty=10, stop=150.0, stop_order_id=7))
    assert saved.id is not None
    loaded = store.active_position("AAPL")
    assert loaded == saved
    assert store.active_positions(Block.TWO) == []


def test_only_one_active_position_per_symbol(store: StateStore) -> None:
    store.insert_position(position("AAPL"))
    with pytest.raises(PersistenceError):
        store.insert_position(position("AAPL", block=Block.TWO))


def test_closed_position_frees_symbol_and_keeps_history(store: StateStore) -> None:
    p = store.insert_position(position("AAPL"))
    closed = store.close_position(p, reason="stop", exit_price=95.0, closed_at=NOW)
    assert closed.status is PositionStatus.CLOSED
    assert store.active_positions() == []
    store.insert_position(position("AAPL"))  # re-entry allowed after close
    assert [c.exit_price for c in store.closed_positions()] == [95.0]


def test_update_requires_existing_id(store: StateStore) -> None:
    with pytest.raises(PersistenceError):
        store.update_position(position("X"))
    with pytest.raises(PersistenceError):
        store.update_position(position("X", pid=999))


def test_transaction_rolls_back_on_error(store: StateStore) -> None:
    def failing_batch() -> None:
        with store.transaction():
            store.insert_position(position("A"))
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        failing_batch()
    assert store.active_positions() == []


def test_events_newest_first_with_filters(store: StateStore) -> None:
    store.record_event("FILL", ts=NOW, symbol="A", qty=1, price=2.0, details={"x": 1})
    store.record_event("EXIT", ts=NOW, symbol="A")
    events = store.events(limit=10)
    assert [e.kind for e in events] == ["EXIT", "FILL"]
    assert events[1].details == {"x": 1}
    assert [e.kind for e in store.events(kinds=["FILL"])] == ["FILL"]
    assert store.events(after_id=events[0].id) == []


def test_day_counters(store: StateStore) -> None:
    assert store.entries_today(DAY, Block.ONE) == 0
    store.increment_entries(DAY, Block.ONE)
    store.increment_entries(DAY, Block.ONE)
    assert store.entries_today(DAY, Block.ONE) == 2
    assert store.entries_today(DAY, Block.TWO) == 0
    store.mark_traded(DAY, "A")
    store.mark_traded(DAY, "A")
    assert store.traded_today(DAY) == {"A"}


def test_jobs_and_meta(store: StateStore) -> None:
    assert store.job(DAY, "scan") is None
    store.set_job(DAY, "scan", "failed", attempts=1, error="x")
    store.set_job(DAY, "scan", "done", attempts=2)
    job = store.job(DAY, "scan")
    assert job is not None
    assert (job.status, job.attempts, job.last_error) == ("done", 2, None)
    store.set_meta("k", "v1")
    store.set_meta("k", "v2")
    assert store.get_meta("k") == "v2"
    assert store.get_meta("missing") is None


def test_signals_latest_scan(store: StateStore) -> None:
    assert store.signals() == []
    store.record_signals(DAY, [Candidate("A", 10, 9, 0.05, 2), Candidate("B", 10, 9, 0.2, 2)], NOW)
    assert [s["symbol"] for s in store.signals()] == ["B", "A"]


def test_reconciliation_records(store: StateStore) -> None:
    run = store.record_reconciliation(
        ts=NOW,
        outcome="ok",
        update_stops=True,
        n_local=1,
        n_broker=1,
        n_actions=0,
        message=None,
        discrepancies=[DiscrepancyRecord("missing_stop", "warning", "A", "l", "b", "r")],
    )
    latest = store.reconciliations(limit=1)[0]
    assert latest.id == run
    assert latest.discrepancies[0].kind == "missing_stop"


def test_orphan_counters_keep_first_seen(store: StateStore) -> None:
    store.replace_orphan_cycles({1: 1}, {1: "A"}, NOW)
    store.replace_orphan_cycles({1: 2, 2: 1}, {1: "A", 2: "B"}, NOW + dt.timedelta(hours=1))
    assert store.orphan_cycles() == {1: 2, 2: 1}
    store.replace_orphan_cycles({}, {}, NOW)
    assert store.orphan_cycles() == {}


def test_account_history_and_data_version(store: StateStore) -> None:
    v0 = store.data_version()
    snap = AccountSnapshot(100.0, "CHF", None, None, 1.25)
    store.record_account(snap, NOW)
    assert store.account_history() == [(NOW, snap)]
    assert store.data_version() != v0


def test_persists_across_connections(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    first = StateStore(path)
    first.insert_position(position("AAPL"))
    first.close()
    second = StateStore(path)
    assert [p.symbol for p in second.active_positions()] == ["AAPL"]
    second.close()


def test_rejects_newer_schema(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    StateStore(path).close()
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(PersistenceError, match="newer"):
        StateStore(path)
