"""Stop ownership, cancel-and-replace and unverified legacy stops.

Background (13.-16.10.2026): the 12.10. repair script re-placed the stops with API client id 50.
TWS only lets the owning client id change an order, so every morning the bot's (client id 11)
attempt to trail e.g. SHOP's stop #24 from 136.49 upwards was rejected with error 103. The
legacy bot nevertheless stored the intended stop (finally 150.28) in its state file, and the
17.10. hotfix would have answered the rejection by placing a *second* stop next to #24.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.conftest import MutableClock, RecordingNotifier
from tests.factories import NY, position, snapshot, stop_order
from tests.fakes import FakeBroker
from turtlebot.config import ReconciliationSettings, Settings
from turtlebot.dashboard.quotes import StaticQuotes
from turtlebot.dashboard.repository import DashboardRepository
from turtlebot.domain.models import Block, BrokerOrder, OrderAction, Position
from turtlebot.migration import migrate_legacy
from turtlebot.persistence.store import StateStore
from turtlebot.reconciliation.planner import (
    CancelOrder,
    DiscrepancyKind,
    ModifyStop,
    Outcome,
    PlaceStop,
    ReconciliationInput,
    ReconciliationPlan,
    UpdatePosition,
    plan_reconciliation,
)
from turtlebot.reconciliation.service import ReconciliationService

BOT, REPAIR_SCRIPT = 11, 50
REFS = {"TURTLE": Block.ONE, "TURTLE2": Block.TWO}
BLOCK_REFS = {Block.ONE: "TURTLE", Block.TWO: "TURTLE2"}
SHOP_LOWS = {  # 10-day lows the bot tried to trail to (journal 13.-16.10.)
    dt.date(2026, 10, 13): 142.43,
    dt.date(2026, 10, 14): 147.04,
    dt.date(2026, 10, 15): 148.01,
    dt.date(2026, 10, 16): 150.28,
}


def foreign_stop(symbol: str = "SHOP", order_id: int = 24, *, stop: float = 136.49) -> BrokerOrder:
    return BrokerOrder(
        order_id=order_id,
        symbol=symbol,
        action=OrderAction.SELL,
        order_type="STP",
        qty=114,
        order_ref="TURTLE",
        aux_price=stop,
        client_id=REPAIR_SCRIPT,
    )


def plan(
    local: list[Position],
    snap_orders: list[BrokerOrder],
    *,
    held: dict[str, float] | None = None,
    **kw: object,
) -> ReconciliationPlan:
    snap = snapshot(held if held is not None else {"SHOP": 114}, snap_orders)
    snap = type(snap)(snap.positions, snap.orders, True, snap.taken_at, client_id=BOT)
    return plan_reconciliation(
        ReconciliationInput(
            local=local,
            snapshot=snap,
            sell_fills=kw.get("fills", {}),  # type: ignore[arg-type]
            lows=kw.get("lows", {}),  # type: ignore[arg-type]
            update_stops=bool(kw.get("update_stops", False)),
            order_refs=REFS,
            block_refs=BLOCK_REFS,
            orphan_cycles=kw.get("orphan_cycles", {}),  # type: ignore[arg-type]
        ),
        ReconciliationSettings(),
    )


def actions[T](p: ReconciliationPlan, cls: type[T]) -> list[T]:
    return [a for a in p.actions if isinstance(a, cls)]


def kinds(p: ReconciliationPlan) -> list[DiscrepancyKind]:
    return [d.kind for d in p.discrepancies]


# ---------------------------------------------------------------- 3. foreign client ids
class TestForeignOrdersInPlanner:
    def test_foreign_stop_is_reported_and_never_touched(self) -> None:
        local = [position("SHOP", qty=114, stop=136.49, stop_order_id=24)]
        p = plan(local, [foreign_stop()], lows={"SHOP": 150.28}, update_stops=True)
        assert actions(p, ModifyStop) == []
        assert actions(p, PlaceStop) == []
        assert actions(p, CancelOrder) == []
        foreign = [d for d in p.discrepancies if d.kind is DiscrepancyKind.FOREIGN_CLIENT_ORDER]
        assert len(foreign) == 1
        assert foreign[0].symbol == "SHOP"
        assert "#24" in foreign[0].broker
        assert "client id 50" in foreign[0].broker
        assert "bot: 11" in foreign[0].broker

    def test_foreign_stop_counts_as_protection(self) -> None:
        """No second stop next to a foreign one - that could sell the shares twice."""
        manual = foreign_stop(stop=120.0)
        manual = replace(manual, order_ref="", client_id=0)
        p = plan([position("SHOP", qty=114, stop=120.0)], [manual])
        assert actions(p, PlaceStop) == []
        assert kinds(p) == [DiscrepancyKind.FOREIGN_CLIENT_ORDER]

    def test_foreign_orphan_is_never_cancelled(self) -> None:
        p = plan([], [foreign_stop("GONE", 30)], held={}, orphan_cycles={30: 99})
        assert actions(p, CancelOrder) == []
        assert kinds(p) == [DiscrepancyKind.FOREIGN_CLIENT_ORDER]

    def test_leftover_foreign_stop_of_closed_position_is_reported(self) -> None:
        from tests.factories import ny
        from turtlebot.domain.models import Fill

        fill = Fill("SHOP", OrderAction.SELL, 114, 136.0, ny())
        p = plan([position("SHOP", qty=114)], [foreign_stop()], held={}, fills={"SHOP": fill})
        assert actions(p, CancelOrder) == []
        assert DiscrepancyKind.FOREIGN_CLIENT_ORDER in kinds(p)

    def test_own_stop_next_to_foreign_one_is_still_managed(self) -> None:
        own = replace(foreign_stop(order_id=500, stop=140.0), client_id=BOT)
        local = [position("SHOP", qty=114, stop=140.0, stop_order_id=500)]
        p = plan(local, [foreign_stop(), own], lows={"SHOP": 150.0}, update_stops=True)
        assert [a.order.order_id for a in actions(p, ModifyStop)] == [500]
        assert DiscrepancyKind.DUPLICATE_STOPS in kinds(p)


# ---------------------------------------------------------------- 1. unverified stops
class TestStopPriceMismatch:
    def test_unconfirmed_legacy_stop_is_reported_with_both_values(self) -> None:
        local = [
            position("SHOP", qty=114, stop=150.28, stop_order_id=24).evolve(stop_confirmed=False)
        ]
        own = replace(foreign_stop(), client_id=BOT)
        p = plan(local, [own])
        mismatch = [d for d in p.discrepancies if d.kind is DiscrepancyKind.STOP_PRICE_MISMATCH]
        assert len(mismatch) == 1
        assert "150.28" in mismatch[0].local
        assert "unconfirmed" in mismatch[0].local
        assert "136.49" in mismatch[0].broker
        update = actions(p, UpdatePosition)[0].position
        assert (update.stop, update.stop_confirmed) == (136.49, True)

    def test_matching_unconfirmed_stop_is_just_confirmed(self) -> None:
        local = [position("A", stop=90.0, stop_order_id=1).evolve(stop_confirmed=False)]
        p = plan(local, [stop_order("A", 1, stop=90.0)], held={"A": 100})
        assert p.discrepancies == []
        assert actions(p, UpdatePosition)[0].position.stop_confirmed is True

    def test_confirmed_drift_is_reported_too(self) -> None:
        p = plan(
            [position("A", stop=95.0, stop_order_id=1)],
            [stop_order("A", 1, stop=90.0)],
            held={"A": 100},
        )
        assert kinds(p) == [DiscrepancyKind.STOP_PRICE_MISMATCH]

    def test_mismatch_against_foreign_stop(self) -> None:
        local = [position("SHOP", qty=114, stop=150.28).evolve(stop_confirmed=False)]
        p = plan(local, [foreign_stop()])
        assert set(kinds(p)) == {
            DiscrepancyKind.FOREIGN_CLIENT_ORDER,
            DiscrepancyKind.STOP_PRICE_MISMATCH,
        }
        assert actions(p, UpdatePosition)[0].position.stop == 136.49


# ---------------------------------------------------------------- service level
@pytest.fixture
def shop(broker: FakeBroker, store: StateStore) -> int:
    broker.hold("SHOP", 114, avg_cost=157.84)
    stop_id = broker.add_stop("SHOP", 114, 136.49, client_id=REPAIR_SCRIPT, order_id=24)
    store.insert_position(position("SHOP", qty=114, stop=136.49, entry_price=157.84))
    return stop_id


def stop_placements(broker: FakeBroker) -> list[object]:
    return [arg for name, arg in broker.calls if name in ("place_stop", "place_entry")]


class TestShopRegression:
    """13.-16.10.2026 replayed: daily trailing attempts on a stop owned by client id 50."""

    @pytest.mark.usefixtures("shop")
    def test_known_owner_no_modify_no_second_stop(
        self,
        broker: FakeBroker,
        store: StateStore,
        reconciler: ReconciliationService,
        clock: MutableClock,
        lows: dict[str, float],
    ) -> None:
        for day, low in SHOP_LOWS.items():
            clock.now = dt.datetime.combine(day, dt.time(9, 0), tzinfo=NY)
            lows["SHOP"] = low
            reconciler.run(update_stops=True)
        assert [(s.order_id, s.aux_price) for s in broker.stops("SHOP")] == [(24, 136.49)]
        assert stop_placements(broker) == []
        assert not any(name == "modify_stop" for name, _ in broker.calls)
        assert broker.cancels == []
        stored = store.active_position("SHOP")
        assert stored is not None
        assert stored.stop == 136.49  # the legacy bot recorded 150.28 here
        last = store.reconciliations(1)[0]
        assert any(
            d.kind == "foreign_client_order" and "client id 50" in (d.broker or "")
            for d in last.discrepancies
        )

    @pytest.mark.usefixtures("shop")
    def test_unknown_owner_error_103_never_adds_a_stop(
        self,
        broker: FakeBroker,
        store: StateStore,
        reconciler: ReconciliationService,
        clock: MutableClock,
        lows: dict[str, float],
        notifier: RecordingNotifier,
    ) -> None:
        """Owner not reported -> the bot tries to modify, TWS answers 103, cancel has no effect."""
        broker.report_client_ids = False
        for day, low in SHOP_LOWS.items():
            clock.now = dt.datetime.combine(day, dt.time(9, 0), tzinfo=NY)
            lows["SHOP"] = low
            reconciler.run(update_stops=True)
        modifies = [arg for name, arg in broker.calls if name == "modify_stop"]
        assert len(modifies) == 4  # one rejected attempt per morning, like 13.-16.10.
        assert broker.cancels == [24, 24, 24, 24]  # cancel tried, never confirmed
        assert stop_placements(broker) == []  # the key assertion: NO additional stop
        assert [(s.order_id, s.aux_price) for s in broker.stops("SHOP")] == [(24, 136.49)]
        failures = store.events(kinds=["STOP_REPLACE_FAILED"])
        assert len(failures) == 4
        assert failures[0].details["wanted_stop"] == 150.28
        assert "103" in failures[0].details["error"]
        assert notifier.messages[-1][0] == "Turtle: stop replacement failed"
        stored = store.active_position("SHOP")
        assert stored is not None
        assert stored.stop_order_id == 24


class TestCancelAndReplace:
    def test_rejected_own_stop_is_cancelled_first_then_replaced(
        self,
        broker: FakeBroker,
        store: StateStore,
        reconciler: ReconciliationService,
        lows: dict[str, float],
    ) -> None:
        broker.hold("A", 100)
        sid = broker.add_stop("A", 100, 90.0)
        broker.reject_modify.add(sid)
        store.insert_position(position("A", stop=90.0, stop_order_id=sid))
        lows["A"] = 95.0
        reconciler.run(update_stops=True)
        names = [name for name, _ in broker.calls]
        assert names.index("cancel") < names.index("place_stop")
        assert [(s.qty, s.aux_price) for s in broker.stops("A")] == [(100, 95.0)]

    def test_unconfirmed_cancel_places_nothing(
        self,
        broker: FakeBroker,
        store: StateStore,
        reconciler: ReconciliationService,
        lows: dict[str, float],
    ) -> None:
        broker.hold("A", 100)
        sid = broker.add_stop("A", 100, 90.0)
        broker.uncancellable.add(sid)
        store.insert_position(position("A", stop=90.0, stop_order_id=sid))
        original = broker.modify_stop

        def reject(order: BrokerOrder, qty: float, stop_price: float) -> None:
            from turtlebot.exceptions import OrderRejectedError

            raise OrderRejectedError("10148: cannot modify", order_id=order.order_id)

        broker.modify_stop = reject  # type: ignore[method-assign]
        lows["A"] = 95.0
        reconciler.run(update_stops=True)
        broker.modify_stop = original  # type: ignore[method-assign]
        assert [(s.order_id, s.aux_price) for s in broker.stops("A")] == [(sid, 90.0)]
        assert store.events(kinds=["STOP_REPLACE_FAILED"])

    def test_no_new_stop_if_position_vanished_meanwhile(
        self,
        broker: FakeBroker,
        store: StateStore,
        reconciler: ReconciliationService,
        lows: dict[str, float],
    ) -> None:
        broker.hold("A", 100)
        sid = broker.add_stop("A", 100, 90.0)
        broker.reject_modify.add(sid)
        store.insert_position(position("A", stop=90.0, stop_order_id=sid))
        original_cancel = broker.cancel_order

        def cancel_and_stop_out(order: BrokerOrder) -> None:
            original_cancel(order)
            broker.positions.pop("A", None)  # the old stop filled just before the cancel

        broker.cancel_order = cancel_and_stop_out  # type: ignore[method-assign]
        lows["A"] = 95.0
        reconciler.run(update_stops=True)
        assert broker.stops("A") == []
        assert stop_placements(broker) == []

    def test_executor_refuses_to_modify_foreign_order(
        self, broker: FakeBroker, store: StateStore, reconciler: ReconciliationService, shop: int
    ) -> None:
        snap = broker.snapshot()
        order = broker.orders[shop]
        bad = ModifyStop(order, "SHOP", Block.ONE, 114, 150.0, "TURTLE", "bug")
        reconciler._apply(bad, snap, ReconciliationPlan(outcome=Outcome.OK))
        assert not any(name == "modify_stop" for name, _ in broker.calls)
        assert store.events(kinds=["FOREIGN_ORDER"])[0].details["client_id"] == REPAIR_SCRIPT


# ---------------------------------------------------------------- migration end to end
def test_migrated_stop_is_verified_against_tws_on_first_reconciliation(
    tmp_path: Path, broker: FakeBroker, store: StateStore, reconciler: ReconciliationService
) -> None:
    state = {
        "positions": {
            "SHOP": {
                "status": "open",
                "qty": 114.0,
                "entry_price": 157.84,
                "stop": 150.28,
                "signal_price": 157.84,
                "entry_date": "2026-10-12",
                "block": 1,
            }
        }
    }
    (tmp_path / "state.json").write_text(json.dumps(state))
    migrate_legacy(store, tmp_path / "state.json", tmp_path / "missing.csv")
    imported = store.active_position("SHOP")
    assert imported is not None
    assert (imported.stop, imported.stop_confirmed) == (150.28, False)

    broker.hold("SHOP", 114)
    broker.add_stop("SHOP", 114, 136.49, client_id=REPAIR_SCRIPT, order_id=24)
    reconciler.run()
    run = store.reconciliations(1)[0]
    mismatch = [d for d in run.discrepancies if d.kind == "stop_price_mismatch"]
    assert len(mismatch) == 1
    assert "150.28" in (mismatch[0].local or "")
    assert "136.49" in (mismatch[0].broker or "")
    verified = store.active_position("SHOP")
    assert verified is not None
    assert (verified.stop, verified.stop_confirmed) == (136.49, True)

    reconciler.run()  # second run: values agree, only the foreign owner remains reported
    kinds_second = {d.kind for d in store.reconciliations(1)[0].discrepancies}
    assert kinds_second == {"foreign_client_order"}


def test_dashboard_shows_foreign_stops(
    broker: FakeBroker,
    store: StateStore,
    reconciler: ReconciliationService,
    shop: int,
    settings: Settings,
) -> None:
    reconciler.run()
    repo = DashboardRepository(
        store,
        StaticQuotes({"SHOP": 160.0}),
        settings,
        lambda: dt.datetime(2026, 10, 14, 9, 5, tzinfo=NY),
    )
    alerts = repo.overview().status.alerts
    assert [(a.kind, a.symbol, a.order_id, a.client_id) for a in alerts] == [
        ("foreign_client_order", "SHOP", 24, REPAIR_SCRIPT)
    ]
    shop_row = next(p for p in repo.positions() if p.symbol == "SHOP")
    assert shop_row.stop_alert == "foreign stop (client 50)"


def test_dashboard_shows_failed_replacement(
    broker: FakeBroker,
    store: StateStore,
    reconciler: ReconciliationService,
    shop: int,
    settings: Settings,
    lows: dict[str, float],
) -> None:
    broker.report_client_ids = False
    lows["SHOP"] = 150.28
    reconciler.run(update_stops=True)
    repo = DashboardRepository(
        store, StaticQuotes({}), settings, lambda: dt.datetime(2026, 10, 14, 9, 5, tzinfo=NY)
    )
    alerts = repo.stop_alerts()
    assert [a.kind for a in alerts] == ["stop_replace_failed"]
    assert alerts[0].order_id == 24
    assert "136.49" in alerts[0].message
    assert [e.kind for e in repo.events()][:1] == ["STOP_REPLACE_FAILED"]


def test_audit_filter_lists_orders_not_owned_by_the_bot() -> None:
    from turtlebot.cli import foreign_orders

    own = replace(foreign_stop("A", 1), client_id=BOT)
    unknown = replace(foreign_stop("B", 2), client_id=None)
    result = foreign_orders([own, foreign_stop("SHOP", 24), unknown], BOT)
    assert [o.order_id for o in result] == [24, 2]
