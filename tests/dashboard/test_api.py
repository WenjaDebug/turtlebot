"""Dashboard API tests (REST + WebSocket) against an in-memory store."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import MutableClock
from tests.factories import NY, position
from turtlebot.config import Settings
from turtlebot.dashboard.app import create_app
from turtlebot.dashboard.chat import UnavailableChat
from turtlebot.dashboard.demo import seed_demo_store
from turtlebot.dashboard.quotes import StaticQuotes
from turtlebot.dashboard.repository import DashboardRepository
from turtlebot.domain.models import AccountSnapshot, Block, Candidate, PositionStatus
from turtlebot.persistence.store import DiscrepancyRecord, StateStore

NOW = dt.datetime(2026, 10, 14, 12, 0, tzinfo=NY)


class EchoChat:
    def __init__(self) -> None:
        self.contexts: list[dict[str, object]] = []

    async def ask(
        self, message: str, context: dict[str, object], session_id: str | None
    ) -> tuple[str, str | None]:
        self.contexts.append(context)
        return f"echo: {message}", session_id or "s-1"


@pytest.fixture
def populated(store: StateStore) -> StateStore:
    store.insert_position(position("AAA", qty=10, stop=90.0))
    store.insert_position(position("BBB", qty=5, stop=45.0, entry_price=50.0, block=Block.TWO))
    store.insert_position(position("PEND", status=PositionStatus.PENDING))
    closed = store.insert_position(position("OLD", qty=10, entry_price=20.0))
    store.close_position(closed, reason="stop", exit_price=22.0, closed_at=NOW)
    for i, value in enumerate([100_000.0, 102_000.0, 99_000.0, 101_000.0]):
        store.record_account(
            AccountSnapshot(value, "CHF", None, 50.0, 1.25), NOW + dt.timedelta(days=i)
        )
    store.record_event("FILL", ts=NOW, symbol="AAA", qty=10, price=100.0)
    store.record_event("SIGNAL", ts=NOW, symbol="ZZZ")
    store.record_signals(NOW.date(), [Candidate("ZZZ", 10.0, 9.0, 0.12, 2.5)], NOW)
    store.record_reconciliation(
        ts=NOW,
        outcome="degraded",
        update_stops=False,
        n_local=3,
        n_broker=2,
        n_actions=0,
        message="mass disappearance",
        discrepancies=[DiscrepancyRecord("mass_disappearance", "critical", None, "3", "1", "x")],
    )
    store.set_meta("engine_heartbeat", NOW.isoformat())
    store.set_meta("engine_mode", "paper")
    return store


@pytest.fixture
def chat() -> EchoChat:
    return EchoChat()


@pytest.fixture
def client(populated: StateStore, chat: EchoChat) -> Iterator[TestClient]:
    app = create_app(
        Settings(_env_file=None),
        store=populated,
        quotes=StaticQuotes({"AAA": 110.0}),
        chat=chat,
        clock=MutableClock(NOW + dt.timedelta(seconds=30)),
    )
    with TestClient(app) as c:
        yield c


def test_health_and_security_headers(client: TestClient) -> None:
    res = client.get("/api/v1/health")
    assert res.json()["status"] == "ok"
    assert "default-src 'self'" in res.headers["content-security-policy"]
    assert res.headers["x-frame-options"] == "DENY"


def test_positions_are_valued_with_quotes_and_fallback(client: TestClient) -> None:
    rows = {r["symbol"]: r for r in client.get("/api/v1/positions").json()}
    aaa = rows["AAA"]
    assert aaa["last_price"] == 110.0
    assert aaa["unrealized_pnl"] == pytest.approx(100.0)
    assert aaa["stop_distance_pct"] == pytest.approx((110 - 90) / 110)
    assert aaa["risk_to_stop"] == pytest.approx(200.0)
    assert rows["BBB"]["quote_stale"] is True  # no quote -> valued at entry
    assert rows["PEND"]["unrealized_pnl"] is None


def test_overview(client: TestClient) -> None:
    o = client.get("/api/v1/overview").json()
    assert o["status"]["running"] is True
    assert o["status"]["mode"] == "paper"
    assert o["status"]["last_reconciliation"]["critical"] == 1
    assert o["account"]["net_liquidation"] == 101_000.0
    assert o["account"]["net_liquidation_usd"] == pytest.approx(126_250.0)
    m = o["metrics"]
    assert m["max_drawdown"] == pytest.approx(99 / 102 - 1)
    assert m["trades"]["trades"] == 1
    assert m["trades"]["win_rate"] == 1.0
    blocks = {b["block"]: b for b in o["blocks"]}
    assert blocks[1]["open_positions"] == 1
    assert blocks[1]["pending_positions"] == 1
    assert blocks[1]["realized_pnl"] == pytest.approx(20.0)
    assert blocks[2]["min_strength"] == 0.10


def test_equity_trades_signals_events_reconciliations(client: TestClient) -> None:
    eq = client.get("/api/v1/equity").json()
    assert eq["source"] == "account"
    assert eq["drawdown_unit"] == "fraction"
    assert [round(p["drawdown"], 4) for p in eq["points"]][:3] == [0.0, 0.0, -0.0294]
    trades = client.get("/api/v1/trades").json()
    assert trades[0]["return_pct"] == pytest.approx(0.1)
    assert client.get("/api/v1/signals").json()[0]["symbol"] == "ZZZ"
    kinds = [e["kind"] for e in client.get("/api/v1/events").json()]
    assert kinds == ["FILL"]  # raw signals are not part of the feed
    recon = client.get("/api/v1/reconciliations").json()[0]
    assert recon["outcome"] == "degraded"
    assert client.get("/api/v1/trades?limit=0").status_code == 422


def test_websocket_pushes_snapshot_and_updates(client: TestClient, populated: StateStore) -> None:
    with client.websocket_connect("/api/v1/stream") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot"
        assert {p["symbol"] for p in first["positions"]} == {"AAA", "BBB", "PEND"}
        populated.record_event("EXIT", ts=NOW, symbol="AAA")
        ws.send_text("refresh")
        second = ws.receive_json()
        assert second["data_version"] != first["data_version"]


def test_chat_receives_live_context(client: TestClient, chat: EchoChat) -> None:
    res = client.post("/api/v1/chat", json={"message": "How is block 2?"})
    assert res.json() == {"answer": "echo: How is block 2?", "session_id": "s-1"}
    context = chat.contexts[0]
    assert {"overview", "positions", "recent_events", "latest_signals"} <= set(context)


def test_chat_validation_and_errors(populated: StateStore) -> None:
    app = create_app(
        Settings(_env_file=None), store=populated, quotes=StaticQuotes({}), chat=UnavailableChat()
    )
    with TestClient(app) as c:
        assert c.post("/api/v1/chat", json={"message": ""}).status_code == 422
        bad_session = {"message": "hi", "session_id": "../../etc"}
        assert c.post("/api/v1/chat", json=bad_session).status_code == 422
        res = c.post("/api/v1/chat", json={"message": "hi"})
        assert res.status_code == 503
        assert "disabled" in res.json()["detail"]


def test_static_frontend_is_served(client: TestClient) -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert "turtlebot" in res.text
    assert client.get("/js/main.js").status_code == 200


def test_hub_broadcasts_only_on_change(populated: StateStore) -> None:
    from turtlebot.dashboard.app import SnapshotHub

    repo = DashboardRepository(populated, StaticQuotes({}), Settings(_env_file=None), lambda: NOW)
    hub = SnapshotHub(repo, poll_s=0.01, refresh_s=3600)

    class Sink:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_text(self, text: str) -> None:
            self.sent.append(text)

    sink = Sink()
    hub.clients.add(sink)  # type: ignore[arg-type]

    async def scenario() -> list[bool]:
        results = [await hub.tick(), await hub.tick()]
        populated.record_event("EXIT", ts=NOW, symbol="AAA")
        results.append(await hub.tick())
        return results

    assert asyncio.run(scenario()) == [True, False, True]
    assert len(sink.sent) == 2


def test_realized_equity_fallback(store: StateStore) -> None:
    for i, (entry, exit_) in enumerate([(10.0, 12.0), (10.0, 7.0), (10.0, 11.0)]):
        p = store.insert_position(position(f"T{i}", qty=100, entry_price=entry))
        store.close_position(p, reason="x", exit_price=exit_, closed_at=NOW + dt.timedelta(days=i))
    repo = DashboardRepository(store, StaticQuotes({}), Settings(_env_file=None), lambda: NOW)
    eq = repo.equity()
    assert eq.source == "realized"
    assert eq.drawdown_unit == "currency"
    assert [p.equity for p in eq.points] == [200.0, -100.0, 0.0]
    assert [p.drawdown for p in eq.points] == [0.0, -300.0, -200.0]


def test_empty_store(store: StateStore) -> None:
    repo = DashboardRepository(store, StaticQuotes({}), Settings(_env_file=None), lambda: NOW)
    assert repo.equity().source == "none"
    o = repo.overview()
    assert o.account is None
    assert o.status.running is False
    assert o.metrics.sharpe is None


def test_demo_seed(tmp_path: Path) -> None:
    db = tmp_path / "demo.db"
    quotes = seed_demo_store(db, today=dt.date(2026, 10, 16))
    store = StateStore(db)
    try:
        assert len(store.active_positions()) == len(quotes) == 17
        assert len(store.account_history()) > 80
        assert store.closed_positions()
    finally:
        store.close()
    seed_demo_store(db, today=dt.date(2026, 10, 16))  # idempotent re-seed
