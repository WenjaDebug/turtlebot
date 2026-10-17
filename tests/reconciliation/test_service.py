"""Tests for executing reconciliation plans."""

from __future__ import annotations

from collections.abc import Sequence

from tests.conftest import MutableClock, RecordingNotifier
from tests.factories import position
from tests.fakes import FakeBroker
from turtlebot.config import Settings
from turtlebot.domain.models import PositionStatus
from turtlebot.exceptions import MarketDataError
from turtlebot.persistence.store import StateStore
from turtlebot.reconciliation.planner import Outcome
from turtlebot.reconciliation.service import LAST_STOP_UPDATE_KEY, ReconciliationService


def test_missing_stop_is_placed_and_recorded(
    broker: FakeBroker, store: StateStore, reconciler: ReconciliationService, lows: dict[str, float]
) -> None:
    broker.hold("A", 100)
    store.insert_position(position("A", stop=90.0))
    lows["A"] = 93.0
    reconciler.run()
    stops = broker.stops("A")
    assert [(s.qty, s.aux_price) for s in stops] == [(100, 93.0)]
    p = store.active_position("A")
    assert p is not None
    assert (p.stop, p.stop_order_id) == (93.0, stops[0].order_id)
    assert store.events(kinds=["STOP_NEW"])[0].symbol == "A"


def test_trailing_updates_broker_store_and_marks_stop_update(
    broker: FakeBroker, store: StateStore, reconciler: ReconciliationService, lows: dict[str, float]
) -> None:
    broker.hold("A", 100)
    sid = broker.add_stop("A", 100, 90.0)
    store.insert_position(position("A", stop=90.0, stop_order_id=sid))
    lows["A"] = 94.5
    reconciler.run(update_stops=True)
    assert broker.orders[sid].aux_price == 94.5
    p = store.active_position("A")
    assert p is not None
    assert p.stop == 94.5
    assert store.get_meta(LAST_STOP_UPDATE_KEY) is not None


def test_rejected_modification_is_replaced_by_new_stop(
    broker: FakeBroker, store: StateStore, reconciler: ReconciliationService, lows: dict[str, float]
) -> None:
    """17.10.2026 finding: a dead order id must not leave the position unprotected."""
    broker.hold("A", 100)
    sid = broker.add_stop("A", 100, 90.0)
    broker.reject_modify.add(sid)
    store.insert_position(position("A", stop=90.0, stop_order_id=sid))
    lows["A"] = 95.0
    reconciler.run(update_stops=True)
    stops = broker.stops("A")
    assert len(stops) == 1
    assert stops[0].order_id != sid
    assert stops[0].aux_price == 95.0
    p = store.active_position("A")
    assert p is not None
    assert p.stop_order_id == stops[0].order_id


def test_failure_in_one_symbol_does_not_block_others(
    broker: FakeBroker, store: StateStore, reconciler: ReconciliationService
) -> None:
    broker.hold("A", 100)
    broker.hold("B", 100)
    store.insert_position(position("A"))
    store.insert_position(position("B"))
    original = broker.place_stop

    def flaky(symbol: str, qty: float, stop_price: float, order_ref: str) -> int:
        if symbol == "A":
            from turtlebot.exceptions import OrderRejectedError

            raise OrderRejectedError("A rejected")
        return original(symbol, qty, stop_price, order_ref)

    broker.place_stop = flaky  # type: ignore[method-assign]
    reconciler.run()
    assert [s.symbol for s in broker.stops()] == ["B"]


def test_lows_failure_degrades_gracefully(
    broker: FakeBroker,
    store: StateStore,
    settings: Settings,
    notifier: RecordingNotifier,
    clock: MutableClock,
) -> None:
    def failing(_: Sequence[str]) -> dict[str, float]:
        raise MarketDataError("yahoo down")

    service = ReconciliationService(broker, store, failing, settings, notifier, clock)
    broker.hold("A", 100)
    store.insert_position(position("A", stop=90.0))
    assert service.run().outcome is Outcome.OK
    assert broker.stops("A")[0].aux_price == 90.0


def test_aborted_run_does_not_mark_stop_update(
    broker: FakeBroker, store: StateStore, reconciler: ReconciliationService
) -> None:
    broker.hold("A", 100)
    store.insert_position(position("A"))
    broker.synced = False
    reconciler.run(update_stops=True)
    assert store.get_meta(LAST_STOP_UPDATE_KEY) is None


def test_pending_fill_detected(
    broker: FakeBroker, store: StateStore, reconciler: ReconciliationService
) -> None:
    store.insert_position(position("A", status=PositionStatus.PENDING))
    broker.hold("A", 100, avg_cost=101.0)
    broker.add_stop("A", 100, 90.0)
    reconciler.run()
    p = store.active_position("A")
    assert p is not None
    assert (p.status, p.entry_price) == (PositionStatus.OPEN, 101.0)


def test_dry_run_gateway_never_changes_broker(
    broker: FakeBroker,
    store: StateStore,
    settings: Settings,
    notifier: RecordingNotifier,
    clock: MutableClock,
) -> None:
    from turtlebot.broker.gateway import DryRunGateway

    service = ReconciliationService(
        DryRunGateway(broker), store, lambda s: {}, settings, notifier, clock
    )
    broker.hold("A", 100)
    store.insert_position(position("A"))
    broker.add_stop("Z", 10, 5.0)
    service.run()
    service.run()
    assert broker.stops("A") == []
    assert broker.cancels == []
    assert [s.symbol for s in broker.stops()] == ["Z"]
