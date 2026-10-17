"""Analyse der Turtle-Breakout-Strategie (Hälfte 2) auf Basis ALLER Breakout-Signale im Backtest-Fenster.

Der Portfolio-Backtest nimmt wegen der 5 Slots nur einen Bruchteil der Signale. Hier wird jedes Signal
einzeln als Trade simuliert (Kauf zum nächsten Open, pro Ticker keine überlappenden Trades), um
1) Merkmale zum Signalzeitpunkt mit dem Ergebnis zu vergleichen (Verlierer vermeiden) und
2) alternative Exit-Regeln auf denselben Einstiegen zu vergleichen.

Aufruf: python analyze_turtle.py
"""

import numpy as np
import pandas as pd

from backtest_two_halves import (BENCHMARK, HISTORY_START, MIN_MARKET_CAP, TODAY, WINDOW_END,
                                 WINDOW_START)
from data_sources import load_index_details, load_market_caps, load_prices, load_universe
from strategies.signals import turtle_breakout

pd.set_option("display.width", 160)


def atr(df, period=14):
    prev_close = df["Close"].shift(1)
    true_range = pd.concat([df["High"] - df["Low"], (df["High"] - prev_close).abs(),
                            (df["Low"] - prev_close).abs()], axis=1).max(axis=1)
    return true_range.rolling(period).mean()


def rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss)


# ---------- Exit-Regeln: bekommen Indikatoren + Einstiegsindex, liefern (Exit-Index, Preis, offen?) ----------

def channel_exit(period):
    def rule(ind, i0):
        low = ind["Low"].rolling(period).min().shift(1)
        for j in range(i0, len(ind) - 1):
            if ind["Close"].iat[j] < low.iat[j]:
                return j + 1, ind["Open"].iat[j + 1], False
        return len(ind) - 1, ind["Close"].iat[-1], True
    return rule


def chandelier_exit(mult):
    """Trailing Stop: Schluss unter (höchstes Hoch seit Einstieg − mult × ATR) -> Verkauf zum nächsten Open."""
    def rule(ind, i0):
        peak = -np.inf
        for j in range(i0, len(ind) - 1):
            peak = max(peak, ind["High"].iat[j])
            if ind["Close"].iat[j] < peak - mult * ind["atr"].iat[j]:
                return j + 1, ind["Open"].iat[j + 1], False
        return len(ind) - 1, ind["Close"].iat[-1], True
    return rule


def with_initial_stop(base, mult):
    """Turtle-Klassiker: zusätzlich harter Stop mult × ATR unter dem Einstieg (intraday, Gap -> Open)."""
    def rule(ind, i0):
        j_base, p_base, still_open = base(ind, i0)
        stop = ind["Open"].iat[i0] - mult * ind["atr"].iat[i0 - 1]
        for j in range(i0, j_base):
            if ind["Open"].iat[j] <= stop:
                return j, ind["Open"].iat[j], False
            if ind["Low"].iat[j] <= stop:
                return j, stop, False
        return j_base, p_base, still_open
    return rule


def tighten_after_gain(gain, wide=10, tight=5):
    """10-Tage-Tief-Exit, aber sobald der Trade einmal +gain im Plus war, auf das 5-Tage-Tief umschalten."""
    def rule(ind, i0):
        low_wide = ind["Low"].rolling(wide).min().shift(1)
        low_tight = ind["Low"].rolling(tight).min().shift(1)
        entry, tightened = ind["Open"].iat[i0], False
        for j in range(i0, len(ind) - 1):
            tightened |= ind["High"].iat[j] >= entry * (1 + gain)
            if ind["Close"].iat[j] < (low_tight if tightened else low_wide).iat[j]:
                return j + 1, ind["Open"].iat[j + 1], False
        return len(ind) - 1, ind["Close"].iat[-1], True
    return rule


def time_stop_losers(days, base):
    """Basis-Exit, aber nach `days` Handelstagen verkaufen, falls der Trade dann im Minus liegt."""
    def rule(ind, i0):
        j_base, p_base, still_open = base(ind, i0)
        j = i0 + days
        if j < j_base and j < len(ind) - 1 and ind["Close"].iat[j] < ind["Open"].iat[i0]:
            return j + 1, ind["Open"].iat[j + 1], False
        return j_base, p_base, still_open
    return rule


BASE = channel_exit(10)
EXITS = {
    "Basis: Schluss < 10-Tage-Tief": BASE,
    "Schluss < 5-Tage-Tief": channel_exit(5),
    "Schluss < 20-Tage-Tief": channel_exit(20),
    "Basis + Stop 2×ATR (Turtle 2N)": with_initial_stop(BASE, 2),
    "Chandelier 3×ATR (Trailing)": chandelier_exit(3),
    "Basis, ab +20 % auf 5-Tage-Tief": tighten_after_gain(0.20),
    "Basis + Zeit-Stop: Tag 10 im Minus": time_stop_losers(10, BASE),
}


