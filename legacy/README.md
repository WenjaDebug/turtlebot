# Legacy code

The first version of the bot and the research that led to the current strategy, kept for traceability.
Nothing in `src/` imports from here.

| Path | Content |
|------|---------|
| `live_trade_turtle.py` | The original monolithic live bot (v1), replaced by the `turtlebot` package after the 12.10.2026 stop-loss incident. Run from the repo root: `python legacy/live_trade_turtle.py --dry-run`. |
| `data_sources.py`, `strategies/` | Data and signal modules used by the v1 bot and the research scripts. |
| `research/` | Exploratory backtests and analyses (SMA crossover, RSI, Bollinger, dual strategy, trend following, turtle filters, MOC vs. next open). Run from the repo root with `legacy/` on `PYTHONPATH`. |
