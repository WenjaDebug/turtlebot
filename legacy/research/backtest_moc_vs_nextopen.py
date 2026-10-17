"""
backtest_moc_vs_nextopen.py
----------------------------
Vergleich zweier Einstiegsvarianten fuer dasselbe Turtle-Breakout-Signal (Produktionsparameter,
identische Exit-Logik wie backtest_turtle_filters.py / live_trade_turtle.py):

  A) STATUS QUO:  Kauf per Market-on-Open am naechsten Handelstag nach dem Signal
  B) MOC-VARIANTE: Kauf per Market-on-Close noch am Signaltag selbst (Produktion scannt nach
     Boersenschluss auf Basis des Tagesschlusses - eine MOC-Order wuerde VOR Schluss anhand fast
     fertiger Daten gesendet und zum Schlusskurs ausgefuehrt)

Exit (Stop) ist in beiden Varianten identisch: 10-Tage-Tief inkl. Signaltag, morgens nachgezogen,
Ausloesung ab dem ersten Handelstag NACH dem Signaltag (Position existiert ab da so oder so).
Dieselben Kosten (COST_ROUND_TRIP) fuer beide Varianten, damit der Vergleich fair ist.

Nutzt dieselbe Preis-Cache (.cache/prices_...) wie backtest_turtle_filters.py - kein Netzwerk noetig.
"""
import json
import pathlib

import numpy as np
import pandas as pd

from data_sources import load_prices
from strategies.signals import turtle_breakout

UNIVERSE_FILE = pathlib.Path(".cache/live/eligible_20261009.json")
HISTORY_START = pd.Timestamp("2015-06-01")
HISTORY_END = pd.Timestamp("2026-10-10")
SIGNAL_START = pd.Timestamp("2016-01-01")

ENTRY_PERIOD, VOLUME_FACTOR, VOLUME_PERIOD = 20, 1.5, 20   # = PRODUCTION in live_trade_turtle.py
STOP_PERIOD = 10
COST_ROUND_TRIP = 0.002
BEST_STRENGTH = 0.10   # Block-2-Schwelle, separat ausgewertet

pd.set_option("display.width", 200)


