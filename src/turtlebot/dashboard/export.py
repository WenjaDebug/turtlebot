"""Static export of the demo dashboard (e.g. for GitHub Pages).

The export is the regular frontend plus one JSON file per API endpoint, frozen at export time.
``index.html`` gets a ``data-static`` attribute; ``api.js`` then reads those files instead of
calling the server, and the assistant is disabled. Nothing here touches a real account: the
data comes from the same synthetic generator as ``turtlebot dashboard --demo``.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import tempfile
from pathlib import Path
from typing import Final

from pydantic import BaseModel

from turtlebot.config import Settings
from turtlebot.dashboard.demo import demo_benchmark, demo_settings, seed_demo_store
from turtlebot.dashboard.quotes import StaticBenchmark, StaticQuotes
from turtlebot.dashboard.repository import DashboardRepository
from turtlebot.persistence.store import StateStore
from turtlebot.timeutil import Clock, system_clock

STATIC_DIR: Final = Path(__file__).parent / "static"
DATA_DIR: Final = "data"
_API_LINK: Final = ' · <a href="/api/docs">API</a>'


def _dump(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_dump(v) for v in value]
    return value


def export_static_demo(
    out: Path, settings: Settings, *, clock: Clock | None = None, today: dt.date | None = None
) -> list[Path]:
    """Write a self-contained static copy of the demo dashboard to ``out``.

    Args:
        out: Target directory; created if needed. Existing files with the same names are replaced.
        settings: Base settings (the demo phases are added).
        clock: Clock for the snapshot (defaults to the exchange clock).
        today: Last day of the synthetic history (defaults to today).

    Returns:
        The JSON files written.
    """
    settings = demo_settings(settings, today)
    clock = clock or system_clock(settings.schedule.tz)
    out.mkdir(parents=True, exist_ok=True)
    shutil.copytree(STATIC_DIR, out, dirs_exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "demo.db"
        quotes = StaticQuotes(seed_demo_store(db, today=today))
        store = StateStore(db)
        try:
            repo = DashboardRepository(
                store,
                quotes,
                settings,
                clock,
                benchmark=StaticBenchmark(demo_benchmark(today=today)),
                demo=True,
            )
            payloads: dict[str, object] = {
                "stream": repo.snapshot(),
                "equity": repo.equity(),
                "trades": repo.trades(limit=200),
                "signals": repo.signals(),
                "reconciliations": repo.reconciliations(limit=1),
            }
        finally:
            store.close()

    data_dir = out / DATA_DIR
    data_dir.mkdir(exist_ok=True)
    written = []
    for name, payload in payloads.items():
        path = data_dir / f"{name}.json"
        path.write_text(json.dumps(_dump(payload), separators=(",", ":")))
        written.append(path)

    index = out / "index.html"
    html = index.read_text()
    html = html.replace('<html lang="en">', f'<html lang="en" data-static="{DATA_DIR}">', 1)
    index.write_text(html.replace(_API_LINK, ""))
    return written
