"""Benchmark comparison and project phases on the equity endpoint."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from tests.conftest import MutableClock
from tests.factories import NY
from turtlebot.config import DashboardSettings, Settings
from turtlebot.dashboard.demo import demo_benchmark, demo_phases
from turtlebot.dashboard.quotes import StaticBenchmark, StaticQuotes, YahooBenchmark
from turtlebot.dashboard.repository import DashboardRepository
from turtlebot.domain.models import AccountSnapshot
from turtlebot.persistence.store import StateStore

DAYS = [dt.date(2026, 11, 2), dt.date(2026, 11, 3), dt.date(2026, 11, 4), dt.date(2026, 11, 5)]


def with_account(store: StateStore) -> StateStore:
    for d, value in zip(DAYS, [100_000.0, 101_000.0, 100_500.0, 102_000.0], strict=True):
        ts = dt.datetime.combine(d, dt.time(16, 5), tzinfo=NY)
        store.record_account(AccountSnapshot(value, "USD", None, 0.0, 1.0), ts)
    return store


def repo(
    store: StateStore, settings: Settings, benchmark: object | None = None
) -> DashboardRepository:
    return DashboardRepository(
        store,
        StaticQuotes({}),
        settings,
        MutableClock(dt.datetime(2026, 11, 5, 17, tzinfo=NY)),
        benchmark=benchmark,  # type: ignore[arg-type]
    )


def test_benchmark_is_aligned_to_equity_days(store: StateStore, settings: Settings) -> None:
    # No close on 04.11. (holiday in this fixture): the 03.11. close is carried forward.
    closes = pd.Series(
        [500.0, 505.0, 510.0, 520.0],
        index=pd.DatetimeIndex(["2026-10-30", "2026-11-02", "2026-11-03", "2026-11-05"]),
    )
    view = repo(with_account(store), settings, StaticBenchmark(closes)).equity()
    assert view.benchmark_symbol == "SPY"
    assert [b.close for b in view.benchmark] == [505.0, 510.0, 510.0, 520.0]
    assert [b.ts for b in view.benchmark] == [p.ts for p in view.points]


def test_benchmark_failures_never_break_the_curve(store: StateStore, settings: Settings) -> None:
    class Broken:
        def closes(self, symbol: str, start: dt.date) -> pd.Series:
            raise RuntimeError("vendor down")

    store = with_account(store)
    assert repo(store, settings, Broken()).equity().benchmark == []
    assert repo(store, settings, StaticBenchmark(pd.Series(dtype=float))).equity().benchmark == []
    assert repo(store, settings, None).equity().benchmark == []
    assert len(repo(store, settings, None).equity().points) == 4


def test_no_benchmark_for_a_realised_pnl_curve(store: StateStore, settings: Settings) -> None:
    closes = pd.Series([1.0], index=pd.DatetimeIndex(["2026-11-02"]))
    view = repo(store, settings, StaticBenchmark(closes)).equity()
    assert view.source == "none"
    assert view.benchmark == []


def test_phases_are_sorted_and_chained(store: StateStore) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        dashboard=DashboardSettings(
            phases=[
                {"label": "v2", "start": "2026-10-27"},  # type: ignore[list-item]
                {"label": "test", "start": "2026-10-05"},  # type: ignore[list-item]
            ],
            benchmark_symbol="  ",
        ),
    )
    view = repo(with_account(store), settings).equity()
    assert [(p.label, p.start, p.end) for p in view.phases] == [
        ("test", dt.date(2026, 10, 5), dt.date(2026, 10, 27)),
        ("v2", dt.date(2026, 10, 27), None),
    ]
    assert view.benchmark_symbol is None


def test_yahoo_benchmark_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    good = pd.Series([1.0, 2.0], index=pd.DatetimeIndex(["2026-11-02", "2026-11-03"]))
    results = [good, pd.Series(dtype=float)]

    def fake_download(symbol: str, start: dt.date) -> pd.Series:
        calls.append(symbol)
        return results.pop(0)

    monkeypatch.setattr(YahooBenchmark, "_download", staticmethod(fake_download))
    bench = YahooBenchmark(ttl_s=60)
    start = dt.date(2026, 11, 2)
    assert bench.closes("SPY", start).tolist() == [1.0, 2.0]
    assert bench.closes("SPY", start).tolist() == [1.0, 2.0]
    assert calls == ["SPY"]
    bench.ttl_s = -1  # expired, vendor returns nothing -> keep serving the last good series
    assert bench.closes("SPY", start).tolist() == [1.0, 2.0]
    assert calls == ["SPY", "SPY"]


def test_demo_benchmark_and_phases() -> None:
    today = dt.date(2026, 11, 11)
    series = demo_benchmark(today=today)
    assert series.index[-1] == pd.Timestamp(today)
    assert (series > 0).all()
    phases = demo_phases(today)
    assert [p["start"] for p in phases] == sorted(p["start"] for p in phases)
