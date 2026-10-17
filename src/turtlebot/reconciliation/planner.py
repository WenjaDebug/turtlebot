"""Reconciliation planning: compare local state with the broker and decide what to do.

This module is pure: it receives the local positions, a broker snapshot and auxiliary data, and
returns a :class:`ReconciliationPlan` (a list of discrepancies plus the actions that resolve
them). Executing the plan is the job of :mod:`turtlebot.reconciliation.service`.

Safety rules (lessons from the 12.10.2026 incident, where an incomplete position snapshot right
after a reconnect led the legacy bot to cancel the stop-loss orders of all 20 open positions):

1. A snapshot whose position stream was not confirmed complete is never acted upon.
2. If the broker reports *no* positions while positions are tracked locally, and no SELL
   executions explain it, the run is aborted.
3. If a large share of tracked positions disappears at once without executions
   ("mass disappearance"), the run is *degraded*: nothing is closed or cancelled, only
   non-destructive actions (e.g. placing a missing stop for a held position) are allowed.
4. A position that is not reported by the broker is only treated as closed if a SELL execution
   proves it, or if it has been missing for ``close_confirmation_cycles`` consecutive runs.
5. A stop order is only cancelled once the position it protects is confirmed closed, or (for
   orphans) after it was seen without position for ``orphan_confirmation_cycles`` runs. A stop of
   a symbol the broker reports as held is never cancelled.
6. Every deviation is reported as a :class:`Discrepancy`; nothing is overwritten silently.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

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


class Severity(StrEnum):
    """How urgent a discrepancy is."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Outcome(StrEnum):
    """Overall result of a reconciliation run."""

    OK = "ok"
    DEGRADED = "degraded"  # destructive actions suppressed
    ABORTED = "aborted"  # no action at all


class DiscrepancyKind(StrEnum):
    """Categories of differences between local state and broker."""

    SNAPSHOT_NOT_SYNCED = "snapshot_not_synced"
    SNAPSHOT_EMPTY = "snapshot_empty"
    MASS_DISAPPEARANCE = "mass_disappearance"
    POSITION_MISSING = "position_missing"
    POSITION_REAPPEARED = "position_reappeared"
    POSITION_CLOSED = "position_closed"
    PENDING_FILLED = "pending_filled"
    PENDING_PARTIALLY_FILLED = "pending_partially_filled"
    PENDING_MISSING = "pending_missing"
    PENDING_DROPPED = "pending_dropped"
    QUANTITY_MISMATCH = "quantity_mismatch"
    UNTRACKED_ADOPTED = "untracked_position_adopted"
    UNTRACKED_FOREIGN = "untracked_foreign_position"
    MISSING_STOP = "missing_stop"
    STOP_QUANTITY_MISMATCH = "stop_quantity_mismatch"
    STOP_TRAILED = "stop_trailed"
    DUPLICATE_STOPS = "duplicate_stops"
    ORPHAN_ORDER = "orphan_order"
    ORPHAN_CANCELLED = "orphan_order_cancelled"


@dataclass(frozen=True, slots=True)
class Discrepancy:
    """One observed difference and how it is resolved."""

    kind: DiscrepancyKind
    severity: Severity
    symbol: str | None
    local: str
    broker: str
    resolution: str


# ------------------------------------------------------------------------------ actions
@dataclass(frozen=True, slots=True)
class UpdatePosition:
    """Persist a changed version of a tracked position."""

    position: Position


@dataclass(frozen=True, slots=True)
class AdoptPosition:
    """Start tracking a broker position that carries one of the bot's stops."""

    position: Position


@dataclass(frozen=True, slots=True)
class ClosePosition:
    """Mark a tracked position as closed."""

    position: Position
    reason: str
    exit_price: float | None
    exit_qty: float | None


@dataclass(frozen=True, slots=True)
class CancelOrder:
    """Cancel a working order (destructive)."""

    order: BrokerOrder
    reason: str


@dataclass(frozen=True, slots=True)
class PlaceStop:
    """Place a new protective stop."""

    symbol: str
    block: Block
    qty: float
    stop_price: float
    order_ref: str


@dataclass(frozen=True, slots=True)
class ModifyStop:
    """Change quantity and/or price of an existing stop."""

    order: BrokerOrder
    symbol: str
    block: Block
    qty: float
    stop_price: float
    order_ref: str
    reason: str


