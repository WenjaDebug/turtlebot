import datetime as dt
import sys
sys.path.insert(0, ".")

import pandas as pd
from data_sources import load_index_details, load_market_caps, load_prices, load_universe
from strategies.signals import turtle_breakout
import analyze_turtle as AT

UNIV_DATE = dt.date(2026, 10, 3)
CAP_DATE = dt.date(2026, 10, 9)
DETAILS_DATE = dt.date(2026, 10, 5)
START = pd.Timestamp("2026-07-26")
END = pd.Timestamp("2026-10-09")
BENCHMARK = "SPY"
MIN_MARKET_CAP = 500e6

WINDOW_START = pd.Timestamp("2026-08-24")
WINDOW_END = END

universe = load_universe(UNIV_DATE)["ticker"].tolist()
prices = load_prices(universe + [BENCHMARK], START, END, cache=True)
spy = prices.pop(BENCHMARK)
caps = load_market_caps(universe, CAP_DATE)
details = load_index_details(DETAILS_DATE).set_index("ticker")

prepared = {t: AT.prepare(df, spy) for t, df in prices.items() if (caps.get(t) or 0) > MIN_MARKET_CAP}
print(f"Geladen: {len(prices)} Ticker, {len(prepared)} nach Market-Cap-Filter")

AT.WINDOW_START = WINDOW_START
AT.WINDOW_END = WINDOW_END

results = {}
for name, rule in AT.EXITS.items():
    rows = [dict(ticker=t, **tr) for t, ind in prepared.items() for tr in AT.simulate(ind, rule)]
    results[name] = pd.DataFrame(rows)
    print(name, "->", len(rows), "trades")

summary = pd.DataFrame({name: AT.summarize(df) for name, df in results.items() if len(df)}).T
print()
print(AT.fmt_summary(summary))
summary.to_csv("exit_summary_cached.csv")
