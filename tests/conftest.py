"""Shared fixtures. No test ever talks to TWS: the broker is always :class:`FakeBroker`."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator, Sequence

import pytest

from tests.factories import NY
from tests.fakes import FakeBroker
from turtlebot.config import Settings
from turtlebot.persistence.store import StateStore
from turtlebot.reconciliation.service import ReconciliationService


class RecordingNotifier:
    """Notifier that remembers alerts."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.messages.append((title, message))


class MutableClock:
    """Clock that tests can move forward."""

    def __init__(self, now: dt.datetime) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += dt.timedelta(**kwargs)


@pytest.fixture
def settings(tmp_path: object) -> Settings:
    return Settings(_env_file=None)


@pytest.fixture
def store() -> Iterator[StateStore]:
    s = StateStore(":memory:")
    yield s
    s.close()


@pytest.fixture
def broker() -> FakeBroker:
    return FakeBroker()


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock(dt.datetime(2026, 10, 14, 9, 0, tzinfo=NY))


@pytest.fixture
def lows() -> dict[str, float]:
    """Recent lows served by the fake loader (tests may mutate it)."""
    return {}


@pytest.fixture
def reconciler(
    broker: FakeBroker,
    store: StateStore,
    settings: Settings,
    notifier: RecordingNotifier,
    clock: MutableClock,
    lows: dict[str, float],
) -> ReconciliationService:
    def load_lows(symbols: Sequence[str]) -> dict[str, float]:
        return {s: lows[s] for s in symbols if s in lows}

    return ReconciliationService(broker, store, load_lows, settings, notifier, clock)