type Action = UpdatePosition | AdoptPosition | ClosePosition | CancelOrder | PlaceStop | ModifyStop
DESTRUCTIVE = (ClosePosition, CancelOrder)


@dataclass(slots=True)
class ReconciliationPlan:
    """Result of :func:`plan_reconciliation`."""

    outcome: Outcome
    discrepancies: list[Discrepancy] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    orphan_cycles: dict[int, int] = field(default_factory=dict)
    message: str = ""

    @property
    def destructive_actions(self) -> list[Action]:
        """Actions that close positions or cancel orders."""
        return [a for a in self.actions if isinstance(a, DESTRUCTIVE)]


@dataclass(frozen=True, slots=True)
class ReconciliationInput:
    """Everything the planner needs.

    Attributes:
        local: Active (pending/open) positions from the state store.
        snapshot: Broker snapshot.
        sell_fills: Latest SELL execution per symbol (only for symbols the broker no longer
            reports), used to confirm exits.
        lows: Lowest low of the last ``exit_period`` completed days per symbol (may be partial).
        update_stops: Whether stops should be trailed upwards in this run (09:00 job).
        order_refs: Order reference -> block of the bot's own orders.
        block_refs: Block -> order reference for new orders.
        orphan_cycles: Previous observation counters of orphaned SELL orders.
    """

    local: Sequence[Position]
    snapshot: BrokerSnapshot
    sell_fills: Mapping[str, Fill]
    lows: Mapping[str, float]
    update_stops: bool
    order_refs: Mapping[str, Block]
    block_refs: Mapping[Block, str]
    orphan_cycles: Mapping[int, int] = field(default_factory=dict)


def symbols_needing_lows(
    local: Sequence[Position], snapshot: BrokerSnapshot, update_stops: bool
) -> list[str]:
    """Symbols for which the 10-day low must be loaded before planning.

    Legacy behaviour: lows are loaded for all held positions if stops are trailed, or if at least
    one held position has no stop.
    """
    held = [
        p.symbol
        for p in local
        if p.symbol in snapshot.positions
        and p.status in (PositionStatus.OPEN, PositionStatus.PENDING)
    ]
    adopted = [
        s
        for s in snapshot.positions
        if s not in {p.symbol for p in local} and snapshot.stops_for(s)
    ]
    candidates = sorted(set(held) | set(adopted))
    if update_stops or any(not snapshot.stops_for(s) for s in candidates):
        return candidates
    return []


def _fmt_qty(qty: float) -> str:
    return f"{qty:g}"


