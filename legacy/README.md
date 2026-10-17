# Legacy code

Kept for traceability and as a fallback while the new `turtlebot` package is validated.
Nothing in `src/` imports from here.

| Path | Content |
|------|---------|
| `live_trade_turtle.py` | The original monolithic live bot (run from the repo root: `python legacy/live_trade_turtle.py --dry-run`, it resolves its state/journal files relative to the working directory). |
| `data_sources.py`, `strategies/` | Data and signal modules used by the legacy bot and research scripts. |
| `repair_block1_positions.py` | One-off repair script for the 12.10.2026 stop-loss incident. |
| `research/` | Exploratory backtests and analyses (SMA, RSI, Bollinger, dual, turtle filters, MOC vs. next open). They expect to be run from the repo root with `legacy/` on `PYTHONPATH`. |

The old Streamlit dashboard still lives in `dashboard/` and is superseded by `turtlebot.dashboard`.
