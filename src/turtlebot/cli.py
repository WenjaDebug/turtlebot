"""Command line interface: ``turtlebot <command>``.

Commands:
    run             Run the trading engine (``--dry-run`` sends no orders, ``--once`` scans once).
    report          Summary of positions, recent events and the last reconciliation.
    migrate-legacy  Import ``live_turtle_state.json`` and ``live_turtle_journal.csv``.
    dashboard       Serve the web dashboard (``--demo`` uses synthetic data).
    backtest        Run the portfolio backtest on cached/downloaded daily data.
    audit-orders    Read-only: list working orders not owned by the bot's client id.
    import-executions  Fill in unknown exit prices from an IBKR trade export (CSV).
    confirm-closed  Close a position you have verified as sold in TWS, without waiting for
                    the reconciliation's confirmation cycles.
    config          Print the effective configuration (secrets masked).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import logging
import os
import resource
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import IO

from turtlebot.config import Settings, get_settings
from turtlebot.domain.models import BrokerOrder, BrokerSnapshot
from turtlebot.exceptions import BrokerConnectionError, NotPaperAccountError, TurtleBotError
from turtlebot.logging_setup import configure_logging

log = logging.getLogger("turtlebot.cli")

FILE_LIMIT_TARGET = 10_240  # yfinance needs ~300-500 handles for threaded downloads
EXIT_REFUSED = 2


# ------------------------------------------------------------------------------ helpers
def raise_file_limit() -> bool:
    """Raise the open-file limit; returns whether threaded downloads are safe."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = FILE_LIMIT_TARGET if hard == resource.RLIM_INFINITY else min(hard, FILE_LIMIT_TARGET)
    if soft < target:
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    log.info("Open-file limit: %d", soft)
    return soft >= 2048


@contextlib.contextmanager
def client_id_lock(lock_dir: Path, candidates: Sequence[int]) -> Iterator[int]:
    """Hold an exclusive lock file for the first free client id.

    Prevents two bot instances from sharing a TWS client id (only the owning client id may modify
    its own stop orders).

    Raises:
        TurtleBotError: If all candidate ids are taken.
    """
    lock_dir.mkdir(parents=True, exist_ok=True)
    for cid in candidates:
        handle: IO[str] = (lock_dir / f"client_{cid}.lock").open("w")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            continue
        handle.write(f"pid={os.getpid()} since={dt.datetime.now(dt.UTC).isoformat()}\n")
        handle.flush()
        try:
            yield cid
        finally:
            handle.close()
        return
    raise TurtleBotError(f"all client ids {list(candidates)} are in use by running instances")


def _db_path(settings: Settings, dry_run: bool) -> Path:
    path = settings.paths.db_path
    return path.with_name(f"{path.stem}_dryrun{path.suffix}") if dry_run else path


# ------------------------------------------------------------------------------ commands
def cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    """Run the engine against TWS."""
    from turtlebot.broker.gateway import BrokerGateway, DryRunGateway
    from turtlebot.broker.ibkr import IBKRGateway
    from turtlebot.data.yahoo import YahooMarketData
    from turtlebot.engine.engine import TradingEngine
    from turtlebot.notify import default_notifier
    from turtlebot.persistence.store import StateStore
    from turtlebot.timeutil import system_clock

    if args.max_entries is not None:
        settings.risk.max_new_entries_per_day = args.max_entries
    if not args.dry_run and not settings.trading_enabled:
        sys.stderr.write(
            "Refusing to trade: TURTLE_TRADING_ENABLED is false.\n"
            "Use --dry-run, or set TURTLE_TRADING_ENABLED=true once you have approved a restart.\n"
        )
        return EXIT_REFUSED
    if args.reconcile_only and not args.dry_run:
        sys.stderr.write("--reconcile-only is only available with --dry-run.\n")
        return EXIT_REFUSED

    candidates = settings.broker.dry_run_client_ids if args.dry_run else [settings.broker.client_id]
    with client_id_lock(settings.paths.lock_dir, candidates) as client_id:
        threads = raise_file_limit()
        store = StateStore(_db_path(settings, args.dry_run))
        ibkr = IBKRGateway(
            settings.broker,
            client_id,
            list(settings.order_refs),
            readonly=args.dry_run,
            owner_client_id=settings.broker.client_id,
        )
        gateway: BrokerGateway = DryRunGateway(ibkr) if args.dry_run else ibkr
        engine = TradingEngine(
            settings,
            gateway,
            store,
            YahooMarketData(settings.paths.cache_dir, threads=threads),
            default_notifier(),
            system_clock(settings.schedule.tz),
            dry_run=args.dry_run,
        )
        try:
            if args.reconcile_only:
                engine.start()
            elif args.once:
                engine.run_once()
            else:
                engine.run_forever()
        except KeyboardInterrupt:
            log.info("Manual stop (Ctrl+C). Open positions remain protected by their stop orders.")
        except NotPaperAccountError as exc:
            log.critical("ABORT: %s", exc)
            return EXIT_REFUSED
        finally:
            gateway.disconnect()
            store.close()
    return 0


