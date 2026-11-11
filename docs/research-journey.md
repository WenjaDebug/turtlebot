# Research journey

How a weekend experiment with moving averages turned into a two-block breakout system with its own
state store and reconciliation, in about six weeks. The scripts of every step are kept in
[`legacy/research/`](../legacy/research) so the results can be traced back to code.

Every step was built with Claude Code as a pair programmer: I set the questions, the rules and the
acceptance criteria, reviewed the results and decided what to keep.

## 1. Classic indicators on a handful of stocks (29–30 Sep)

The first tests used single stocks and textbook rules, mainly to learn the tooling (yfinance,
backtrader, IBKR paper trading) and how easily a backtest misleads.

| Test | Result | Lesson |
|---|---|---|
| SMA 50/200 crossover, AAPL, 2 years | +38.6 % vs. +44.2 % buy & hold, one trade | Without warm-up data the 200-day average starts late; on AMD the strategy never traded while the stock rose 280 % |
| RSI(14) mean reversion vs. SMA crossover, 5 stocks, 5 years | Both far behind buy & hold on all five | The "RSI above 70" exit almost never fired: 30 of 32 trades ended on the 20-day time limit |
| Bollinger mean reversion with an ATR filter, 5 stocks, 12 months | +1.4 % vs. +15.7 % for SPY | Low drawdown only because most of the capital sat in cash; the filter never blocked a trade |

Conclusion: rules that look sensible on a chart did not beat holding the index, and small samples
(2–8 trades per stock) cannot tell good from lucky.

## 2. Portfolios on a broad universe (01–03 Oct)

The next tests moved to portfolios with a fixed number of slots on the S&P 500 and Nasdaq-100,
and the signal code was separated into pure functions so the same code could later run live.

* **Two-strategy portfolio, 6 months.** SMA crossover on energy and tech: +2.7 %. 20-day Donchian
  breakout with volume confirmation: +11.2 %. SPY: +16.4 %. The breakout was the only idea with
  a recognisable trend-following profile: more losers than winners, but winners of +30 to +40 %.
* **Two halves, 12 months.** A 2-day loser reversal made +11.9 % with a 52 % win rate, which is a
  coin flip that costs would eat. The leveraged breakout showed +200 %, which was immediately
  treated as suspicious: names such as LITE and ECHO were bought months before they joined the
  index. **Survivorship bias** became a standing item in every later test.

## 3. Taking the breakout apart (05 Oct)

Before changing any rule, the question was *why* the breakout result was so extreme.

* **Start-day sensitivity.** The unchanged strategy, started on 20 different days in the same
  month, returned anything from −7 % to +163 %. With ~1,000 signals a year and 5 slots, early
  random choices decide which later signals fit. From here on, variants were compared across many
  start days or across all signals individually.
* **What a winner looks like on the signal day.** All 1,047 signals simulated one by one and split
  into quintiles: breakout strength, 20-day momentum and distance above the 50-day average
  separated winners from losers in both halves of the year. The volume ratio, used for ranking
  until then, did not.
* **Ranking.** Switching the ranking from volume ratio to breakout strength raised the median
  result across start days, before and after removing late index joiners.

The same day the strategy went onto the IBKR paper account, first in a deliberately loose
**test mode** (10-day breakout, 1.3× volume, hourly scans) to prove order routing, fills, stop
orders and notifications under real conditions. Those first 20 positions account for almost the
whole loss so far; see [results.md](results.md).

## 4. A detour: cross-asset trend following (07 Oct)

A monthly time-series momentum strategy on seven ETFs (stocks, bonds, commodities) with a
volatility target, 2006–2026: CAGR 5.6 % vs. 11.2 % for SPY, max drawdown −16.5 % vs. −55.2 %,
+8.4 % in 2008. A good crash hedge, but a lower Sharpe ratio than simply holding SPY (0.48 vs.
0.54), so it was not pursued.

## 5. Ten years of signals and a second block (11–12 Oct)

The question for block 2 was whether a filter known at signal time improves the breakout. Every
signal in the live universe from 2016 to October 2026 was simulated with the production exit and
costs: 52,998 trades.

* A **minimum breakout strength of 10 %** raised the profit factor from 1.31 to 2.08 and was
  positive in 10 of 11 years.
* A **cap on the stop distance** hurt in every combination, because it removes the violent
  breakouts that carry the result.
* **Volatility contraction** before the breakout helped somewhat on its own, but much of the
  combined effect came from 2016–2020 and faded afterwards.

Block 2 went live with the strength filter as its only difference to block 1. Details in
[strategy.md](strategy.md).

## 6. The incident and the rewrite (12–27 Oct)

On 12 Oct the legacy bot reconnected to TWS, received an empty position list before TWS had
finished syncing, concluded that all 20 positions were closed and **cancelled their stop orders**.
The positions stayed open without protection until a repair script re-placed the stops, and the
repair exposed a second problem: stops placed by another API client id could no longer be
modified by the bot.

That was the end of the monolithic script. The replacement, the `turtlebot` package in `src/`, was
designed around one rule: unclear broker data means do nothing destructive and alert. It brought a
SQLite state store, a reconciliation that produces an explicit plan instead of side effects,
stop-ownership handling, a backtest that shares the live signal code, 260+ tests and CI. The new
engine took over the paper account on 27 Oct after dry runs against TWS.

## 7. Review and publication (11 Nov)

A full performance review separated the test-mode losses from the production rules, compared the
bot with rebuilt indices, and turned the operational findings into code: exact exit prices from
the IBKR trade export, the nightly TWS reset treated as a normal disconnect, and a dashboard that
shows the bot against the S&P 500 with the project phases. The results are in
[results.md](results.md).

## What I would do differently

* Start with the full universe and a per-signal simulation, not with single stocks.
* Measure start-day sensitivity and survivorship bias before believing any portfolio number.
* Keep test mode on a separate paper account, so its trades do not mix with the production record.
* Build the reconciliation first. The strategy was never the risky part; state handling was.