def atr(df, period):
    prev_close = df["Close"].shift(1)
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - prev_close).abs(),
                    (df["Low"] - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def stop_exit(o, h, l, c, low_wide, i_entry, stop0):
    stop = stop0
    for d in range(i_entry, len(o)):
        if d > i_entry and not np.isnan(low_wide[d]):
            stop = max(stop, low_wide[d])
        if o[d] <= stop:
            return d, o[d], False
        if l[d] <= stop:
            return d, stop, False
    return len(o) - 1, c[-1], True


def signals_for(ticker, df):
    sig = turtle_breakout(df, entry_period=ENTRY_PERIOD, exit_period=STOP_PERIOD,
                          volume_period=VOLUME_PERIOD, volume_factor=VOLUME_FACTOR)
    o, h, l, c = (df[k].to_numpy(float) for k in ("Open", "High", "Low", "Close"))
    low_incl = df["Low"].rolling(STOP_PERIOD).min().to_numpy()
    low_wide = df["Low"].rolling(STOP_PERIOD).min().shift(1).to_numpy()

    rows = []
    for i in np.flatnonzero(sig["entry"].to_numpy() & (df.index >= SIGNAL_START)):
        if i + 1 >= len(df) or np.isnan(low_incl[i]) or c[i] <= 0 or o[i + 1] <= 0:
            continue
        j, price, still_open = stop_exit(o, h, l, c, low_wide, i + 1, low_incl[i])
        rows.append(dict(
            ticker=ticker, i=i, j=j, signal_date=df.index[i],
            ret_open=price / o[i + 1] - 1 - COST_ROUND_TRIP,       # A) Kauf naechster Morgen
            ret_moc=price / c[i] - 1 - COST_ROUND_TRIP,            # B) Kauf am Signaltag per MOC
            days_open=j - (i + 1), days_moc=j - i, still_open=still_open,
            strength=float(sig["breakout_strength"].iat[i]),
            volume_ratio=float(sig["volume_ratio"].iat[i]),
        ))
    return rows


def load_signals():
    cache = pathlib.Path(".cache/backtest_moc_vs_open_signals.pkl")
    if cache.exists():
        return pd.read_pickle(cache)
    tickers = json.loads(UNIVERSE_FILE.read_text())["tickers"]
    prices = load_prices(tickers, HISTORY_START, HISTORY_END)
    rows = []
    for n, (ticker, df) in enumerate(prices.items(), 1):
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        df = df[df["Volume"] > 0]
        if len(df) > ENTRY_PERIOD + 1:
            rows += signals_for(ticker, df)
        if n % 500 == 0:
            print(f"  {n}/{len(prices)} Titel ausgewertet, {len(rows)} Signale")
    sigs = pd.DataFrame(rows).sort_values(["ticker", "i"]).reset_index(drop=True)
    sigs.to_pickle(cache)
    return sigs


def select(sigs, mask):
    """Wie live: ein Signal zaehlt nur, wenn der vorige Trade (ueber den LAENGEREN MOC-Haltezeitraum,
    konservativ) vor dem Signaltag endete - keine ueberlappenden Trades je Ticker."""
    chosen, busy_until, last_ticker = [], -1, None
    for idx, ticker, i, j in sigs.loc[mask, ["ticker", "i", "j"]].itertuples():
        if ticker != last_ticker:
            busy_until, last_ticker = -1, ticker
        if i <= busy_until:
            continue
        chosen.append(idx)
        busy_until = j
    return sigs.loc[chosen]


def profit_factor(r):
    return r[r > 0].sum() / -r[r <= 0].sum()


def summarize(t, ret_col, days_col, n_days):
    r = t[ret_col]
    return pd.Series({
        "Trades": len(t), "Trades/Tag": len(t) / n_days, "Treffer": (r > 0).mean(),
        "Ø Rendite": r.mean(), "Median": r.median(), "PF": profit_factor(r),
        "Haltedauer": t[days_col].mean(), "Summe Rendite (unverzinst)": r.sum(),
    })


FORMATS = {
    "Trades/Tag": "{:.2f}".format, "Treffer": "{:.1%}".format, "Ø Rendite": "{:+.3%}".format,
    "Median": "{:+.3%}".format, "PF": "{:.2f}".format, "Haltedauer": "{:.1f} T".format,
    "Summe Rendite (unverzinst)": "{:+.1f}".format,
}


def show(table):
    out = table.copy().astype(object)
    for c in table.columns:
        if c in FORMATS:
            out[c] = table[c].map(FORMATS[c])
        elif c == "Trades":
            out[c] = table[c].astype(int)
    return out.to_string()


def main():
    sigs = load_signals()
    n_days = sigs["signal_date"].nunique()

    for label, mask in [("Alle Signale (Block 1, kein Strength-Filter)", pd.Series(True, index=sigs.index)),
                        (f"Nur Strength >= {BEST_STRENGTH:.0%} (Block 2)", sigs["strength"] >= BEST_STRENGTH)]:
        chosen = select(sigs, mask)
        table = pd.DataFrame({
            "A) Kauf naechster Morgen (Status quo)": summarize(chosen, "ret_open", "days_open", n_days),
            "B) Kauf MOC am Signaltag": summarize(chosen, "ret_moc", "days_moc", n_days),
        }).T
        print(f"\n===== {label} =====")
        print(show(table))
        diff = chosen["ret_moc"] - chosen["ret_open"]
        print(f"  Differenz je Trade (B − A): Ø {diff.mean():+.3%}, Median {diff.median():+.3%}, "
              f"B besser in {(diff > 0).mean():.1%} der Trades")

    sigs.to_csv("backtest_moc_vs_nextopen_signals.csv", index=False)
    print("\nGespeichert: backtest_moc_vs_nextopen_signals.csv (alle Signale mit beiden Renditen)")


if __name__ == "__main__":
    main()