def foreign_orders(orders: Sequence[BrokerOrder], bot_client_id: int) -> list[BrokerOrder]:
    """Orders the live bot cannot modify: placed by another client id or without a bot ref."""
    return [o for o in orders if o.client_id != bot_client_id]


def cmd_audit_orders(args: argparse.Namespace, settings: Settings) -> int:
    """List working orders that the bot's client id does not own (read-only TWS session).

    Prints nothing but an error if the order list may be incomplete: a dropped connection
    before ``openOrderEnd``, an unexpected disconnect during the session or an unsynced
    position stream.
    """
    from turtlebot.broker.ibkr import IBKRGateway

    with client_id_lock(settings.paths.lock_dir, settings.broker.dry_run_client_ids) as cid:
        gateway = IBKRGateway(settings.broker, cid, list(settings.order_refs), readonly=True)
        gateway.connect()
        try:
            snapshot = gateway.snapshot()
        except (ConnectionError, OSError, TimeoutError) as exc:
            raise BrokerConnectionError(
                f"audit incomplete, order request interrupted: {exc}"
            ) from exc
        finally:
            dropped = gateway.unexpected_disconnects
            gateway.disconnect()
    return write_audit(snapshot, settings.broker.client_id, dropped, sys.stdout)


def write_audit(snapshot: BrokerSnapshot, bot_client_id: int, dropped: int, out: IO[str]) -> int:
    """Print the audit table; refuses (exit code 1) if the snapshot may be incomplete."""
    if dropped or not snapshot.positions_synced:
        log.error(
            "Audit incomplete (unexpected disconnects: %d, positions synced: %s) - "
            "no order list printed, run it again",
            dropped,
            snapshot.positions_synced,
        )
        return 1
    foreign = foreign_orders(snapshot.orders, bot_client_id)
    orphans = [o for o in snapshot.orders if o.is_stop and o.symbol not in snapshot.positions]
    out.write(
        f"Snapshot {snapshot.taken_at:%Y-%m-%d %H:%M:%S} UTC: connection stable, "
        f"{len(snapshot.positions)} positions, {len(snapshot.orders)} relevant working orders "
        f"(all client ids)\n"
    )
    out.write(f"Bot client id: {bot_client_id}\n")
    out.write(
        f"{'symbol':8s} {'order':>7s} {'perm id':>11s} {'client':>6s} {'type':5s} "
        f"{'qty':>8s} {'stop':>9s}  ref\n"
    )
    for o in sorted(foreign, key=lambda o: (o.symbol, o.order_id)):
        out.write(
            f"{o.symbol:8s} {o.order_id:>7d} {o.perm_id or 0:>11d} {o.client_id!s:>6s} "
            f"{o.order_type:5s} {o.qty:>8g} {o.aux_price if o.aux_price is not None else '-':>9}  "
            f"{o.order_ref or '-'}\n"
        )
    out.write(f"\n{len(foreign)} working order(s) not owned by client id {bot_client_id}.\n")
    out.write(f"{len(orphans)} working stop(s) without a held position (short risk)")
    out.write(
        ":\n" + "".join(f"  {o.symbol} #{o.order_id} (client {o.client_id})\n" for o in orphans)
        if orphans
        else ".\n"
    )
    return 0


