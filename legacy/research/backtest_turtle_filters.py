"""Filter-Backtest für Block 2 der Turtle-Breakout-Strategie (Vorstufe zum Live-Einbau).

Jedes Breakout-Signal im Live-Universum wird als Einzeltrade simuliert (wie analyze_turtle.py), aber
mit den Produktionsparametern und dem Exit, wie ihn live_trade_turtle.py tatsächlich ausführt:
  - Signal: Schluss > 20-Tage-Hoch (ohne heute) und Volumen > 1,5 × 20-Tage-Schnitt (strategies/signals.py)
  - Kauf: Market-on-Open am Folgetag
  - Stop: Stop-Order am tiefsten Tief der letzten 10 Tage inkl. Signaltag, jeden Morgen auf das
    10-Tage-Tief der abgeschlossenen Tage nachgezogen (nur nach oben); Auslösung intraday zum Stop,
    bei einer Eröffnungslücke darunter zum Open
  - je Ticker keine überlappenden Trades; am Ausstiegstag kein Neukauf desselben Tickers
Kosten: COST_ROUND_TRIP pauschal je Trade (Kommission + Slippage, Live-Woche: Ø +0,13 % beim Kauf).

Getestete Filter (alle beim 16:15-Scan bekannt):
  - Breakout-Strength = Schluss / 20-Tage-Hoch − 1, Mindestwert
  - Volatility Contraction: wie ruhig war der Titel VOR dem Ausbruch? Gemessen am Vortag des Signals,
    damit die grosse Spanne des Ausbruchstags selbst nicht mitzählt:
      cr_10_50   = ATR(10) / ATR(50)
      cr_10_100  = ATR(10) / ATR(100)
      atr_pctile = Perzentil von ATR(14)/Kurs innerhalb der letzten 252 Handelstage desselben Titels
    Schwelle = unterstes Quartil bzw. Terzil über alle Signale (als feste Zahl ausgegeben)
Die Obergrenze für den Stop-Abstand (getestet am 11.10.) hat in jeder Kombination geschadet und ist
verworfen; der Stop-Abstand wird nur noch zur Einordnung (Korrelation) mitgeführt.

Robustheit: Kennzahlen zusätzlich ohne die besten und schlechtesten je 1 % der Trades einer Zeile, und
wie viele der Extremgewinner (beste 1 % aller Trades ohne Filter) ein Filter behält.

Einschränkung: Universum = heutige Live-Liste (Survivorship-Bias: später delistete Titel fehlen).

Aufruf: python backtest_turtle_filters.py
"""

import json
import pathlib
import resource

import numpy as np
import pandas as pd

from data_sources import load_prices
from strategies.signals import turtle_breakout

UNIVERSE_FILE = pathlib.Path(".cache/live/eligible_20261009.json")
HISTORY_START = pd.Timestamp("2015-06-01")   # Vorlauf für Kanal, Volumen und ATR(100)
HISTORY_END = pd.Timestamp("2026-10-10")     # exklusiv
SIGNAL_START = pd.Timestamp("2016-01-01")
SPLIT_DATE = pd.Timestamp("2021-01-01")      # Stabilitäts-Check: 2016–2020 vs. 2021–2026

ENTRY_PERIOD, VOLUME_FACTOR, VOLUME_PERIOD = 20, 1.5, 20   # = PRODUCTION in live_trade_turtle.py
STOP_PERIOD = 10
COST_ROUND_TRIP = 0.002
TRIM = 0.01                                  # je 1 % beste/schlechteste Trades für die Robustheit

STRENGTH_LEVELS = [0.03, 0.10, 0.15]
BEST_STRENGTH = 0.10                         # beste Schwelle aus dem Lauf vom 11.10.
VC_MEASURES = {"cr_10_50": "ATR10/ATR50", "cr_10_100": "ATR10/ATR100", "atr_pctile": "ATR-Perzentil 252T"}
VC_QUANTILES = {0.25: "unterstes Quartil", 1 / 3: "unterstes Terzil"}

