"""Command line interface: ``turtlebot <command>``.

Commands:
    run             Run the trading engine (``--dry-run`` sends no orders, ``--once`` scans once).
    report          Summary of positions, recent events and the last reconciliation.
    migrate-legacy  Import ``live_turtle_state.json`` and ``live_turtle_journal.csv``.
    dashboard       Serve the web dashboard (``--demo`` uses synthetic data).
    backtest        Run the portfolio backtest on cached/downloaded daily data.
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
from turtlebot.exceptions import NotPaperAccountError, TurtleBotError
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

    candidates = settings.broker.dry_run_client_ids if args.dry_run else [settings.broker.client_id]
    with client_id_lock(settings.paths.lock_dir, candidates) as client_id:
        threads = raise_file_limit()
        store = StateStore(_db_path(settings, args.dry_run))
        ibkr = IBKRGateway(settings.broker, client_id, list(settings.order_refs))
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
            if args.once:
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


def cmd_config(args: argparse.Namespace, settings: Settings) -> int:
    """Print the effective configuration as JSON."""
    sys.stdout.write(json.dumps(settings.model_dump(mode="json"), indent=2) + "\n")
    return 0


def cmd_dashboard(args: argparse.Namespace, settings: Settings) -> int:
    """Serve the dashboard."""
    import uvicorn

    from turtlebot.dashboard.app import create_app
    from turtlebot.dashboard.demo import seed_demo_store
    from turtlebot.dashboard.quotes import StaticQuotes

    if args.demo:
        db = settings.paths.data_dir / "demo.db"
        app = create_app(settings, db_path=db, quotes=StaticQuotes(seed_demo_store(db)), demo=True)
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