def prepare(df, spy):
    ind = df.copy()
    sig = turtle_breakout(df)
    ind["entry"] = sig["entry"]
    ind["volume_ratio"] = sig["volume_ratio"]
    ind["breakout_pct"] = df["Close"] / sig["donchian_high"] - 1
    ind["atr"] = atr(df)
    ind["atr_pct"] = ind["atr"] / df["Close"]
    ind["day_return"] = df["Close"].pct_change()
    ind["mom_20d"] = df["Close"].pct_change(20)
    ind["ext_sma50"] = df["Close"] / df["Close"].rolling(50).mean() - 1
    ind["rsi14"] = rsi(df["Close"])
    ind["close_location"] = (df["Close"] - df["Low"]) / (df["High"] - df["Low"])
    ind["gap_next_open"] = df["Open"].shift(-1) / df["Close"] - 1
    spy_close = spy["Close"].reindex(df.index).ffill()
    ind["spy_mom_20d"] = spy_close.pct_change(20)
    ind["spy_above_sma50"] = spy_close > spy_close.rolling(50).mean()
    return ind


def simulate(ind, rule):
    """Alle Signale im Fenster als Einzeltrades; während ein Trade läuft, keine neuen Signale desselben Tickers."""
    trades, busy_until = [], -1
    in_window = (ind.index >= WINDOW_START) & (ind.index < WINDOW_END)
    for i in np.flatnonzero(ind["entry"].to_numpy() & in_window):
        if i <= busy_until or i + 1 >= len(ind):
            continue
        j, price, still_open = rule(ind, i + 1)
        entry = ind["Open"].iat[i + 1]
        path = ind.iloc[i + 1:j + 1]
        trades.append({"signal_date": ind.index[i], "exit_date": ind.index[j], "entry": entry,
                       "exit": price, "return": price / entry - 1, "days": j - (i + 1),
                       "mfe": path["High"].max() / entry - 1, "mae": path["Low"].min() / entry - 1,
                       "open": still_open, **ind.iloc[i][FEATURES].to_dict()})
        busy_until = j
    return trades


FEATURES = ["volume_ratio", "breakout_pct", "atr_pct", "day_return", "mom_20d", "ext_sma50",
            "rsi14", "close_location", "gap_next_open", "spy_mom_20d", "spy_above_sma50"]
LABELS = {
    "volume_ratio": "Volumen / 20T-Schnitt", "breakout_pct": "Schluss über Donchian-Hoch",
    "atr_pct": "Volatilität (ATR in % vom Kurs)", "day_return": "Tagesrendite am Signaltag",
    "mom_20d": "Momentum 20 Tage", "ext_sma50": "Abstand zur SMA50", "rsi14": "RSI(14)",
    "close_location": "Schluss in Tagesspanne (0=Tief, 1=Hoch)",
    "gap_next_open": "Gap zum Einstiegs-Open", "spy_mom_20d": "S&P 500 Momentum 20 Tage",
}


def summarize(trades):
    closed = trades[~trades["open"]]
    wins, losses = trades[trades["return"] > 0], trades[trades["return"] <= 0]
    return pd.Series({
        "Trades": len(trades),
        "Trefferquote": len(wins) / len(trades),
        "Ø Rendite": trades["return"].mean(),
        "Median": trades["return"].median(),
        "Ø Gewinner": wins["return"].mean(),
        "Ø Verlierer": losses["return"].mean(),
        "Profit-Faktor": wins["return"].sum() / -losses["return"].sum(),
        "Ø Haltedauer": trades["days"].mean(),
        "noch offen": int(trades["open"].sum()),
        "Ø Gewinn verschenkt": (closed["mfe"] - closed["return"]).mean(),
    })


def fmt_summary(df):
    pct = ["Trefferquote", "Ø Rendite", "Median", "Ø Gewinner", "Ø Verlierer", "Ø Gewinn verschenkt"]
    out = df.copy().astype(object)
    for c in pct:
        out[c] = df[c].map("{:+.1%}".format if c != "Trefferquote" else "{:.0%}".format)
    out["Profit-Faktor"] = df["Profit-Faktor"].map("{:.2f}".format)
    out["Ø Haltedauer"] = df["Ø Haltedauer"].map("{:.0f} T".format)
    out["Trades"] = df["Trades"].astype(int)
    return out.to_string()


