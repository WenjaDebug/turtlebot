"""``audit-orders`` must never print a list that a dropped connection could have truncated."""

from __future__ import annotations

import datetime as dt
import io
import logging

import pytest

from turtlebot.broker.ibkr import IBKRGateway
from turtlebot.cli import write_audit
from turtlebot.config import BrokerSettings
from turtlebot.domain.models import BrokerOrder, BrokerPosition, BrokerSnapshot, OrderAction

BOT = 11


def stop(symbol: str, oid: int, client_id: int = 50) -> BrokerOrder:
    return BrokerOrder(
        oid, symbol, OrderAction.SELL, "STP", 10, "TURTLE", aux_price=1.0, client_id=client_id
    )


def snapshot(*, synced: bool = True) -> BrokerSnapshot:
    return BrokerSnapshot(
        positions={"SHOP": BrokerPosition("SHOP", 10, 140.0)},
        orders=[stop("SHOP", 24), stop("SPCX", 22), stop("CIEN", 34, client_id=BOT)],
        positions_synced=synced,
        taken_at=dt.datetime(2026, 10, 17, 19, 45, tzinfo=dt.UTC),
        client_id=21,
    )


def test_audit_lists_foreign_and_orphaned_stops() -> None:
    out = io.StringIO()
    assert write_audit(snapshot(), BOT, 0, out) == 0
    text = out.getvalue()
    assert "connection stable" in text
    assert "2 working order(s) not owned by client id 11" in text
    assert "2 working stop(s) without a held position" in text
    assert "SPCX #22 (client 50)" in text
    assert "CIEN #34 (client 11)" in text


@pytest.mark.parametrize(("dropped", "synced"), [(1, True), (0, False)])
def test_audit_refuses_incomplete_snapshot(
    dropped: int, synced: bool, caplog: pytest.LogCaptureFixture
) -> None:
    out = io.StringIO()
    assert write_audit(snapshot(synced=synced), BOT, dropped, out) == 1
    assert out.getvalue() == ""
    assert "Audit incomplete" in caplog.text


def test_own_disconnect_is_not_reported_as_lost_connection(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    gw = IBKRGateway(BrokerSettings(), 21, ["TURTLE"], readonly=True)
    monkeypatch.setattr(gw.ib, "isConnected", lambda: True)
    monkeypatch.setattr(gw.ib, "disconnect", lambda: gw.ib.disconnectedEvent.emit())
    with caplog.at_level(logging.INFO):
        gw.disconnect()
    assert "Disconnected from TWS" in caplog.text
    assert "connection lost" not in caplog.text
    assert gw.unexpected_disconnects == 0

    gw.ib.disconnectedEvent.emit()  # socket dropped by TWS
    assert "TWS connection lost" in caplog.text
    assert gw.unexpected_disconnects == 1


def test_dry_run_session_judges_ownership_like_the_live_bot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gw = IBKRGateway(BrokerSettings(), 21, ["TURTLE"], readonly=True, owner_client_id=BOT)
    monkeypatch.setattr(gw.ib, "isConnected", lambda: True)
    monkeypatch.setattr(gw.ib, "reqPositions", list)
    monkeypatch.setattr(gw.ib, "managedAccounts", lambda: ["DU123"])
    monkeypatch.setattr(gw.ib, "reqAllOpenOrders", list)
    snap = gw.snapshot()
    assert snap.client_id == BOT
    assert not snap.is_foreign(stop("SHOP", 24, client_id=BOT))


def test_tws_daily_bars_are_requested_adjusted_and_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    gw = IBKRGateway(BrokerSettings(), 21, ["TURTLE"], readonly=True)
    requests: list[dict[str, object]] = []

    def history(contract: object, **kw: object) -> list[SimpleNamespace]:
        requests.append(kw)
        return [
            SimpleNamespace(
                date=dt.date(2026, 10, 7),
                open=2.49,
                high=2.52,
                low=2.44,
                close=2.5,
                volume=1581953.0,
            )
        ]

    monkeypatch.setattr(gw, "_qualify", lambda symbol: object())
    monkeypatch.setattr(gw.ib, "reqHistoricalData", history)
    (bar,) = gw.daily_bars("BMBL", 13)
    assert (bar.date, bar.low, bar.close) == (dt.date(2026, 10, 7), 2.44, 2.5)
    assert requests[0]["whatToShow"] == "ADJUSTED_LAST"
    assert requests[0]["durationStr"] == "13 D"

    def broken(contract: object, **kw: object) -> list[SimpleNamespace]:
        raise ConnectionError("Socket disconnect")

    monkeypatch.setattr(gw.ib, "reqHistoricalData", broken)
    assert gw.daily_bars("BMBL", 13) == []


def test_wait_interrupted_by_tws_disconnect_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    gw = IBKRGateway(BrokerSettings(), 21, ["TURTLE"], readonly=True)

    def cancelled(_: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(gw.ib, "sleep", cancelled)
    monkeypatch.setattr(gw.ib, "isConnected", lambda: False)
    gw.sleep(5)  # nightly TWS reset: swallowed, the engine reconnects
    monkeypatch.setattr(gw.ib, "isConnected", lambda: True)
    with pytest.raises(asyncio.CancelledError):
        gw.sleep(5)  # still connected: a genuine cancellation (e.g. Ctrl+C) propagates


def test_fx_rate_comes_from_the_account_without_market_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    gw = IBKRGateway(BrokerSettings(), 21, ["TURTLE"], readonly=True)
    monkeypatch.setattr(gw.ib, "managedAccounts", lambda: ["DU123"])
    monkeypatch.setattr(
        gw.ib,
        "accountValues",
        lambda _acct: [SimpleNamespace(tag="ExchangeRate", currency="USD", value="0.80")],
    )

    def no_market_data(*_: object, **__: object) -> list[object]:
        raise AssertionError("forex market data must not be requested")

    monkeypatch.setattr(gw.ib, "reqHistoricalData", no_market_data)
    assert gw._usd_per_base("CHF") == pytest.approx(1.25)


def test_forex_permission_error_disables_tws_forex_requests() -> None:
    gw = IBKRGateway(BrokerSettings(), 21, ["TURTLE"], readonly=True)
    gw._on_error(7, 162, "No market data permissions for IDEALPRO CASH", None)
    assert gw._fx_from_tws is False


@pytest.mark.parametrize("code", [2103, 2105, 2157])
def test_nightly_farm_disconnects_are_logged_as_info(
    code: int, caplog: pytest.LogCaptureFixture
) -> None:
    gw = IBKRGateway(BrokerSettings(), 21, ["TURTLE"], readonly=True)
    with caplog.at_level(logging.INFO):
        gw._on_error(-1, code, "farm connection is broken:usfarm", None)
    assert [r.levelno for r in caplog.records] == [logging.INFO]
