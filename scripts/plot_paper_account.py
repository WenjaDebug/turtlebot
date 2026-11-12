"""Render the paper-account equity chart used in the README and ``docs/results.md``.

Reads ``docs/data/paper_account.csv`` (net liquidation in CHF, as reported by TWS) and writes a
light and a dark PNG to ``docs/img/``. Requires the ``docs`` extra: ``pip install -e ".[docs]"``.

Usage:
    python scripts/plot_paper_account.py
"""

from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import matplotlib as mpl

mpl.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs" / "data" / "paper_account.csv"
OUT = ROOT / "docs" / "img"

PHASES = [
    ("Test mode", dt.datetime(2026, 10, 5, tzinfo=dt.UTC)),
    ("v1 legacy bot", dt.datetime(2026, 10, 13, tzinfo=dt.UTC)),
    ("v2 turtlebot engine", dt.datetime(2026, 10, 27, tzinfo=dt.UTC)),
]


@dataclass(frozen=True)
class Theme:
    """Colours for one variant of the chart."""

    name: str
    bg: str
    ink: str
    muted: str
    grid: str
    line: str
    neg: str
    bands: tuple[str, str, str]


THEMES = [
    Theme(
        "light",
        "#ffffff",
        "#1d232b",
        "#6b7380",
        "#e6e9ee",
        "#12805c",
        "#c73a3a",
        ("#eef0f3", "#e3eaf7", "#e2f2ea"),
    ),
    Theme(
        "dark",
        "#0e1116",
        "#e8ecf2",
        "#8b94a3",
        "#232a33",
        "#3ccf91",
        "#ef6461",
        ("#1a1f27", "#1b2433", "#17261f"),
    ),
]


def load() -> tuple[list[dt.datetime], list[float]]:
    """Read the CSV into timestamps (UTC) and values."""
    with DATA.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    ts = [dt.datetime.fromisoformat(r["timestamp_utc"].replace("Z", "+00:00")) for r in rows]
    return ts, [float(r["net_liquidation_chf"]) for r in rows]


def render(theme: Theme, ts: list[dt.datetime], values: list[float]) -> Path:
    """Draw one variant and return the written path."""
    start = values[0]
    pct = [(v / start - 1) * 100 for v in values]
    peak, trough_i, max_dd = start, 0, 0.0
    for i, v in enumerate(values):
        peak = max(peak, v)
        if (dd := v / peak - 1) < max_dd:
            max_dd, trough_i = dd, i

    fig, ax = plt.subplots(figsize=(10, 4.2), dpi=200)
    fig.patch.set_facecolor(theme.bg)
    ax.set_facecolor(theme.bg)
    end = ts[-1] + dt.timedelta(hours=12)
    bounds = [p[1] for p in PHASES] + [end]
    for (label, x0), x1, colour in zip(PHASES, bounds[1:], theme.bands, strict=True):
        ax.axvspan(x0, x1, color=colour, zorder=0, lw=0)
        ax.text(
            x0 + dt.timedelta(hours=10),
            0.035,
            label.upper(),
            transform=ax.get_xaxis_transform(),
            fontsize=8,
            color=theme.muted,
            fontweight="bold",
        )
    ax.axhline(0, color=theme.muted, lw=1, zorder=1)
    ax.plot(ts, pct, color=theme.line, lw=2.2, zorder=3, marker="o", ms=3)
    ax.annotate(
        f"max drawdown {max_dd * 100:.1f} %",
        (ts[trough_i], pct[trough_i]),
        xytext=(0, -16),
        textcoords="offset points",
        ha="center",
        fontsize=8.5,
        color=theme.neg,
    )
    ax.annotate(
        f"{pct[-1]:+.1f} %",
        (ts[-1], pct[-1]),
        xytext=(8, 0),
        textcoords="offset points",
        va="center",
        fontsize=10,
        color=theme.ink,
        fontweight="bold",
    )

    ax.set_xlim(ts[0] - dt.timedelta(hours=12), end)
    ax.set_ylim(min(pct) - 0.9, max(max(pct), 0) + 0.5)
    ax.yaxis.set_major_locator(mticker.MultipleLocator(1))
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:+.0f} %" if round(v) else "0 %")
    ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.MO))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax.grid(axis="y", color=theme.grid, lw=0.8)
    ax.tick_params(colors=theme.muted, labelsize=9, length=0)
    for side in ax.spines.values():
        side.set_visible(False)
    ax.set_title(
        "Paper account, net liquidation since the first trade (CHF)",
        loc="left",
        color=theme.ink,
        fontsize=11,
        pad=10,
    )
    fig.tight_layout()
    path = OUT / f"paper-account-{theme.name}.png"
    fig.savefig(path, facecolor=theme.bg)
    plt.close(fig)
    return path


def main() -> None:
    """Render both variants."""
    OUT.mkdir(parents=True, exist_ok=True)
    ts, values = load()
    for theme in THEMES:
        render(theme, ts, values)


if __name__ == "__main__":
    main()
