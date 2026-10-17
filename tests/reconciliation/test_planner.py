"""Unit tests for the pure reconciliation planner."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest

from tests.factories import entry_order, ny, position, snapshot, stop_order
from turtlebot.config import ReconciliationSettings
from turtlebot.domain.models import (
    Block,
    BrokerOrder,
    BrokerSnapshot,
    Fill,
    OrderAction,
    Position,
    PositionStatus,
)
from turtlebot.reconciliation.planner import (
    AdoptPosition,
    CancelOrder,
    ClosePosition,
    DiscrepancyKind,
    ModifyStop,
    Outcome,
    PlaceStop,
    ReconciliationInput,
    ReconciliationPlan,
    UpdatePosition,
    plan_reconciliation,
    symbols_needing_lows,
)

SETTINGS = ReconciliationSettings()
REFS = {"TURTLE": Block.ONE, "TURTLE2": Block.TWO}
BLOCK_REFS = {Block.ONE: "TURTLE", Block.TWO: "TURTLE2"}


def plan(
    local: Sequence[Position],
    snap: BrokerSnapshot,
    *,
    fills: Mapping[str, Fill] | None = None,
    lows: Mapping[str, float] | None = None,
    update_stops: bool = False,
    orphan_cycles: Mapping[int, int] | None = None,
    settings: ReconciliationSettings = SETTINGS,
) -> ReconciliationPlan:
    return plan_reconciliation(
        ReconciliationInput(
            local=local,
            snapshot=snap,
            sell_fills=fills or {},
            lows=lows or {},
            update_stops=update_stops,
            order_refs=REFS,
            block_refs=BLOCK_REFS,
            orphan_cycles=orphan_cycles or {},
        ),
        settings,
    )


def kinds(p: ReconciliationPlan) -> list[DiscrepancyKind]:
    return [d.kind for d in p.discrepancies]


def of_type[T](p: ReconciliationPlan, cls: type[T]) -> list[T]:
    return [a for a in p.actions if isinstance(a, cls)]


def sell_fill(symbol: str, price: float = 88.0, qty: float = 100) -> Fill:
    return Fill(symbol, OrderAction.SELL, qty, price, ny())


def test_consistent_state_produces_no_actions() -> None:
    local = [position("A", stop_order_id=1)]
    p = plan(local, snapshot({"A": 100}, [stop_order("A", 1)]))
    assert p.outcome is Outcome.OK
    assert p.actions == []
    assert p.discrepancies == []


class TestSafetyRules:
    def test_unsynced_snapshot_aborts(self) -> None:
        p = plan([position("A")], snapshot({}, synced=False))
        assert p.outcome is Outcome.ABORTED
        assert p.actions == []
        assert kinds(p) == [DiscrepancyKind.SNAPSHOT_NOT_SYNCED]

    def test_empty_snapshot_with_tracked_positions_aborts(self) -> None:
        local = [position(s, stop_order_id=i) for i, s in enumerate("ABC")]
        p = plan(local, snapshot({}, [stop_order(s, i) for i, s in enumerate("ABC")]))
        assert p.outcome is Outcome.ABORTED
        assert p.actions == []

    def test_empty_snapshot_explained_by_fills_is_processed(self) -> None:
        local = [position("A"), position("B")]
        fills = {"A": sell_fill("A"), "B": sell_fill("B")}
        p = plan(local, snapshot({}), fills=fills)
        assert p.outcome is Outcome.OK
        assert len(of_type(p, ClosePosition)) == 2

    def test_mass_disappearance_degrades_and_suppresses_destruction(self) -> None:
        local = [position(s, missing_cycles=5) for s in "ABCDEFGH"]
        held = {s: 100 for s in "ABCDE"}  # 3 of 8 vanish at once (>= 25 %)
        orders = [stop_order(s, i) for i, s in enumerate("ABCDEFGH")]
        p = plan(local, snapshot(held, orders))
        assert p.outcome is Outcome.DEGRADED
        assert DiscrepancyKind.MASS_DISAPPEARANCE in kinds(p)
        assert p.destructive_actions == []
        missing = [a.position for a in of_type(p, UpdatePosition) if a.position.symbol in "FGH"]
        assert {m.missing_cycles for m in missing} == {6}

    def test_degraded_run_still_places_missing_stops(self) -> None:
        local = [position(s) for s in "ABCD"]
        p = plan(local, snapshot({"A": 100, "B": 100}))  # C, D missing; A, B have no stop
        assert p.outcome is Outcome.DEGRADED
        assert {a.symbol for a in of_type(p, PlaceStop)} == {"A", "B"}

    def test_degraded_run_closes_only_fill_confirmed_exits(self) -> None:
        local = [position(s) for s in "ABCD"]
        orders = [stop_order(s, i) for i, s in enumerate("ABCD")]
        p = plan(local, snapshot({"A": 100}, orders), fills={"B": sell_fill("B")})
        assert p.outcome is Outcome.DEGRADED  # C and D vanish without executions
        assert [a.position.symbol for a in of_type(p, ClosePosition)] == ["B"]
        assert [a.order.symbol for a in of_type(p, CancelOrder)] == ["B"]


class TestMissingPositions:
    def test_first_miss_only_increments_counter(self) -> None:
        local = [position("A", stop_order_id=1), *[position(s) for s in "BCDEFGH"]]
        held = {s: 100 for s in "BCDEFGH"}
        orders = [stop_order(s, i) for i, s in enumerate("ABCDEFGH", start=1)]
        p = plan(local, snapshot(held, orders))
        assert p.outcome is Outcome.OK
        assert DiscrepancyKind.POSITION_MISSING in kinds(p)
        assert of_type(p, CancelOrder) == []
        update = next(a for a in of_type(p, UpdatePosition) if a.position.symbol == "A")
        assert update.position.missing_cycles == 1

    def test_confirmed_after_n_cycles_closes_and_cancels_stop(self) -> None:
        local = [position("A", missing_cycles=1), *[position(s) for s in "BCDEFGH"]]
        held = {s: 100 for s in "BCDEFGH"}
        orders = [stop_order(s, i) for i, s in enumerate("ABCDEFGH", start=1)]
        p = plan(local, snapshot(held, orders))
        close = of_type(p, ClosePosition)
        assert [c.position.symbol for c in close] == ["A"]
        assert close[0].exit_price is None
        assert [c.order.order_id for c in of_type(p, CancelOrder)] == [1]

    def test_fill_confirms_exit_immediately(self) -> None:
        local = [position("A"), position("B")]
        p = plan(local, snapshot({"B": 100}, [stop_order("B", 2)]), fills={"A": sell_fill("A")})
        close = of_type(p, ClosePosition)[0]
        assert (close.exit_price, close.exit_qty) == (88.0, 100)

    def test_reappearing_position_resets_counter(self) -> None:
        p = plan([position("A", missing_cycles=1)], snapshot({"A": 100}, [stop_order("A", 1)]))
        assert DiscrepancyKind.POSITION_REAPPEARED in kinds(p)
        update = of_type(p, UpdatePosition)[0]
        assert update.position.missing_cycles == 0


class TestPendingEntries:
    def test_filled_entry_becomes_open(self) -> None:
        local = [position("A", status=PositionStatus.PENDING, qty=100)]
        p = plan(local, snapshot({"A": 100}, [stop_order("A", 2)], avg_cost=101.5))
        opened = of_type(p, UpdatePosition)[0].position
        assert opened.status is PositionStatus.OPEN
        assert opened.entry_price == 101.5
        assert kinds(p) == [DiscrepancyKind.PENDING_FILLED]

    def test_partial_fill_opens_with_broker_qty_and_resizes_stop(self) -> None:
        local = [position("A", status=PositionStatus.PENDING, qty=100)]
        orders = [entry_order("A", 1, filled=40), stop_order("A", 2, qty=100)]
        p = plan(local, snapshot({"A": 40}, orders))
        assert DiscrepancyKind.PENDING_PARTIALLY_FILLED in kinds(p)
        opened = of_type(p, UpdatePosition)[0].position
        assert opened.qty == 40
        modify = of_type(p, ModifyStop)[0]
        assert (modify.order.order_id, modify.qty) == (2, 40)

    def test_working_entry_is_left_alone(self) -> None:
        local = [position("A", status=PositionStatus.PENDING)]
        p = plan(local, snapshot({}, [entry_order("A", 1), stop_order("A", 2)]))
        assert p.actions == []

    def test_vanished_entry_needs_confirmation_then_is_dropped(self) -> None:
        pending = position("A", status=PositionStatus.PENDING)
        first = plan([pending], snapshot({}, [stop_order("A", 2)]))
        assert kinds(first) == [DiscrepancyKind.PENDING_MISSING]
        assert first.destructive_actions == []
        second = plan([pending.evolve(missing_cycles=1)], snapshot({}, [stop_order("A", 2)]))
        assert DiscrepancyKind.PENDING_DROPPED in kinds(second)
        assert [c.order.order_id for c in of_type(second, CancelOrder)] == [2]


class TestUntracked:
    def test_position_with_own_stop_is_adopted_into_its_block(self) -> None:
        p = plan([], snapshot({"X": 50}, [stop_order("X", 9, qty=50, stop=7.5, ref="TURTLE2")]))
        adopted = of_type(p, AdoptPosition)[0].position
        assert (adopted.block, adopted.qty, adopted.stop, adopted.stop_order_id) == (
            Block.TWO,
            50,
            7.5,
            9,
        )

    def test_foreign_position_is_ignored(self) -> None:
        p = plan([], snapshot({"SPY": 10}))
        assert p.actions == []
        assert kinds(p) == [DiscrepancyKind.UNTRACKED_FOREIGN]


class TestOrphans:
    def test_orphan_stop_needs_confirmation(self) -> None:
        orders = [stop_order("Z", 5)]
        first = plan([], snapshot({}, orders))
        assert first.actions == []
        assert first.orphan_cycles == {5: 1}
        second = plan([], snapshot({}, orders), orphan_cycles=first.orphan_cycles)
        assert [c.order.order_id for c in of_type(second, CancelOrder)] == [5]
        assert second.orphan_cycles == {}

    def test_stop_of_missing_tracked_position_is_no_orphan(self) -> None:
        local = [position("A", missing_cycles=0), *[position(s) for s in "BCDEFGH"]]
        held = {s: 100 for s in "BCDEFGH"}
        orders = [stop_order(s, i) for i, s in enumerate("ABCDEFGH", start=1)]
        p = plan(local, snapshot(held, orders), orphan_cycles={1: 5})
        assert of_type(p, CancelOrder) == []

    def test_orphan_buy_is_never_cancelled(self) -> None:
        p = plan([], snapshot({}, [entry_order("Q", 3)]), orphan_cycles={3: 9})
        assert p.actions == []


class TestStops:
    def test_missing_stop_is_placed_at_max_of_low_and_last_stop(self) -> None:
        p = plan([position("A", stop=90.0)], snapshot({"A": 100}), lows={"A": 92.123})
        place = of_type(p, PlaceStop)[0]
        assert (place.qty, place.stop_price, place.order_ref) == (100, 92.12, "TURTLE")
        p2 = plan([position("A", stop=90.0)], snapshot({"A": 100}), lows={"A": 85.0})
        assert of_type(p2, PlaceStop)[0].stop_price == 90.0

    def test_missing_stop_without_low_data_uses_last_stop(self) -> None:
        p = plan([position("A", stop=90.0, block=Block.TWO)], snapshot({"A": 100}))
        place = of_type(p, PlaceStop)[0]
        assert (place.stop_price, place.order_ref) == (90.0, "TURTLE2")

    def test_trailing_only_upwards_and_only_when_requested(self) -> None:
        local = [position("A", stop=90.0, stop_order_id=1)]
        snap = snapshot({"A": 100}, [stop_order("A", 1, stop=90.0)])
        assert of_type(plan(local, snap, lows={"A": 95.0}), ModifyStop) == []
        up = of_type(plan(local, snap, lows={"A": 95.0}, update_stops=True), ModifyStop)
        assert up[0].stop_price == 95.0
        down = plan(local, snap, lows={"A": 80.0}, update_stops=True)
        assert of_type(down, ModifyStop) == []

    def test_trailed_stop_updates_local_stop(self) -> None:
        local = [position("A", stop=90.0, stop_order_id=1)]
        snap = snapshot({"A": 100}, [stop_order("A", 1, stop=90.0)])
        p = plan(local, snap, lows={"A": 95.0}, update_stops=True)
        assert of_type(p, UpdatePosition)[0].position.stop == 95.0

    def test_stop_quantity_follows_broker_quantity(self) -> None:
        local = [position("A", qty=100, stop_order_id=1)]
        p = plan(local, snapshot({"A": 60}, [stop_order("A", 1, qty=100)]))
        assert {DiscrepancyKind.QUANTITY_MISMATCH, DiscrepancyKind.STOP_QUANTITY_MISMATCH} <= set(
            kinds(p)
        )
        assert of_type(p, ModifyStop)[0].qty == 60
        assert of_type(p, UpdatePosition)[0].position.qty == 60

    def test_duplicate_stops_are_reported_not_cancelled(self) -> None:
        local = [position("A", stop_order_id=2)]
        orders: list[BrokerOrder] = [stop_order("A", 1, stop=80), stop_order("A", 2, stop=90)]
        p = plan(local, snapshot({"A": 100}, orders))
        assert DiscrepancyKind.DUPLICATE_STOPS in kinds(p)
        assert of_type(p, CancelOrder) == []

    def test_stop_order_id_is_learned(self) -> None:
        p = plan([position("A", stop_order_id=None)], snapshot({"A": 100}, [stop_order("A", 7)]))
        assert of_type(p, UpdatePosition)[0].position.stop_order_id == 7

    def test_nan_low_is_ignored(self) -> None:
        p = plan([position("A")], snapshot({"A": 100}), lows={"A": float("nan")})
        assert of_type(p, PlaceStop)[0].stop_price == 90.0


@pytest.mark.parametrize("update_stops", [True, False])
def test_symbols_needing_lows(update_stops: bool) -> None:
    local = [position("A", stop_order_id=1), position("B")]
    snap = snapshot({"A": 100, "B": 100, "X": 5}, [stop_order("A", 1), stop_order("X", 3)])
    # B has no stop -> lows needed for every held/adopted symbol either way.
    assert symbols_needing_lows(local, snap, update_stops) == ["A", "B", "X"]
    full = snapshot({"A": 100}, [stop_order("A", 1)])
    assert symbols_needing_lows(local[:1], full, update_stops) == (["A"] if update_stops else [])