class _Planner:
    """Stateful helper that builds one plan (keeps :func:`plan_reconciliation` readable)."""

    def __init__(self, data: ReconciliationInput, settings: ReconciliationSettings) -> None:
        self.d = data
        self.s = settings
        self.snap = data.snapshot
        self.held = data.snapshot.positions
        self.plan = ReconciliationPlan(outcome=Outcome.OK)
        self.destructive_allowed = True
        # Positions (after this run's updates) whose stop must be checked: symbol -> position.
        self.protect: dict[str, Position] = {}

    # -------------------------------------------------------------- helpers
    def report(
        self,
        kind: DiscrepancyKind,
        severity: Severity,
        symbol: str | None,
        local: str,
        broker: str,
        resolution: str,
    ) -> None:
        self.plan.discrepancies.append(
            Discrepancy(kind, severity, symbol, local, broker, resolution)
        )

    def act(self, action: Action) -> None:
        self.plan.actions.append(action)

    def confirming_fill(self, p: Position) -> Fill | None:
        fill = self.d.sell_fills.get(p.symbol)
        if fill is None or fill.action is not OrderAction.SELL:
            return None
        return fill

    def cancel_leftovers(self, symbol: str, reason: str) -> None:
        for order in self.snap.orders_for(symbol):
            self.act(CancelOrder(order, reason))

    # -------------------------------------------------------------- phases
    def run(self) -> ReconciliationPlan:
        if not self.snap.positions_synced:
            self.plan.outcome = Outcome.ABORTED
            self.plan.message = "broker position snapshot not confirmed complete"
            self.report(
                DiscrepancyKind.SNAPSHOT_NOT_SYNCED,
                Severity.CRITICAL,
                None,
                f"{len(self.d.local)} active",
                "position stream incomplete",
                "reconciliation skipped, nothing changed",
            )
            self.plan.orphan_cycles = dict(self.d.orphan_cycles)
            return self.plan

        open_local = [p for p in self.d.local if p.status is PositionStatus.OPEN]
        missing = [p for p in open_local if p.symbol not in self.held]
        unconfirmed = [p for p in missing if self.confirming_fill(p) is None]

        if open_local and not self.held and unconfirmed:
            self.plan.outcome = Outcome.ABORTED
            names = ", ".join(sorted(p.symbol for p in open_local))
            self.plan.message = (
                f"broker reports 0 positions but {len(open_local)} are tracked "
                "without matching executions"
            )
            self.report(
                DiscrepancyKind.SNAPSHOT_EMPTY,
                Severity.CRITICAL,
                None,
                f"{len(open_local)} open: {names}",
                "0 positions",
                "reconciliation aborted; positions and stop orders left untouched",
            )
            self.plan.orphan_cycles = dict(self.d.orphan_cycles)
            return self.plan

        threshold = max(
            self.s.mass_disappearance_min_count,
            math.ceil(self.s.mass_disappearance_fraction * len(open_local)),
        )
        if unconfirmed and len(unconfirmed) >= threshold:
            self.destructive_allowed = False
            self.plan.outcome = Outcome.DEGRADED
            self.plan.message = (
                f"{len(unconfirmed)} of {len(open_local)} positions vanished at once "
                "without executions; destructive actions suppressed"
            )
            self.report(
                DiscrepancyKind.MASS_DISAPPEARANCE,
                Severity.CRITICAL,
                None,
                f"{len(open_local)} open",
                f"{len(unconfirmed)} missing: {', '.join(sorted(p.symbol for p in unconfirmed))}",
                "no position closed, no order cancelled in this run",
            )

        for p in self.d.local:
            if p.status is PositionStatus.OPEN:
                self.open_position(p)
            elif p.status is PositionStatus.PENDING:
                self.pending_position(p)
        self.untracked_positions()
        self.orphans()
        self.stops()
        return self.plan

    def open_position(self, p: Position) -> None:
        if p.symbol not in self.held:
            self.missing_open(p)
            return
        bp = self.held[p.symbol]
        updated = p
        if p.missing_cycles:
            self.report(
                DiscrepancyKind.POSITION_REAPPEARED,
                Severity.INFO,
                p.symbol,
                f"missing for {p.missing_cycles} run(s)",
                f"held {_fmt_qty(bp.qty)}",
                "missing counter reset (transient broker snapshot)",
            )
            updated = updated.evolve(missing_cycles=0)
        if bp.qty != p.qty:
            self.report(
                DiscrepancyKind.QUANTITY_MISMATCH,
                Severity.WARNING,
                p.symbol,
                f"qty {_fmt_qty(p.qty)}",
                f"qty {_fmt_qty(bp.qty)}",
                "local quantity set to broker quantity",
            )
            updated = updated.evolve(qty=bp.qty)
        if updated != p:
            self.act(UpdatePosition(updated))
        self.protect[p.symbol] = updated

    def missing_open(self, p: Position) -> None:
        fill = self.confirming_fill(p)
        if fill is not None:
            self.report(
                DiscrepancyKind.POSITION_CLOSED,
                Severity.INFO,
                p.symbol,
                f"open {_fmt_qty(p.qty)}",
                f"not held, SELL {_fmt_qty(fill.qty)} @ {fill.price:.2f}",
                "closed (exit confirmed by execution)",
            )
            self.act(ClosePosition(p, "exit execution found", fill.price, fill.qty))
            self.cancel_leftovers(p.symbol, "position closed, leftover order")
            return
        cycles = p.missing_cycles + 1
        needed = self.s.close_confirmation_cycles
        if self.destructive_allowed and cycles >= needed:
            self.report(
                DiscrepancyKind.POSITION_CLOSED,
                Severity.WARNING,
                p.symbol,
                f"open {_fmt_qty(p.qty)}",
                f"not held for {cycles} consecutive runs, no execution found",
                "closed (exit price unknown)",
            )
            self.act(ClosePosition(p, f"not reported by broker for {cycles} runs", None, None))
            self.cancel_leftovers(p.symbol, "position closed, leftover order")
            return
        self.report(
            DiscrepancyKind.POSITION_MISSING,
            Severity.WARNING,
            p.symbol,
            f"open {_fmt_qty(p.qty)}",
            "not reported",
            f"awaiting confirmation ({cycles}/{needed}); stop orders kept",
        )
        self.act(UpdatePosition(p.evolve(missing_cycles=cycles)))

    def pending_position(self, p: Position) -> None:
        entry_working = self.snap.has_open_entry(p.symbol)
        if p.symbol in self.held:
            bp = self.held[p.symbol]
            kind = (
                DiscrepancyKind.PENDING_PARTIALLY_FILLED
                if entry_working
                else DiscrepancyKind.PENDING_FILLED
            )
            self.report(
                kind,
                Severity.INFO,
                p.symbol,
                f"pending {_fmt_qty(p.qty)}",
                f"held {_fmt_qty(bp.qty)} @ {bp.avg_cost:.2f}",
                "marked open with broker quantity",
            )
            opened = p.evolve(
                status=PositionStatus.OPEN,
                qty=bp.qty,
                entry_price=bp.avg_cost,
                entry_date=self.snap.taken_at.date(),
                missing_cycles=0,
            )
            self.act(UpdatePosition(opened))
            self.protect[p.symbol] = opened
            return
        if entry_working:
            if p.missing_cycles:
                self.act(UpdatePosition(p.evolve(missing_cycles=0)))
            return
        cycles = p.missing_cycles + 1
        if self.destructive_allowed and cycles >= self.s.close_confirmation_cycles:
            self.report(
                DiscrepancyKind.PENDING_DROPPED,
                Severity.WARNING,
                p.symbol,
                f"pending {_fmt_qty(p.qty)}",
                "entry neither working nor filled",
                "pending entry removed",
            )
            self.act(ClosePosition(p, "entry neither working nor filled", None, None))
            # A stop without position would open a short if triggered.
            self.cancel_leftovers(p.symbol, "entry gone, leftover order")
            return
        self.report(
            DiscrepancyKind.PENDING_MISSING,
            Severity.WARNING,
            p.symbol,
            f"pending {_fmt_qty(p.qty)}",
            "entry neither working nor filled",
            f"awaiting confirmation ({cycles}/{self.s.close_confirmation_cycles})",
        )
        self.act(UpdatePosition(p.evolve(missing_cycles=cycles)))

    def untracked_positions(self) -> None:
        tracked = {p.symbol for p in self.d.local}
        for symbol, bp in sorted(self.held.items()):
            if symbol in tracked:
                continue
            stops = [o for o in self.snap.stops_for(symbol) if o.order_ref in self.d.order_refs]
            if not stops:
                self.report(
                    DiscrepancyKind.UNTRACKED_FOREIGN,
                    Severity.INFO,
                    symbol,
                    "not tracked",
                    f"held {_fmt_qty(bp.qty)}, no bot order",
                    "ignored (not managed by this strategy)",
                )
                continue
            stop = max(stops, key=lambda o: o.aux_price or 0.0)
            adopted = Position(
                symbol=symbol,
                block=self.d.order_refs[stop.order_ref],
                status=PositionStatus.OPEN,
                qty=bp.qty,
                stop=stop.aux_price or 0.0,
                signal_price=bp.avg_cost,
                entry_price=bp.avg_cost,
                entry_date=self.snap.taken_at.date(),
                stop_order_id=stop.order_id,
            )
            self.report(
                DiscrepancyKind.UNTRACKED_ADOPTED,
                Severity.WARNING,
                symbol,
                "not tracked",
                f"held {_fmt_qty(bp.qty)} with {stop.order_ref} stop @ {stop.aux_price}",
                f"adopted into block {int(adopted.block)}",
            )
            self.act(AdoptPosition(adopted))
            self.protect[symbol] = adopted

    def orphans(self) -> None:
        tracked = {p.symbol for p in self.d.local} | set(self.protect)
        cycles_out: dict[int, int] = {}
        for order in self.snap.orders:
            if order.symbol in self.held or order.symbol in tracked:
                continue
            if order.action is not OrderAction.SELL:
                self.report(
                    DiscrepancyKind.ORPHAN_ORDER,
                    Severity.INFO,
                    order.symbol,
                    "not tracked",
                    f"working BUY #{order.order_id}",
                    "left alone (fill will be adopted via its stop)",
                )
                continue
            cycles = self.d.orphan_cycles.get(order.order_id, 0) + 1
            if self.destructive_allowed and cycles >= self.s.orphan_confirmation_cycles:
                self.report(
                    DiscrepancyKind.ORPHAN_CANCELLED,
                    Severity.WARNING,
                    order.symbol,
                    "no position",
                    f"working SELL {order.order_type} #{order.order_id} without position",
                    "cancelled (would open a short)",
                )
                self.act(CancelOrder(order, "orphaned SELL order without position"))
            else:
                cycles_out[order.order_id] = cycles
                self.report(
                    DiscrepancyKind.ORPHAN_ORDER,
                    Severity.WARNING,
                    order.symbol,
                    "no position",
                    f"working SELL {order.order_type} #{order.order_id} without position",
                    f"awaiting confirmation ({cycles}/{self.s.orphan_confirmation_cycles})",
                )
        self.plan.orphan_cycles = cycles_out

    def stops(self) -> None:
        for symbol, p in sorted(self.protect.items()):
            qty = abs(self.held[symbol].qty)
            ref = self.d.block_refs[p.block]
            stops = self.snap.stops_for(symbol)
            low = self.d.lows.get(symbol)
            new_low = round(low, 2) if low is not None and math.isfinite(low) else None
            if not stops:
                price = max(new_low, p.stop) if new_low is not None else p.stop
                self.report(
                    DiscrepancyKind.MISSING_STOP,
                    Severity.WARNING,
                    symbol,
                    f"stop {p.stop}",
                    "no working stop order",
                    f"new stop SELL {_fmt_qty(qty)} @ {price}",
                )
                self.act(PlaceStop(symbol, p.block, qty, price, ref))
                continue
            if len(stops) > 1:
                self.report(
                    DiscrepancyKind.DUPLICATE_STOPS,
                    Severity.WARNING,
                    symbol,
                    f"stop #{p.stop_order_id}",
                    ", ".join(f"#{o.order_id} @ {o.aux_price}" for o in stops),
                    "kept all; managing the known/highest one",
                )
            stop = next(
                (o for o in stops if o.order_id == p.stop_order_id),
                max(stops, key=lambda o: o.aux_price or 0.0),
            )
            current = stop.aux_price or 0.0
            price, reasons = current, []
            if self.d.update_stops and new_low is not None and new_low > current:
                price = new_low
                reasons.append(f"stop {current} -> {new_low}")
            if stop.qty != qty:
                reasons.append(f"qty {_fmt_qty(stop.qty)} -> {_fmt_qty(qty)}")
                self.report(
                    DiscrepancyKind.STOP_QUANTITY_MISMATCH,
                    Severity.WARNING,
                    symbol,
                    f"position {_fmt_qty(qty)}",
                    f"stop #{stop.order_id} qty {_fmt_qty(stop.qty)}",
                    "stop quantity adjusted",
                )
            # Persist the expected local state first; the executor may still replace the stop id
            # afterwards (e.g. when TWS rejects the modification and a new stop is placed).
            synced = p.evolve(stop=price, stop_order_id=stop.order_id)
            if synced != p:
                self._merge_update(synced)
            if reasons:
                self.act(ModifyStop(stop, symbol, p.block, qty, price, ref, "; ".join(reasons)))

    def _merge_update(self, position: Position) -> None:
        """Replace an earlier UpdatePosition/AdoptPosition of the same symbol or add one."""
        for i, action in enumerate(self.plan.actions):
            if (
                isinstance(action, UpdatePosition | AdoptPosition)
                and action.position.symbol == position.symbol
            ):
                self.plan.actions[i] = type(action)(position)
                return
        self.act(UpdatePosition(position))


def plan_reconciliation(
    data: ReconciliationInput, settings: ReconciliationSettings
) -> ReconciliationPlan:
    """Compare local state with the broker snapshot and plan corrective actions.

    Args:
        data: Local positions, broker snapshot and auxiliary data.
        settings: Safety thresholds.

    Returns:
        The plan. An aborted plan contains no actions; a degraded plan only closes positions
        whose exit is proven by an execution.
    """
    return _Planner(data, settings).run()