def cmd_report(args: argparse.Namespace, settings: Settings) -> int:
    """Print a summary from the state store."""
    from turtlebot.persistence.store import StateStore

    store = StateStore(_db_path(settings, args.dry_run))
    out = sys.stdout
    positions = store.active_positions()
    out.write(f"===== Active positions: {len(positions)} =====\n")
    for p in sorted(positions, key=lambda p: (p.block, p.symbol)):
        out.write(
            f"  Block {int(p.block)}  {p.symbol:6s} {p.status.value:8s} {p.qty:>7.0f}  "
            f"entry {p.entry_price if p.entry_price is not None else '-':>9}  stop {p.stop}"
            f"{'  MISSING x' + str(p.missing_cycles) if p.missing_cycles else ''}\n"
        )
    out.write("\n===== Recent events =====\n")
    for e in reversed(store.events(limit=args.events, kinds=None)):
        if e.kind == "SIGNAL":
            continue
        out.write(
            f"  {e.ts:%Y-%m-%d %H:%M} {e.kind:12s} {e.symbol or '':6s} {e.qty or ''} "
            f"{e.price or ''}\n"
        )
    runs = store.reconciliations(limit=1)
    if runs:
        run = runs[0]
        out.write(f"\n===== Last reconciliation {run.ts:%Y-%m-%d %H:%M}: {run.outcome} =====\n")
        for d in run.discrepancies:
            out.write(f"  [{d.severity}] {d.kind} {d.symbol or ''}: {d.resolution}\n")
    store.close()
    return 0


def cmd_migrate(args: argparse.Namespace, settings: Settings) -> int:
    """Import the legacy JSON state and CSV journal."""
    from turtlebot.migration import migrate_legacy
    from turtlebot.persistence.store import StateStore

    store = StateStore(args.db or settings.paths.db_path)
    try:
        report = migrate_legacy(store, args.state, args.journal, force=args.force)
    finally:
        store.close()
    sys.stdout.write(f"{report}\n")
    return 0


def cmd_import_executions(args: argparse.Namespace, settings: Settings) -> int:
    """Backfill exit prices from an IBKR trade export."""
    from turtlebot.executions import backfill_exit_prices, load_trade_export
    from turtlebot.persistence.store import StateStore

    executions = load_trade_export(args.csv)
    store = StateStore(_db_path(settings, args.dry_run))
    try:
        results = backfill_exit_prices(
            store, executions, apply=args.apply, now=dt.datetime.now(dt.UTC)
        )
    finally:
        store.close()
    out = sys.stdout
    out.write(f"{len(executions)} executions read, {len(results)} closed positions checked\n")
    for r in results:
        price = f"{r.exit_price:.4f}" if r.exit_price is not None else "-"
        out.write(f"  {r.symbol:6s} {r.status:13s} {price:>10s}  {r.detail}\n")
    if not args.apply and any(r.status == "would_update" for r in results):
        out.write("Nothing written. Re-run with --apply to store these prices.\n")
    return 0


def cmd_confirm_closed(args: argparse.Namespace, settings: Settings) -> int:
    """Close an active position that was verified as sold in TWS."""
    from turtlebot.persistence.store import StateStore

    store = StateStore(_db_path(settings, args.dry_run))
    try:
        p = store.active_position(args.symbol.upper())
        if p is None:
            sys.stderr.write(f"{args.symbol.upper()}: no active position\n")
            return 1
        if not args.yes:
            sys.stderr.write(
                f"{p.symbol}: block {int(p.block)}, {p.qty:g} shares, stop {p.stop}. "
                "Only confirm if TWS shows no position. Re-run with --yes to close it.\n"
            )
            return EXIT_REFUSED
        now = dt.datetime.now(dt.UTC)
        closed = store.close_position(
            p, reason="confirmed closed manually", exit_price=args.price, closed_at=now
        )
        store.record_event(
            "MANUAL_CLOSE",
            ts=now,
            symbol=p.symbol,
            block=p.block,
            qty=p.qty,
            price=args.price,
            details={"missing_cycles": p.missing_cycles},
        )
    finally:
        store.close()
    sys.stdout.write(f"{closed.symbol}: closed (confirmed manually)\n")
    return 0


def cmd_config(args: argparse.Namespace, settings: Settings) -> int:
    """Print the effective configuration as JSON."""
    sys.stdout.write(json.dumps(settings.model_dump(mode="json"), indent=2) + "\n")
    return 0


def cmd_dashboard(args: argparse.Namespace, settings: Settings) -> int:
    """Serve the dashboard."""
    import uvicorn

    from turtlebot.dashboard.app import create_app
    from turtlebot.dashboard.demo import demo_benchmark, demo_phases, seed_demo_store
    from turtlebot.dashboard.quotes import StaticBenchmark, StaticQuotes

    if args.demo:
        db = settings.paths.data_dir / "demo.db"
        demo_settings = settings.model_copy(
            update={
                "dashboard": settings.dashboard.model_validate(
                    {**settings.dashboard.model_dump(), "phases": demo_phases()}
                )
            }
        )
        app = create_app(
            demo_settings,
            db_path=db,
            quotes=StaticQuotes(seed_demo_store(db)),
            benchmark=StaticBenchmark(demo_benchmark()),
            demo=True,
        )
    else:
        app = create_app(settings, db_path=settings.paths.db_path)
    uvicorn.run(
        app, host=args.host or settings.dashboard.host, port=args.port or settings.dashboard.port
    )
    return 0