SIGNALS_CACHE = pathlib.Path(".cache/backtest_turtle_signals.pkl")
RESULTS_FILE = "backtest_turtle_filters_trades.csv"
pd.set_option("display.width", 220)


def atr(df, period):
    prev_close = df["Close"].shift(1)
    true_range = pd.concat([df["High"] - df["Low"], (df["High"] - prev_close).abs(),
                            (df["Low"] - prev_close).abs()], axis=1).max(axis=1)
    return true_range.rolling(period).mean()


def stop_exit(o, h, l, c, low_wide, i_entry, stop0):
    """Live-Stop Tag für Tag ab dem Einstiegstag. Gibt (Exit-Index, Exit-Preis, noch offen?) zurück.

    low_wide[d] = tiefstes Tief der 10 Tage VOR Tag d (Stand der 09:00-Anpassung).
    """
    stop = stop0
    for d in range(i_entry, len(o)):
        if d > i_entry and not np.isnan(low_wide[d]):  # morgendliche Anpassung, nur nach oben
            stop = max(stop, low_wide[d])
        if o[d] <= stop:
            return d, o[d], False
        if l[d] <= stop:
            return d, stop, False
    return len(o) - 1, c[-1], True


def signals_for(ticker, df):
    """Alle Signale eines Tickers mit Merkmalen am Signaltag und Ergebnis mit dem Live-Exit."""
    sig = turtle_breakout(df, entry_period=ENTRY_PERIOD, exit_period=STOP_PERIOD,
                          volume_period=VOLUME_PERIOD, volume_factor=VOLUME_FACTOR)
    o, h, l, c = (df[k].to_numpy(float) for k in ("Open", "High", "Low", "Close"))
    low_incl = df["Low"].rolling(STOP_PERIOD).min().to_numpy()            # inkl. heute (Signal-Stop)
    low_wide = df["Low"].rolling(STOP_PERIOD).min().shift(1).to_numpy()
    # Volatility Contraction, Stand Vortag des Signals (shift(1))
    cr_10_50 = (atr(df, 10) / atr(df, 50)).shift(1).to_numpy()
    cr_10_100 = (atr(df, 10) / atr(df, 100)).shift(1).to_numpy()
    atr_pctile = (atr(df, 14) / df["Close"]).rolling(252).rank(pct=True).shift(1).to_numpy()

    rows = []
    for i in np.flatnonzero(sig["entry"].to_numpy() & (df.index >= SIGNAL_START)):
        if i + 1 >= len(df) or np.isnan(low_incl[i]) or c[i] <= 0 or o[i + 1] <= 0:
            continue
        j, price, still_open = stop_exit(o, h, l, c, low_wide, i + 1, low_incl[i])
        rows.append(dict(ticker=ticker, i=i, j=j, signal_date=df.index[i],
                         ret=price / o[i + 1] - 1 - COST_ROUND_TRIP, days=j - (i + 1), open=still_open,
                         strength=float(sig["breakout_strength"].iat[i]), stop_dist=1 - low_incl[i] / c[i],
                         volume_ratio=float(sig["volume_ratio"].iat[i]),
                         cr_10_50=cr_10_50[i], cr_10_100=cr_10_100[i], atr_pctile=atr_pctile[i]))
    return rows


def load_signals():
    if SIGNALS_CACHE.exists():
        return pd.read_pickle(SIGNALS_CACHE)
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(10240, hard), hard))
    tickers = json.loads(UNIVERSE_FILE.read_text())["tickers"]
    prices = load_prices(tickers + ["SPY"], HISTORY_START, HISTORY_END)
    prices.pop("SPY", None)
    rows = []
    for n, (ticker, df) in enumerate(prices.items(), 1):
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        df = df[df["Volume"] > 0]
        if len(df) > ENTRY_PERIOD + 1:
            rows += signals_for(ticker, df)
        if n % 500 == 0:
            print(f"  {n}/{len(prices)} Titel ausgewertet, {len(rows)} Signale")
    sigs = pd.DataFrame(rows).sort_values(["ticker", "i"]).reset_index(drop=True)
    sigs.to_pickle(SIGNALS_CACHE)
    return sigs


