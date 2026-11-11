"""Exit accounting: combined partial fills and the trade-export backfill."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from tests.factories import position
from turtlebot import cli
from turtlebot.config import get_settings
from turtlebot.domain.models import Fill, OrderAction, combine_order_fills
from turtlebot.executions import (
    EXPORT_TZ,
    ExecutionImportError,
    backfill_exit_prices,
    parse_trade_export,
)
from turtlebot.persistence.store import StateStore

UTC = dt.UTC


def fill(qty: float, price: float, minute: int, order_id: int | None = 7) -> Fill:
    return Fill(
        symbol="MTUS",
        action=OrderAction.SELL,
        qty=qty,
        price=price,
        time=dt.datetime(2026, 11, 3, 10, minute, tzinfo=UTC),
        order_id=order_id,
    )


# ------------------------------------------------------------------------------ partial fills
def test_partial_fills_of_one_stop_are_combined() -> None:
    # The 03.11. case: a stop for 848 shares reported as 100 + 748 shares.
    combined = combine_order_fills([fill(100, 19.50, 1), fill(748, 19.40, 2)])
    assert combined is not None
    assert combined.qty == 848
    assert combined.price == pytest.approx((100 * 19.50 + 748 * 19.40) / 848)
    assert combined.time.minute == 2


def test_fills_of_an_older_order_are_ignored() -> None:
    combined = combine_order_fills([fill(50, 25.0, 1, order_id=3), fill(100, 19.5, 5)])
    assert combined is not None
    assert (combined.qty, combined.price, combined.order_id) == (100, 19.5, 7)


def test_empty_and_unidentified_fills() -> None:
    assert combine_order_fills([]) is None
    lone = fill(10, 5.0, 1, order_id=None)
    assert combine_order_fills([fill(3, 4.0, 0, order_id=None), lone]) == lone


# ------------------------------------------------------------------------------ parsing
FLEX = """Symbol,Buy/Sell,Quantity,TradePrice,DateTime
CEG,SELL,-60,281.10,20261020;115349
HTZ,SELL,-8698,6.12,20261019;093142
BRK B,BUY,5,410.00,20261021;100000
"""

STATEMENT = """Statement,Header,Field Name,Field Value
Statement,Data,Title,Activity Statement
Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,Quantity,T. Price,C. Price
Trades,Data,Order,Stocks,USD,XP,"2026-10-26, 10:22:22",-400,17.80,17.75
Trades,Data,Order,Stocks,USD,XP,"2026-10-26, 10:23:01",-238,17.70,17.75
Trades,SubTotal,,Stocks,USD,XP,,-638,,
"""


def test_parse_flex_query() -> None:
    rows = parse_trade_export(FLEX)
    assert [(r.symbol, r.side, r.qty) for r in rows] == [
        ("CEG", "SELL", 60),
        ("HTZ", "SELL", 8698),
        ("BRK-B", "BUY", 5),
    ]
    assert rows[0].time == dt.datetime(2026, 10, 20, 11, 53, 49, tzinfo=EXPORT_TZ)


def test_parse_activity_statement_reads_only_trade_rows() -> None:
    rows = parse_trade_export(STATEMENT)
    assert [(r.symbol, r.side, r.qty, r.price) for r in rows] == [
        ("XP", "SELL", 400, 17.80),
        ("XP", "SELL", 238, 17.70),
    ]


def test_parse_rejects_files_without_trade_columns() -> None:
    with pytest.raises(ExecutionImportError):
        parse_trade_export("a,b,c\n1,2,3\n")
    with pytest.raises(ExecutionImportError):
        parse_trade_export("")


# ------------------------------------------------------------------------------ backfill
def closed(store: StateStore, symbol: str, qty: float, reason: str, price: float | None) -> None:
    p = store.insert_position(position(symbol, qty=qty))
    store.close_position(
        p, reason=reason, exit_price=price, closed_at=dt.datetime(2026, 11, 2, 15, tzinfo=UTC)
    )


def test_backfill_updates_only_complete_matches(store: StateStore) -> None:
    closed(store, "XP", 638, "not reported by broker for 13 runs", None)
    closed(store, "CEG", 100, "not reported by broker for 13 runs", None)  # only 60 sold
    closed(store, "VST", 112, "not reported by broker for 13 runs", None)  # nothing in export
    closed(store, "SIG", 150, "STP filled", 101.0)  # already exact, never touched
    executions = parse_trade_export(STATEMENT) + parse_trade_export(FLEX)
    now = dt.datetime(2026, 11, 12, tzinfo=UTC)

    preview = backfill_exit_prices(store, executions, apply=False, now=now)
    assert {r.symbol: r.status for r in preview} == {
        "XP": "would_update",
        "CEG": "qty_mismatch",
        "VST": "no_executions",
    }
    assert all(p.exit_price is None for p in store.closed_positions() if p.symbol == "XP")

    applied = backfill_exit_prices(store, executions, apply=True, now=now)
    xp = next(p for p in store.closed_positions() if p.symbol == "XP")
    assert next(r for r in applied if r.symbol == "XP").status == "updated"
    assert xp.exit_price == pytest.approx((400 * 17.80 + 238 * 17.70) / 638)
    assert (xp.close_reason or "").startswith("exit price from trade")
    assert [e.kind for e in store.events(kinds=["EXIT_PRICE_BACKFILL"])] == ["EXIT_PRICE_BACKFILL"]
    # A second run finds nothing left to do for XP.
    again = backfill_exit_prices(store, executions, apply=True, now=now)
    assert "XP" not in {r.symbol for r in again}


def test_backfill_recomputes_exits_taken_from_a_single_partial_fill(store: StateStore) -> None:
    closed(store, "XP", 638, "exit execution found", 17.80)  # only the first 400 shares were seen
    results = backfill_exit_prices(
        store, parse_trade_export(STATEMENT), apply=True, now=dt.datetime.now(UTC)
    )
    assert results[0].status == "updated"
    assert store.closed_positions()[0].exit_price == pytest.approx(17.7627, abs=1e-4)


# ------------------------------------------------------------------------------ CLI
@pytest.fixture
def cli_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StateStore:
    monkeypatch.setenv("TURTLE_PATHS__DATA_DIR", str(tmp_path / "var"))
    get_settings.cache_clear()
    return StateStore(get_settings().paths.db_path)


def test_cli_import_executions_previews_then_applies(
    cli_store: StateStore, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    closed(cli_store, "XP", 638, "not reported by broker for 13 runs", None)
    cli_store.close()
    export = tmp_path / "trades.csv"
    export.write_text(STATEMENT)

    assert cli.main(["import-executions", str(export)]) == 0
    assert "Re-run with --apply" in capsys.readouterr().out
    assert cli.main(["import-executions", str(export), "--apply"]) == 0
    assert "updated" in capsys.readouterr().out
    store = StateStore(get_settings().paths.db_path)
    assert store.closed_positions()[0].exit_price is not None
    store.close()
    get_settings.cache_clear()


def test_cli_confirm_closed_requires_yes(
    cli_store: StateStore, capsys: pytest.CaptureFixture[str]
) -> None:
    cli_store.insert_position(position("HTZ", qty=8698, missing_cycles=4))
    cli_store.close()

    assert cli.main(["confirm-closed", "htz"]) == cli.EXIT_REFUSED
    assert "--yes" in capsys.readouterr().err
    assert cli.main(["confirm-closed", "HTZ", "--price", "6.12", "--yes"]) == 0
    assert cli.main(["confirm-closed", "HTZ", "--yes"]) == 1  # nothing active any more

    store = StateStore(get_settings().paths.db_path)
    (p,) = store.closed_positions()
    assert (p.close_reason, p.exit_price) == ("confirmed closed manually", 6.12)
    assert [e.kind for e in store.events(kinds=["MANUAL_CLOSE"])] == ["MANUAL_CLOSE"]
    store.close()
    get_settings.cache_clear()
