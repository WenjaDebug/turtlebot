"""Tests for the breakout signal."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import numpy as np
import pandas as pd
import pytest

from tests.factories import breakout_frame, ohlcv
from turtlebot.strategy.signals import standardize_ohlcv, turtle_breakout

LEGACY = Path(__file__).resolve().parents[2] / "legacy" / "strategies" / "signals.py"


def _legacy_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("legacy_signals", LEGACY)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_breakout_with_volume_triggers_entry() -> None:
    sig = turtle_breakout(breakout_frame())
    assert bool(sig["entry"].iloc[-1])
    assert sig["entry"].iloc[:-1].sum() == 0
    assert sig["breakout_strength"].iloc[-1] == pytest.approx(55 / 50.5 - 1)
    assert sig["volume_ratio"].iloc[-1] == pytest.approx(3.0)


def test_breakout_without_volume_is_ignored() -> None:
    sig = turtle_breakout(breakout_frame(vol_mult=1.5))  # needs strictly more than 1.5x
    assert not bool(sig["entry"].iloc[-1])


def test_close_equal_to_donchian_high_is_no_breakout() -> None:
    df = breakout_frame(jump=0.5)  # close == previous high (50 + 0.5 spread)
    assert not bool(turtle_breakout(df)["entry"].iloc[-1])


def test_warmup_period_never_signals() -> None:
    df = ohlcv(np.linspace(10, 30, 15))  # shorter than the 20-day channel
    assert not turtle_breakout(df)["entry"].any()


def test_exit_when_close_below_donchian_low() -> None:
    closes = [50.0] * 30 + [40.0]
    sig = turtle_breakout(ohlcv(closes))
    assert bool(sig["exit"].iloc[-1])


@pytest.mark.parametrize("rank_by", ["volume_ratio", "breakout_strength"])
def test_score_follows_rank_by(rank_by: str) -> None:
    sig = turtle_breakout(breakout_frame(), rank_by=rank_by)
    pd.testing.assert_series_equal(sig["score"], sig[rank_by], check_names=False)


def test_matches_legacy_implementation() -> None:
    """Guard rail: the trading logic must stay identical to the legacy bot."""
    rng = np.random.default_rng(7)
    closes = 50 * np.exp(np.cumsum(rng.normal(0.004, 0.02, 400)))
    volumes = rng.lognormal(13, 0.8, 400)
    df = ohlcv(closes, volumes=volumes)
    df["High"] = df["Close"] * (1 + rng.uniform(0, 0.03, 400))
    df["Low"] = df["Close"] * (1 - rng.uniform(0, 0.03, 400))
    legacy = _legacy_module()
    for rank_by in ("volume_ratio", "breakout_strength"):
        new = turtle_breakout(df, 20, 10, 20, 1.5, rank_by=rank_by)
        old = legacy.turtle_breakout(df, 20, 10, 20, 1.5, rank_by=rank_by)
        pd.testing.assert_frame_equal(new, old)
    assert new["entry"].sum() > 0  # the sample actually exercises the entry branch


def test_standardize_ohlcv_from_ib_bars() -> None:
    raw = pd.DataFrame(
        {
            "date": ["2026-10-02", "2026-10-01"],
            "open": [1.0, 2.0],
            "high": [1.5, 2.5],
            "low": [0.5, 1.5],
            "close": [1.2, 2.2],
            "volume": [10, 20],
            "average": [0, 0],
        }
    )
    df = standardize_ohlcv(raw)
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert df.index.is_monotonic_increasing
    assert df["Close"].iloc[0] == 2.2