def select(sigs, mask):
    """Filter anwenden, dann je Ticker chronologisch: ein Signal zählt nur, wenn der vorige Trade vor dem
    Signaltag endete (wie live: gehaltene und am selben Tag verkaufte Ticker werden nicht gekauft)."""
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


def summarize(t, n_days, ref_count, extreme_keys):
    r = t["ret"]
    lo, hi = r.quantile([TRIM, 1 - TRIM])
    trimmed = r[(r >= lo) & (r <= hi)]
    early, late = r[t["signal_date"] < SPLIT_DATE], r[t["signal_date"] >= SPLIT_DATE]
    years = r.groupby(t["signal_date"].dt.year).mean()
    kept = len(extreme_keys & set(zip(t["ticker"], t["signal_date"])))
    return pd.Series({
        "Trades": len(t), "Trades/Tag": len(t) / n_days, "Treffer": (r > 0).mean(),
        "Ø Rendite": r.mean(), "Median": r.median(), "PF": profit_factor(r),
        "Haltedauer": t["days"].mean(), "Ø 16–20": early.mean(), "Ø 21–26": late.mean(),
        "Jahre>0": f"{(years > 0).sum()}/{len(years)}",
        "Ø ±1% gekürzt": trimmed.mean(), "PF ±1% gekürzt": profit_factor(trimmed),
        "Extremgew. behalten": f"{kept}/{len(extreme_keys)}",
        # >1 = der Filter behält anteilig mehr Extremgewinner als Trades insgesamt
        "Anreicherung": (kept / len(extreme_keys)) / (len(t) / ref_count),
    })


FORMATS = {
    "Trades/Tag": "{:.2f}".format, "Treffer": "{:.1%}".format, "Ø Rendite": "{:+.2%}".format,
    "Median": "{:+.2%}".format, "PF": "{:.2f}".format, "Haltedauer": "{:.1f} T".format,
    "Ø 16–20": "{:+.2%}".format, "Ø 21–26": "{:+.2%}".format, "Ø ±1% gekürzt": "{:+.2%}".format,
    "PF ±1% gekürzt": "{:.2f}".format, "Anreicherung": "{:.2f}×".format,
}


def show(table, cols):
    out = table[cols].copy().astype(object)
    for c in cols:
        if c in FORMATS:
            out[c] = table[c].map(FORMATS[c])
        elif c == "Trades":
            out[c] = table[c].astype(int)
    return out.to_string()


def bucket_table(t, feature, q=5):
    buckets = pd.qcut(t[feature], q, duplicates="drop")
    g = t.groupby(buckets, observed=True)["ret"]
    lo, hi = t["ret"].quantile([TRIM, 1 - TRIM])
    trimmed = t[(t["ret"] >= lo) & (t["ret"] <= hi)].groupby(buckets, observed=True)["ret"].mean()
    extreme = t["ret"] >= t["ret"].quantile(1 - TRIM)
    table = pd.DataFrame({"Trades": g.size(), "Treffer": g.apply(lambda r: (r > 0).mean()),
                          "Ø Rendite": g.mean(), "Median": g.median(), "Ø ±1% gekürzt": trimmed,
                          "Extremgew.": extreme.groupby(buckets, observed=True).sum()})
    table.index = [f"{iv.left:.3f} … {iv.right:.3f}" for iv in table.index]
    return table.to_string(formatters={"Treffer": "{:.0%}".format, "Ø Rendite": "{:+.2%}".format,
                                       "Median": "{:+.2%}".format, "Ø ±1% gekürzt": "{:+.2%}".format})


