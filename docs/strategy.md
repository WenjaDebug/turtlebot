# Strategy

A long-only Donchian breakout in the tradition of the Turtle traders, applied to roughly 2,200
liquid US stocks. The rules are deliberately simple; the work went into choosing *which* signals
get capital and into executing them safely.

## Rules

| | Rule | Setting |
|---|---|---|
| Universe | S&P 500, S&P 400, S&P 600 and the Nasdaq Composite, without ETFs, SPACs and test issues | rebuilt every morning |
| Liquidity | 20-day average dollar volume > 5 M USD, market cap > 500 M USD, price above a floor | `TURTLE_RISK__MIN_*` |
| Entry signal | Close above the highest high of the previous 20 days, volume > 1.5× its 20-day average | `TURTLE_STRATEGY__*` |
| Ranking | Breakout strength = close / 20-day high − 1, strongest first | `RANK_BY=breakout_strength` |
| Execution | Scan at 15:45 ET on near-final data, market-on-close order; market-on-open after the MOC cutoff | `TURTLE_SCHEDULE__SCAN_TIMES` |
| Initial stop | Lowest low of the last 10 days, as a GTC stop order attached to the entry | bracket order |
| Trailing stop | Every morning raised to the new 10-day low, never lowered | 09:00 ET |
| Position size | 1.5 % of net liquidation, whole shares, after a TWS what-if margin check | `TURTLE_RISK__POSITION_PCT` |
| Limits | Max 10 new entries per block and day; daily loss kill switch at −2 % | |

There is no profit target. A position ends only when its trailing stop is hit.

## Two blocks

The same scan feeds two independent pools of 20 slots each:

* **Block 1** takes the strongest signals of the day, no further filter.
* **Block 2** only takes signals with a breakout strength of at least 10 % that block 1 does not
  already hold or trade that day.

Both blocks use identical entry, exit and sizing rules. Splitting them keeps the evidence clean:
block 2 is a live test of the strongest result of the research below, while block 1 stays
comparable with the original strategy.

## Why these choices

The research is described step by step in [research-journey.md](research-journey.md). The
decisions that shaped the rules:

**The selection rule matters more than the exit.** There are around 1,000 breakout signals a year
but only a few free slots on any given day. In a 5-slot portfolio backtest, the same rules
produced anything from −7 % to +163 % depending only on the start day. A single backtest run is
therefore no basis for a decision; every variant was judged across many start days or across all
signals individually.

**Rank by breakout strength, not by volume.** On 1,047 signals split into quintiles, the volume
ratio showed no relationship with the outcome (1.5× was as good as 3×), while breakout strength,
20-day momentum and the distance above the 50-day average did, in both halves of the year. Across
20 start days and after a rough survivorship correction, the median result was +94 % when ranking
by breakout strength and +33 % when ranking by volume ratio.

**A minimum breakout strength for block 2, no cap on the stop distance.** Every breakout signal
in the live universe from 2016 to October 2026 (52,998 trades) was simulated individually with the
production exit and 0.2 % costs per trade:

| Filter | Trades | Win rate | Avg. return | Median | Profit factor | Avg. without top 1 % | Years with avg. > 0 |
|---|---|---|---|---|---|---|---|
| none | 52,998 | 37.7 % | +1.21 % | −2.03 % | 1.31 | −0.45 % | 8/11 |
| strength ≥ 3 % | 24,621 | 39.2 % | +2.29 % | −2.29 % | 1.47 | −0.28 % | 8/11 |
| **strength ≥ 10 %** | **5,554** | **41.3 %** | **+7.31 %** | −2.43 % | **2.08** | **+0.73 %** | **10/11** |
| strength ≥ 15 % | 2,523 | 41.7 % | +4.35 % | −2.73 % | 1.52 | +1.06 % | 9/11 |
| stop distance ≤ 20 % | 45,688 | 37.6 % | +0.39 % | −1.89 % | 1.12 | −0.42 % | 8/11 |
| strength ≥ 10 % and stop ≤ 20 % | 1,061 | 43.3 % | +0.19 % | −1.23 % | 1.06 | −0.40 % | 6/11 |

Two things stand out. Capping the stop distance hurt in every combination: it removes exactly the
violent breakouts that make the money. And the edge of a strength filter comes from a few large
winners, not from the typical trade, whose median is negative in every row. That is the nature of
trend following, and it is why the strategy has no profit target and why its results must be
judged on total equity rather than on closed trades.

**Market-on-close instead of next-day open.** The scan runs shortly before the close on almost
final daily bars, so the order can still be filled at the signal day's close instead of being
exposed to the overnight gap. `legacy/research/backtest_moc_vs_nextopen.py` compares both entry
variants with an identical exit.

## Known weaknesses

* **Wide initial stops.** The 10-day low is on average 27 % below the entry for a fresh breakout.
  Position size is fixed at 1.5 % of equity rather than derived from the stop distance, so a single
  full stop-out can cost up to about 0.6 % of the account.
* **Gaps.** A stop is a promise only during trading hours. In October four Brazilian fintech
  stocks gapped 17–48 % below their stops on the same morning.
* **No concentration limit.** Nothing stops several positions on the same theme or country.
* **Takeover targets.** Ranking by breakout strength favours stocks that jump on an acquisition
  announcement and then stop moving.
* **Survivorship bias in the research.** The historical tests use today's index constituents.
  The bias was measured where possible (for example by removing names that only joined an index
  during the test window) and the decisions above hold with and without it, but the absolute
  numbers are optimistic.

Live results: [results.md](results.md).