def cmd_backtest(args: argparse.Namespace, settings: Settings) -> int:
    """Run the portfolio backtest."""
    from turtlebot.backtest.engine import BacktestConfig, run_backtest
    from turtlebot.data.yahoo import YahooMarketData

    tickers = json.loads(Path(args.universe).read_text())["tickers"]
    if args.limit:
        tickers = tickers[: args.limit]
    provider = YahooMarketData(settings.paths.cache_dir, threads=raise_file_limit())
    prices = provider.daily_bars(tickers, args.start, args.end)
    result = run_backtest(prices, settings, BacktestConfig(initial_capital=args.capital))
    sys.stdout.write(json.dumps(result.metrics.as_dict(), indent=2) + "\n")
    if args.trades_csv:
        result.trades_frame().to_csv(args.trades_csv, index=False)
    return 0


# ------------------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    """Argument parser of the CLI."""
    parser = argparse.ArgumentParser(prog="turtlebot", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the trading engine")
    run.add_argument("--dry-run", action="store_true", help="never send orders")
    run.add_argument("--once", action="store_true", help="reconcile + one scan, then exit")
    run.add_argument(
        "--reconcile-only",
        action="store_true",
        help="with --dry-run: reconcile (incl. a missed stop update), no scan, then exit",
    )
    run.add_argument("--max-entries", type=int, help="override new entries per day and block")
    run.set_defaults(func=cmd_run)

    report = sub.add_parser("report", help="summary from the state store")
    report.add_argument("--dry-run", action="store_true", help="read the dry-run database")
    report.add_argument("--events", type=int, default=40)
    report.set_defaults(func=cmd_report)

    mig = sub.add_parser("migrate-legacy", help="import legacy JSON state and CSV journal")
    mig.add_argument("--state", type=Path, default=Path("live_turtle_state.json"))
    mig.add_argument("--journal", type=Path, default=Path("live_turtle_journal.csv"))
    mig.add_argument("--db", type=Path, help="target database (default: configured db)")
    mig.add_argument("--force", action="store_true", help="import into a non-empty database")
    mig.set_defaults(func=cmd_migrate)

    dash = sub.add_parser("dashboard", help="serve the web dashboard")
    dash.add_argument("--demo", action="store_true", help="synthetic data, no account needed")
    dash.add_argument("--host")
    dash.add_argument("--port", type=int)
    dash.set_defaults(func=cmd_dashboard)

    bt = sub.add_parser("backtest", help="portfolio backtest on daily data")
    bt.add_argument("--universe", required=True, help="eligible_YYYYMMDD.json with a tickers list")
    bt.add_argument("--start", type=dt.date.fromisoformat, required=True)
    bt.add_argument("--end", type=dt.date.fromisoformat, required=True)
    bt.add_argument("--capital", type=float, default=100_000.0)
    bt.add_argument("--limit", type=int, help="only the first N tickers")
    bt.add_argument("--trades-csv", type=Path)
    bt.set_defaults(func=cmd_backtest)

    audit = sub.add_parser(
        "audit-orders", help="read-only: list working orders not owned by the bot's client id"
    )
    audit.set_defaults(func=cmd_audit_orders)

    imp = sub.add_parser(
        "import-executions", help="fill in unknown exit prices from an IBKR trade export"
    )
    imp.add_argument("csv", type=Path, help="Flex Query or Activity Statement CSV")
    imp.add_argument("--apply", action="store_true", help="write the prices (default: preview)")
    imp.add_argument("--dry-run", action="store_true", help="use the dry-run database")
    imp.set_defaults(func=cmd_import_executions)

    conf = sub.add_parser("confirm-closed", help="close a position you verified as sold in TWS")
    conf.add_argument("symbol")
    conf.add_argument("--price", type=float, help="exit price, if known")
    conf.add_argument("--yes", action="store_true", help="really close it")
    conf.add_argument("--dry-run", action="store_true", help="use the dry-run database")
    conf.set_defaults(func=cmd_confirm_closed)

    cfg = sub.add_parser("config", help="print the effective configuration")
    cfg.set_defaults(func=cmd_config)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of the ``turtlebot`` command."""
    args = build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log, settings.paths.log_dir if args.command == "run" else None)
    try:
        return int(args.func(args, settings))
    except TurtleBotError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
