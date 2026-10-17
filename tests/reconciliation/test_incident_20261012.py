"""Regression tests for the 12.10.2026 incident.

What happened: right after a reconnect TWS delivered an empty/incomplete position list. The legacy
``reconcile()`` read this as "all 20 positions were closed", removed them from its JSON state and
cancelled all their stop-loss orders. The positions themselves stayed open - unprotected.

These tests replay the scenario end to end through :class:`ReconciliationService` with the fake
broker and assert that no stop is ever cancelled and no position is forgotten.
"""

from __future__ import annotations

import pytest

from tests.conftest import RecordingNotifier
from tests.factories import position
from tests.fakes import FakeBroker
from turtlebot.domain.models import PositionStatus
from turtlebot.persistence.store import StateStore
from turtlebot.reconciliation.planner import Outcome
from turtlebot.reconciliation.service import ReconciliationService

SYMBOLS = [f"S{i:02d}" for i in range(20)]


@pytest.fixture
def twenty_protected_positions(broker: FakeBroker, store: StateStore) -> None:
    for symbol in SYMBOLS:
        broker.hold(symbol, 100)
        stop_id = broker.add_stop(symbol, 100, 90.0)
        store.insert_position(position(symbol, qty=100, stop=90.0, stop_order_id=stop_id))


def assert_fully_protected(broker: FakeBroker, store: StateStore) -> None:
    assert broker.cancels == []
    assert {s.symbol for s in broker.stops()} == set(SYMBOLS)
    active = store.active_positions()
    assert {p.symbol for p in active} == set(SYMBOLS)
    assert all(p.status is PositionStatus.OPEN for p in active)


@pytest.mark.usefixtures("twenty_protected_positions")
class TestIncident:
    def test_position_list_vanishes_after_reconnect(
        self,
        broker: FakeBroker,
        store: StateStore,
        reconciler: ReconciliationService,
        notifier: RecordingNotifier,
    ) -> None:
        broker.hide_positions = True  # the glitch: 0 positions, orders still there
        plan = reconciler.run()
        assert plan.outcome is Outcome.ABORTED
        assert_fully_protected(broker, store)
        assert notifier.messages  # the user is alerted
        assert store.reconciliations(1)[0].outcome == "aborted"

    def test_glitch_persisting_for_many_cycles_never_cancels(
        self, broker: FakeBroker, store: StateStore, reconciler: ReconciliationService
    ) -> None:
        broker.hide_positions = True
        for _ in range(10):
            reconciler.run(update_stops=True)
        assert_fully_protected(broker, store)

    def test_unsynced_snapshot_is_not_acted_upon(
        self, broker: FakeBroker, store: StateStore, reconciler: ReconciliationService
    ) -> None:
        broker.synced = False
        broker.hide_symbols = set(SYMBOLS[:19])
        assert reconciler.run().outcome is Outcome.ABORTED
        assert_fully_protected(broker, store)

    def test_partially_synced_list_does_not_close_positions(
        self, broker: FakeBroker, store: StateStore, reconciler: ReconciliationService
    ) -> None:
        # Only 5 of 20 positions arrive -- the legacy hotfix ("0 positions -> skip") missed this.
        broker.hide_symbols = set(SYMBOLS[5:])
        for _ in range(5):
            assert reconciler.run().outcome is Outcome.DEGRADED
        assert_fully_protected(broker, store)

    def test_recovery_resets_counters_and_resumes_normal_operation(
        self, broker: FakeBroker, store: StateStore, reconciler: ReconciliationService
    ) -> None:
        broker.hide_symbols = set(SYMBOLS[5:])
        reconciler.run()
        assert max(p.missing_cycles for p in store.active_positions()) == 1
        broker.hide_symbols = set()
        assert reconciler.run().outcome is Outcome.OK
        assert all(p.missing_cycles == 0 for p in store.active_positions())
        assert_fully_protected(broker, store)

    def test_local_state_lost_positions_are_adopted_not_cancelled(
        self, broker: FakeBroker, reconciler: ReconciliationService
    ) -> None:
        empty = StateStore(":memory:")
        reconciler.store = empty  # bot "forgot" everything, broker is fine
        reconciler.run()
        reconciler.run()
        assert broker.cancels == []
        assert {p.symbol for p in empty.active_positions()} == set(SYMBOLS)
        empty.close()

    def test_genuine_mass_stop_out_is_recognised_via_executions(
        self, broker: FakeBroker, store: StateStore, reconciler: ReconciliationService
    ) -> None:
        for symbol in SYMBOLS:
            broker.trigger_stop(symbol, 88.0)
        plan = reconciler.run()
        assert plan.outcome is Outcome.OK
        assert store.active_positions() == []
        assert {p.exit_price for p in store.closed_positions()} == {88.0}


def test_single_genuine_close_is_confirmed_after_two_cycles(
    broker: FakeBroker, store: StateStore, reconciler: ReconciliationService
) -> None:
    for symbol in SYMBOLS:
        broker.hold(symbol, 100)
        stop_id = broker.add_stop(symbol, 100, 90.0)
        store.insert_position(position(symbol, stop_order_id=stop_id))
    del broker.positions["S00"]  # closed manually in TWS, stop still working
    reconciler.run()
    assert broker.cancels == []
    assert store.active_position("S00") is not None
    reconciler.run()
    assert store.active_position("S00") is None
    assert len(broker.cancels) == 1  # the leftover stop, otherwise it would open a short
    assert {s.symbol for s in broker.stops()} == set(SYMBOLS[1:])


def test_service_refuses_to_cancel_stop_of_held_position(
    broker: FakeBroker, store: StateStore, reconciler: ReconciliationService
) -> None:
    """Defence in depth: even a wrong plan cannot strip a held position's protection."""
    from turtlebot.reconciliation.planner import CancelOrder, ReconciliationPlan

    broker.hold("A", 100)
    stop_id = broker.add_stop("A", 100, 90.0)
    snap = broker.snapshot()
    order = broker.orders[stop_id]
    bad_plan = ReconciliationPlan(outcome=Outcome.OK, actions=[CancelOrder(order, "bug")])
    reconciler._apply(bad_plan.actions[0], snap, bad_plan)
    assert broker.cancels == []
