# Changelog

All notable changes to this project. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [semantic versioning](https://semver.org/).

## [1.0.0] - unreleased

First public release: the `turtlebot` package that has been trading the IBKR paper account since 27 Oct 2026.

### Added

- **Engine.** Scheduler with universe build (08:00 ET), reconciliation and stop trailing (09:00 ET) and a market-on-close scan (15:45 ET); job retries, watchdog and a trading kill switch (`TURTLE_TRADING_ENABLED`).
- **Strategy.** 20-day Donchian breakout with volume confirmation, ranking by breakout strength, two independent blocks of 20 slots (block 2: breakout strength ≥ 10 %), 10-day-low trailing stop. Defaults reproduce the production setup exactly.
- **State and reconciliation.** SQLite state store (WAL) as the bot's source of truth; reconciliation as a pure planner with persisted discrepancies; safety rules against incomplete broker snapshots, mass disappearance and foreign stop orders (fix for the 12 Oct 2026 incident).
- **Data quality.** Stop lows cross-checked against TWS daily bars; warnings for outlier lows, takeover patterns and very wide stops.
- **Exit accounting.** Partial fills of one stop are combined at their volume-weighted price; `turtlebot import-executions` backfills exit prices from an IBKR Flex Query or Activity Statement; `turtlebot confirm-closed` for manually verified exits.
- **Dashboard.** FastAPI REST and WebSocket backend with a hand-built frontend: equity against the S&P 500 (rebased to 100) with project phases and drawdown, headline KPIs, slot grids per block, the book with cushion to stop, risk panel with safety guards, activity feed and a read-only assistant backed by the local Claude Code CLI. Dark and light theme.
- **Demo.** `turtlebot dashboard --demo` with synthetic data, and `turtlebot export-demo` for a static copy on GitHub Pages (manual workflow).
- **Backtest.** Portfolio backtest that reuses the live signal, allocation and sizing code, plus performance metrics (Sharpe, Sortino, drawdown, profit factor).
- **Documentation.** Strategy, research journey, paper-trading results and an IBKR setup guide under `docs/`.
- **Tooling.** Pinned dependencies, ruff, black, mypy `--strict`, pre-commit, 330+ tests (~93 % coverage, no broker needed) and CI on Python 3.12–3.14.

### Changed

- The nightly TWS reset is treated as an expected disconnect, not an error.

### Legacy

- The original monolithic bot (v1, 05–26 Oct 2026) and the research scripts are kept unchanged in `legacy/`.