def main():
    sigs = load_signals()
    n_days = sigs["signal_date"].nunique()
    all_on = pd.Series(True, index=sigs.index)
    reference = select(sigs, all_on)
    extreme_cut = reference["ret"].quantile(1 - TRIM)
    extreme_keys = set(zip(*reference.loc[reference["ret"] >= extreme_cut, ["ticker", "signal_date"]].T.values))

    cutoffs = {(m, q): sigs[m].quantile(q) for m in VC_MEASURES for q in VC_QUANTILES}
    filters = {"Referenz: kein Filter": all_on}
    for s in STRENGTH_LEVELS:
        filters[f"Strength ≥ {s:.0%}"] = sigs["strength"] >= s
    for strength in (None, BEST_STRENGTH):
        for m, name in VC_MEASURES.items():
            for q, qname in VC_QUANTILES.items():
                mask = sigs[m] <= cutoffs[(m, q)]
                label = f"VC {name} ≤ {cutoffs[(m, q)]:.3f} ({qname})"
                if strength is not None:
                    mask &= sigs["strength"] >= strength
                    label = f"Strength ≥ {strength:.0%} + {label}"
                filters[label] = mask

    trades = {label: select(sigs, mask) for label, mask in filters.items()}
    table = pd.DataFrame({label: summarize(t, n_days, len(reference), extreme_keys)
                          for label, t in trades.items()}).T

    print(f"Universum: {sigs['ticker'].nunique()} Titel mit Signalen (Live-Liste vom 09.10.2026), Signale "
          f"{sigs['signal_date'].min().date()} – {sigs['signal_date'].max().date()}, Exit wie live, "
          f"Kosten {COST_ROUND_TRIP:.1%} pro Trade")
    print(f"Extremgewinner = beste 1 % der Referenz-Trades: {len(extreme_keys)} Trades mit Rendite ≥ {extreme_cut:+.1%}\n")

    print("===== B.1  Filter-Kombinationen =====")
    print(show(table, ["Trades", "Trades/Tag", "Treffer", "Ø Rendite", "Median", "PF", "Haltedauer",
                       "Ø 16–20", "Ø 21–26", "Jahre>0"]))

    print("\n===== B.3  Robustheit: ohne beste und schlechteste je 1 % der Trades, Extremgewinner =====")
    print(show(table, ["Trades", "Ø Rendite", "Ø ±1% gekürzt", "PF", "PF ±1% gekürzt",
                       "Extremgew. behalten", "Anreicherung"]))

    print("\n===== B.2  Spearman-Korrelation Merkmal ↔ Rendite (alle Referenz-Trades / nur Strength ≥ 10 %) =====")
    strong = reference[reference["strength"] >= BEST_STRENGTH]
    for feature in ["strength", "stop_dist", *VC_MEASURES]:
        a = reference[[feature, "ret"]].dropna().corr(method="spearman").iat[0, 1]
        b = strong[[feature, "ret"]].dropna().corr(method="spearman").iat[0, 1]
        print(f"  {feature:<11} {a:+.3f}   |   {b:+.3f}")

    for m, name in VC_MEASURES.items():
        print(f"\n--- Quintile {name} (alle Referenz-Trades) ---")
        print(bucket_table(reference.dropna(subset=[m]), m))
    print("\n--- Quintile ATR10/ATR50, nur Strength ≥ 10 % ---")
    print(bucket_table(strong.dropna(subset=["cr_10_50"]), "cr_10_50"))

    print("\n--- Ø Rendite je Signaljahr ---")
    by_year = pd.DataFrame({label: t.groupby(t["signal_date"].dt.year)["ret"].mean()
                            for label, t in trades.items()}).T
    print(by_year.to_string(float_format="{:+.1%}".format))

    reference.to_csv(RESULTS_FILE, index=False)
    print(f"\nGespeichert: {RESULTS_FILE} (alle Referenz-Trades mit Merkmalen)")


if __name__ == "__main__":
    main()