def bucket_table(trades, feature, q=5):
    buckets = pd.qcut(trades[feature], q, duplicates="drop")
    halves = np.where(trades["signal_date"] < trades["signal_date"].median(), "H1", "H2")
    g = trades.groupby(buckets, observed=True)
    table = pd.DataFrame({
        "Trades": g.size(),
        "Treffer": g["return"].apply(lambda r: (r > 0).mean()),
        "Ø Rendite": g["return"].mean(),
        "Median": g["return"].median(),
        "Ø 1. Hälfte": trades.groupby([buckets, halves], observed=True)["return"].mean().unstack()["H1"],
        "Ø 2. Hälfte": trades.groupby([buckets, halves], observed=True)["return"].mean().unstack()["H2"],
    })
    table.index = [f"{iv.left:+.3f} … {iv.right:+.3f}" if abs(iv.right) < 5 else f"{iv.left:.1f} … {iv.right:.1f}"
                   for iv in table.index]
    return table


def main():
    universe = load_universe(TODAY)["ticker"].tolist()
    prices = load_prices(universe + [BENCHMARK], HISTORY_START, WINDOW_END)
    spy = prices.pop(BENCHMARK)
    caps = load_market_caps(universe, TODAY)
    details = load_index_details(TODAY).set_index("ticker")
    prepared = {t: prepare(df, spy) for t, df in prices.items() if (caps.get(t) or 0) > MIN_MARKET_CAP}

    results = {}
    for name, rule in EXITS.items():
        rows = [dict(ticker=t, **tr) for t, ind in prepared.items() for tr in simulate(ind, rule)]
        results[name] = pd.DataFrame(rows)
    base = results["Basis: Schluss < 10-Tage-Tief"]
    base["sector"] = base["ticker"].map(details["sector"]).fillna("unbekannt")
    base["joined_later"] = base["ticker"].map(details["sp500_added"]) > base["signal_date"]
    base.to_csv("analyze_turtle_signals.csv", index=False)

    print(f"Alle Breakout-Signale {WINDOW_START.date()} – {WINDOW_END.date()}, je Signal ein Einzeltrade "
          f"(Kauf zum nächsten Open, ohne Hebel, ohne Kosten)\n")
    print("===== 1) Exit-Regeln im Vergleich (dieselben Einstiegssignale) =====")
    print("'Ø Gewinn verschenkt' = Ø Abstand zwischen bestem Kurs während des Trades und Verkaufskurs\n")
    print(fmt_summary(pd.DataFrame({n: summarize(r) for n, r in results.items()}).T))

    print("\n===== 2) Merkmale am Signaltag vs. Ergebnis (Basis-Exit, Quintile) =====")
    print("Ø 1./2. Hälfte = dieselbe Auswertung getrennt für frühe und späte Signale (Stabilitäts-Check)")
    for feature, label in LABELS.items():
        print(f"\n--- {label} ---")
        t = bucket_table(base.dropna(subset=[feature]), feature)
        print(t.to_string(formatters={"Treffer": "{:.0%}".format, "Ø Rendite": "{:+.1%}".format,
                                      "Median": "{:+.1%}".format, "Ø 1. Hälfte": "{:+.1%}".format,
                                      "Ø 2. Hälfte": "{:+.1%}".format}))

    print("\n--- Marktumfeld: S&P 500 über seiner SMA50? ---")
    print(base.groupby("spy_above_sma50")["return"].agg(Trades="size", Treffer=lambda r: (r > 0).mean(),
                                                       Rendite="mean").to_string())
    print("\n--- Sektor ---")
    print(base.groupby("sector")["return"].agg(Trades="size", Treffer=lambda r: (r > 0).mean(),
                                               Rendite="mean", Median="median")
          .sort_values("Rendite", ascending=False).to_string(float_format="{:+.3f}".format))
    print("\n--- Survivorship: Titel erst NACH dem Signal in den S&P 500 aufgenommen ---")
    print(base.groupby("joined_later")["return"].agg(Trades="size", Treffer=lambda r: (r > 0).mean(),
                                                     Rendite="mean").to_string())

    print("\n===== 3) Verlauf der Verlierer: Wie weit liefen sie zuerst ins Plus? (Basis-Exit) =====")
    losers = base[(base["return"] <= 0) & ~base["open"]]
    print(f"Verlierer: {len(losers)}; davon zwischenzeitlich mind. +5 % im Plus: {(losers['mfe'] >= 0.05).mean():.0%}, "
          f"mind. +10 %: {(losers['mfe'] >= 0.10).mean():.0%}")
    print(f"Ø Haltedauer Verlierer {losers['days'].mean():.0f} T vs. Gewinner "
          f"{base[base['return'] > 0]['days'].mean():.0f} T; Ø größter Zwischenverlust (MAE) Verlierer "
          f"{losers['mae'].mean():+.1%}, Gewinner {base[base['return'] > 0]['mae'].mean():+.1%}")
    print("\nGespeichert: analyze_turtle_signals.csv (alle Einzeltrades mit Merkmalen)")


if __name__ == "__main__":
    main()
