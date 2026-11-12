"""Static demo export for GitHub Pages."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from tests.conftest import MutableClock
from tests.factories import NY
from turtlebot.config import Settings
from turtlebot.dashboard.export import export_static_demo


def test_export_writes_frontend_and_frozen_api(tmp_path: Path) -> None:
    settings = Settings(_env_file=None)
    clock = MutableClock(dt.datetime(2026, 11, 11, 17, tzinfo=NY))
    out = tmp_path / "site"

    files = export_static_demo(out, settings, clock=clock, today=dt.date(2026, 11, 11))

    assert sorted(p.name for p in files) == [
        "equity.json",
        "reconciliations.json",
        "signals.json",
        "stream.json",
        "trades.json",
    ]
    html = (out / "index.html").read_text()
    assert '<html lang="en" data-static="data">' in html
    assert "/api/docs" not in html  # no server behind a static page
    assert (out / "js" / "main.js").exists()
    assert (out / "app.css").exists()

    equity = json.loads((out / "data" / "equity.json").read_text())
    assert equity["source"] == "account"
    assert equity["benchmark_symbol"] == "SPY"
    assert len(equity["benchmark"]) == len(equity["points"]) > 50
    assert equity["phases"][0]["label"] == "Test mode"

    snapshot = json.loads((out / "data" / "stream.json").read_text())
    assert snapshot["overview"]["status"]["demo"] is True
    assert snapshot["positions"]
    assert len(json.loads((out / "data" / "reconciliations.json").read_text())) == 1


def test_export_is_repeatable(tmp_path: Path) -> None:
    settings = Settings(_env_file=None)
    clock = MutableClock(dt.datetime(2026, 11, 11, 17, tzinfo=NY))
    today = dt.date(2026, 11, 11)
    export_static_demo(tmp_path, settings, clock=clock, today=today)
    first = (tmp_path / "data" / "equity.json").read_text()
    export_static_demo(tmp_path, settings, clock=clock, today=today)
    html = (tmp_path / "index.html").read_text()
    assert html.count("data-static") == 1
    assert (tmp_path / "data" / "equity.json").read_text() == first
