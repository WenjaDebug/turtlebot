"""FastAPI application: REST + WebSocket API and the static single-page frontend.

The dashboard is strictly read-only towards the broker: it never talks to TWS and never writes
trading state. Live updates are pushed over ``/api/v1/stream``: a single background task polls
the store's change marker and broadcasts a fresh snapshot to all clients when something changed
(or when quotes are due for a refresh).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Final

from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from turtlebot import __version__
from turtlebot.config import Settings
from turtlebot.dashboard.chat import ChatError, ChatService, ClaudeCliChat, UnavailableChat
from turtlebot.dashboard.quotes import (
    BenchmarkService,
    QuoteService,
    YahooBenchmark,
    YahooQuotes,
)
from turtlebot.dashboard.repository import DashboardRepository
from turtlebot.dashboard.schemas import (
    ChatRequest,
    ChatResponse,
    EquityView,
    EventView,
    Overview,
    PositionView,
    SignalView,
    TradeView,
)
from turtlebot.persistence.store import StateStore
from turtlebot.timeutil import Clock, system_clock

log = logging.getLogger(__name__)

STATIC_DIR: Final = Path(__file__).parent / "static"
CSP: Final = (
    "default-src 'self'; "
    "script-src 'self' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self' ws: wss:"
)


class SnapshotHub:
    """Broadcasts repository snapshots to connected WebSocket clients."""

    def __init__(self, repo: DashboardRepository, poll_s: float, refresh_s: float) -> None:
        self.repo = repo
        self.poll_s = poll_s
        self.refresh_s = refresh_s
        self.clients: set[WebSocket] = set()
        self._version: int | None = None
        self._last_push = 0.0

    async def snapshot_json(self) -> str:
        """Current snapshot serialised (computed in a worker thread)."""
        snap = await run_in_threadpool(self.repo.snapshot)
        return snap.model_dump_json()

    async def register(self, ws: WebSocket) -> None:
        """Accept a client and send it the current state immediately."""
        await ws.accept()
        self.clients.add(ws)
        await ws.send_text(await self.snapshot_json())

    def unregister(self, ws: WebSocket) -> None:
        """Forget a client."""
        self.clients.discard(ws)

    async def tick(self) -> bool:
        """Broadcast if the data changed or quotes are due. Returns whether it pushed."""
        if not self.clients:
            return False
        version = await run_in_threadpool(self.repo.store.data_version)
        now = time.monotonic()
        if version == self._version and now - self._last_push < self.refresh_s:
            return False
        payload = await self.snapshot_json()
        self._version, self._last_push = version, now
        for ws in list(self.clients):
            try:
                await ws.send_text(payload)
            except (WebSocketDisconnect, RuntimeError):
                self.unregister(ws)
        return True

    async def run(self) -> None:
        """Poll forever (cancelled on shutdown)."""
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - the hub must survive transient errors
                log.exception("Snapshot broadcast failed")
            await asyncio.sleep(self.poll_s)


def create_app(
    settings: Settings,
    *,
    db_path: Path | None = None,
    store: StateStore | None = None,
    quotes: QuoteService | None = None,
    benchmark: BenchmarkService | None = None,
    chat: ChatService | None = None,
    clock: Clock | None = None,
    demo: bool = False,
) -> FastAPI:
    """Build the dashboard application.

    Args:
        settings: Application settings.
        db_path: State database (ignored if ``store`` is given).
        store: Injected store (tests).
        quotes: Injected quote service (default: Yahoo with TTL cache).
        benchmark: Injected benchmark closes (default: Yahoo daily closes; none in demo mode).
        chat: Injected chat service (default: Claude Code CLI if enabled).
        clock: Time source.
        demo: Mark the data as synthetic in the UI.

    Returns:
        The FastAPI app.
    """
    dash = settings.dashboard
    state = store or StateStore(db_path or settings.paths.db_path)
    repo = DashboardRepository(
        state,
        quotes or YahooQuotes(ttl_s=dash.price_refresh_s),
        settings,
        clock or system_clock(settings.schedule.tz),
        benchmark=benchmark or (None if demo or store is not None else YahooBenchmark()),
        demo=demo,
    )
    chat_service = chat or (
        ClaudeCliChat(dash.chat_command, Path.cwd(), dash.chat_timeout_s)
        if dash.chat_enabled
        else UnavailableChat()
    )
    hub = SnapshotHub(repo, dash.push_interval_s, dash.price_refresh_s)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(hub.run())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            if store is None:
                state.close()

    app = FastAPI(
        title="turtlebot dashboard",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.repo = repo
    app.state.hub = hub
    if dash.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=dash.cors_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
        )

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    # ------------------------------------------------------------------ REST
    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/v1/overview")
    def overview() -> Overview:
        return repo.overview()

    @app.get("/api/v1/positions")
    def positions() -> list[PositionView]:
        return repo.positions()

    @app.get("/api/v1/equity")
    def equity() -> EquityView:
        return repo.equity()

    @app.get("/api/v1/trades")
    def trades(limit: Annotated[int, Query(ge=1, le=1000)] = 200) -> list[TradeView]:
        return repo.trades(limit=limit)

    @app.get("/api/v1/signals")
    def signals() -> list[SignalView]:
        return repo.signals()

    @app.get("/api/v1/events")
    def events(
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        after_id: Annotated[int, Query(ge=0)] = 0,
    ) -> list[EventView]:
        return repo.events(limit=limit, after_id=after_id)

    @app.get("/api/v1/reconciliations")
    def reconciliations(
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
    ) -> list[dict[str, object]]:
        return [
            {
                "id": r.id,
                "ts": r.ts.isoformat(),
                "outcome": r.outcome,
                "update_stops": r.update_stops,
                "n_local": r.n_local,
                "n_broker": r.n_broker,
                "n_actions": r.n_actions,
                "message": r.message,
                "discrepancies": [
                    {
                        "kind": d.kind,
                        "severity": d.severity,
                        "symbol": d.symbol,
                        "local": d.local,
                        "broker": d.broker,
                        "resolution": d.resolution,
                    }
                    for d in r.discrepancies
                ],
            }
            for r in state.reconciliations(limit=limit)
        ]

    @app.post("/api/v1/chat")
    async def ask(request: ChatRequest) -> ChatResponse:
        context = await run_in_threadpool(repo.chat_context)
        try:
            answer, session = await chat_service.ask(request.message, context, request.session_id)
        except ChatError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return ChatResponse(answer=answer, session_id=session)

    # ------------------------------------------------------------------ WebSocket
    @app.websocket("/api/v1/stream")
    async def stream(ws: WebSocket) -> None:
        await hub.register(ws)
        try:
            while True:
                message = await ws.receive_text()
                if message == "refresh":
                    await ws.send_text(await hub.snapshot_json())
        except WebSocketDisconnect:
            pass
        finally:
            hub.unregister(ws)

    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
