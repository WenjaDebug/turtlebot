"""Execute reconciliation plans against the broker and the state store."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from turtlebot.broker.gateway import BrokerGateway
from turtlebot.config import Settings
from turtlebot.domain.models import BrokerOrder, BrokerSnapshot, OrderAction, Position
from turtlebot.exceptions import BrokerError, OrderRejectedError, TurtleBotError
from turtlebot.notify import Notifier
from turtlebot.persistence.store import DiscrepancyRecord, StateStore
from turtlebot.reconciliation.planner import (
    Action,
    AdoptPosition,
    CancelOrder,
    ClosePosition,
    ModifyStop,
    Outcome,
    PlaceStop,
    ReconciliationInput,
    ReconciliationPlan,
    Severity,
    UpdatePosition,
    plan_reconciliation,
    symbols_needing_lows,
)
from turtlebot.timeutil import Clock

log = logging.getLogger(__name__)

type LowsLoader = Callable[[Sequence[str]], dict[str, float]]
"""Loads the lowest low of the last ``exit_period`` completed days per symbol."""

LAST_STOP_UPDATE_KEY = "last_stop_update"
_LEVEL = {Severity.INFO: logging.INFO, Severity.WARNING: logging.WARNING}


class ReconciliationService:
    """Compare the state store with the broker and apply the resulting plan.

    Args:
        gateway: Broker adapter (real or dry-run).
        store: Local source of truth.
        load_lows: Callback returning recent lows for stop placement/trailing.
        settings: Application settings.
        notifier: Alert channel for aborted/degraded runs.
        clock: Time source.
    """

    def __init__(
        self,
        gateway: BrokerGateway,
        store: StateStore,
        load_lows: LowsLoader,
        settings: Settings,
        notifier: Notifier,
        clock: Clock,
    ) -> None:
        self.gateway = gateway
        self.store = store
        self.load_lows = load_lows
        self.settings = settings
        self.notifier = notifier
        self.clock = clock

    def run(self, *, update_stops: bool = False) -> ReconciliationPlan:
        """Run one reconciliation cycle.

        Args:
            update_stops: Trail stops up to the latest 10-day low (morning job).

        Returns:
            The executed plan.
        """
        snapshot = self.gateway.snapshot()
        local = self.store.active_positions()
        missing = [p.symbol for p in local if p.symbol not in snapshot.positions]
        fills = (
            self.gateway.recent_sell_fills(missing) if missing and snapshot.positions_synced else {}
        )

        lows: dict[str, float] = {}
        need = (
            symbols_needing_lows(local, snapshot, update_stops) if snapshot.positions_synced else []
        )
        if need:
            try:
                lows = self.load_lows(need)
            except TurtleBotError as exc:
                # Without lows stops are still placed at their last known level, just not trailed.
                log.warning("Recent lows unavailable (%s); stops are not trailed this run", exc)

        plan = plan_reconciliation(
            ReconciliationInput(
                local=local,
                snapshot=snapshot,
                sell_fills=fills,
                lows=lows,
                update_stops=update_stops,
                order_refs=self.settings.order_refs,
                block_refs={b.block: b.order_ref for b in self.settings.blocks},
                orphan_cycles=self.store.orphan_cycles(),
            ),
            self.settings.reconciliation,
        )
        self._log(plan)
        if plan.outcome is not Outcome.ABORTED:
            for action in plan.actions:
                self._apply_safely(action, snapshot, plan)
            self.store.replace_orphan_cycles(
                plan.orphan_cycles,
                {o.order_id: o.symbol for o in snapshot.orders},
                self.clock(),
            )
        self._persist(plan, update_stops, len(local), len(snapshot.positions))
        if plan.outcome is not Outcome.OK:
            self.notifier.notify(f"Turtle: reconciliation {plan.outcome.value}", plan.message)
        elif update_stops:
            self.store.set_meta(LAST_STOP_UPDATE_KEY, self.clock().isoformat(timespec="seconds"))
        return plan

    # ------------------------------------------------------------------ internals
    def _log(self, plan: ReconciliationPlan) -> None:
        for d in plan.discrepancies:
            log.log(
                _LEVEL.get(d.severity, logging.ERROR),
                "Reconciliation: %s %s | local: %s | broker: %s | -> %s",
                d.kind.value,
                d.symbol or "*",
                d.local,
                d.broker,
                d.resolution,
                extra={"ctx": {"event": "reconcile.discrepancy", "kind": d.kind.value}},
            )
        log.info(
            "Reconciliation %s: %d discrepancies, %d actions%s",
            plan.outcome.value,
            len(plan.discrepancies),
            len(plan.actions),
            f" ({plan.message})" if plan.message else "",
        )

    def _persist(
        self, plan: ReconciliationPlan, update_stops: bool, n_local: int, n_broker: int
    ) -> None:
        self.store.record_reconciliation(
            ts=self.clock(),
            outcome=plan.outcome.value,
            update_stops=update_stops,
            n_local=n_local,
            n_broker=n_broker,
            n_actions=len(plan.actions) if plan.outcome is not Outcome.ABORTED else 0,
            message=plan.message or None,
            discrepancies=[
                DiscrepancyRecord(
                    kind=d.kind.value,
                    severity=d.severity.value,
                    symbol=d.symbol,
                    local=d.local,
                    broker=d.broker,
                    resolution=d.resolution,
                )
                for d in plan.discrepancies
            ],
        )

    def _apply_safely(
        self, action: Action, snapshot: BrokerSnapshot, plan: ReconciliationPlan
    ) -> None:
        # One failing symbol must never block the reconciliation of the others.
        try:
            self._apply(action, snapshot, plan)
        except (TurtleBotError, OSError):
            log.exception("Reconciliation action failed: %s", action)

    def _apply(self, action: Action, snapshot: BrokerSnapshot, plan: ReconciliationPlan) -> None:
        now = self.clock()
        match action:
            case UpdatePosition(position):
                self.store.update_position(position)
            case AdoptPosition(position):
                adopted = self.store.insert_position(position)
                self.store.record_event(
                    "ADOPT",
                    ts=now,
                    symbol=adopted.symbol,
                    block=adopted.block,
                    qty=adopted.qty,
                    price=adopted.entry_price,
                    stop=adopted.stop,
                )
            case ClosePosition(position, reason, exit_price, exit_qty):
                self.store.close_position(
                    position, reason=reason, exit_price=exit_price, closed_at=now
                )
                self.store.record_event(
                    "EXIT",
                    ts=now,
                    symbol=position.symbol,
                    block=position.block,
                    qty=exit_qty if exit_qty is not None else position.qty,
                    price=exit_price,
                    details={"source": "reconciliation", "reason": reason},
                )
            case CancelOrder(order, reason):
                self._cancel(order, reason, snapshot, plan)
            case PlaceStop(symbol, block, qty, price, ref):
                order_id = self.gateway.place_stop(symbol, qty, price, ref)
                self._sync_stop(symbol, price, order_id)
                self.store.record_event(
                    "STOP_NEW",
                    ts=now,
                    symbol=symbol,
                    block=block,
                    qty=qty,
                    stop=price,
                    order_id=order_id,
                )
            case ModifyStop(order, symbol, block, qty, price, ref, reason):
                try:
                    self.gateway.modify_stop(order, qty, price)
                except OrderRejectedError as exc:
                    # Never leave the position unprotected: replace the dead order right away.
                    log.warning(
                        "%s: stop update of #%d rejected (%s) -> placing a replacement stop",
                        symbol,
                        order.order_id,
                        exc,
                    )
                    new_id = self.gateway.place_stop(symbol, qty, price, ref)
                    self._sync_stop(symbol, price, new_id)
                    self.store.record_event(
                        "STOP_NEW",
                        ts=now,
                        symbol=symbol,
                        block=block,
                        qty=qty,
                        stop=price,
                        order_id=new_id,
                        details={"replaces": order.order_id},
                    )
                    return
                self._sync_stop(symbol, price, order.order_id)
                self.store.record_event(
                    "STOP_UPDATE",
                    ts=now,
                    symbol=symbol,
                    block=block,
                    qty=qty,
                    stop=price,
                    order_id=order.order_id,
                    details={"change": reason},
                )

    def _sync_stop(self, symbol: str, price: float, order_id: int | None) -> None:
        position: Position | None = self.store.active_position(symbol)
        if position is not None:
            self.store.update_position(position.evolve(stop=price, stop_order_id=order_id))

    def _cancel(
        self, order: BrokerOrder, reason: str, snapshot: BrokerSnapshot, plan: ReconciliationPlan
    ) -> None:
        # Defence in depth on top of the planner's rules: never strip the protection of a
        # position the broker reports as held, and never cancel anything in a suspect run.
        if order.action is OrderAction.SELL and order.symbol in snapshot.positions:
            log.critical(
                "%s: refusing to cancel SELL #%d - the broker still reports the position",
                order.symbol,
                order.order_id,
            )
            return
        if plan.outcome is not Outcome.OK and not self._exit_confirmed(order.symbol, plan):
            log.critical(
                "%s: refusing to cancel #%d during a %s reconciliation",
                order.symbol,
                order.order_id,
                plan.outcome.value,
            )
            return
        try:
            self.gateway.cancel_order(order)
        except BrokerError:
            log.exception("%s: cancel of #%d failed", order.symbol, order.order_id)
            return
        self.store.record_event(
            "CANCEL",
            ts=self.clock(),
            symbol=order.symbol,
            qty=order.qty,
            stop=order.aux_price,
            order_id=order.order_id,
            details={"reason": reason, "order_type": order.order_type},
        )

    @staticmethod
    def _exit_confirmed(symbol: str, plan: ReconciliationPlan) -> bool:
        return any(
            isinstance(a, ClosePosition)
            and a.position.symbol == symbol
            and a.exit_price is not None
            for a in plan.actions
        )
