"""CLI tests. ``run`` is only exercised on its refusal path - nothing here connects to TWS."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.factories import position
from turtlebot import cli
from turtlebot.config import LogSettings, Settings, get_settings
from turtlebot.exceptions import TurtleBotError
from turtlebot.logging_setup import configure_logging
from turtlebot.persistence.store import StateStore


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TURTLE_PATHS__DATA_DIR", str(tmp_path / "var"))
    monkeypatch.delenv("TURTLE_TRADING_ENABLED", raising=False)
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()
    configure_logging(LogSettings(), None)  # close file handlers opened by the CLI


def test_run_refuses_without_trading_switch(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["run"]) == cli.EXIT_REFUSED
    assert "TURTLE_TRADING_ENABLED" in capsys.readouterr().err


def test_config_prints_effective_settings(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["config"]) == 0
    cfg = json.loads(capsys.readouterr().out)
    assert cfg["trading_enabled"] is False
    assert cfg["block2"]["min_strength"] == 0.1
    assert cfg["anthropic_api_key"] is None


def test_migrate_and_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = {
        "positions": {
            "XP": {
                "status": "open",
                "qty": 5,
                "stop": 20.0,
                "entry_price": 28.0,
                "signal_price": 28.0,
                "block": 1,
            }
        }
    }
    (tmp_path / "live_turtle_state.json").write_text(json.dumps(state))
    assert cli.main(["migrate-legacy"]) == 0
    assert "active_positions=1" in capsys.readouterr().out
    assert cli.main(["migrate-legacy"]) == 1  # refuses a non-empty store
    assert cli.main(["report"]) == 0
    out = capsys.readouterr().out
    assert "Active positions: 1" in out
    assert "XP" in out


def test_report_shows_reconciliation(
    isolated_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    import datetime as dt

    from turtlebot.persistence.store import DiscrepancyRecord

    store = StateStore(isolated_settings.paths.db_path)
    store.insert_position(position("AAA", missing_cycles=1))
    store.record_event("FILL", ts=dt.datetime(2026, 10, 14, tzinfo=dt.UTC), symbol="AAA", qty=1)
    store.record_reconciliation(
        ts=dt.datetime(2026, 10, 14, tzinfo=dt.UTC),
        outcome="degraded",
        update_stops=False,
        n_local=1,
        n_broker=0,
        n_actions=0,
        message="m",
        discrepancies=[DiscrepancyRecord("position_missing", "warning", "AAA", "l", "b", "kept")],
    )
    store.close()
    assert cli.main(["report"]) == 0
    out = capsys.readouterr().out
    assert "MISSING x1" in out
    assert "degraded" in out
    assert "position_missing" in out


def test_client_id_lock_is_exclusive(tmp_path: Path) -> None:
    with (
        cli.client_id_lock(tmp_path, [21, 22]) as first,
        cli.client_id_lock(tmp_path, [21, 22]) as second,
    ):
        assert (first, second) == (21, 22)
        with pytest.raises(TurtleBotError), cli.client_id_lock(tmp_path, [21, 22]):
            pass
    with cli.client_id_lock(tmp_path, [21]) as again:
        assert again == 21


def test_raise_file_limit_returns_bool() -> None:
    assert isinstance(cli.raise_file_limit(), bool)


def test_parser_requires_command() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])
