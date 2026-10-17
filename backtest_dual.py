"""Backtest Dual-Strategie-Portfolio: Golden Triangle (A) + Turtle Breakout (B) vs. SPY.

- A: SMA-50/200-Crossover auf Energie & Tech; Aktien mit SMA50 > SMA200 am Fensterstart
  gelten als Kauf zum Schlusskurs des ersten Tages
- B: 20-Tage-Donchian-Breakout mit Volumenbestätigung, Exit unter 10-Tage-Tief
- je 5.000 $, max. 5 Positionen à 1.000 $ (fix, kein Compounding), long-only
- Ausführung zum Schlusskurs des Signaltages, IBKR-Fixed-Kommission, keine Slippage
- Benchmark: 5.000 $ Buy & Hold SPY (nicht Teil der 10.000 $)

Die Signal- und Sizing-Logik liegt in strategies/ und wird unverändert auch live verwendet.

Aufruf: python backtest_dual.py [--no-show]
"""

import argparse
import pathlib
import time

import matplotlib
import matplotlib.ticker as mticker
import pandas as pd
import yfinance as yf

from backtest_rsi_vs_sma import GRID, SURFACE, TEXT, TEXT_MUTED
from strategies.portfolio import (MAX_POSITIONS, POSITION_SIZE, SUBPORTFOLIO_CAPITAL,
                                  ibkr_commission, position_shares, select_entries)
from strategies.signals import golden_triangle, turtle_breakout

WINDOW_START = pd.Timestamp("2026-04-01")
WINDOW_END = pd.Timestamp("2026-10-01")  # exklusiv: letzter Handelstag im Fenster ist der 30.09.
HISTORY_START = pd.Timestamp("2025-02-01")  # > 260 Handelstage vor Fensterstart
BENCHMARK = "SPY"
BENCHMARK_CAPITAL = 5_000.0

UNIVERSE_A = ["XOM", "CVX", "COP", "SLB", "OXY", "AAPL", "MSFT", "NVDA", "GOOGL", "AMD"]
UNIVERSE_B = ["DE", "CAT", "BA", "UNH", "JPM", "V", "HD", "COST", "LMT", "FDX",
              "DOW", "NEM", "PLTR", "CRWD", "GE"]

CACHE_DIR = pathlib.Path(".cache/yfinance")
PLOT_FILE = "backtest_dual.png"
TRADES_FILE = "backtest_dual_trades.csv"
COLORS = {"A": "#2a78d6", "B": "#eb6834", "SPY": "#52514e"}
NAMES = {"A": "Teilportfolio A: Golden Triangle", "B": "Teilportfolio B: Turtle Breakout",
         "SPY": "Benchmark: S&P 500 (SPY)"}


def load_history(ticker):
    """Tagesdaten (nur Split-bereinigt, wie in TWS) mit lokalem Cache gegen Yahoo-Rate-Limits."""
    path = CACHE_DIR / f"{ticker}_{HISTORY_START:%Y%m%d}_{WINDOW_END:%Y%m%d}.pkl"
    if path.exists():
        return pd.read_pickle(path)
    df = yf.Ticker(ticker).history(start=HISTORY_START, end=WINDOW_END, interval="1d",
                                   auto_adjust=False, timeout=15)
    if df.empty:
        raise SystemExit(f"Keine Daten für {ticker} erhalten.")
    df.index = df.index.tz_localize(None)
    df = df[["Open", "High", "Low", "Close", "Volume"]]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_pickle(path)
    time.sleep(0.5)
    return df


def run_subportfolio(data, signal_fn, start_from_trend=False):
    """Tagesschleife über das Fenster. Gibt (Equity-Kurve, Trade-Liste) zurück."""
    signals = {t: signal_fn(df) for t, df in data.items()}
    closes = pd.DataFrame({t: df["Close"] for t, df in data.items()})
    dates = closes.index[(closes.index >= WINDOW_START) & (closes.index < WINDOW_END)]
    closes = closes.ffill()

    cash = SUBPORTFOLIO_CAPITAL
    positions, pending, trades, equity = {}, set(), [], {}
    for i, day in enumerate(dates):
        today = {t: s.loc[day] for t, s in signals.items() if day in s.index}

        # 1) Exits zuerst, damit frei werdende Slots noch heute genutzt werden können
        for ticker in [t for t in positions if t in today and today[t]["exit"]]:
            trade = positions.pop(ticker)
            price = closes.at[day, ticker]
            fee = ibkr_commission(trade["shares"], price)
            cash += trade["shares"] * price - fee
            trade.update(exit_date=day, exit_price=price, fees=trade["fees"] + fee, status="geschlossen")
            trades.append(trade)

        # 2) Kandidaten: neue Signale (bzw. bestehender Trend am Fensterstart) und gültige Wartende
        for ticker, row in today.items():
            if row["entry"] or (i == 0 and start_from_trend and row["valid"]):
                pending.add(ticker)
            if not row["valid"]:
                pending.discard(ticker)
        pending.difference_update(positions)

        # 3) Entries in freie Slots, bester Score zuerst
        candidates = {t: today[t]["score"] for t in pending}
        for ticker in select_entries(candidates, MAX_POSITIONS - len(positions)):
            price = closes.at[day, ticker]
            shares = position_shares(price, min(POSITION_SIZE, cash))
            if shares == 0:
                continue
            fee = ibkr_commission(shares, price)
            cash -= shares * price + fee
            positions[ticker] = dict(ticker=ticker, entry_date=day, entry_price=price,
                                     shares=shares, fees=fee)
            pending.discard(ticker)

        equity[day] = cash + sum(p["shares"] * closes.at[day, t] for t, p in positions.items())

    # Offene Positionen zum letzten Schlusskurs bewerten (ohne Verkaufskommission)
    for ticker, trade in positions.items():
        trade.update(exit_date=pd.NaT, exit_price=closes.at[dates[-1], ticker], status="offen")
        trades.append(trade)
    trades = pd.DataFrame(trades)
    trades["pnl"] = (trades["exit_price"] - trades["entry_price"]) * trades["shares"] - trades["fees"]
    trades["return"] = trades["pnl"] / (trades["entry_price"] * trades["shares"])
    return pd.Series(equity), trades


def trade_stats(trades):
    closed = trades[trades["status"] == "geschlossen"]
    still_open = trades[trades["status"] == "offen"]
    return {
        "Trades gesamt": len(trades),
        "davon geschlossen / offen": f"{len(closed)} / {len(still_open)}",
        "Ø Kaufpreis (alle Trades)": f"{trades['entry_price'].mean():,.2f} $",
        "Ø Verkaufspreis (geschlossene)": f"{closed['exit_price'].mean():,.2f} $" if len(closed) else "–",
        "Gewinn- / Verlust-Trades (geschlossen)":
            f"{(closed['pnl'] > 0).sum()} / {(closed['pnl'] <= 0).sum()}",
        "offene im Plus / Minus (unrealisiert)":
            f"{(still_open['pnl'] > 0).sum()} / {(still_open['pnl'] <= 0).sum()}",
        "Ø Rendite pro Trade": f"{trades['return'].mean():+.2%}",
    }


def plot(curves, summary):
    import matplotlib.pyplot as plt

    fig, (ax, text_ax) = plt.subplots(2, 1, figsize=(12, 7.5), facecolor=SURFACE,
                                      gridspec_kw={"height_ratios": [5, 1]})
    ax.set_facecolor(SURFACE)
    first, last = curves["A"].index[0], curves["A"].index[-1]
    ax.set_xlim(first, last + (last - first) * 0.18)
    ax.axhline(SUBPORTFOLIO_CAPITAL, color=TEXT_MUTED, linewidth=0.8, zorder=1)
    for key, eq in curves.items():
        ax.plot(eq.index, eq.values, color=COLORS[key], linewidth=2, label=NAMES[key],
                zorder=2 if key == "SPY" else 3)
        ax.plot(eq.index[-1], eq.iloc[-1], "o", color=COLORS[key], markersize=6, zorder=4)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f} $"))
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.tick_params(length=0, colors=TEXT_MUTED)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.legend(loc="upper left", frameon=False, fontsize=11)
    ax.set_title(f"Equity-Kurven, je 5.000 $ Start ({first:%d.%m.%Y} – {last:%d.%m.%Y})",
                 loc="left", fontsize=14, fontweight="bold", color=TEXT)

    # Zusammenfassung unter dem Chart
    text_ax.axis("off")
    for i, (key, row) in enumerate(summary.iterrows()):
        text_ax.plot([0.0, 0.03], [0.8 - i * 0.35] * 2, color=COLORS[key], linewidth=3,
                     transform=text_ax.transAxes)
        text_ax.text(0.045, 0.8 - i * 0.35,
                     f"{NAMES[key]}:  {row['Endwert']:,.2f} $  ({row['Rendite']:+.2%})",
                     transform=text_ax.transAxes, va="center", fontsize=11, color=TEXT)
    fig.tight_layout()

    # Endwert-Labels rechts, bei Überlappung auseinandergeschoben
    lo, hi = ax.get_ylim()
    height_pt = ax.bbox.height * 72 / fig.dpi
    prev = None
    for key, eq in sorted(curves.items(), key=lambda kv: kv[1].iloc[-1]):
        value = eq.iloc[-1]
        y = value if prev is None else max(value, prev + (hi - lo) * 0.07)
        prev = y
        ax.annotate(f"{value / SUBPORTFOLIO_CAPITAL - 1:+.1%}", (eq.index[-1], value),
                    xytext=(8, (y - value) / (hi - lo) * height_pt), textcoords="offset points",
                    va="center", fontsize=10, color=TEXT, annotation_clip=False)
    fig.savefig(PLOT_FILE, dpi=120, facecolor=SURFACE)
    print(f"\nChart gespeichert: {PLOT_FILE}")
    return plt


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-show", action="store_true",
                        help="kein interaktives Fenster, nur PNG speichern")
    args = parser.parse_args()
    if args.no_show:
        matplotlib.use("Agg")

    data_a = {t: load_history(t) for t in UNIVERSE_A}
    data_b = {t: load_history(t) for t in UNIVERSE_B}
    spy = load_history(BENCHMARK)["Close"]
    n_before = (spy.index < WINDOW_START).sum()
    print(f"Historie vor Fensterstart: {n_before} Handelstage (SPY)")

    equity_a, trades_a = run_subportfolio(data_a, golden_triangle, start_from_trend=True)
    equity_b, trades_b = run_subportfolio(data_b, turtle_breakout)
    spy = spy[(spy.index >= WINDOW_START) & (spy.index < WINDOW_END)]
    equity_spy = BENCHMARK_CAPITAL * spy / spy.iloc[0]
    curves = {"A": equity_a, "B": equity_b, "SPY": equity_spy}

    for key, trades in (("A", trades_a), ("B", trades_b)):
        print(f"\n===== {NAMES[key]} =====")
        for label, value in trade_stats(trades).items():
            print(f"{label:<40}{value}")
        print()
        print(trades.assign(entry_date=trades["entry_date"].dt.date,
                            exit_date=trades["exit_date"].dt.date)
              .to_string(index=False, columns=["ticker", "entry_date", "entry_price", "exit_date",
                                               "exit_price", "shares", "pnl", "return", "status"],
                         formatters={"entry_price": "{:.2f}".format, "exit_price": "{:.2f}".format,
                                     "pnl": "{:+.2f}".format, "return": "{:+.2%}".format}))

    summary = pd.DataFrame({key: {"Start": eq.iloc[0] if key == "SPY" else SUBPORTFOLIO_CAPITAL,
                                  "Endwert": eq.iloc[-1]}
                            for key, eq in curves.items()}).T
    summary["Rendite"] = summary["Endwert"] / summary["Start"] - 1
    print("\n===== Zusammenfassung =====")
    print(summary.rename(index=NAMES).to_string(formatters={
        "Start": "{:,.2f} $".format, "Endwert": "{:,.2f} $".format, "Rendite": "{:+.2%}".format}))

    pd.concat([trades_a.assign(portfolio="A"), trades_b.assign(portfolio="B")]).to_csv(
        TRADES_FILE, index=False)
    print(f"\nTrade-Liste gespeichert: {TRADES_FILE}")

    plt = plot(curves, summary)
    if not args.no_show:
        print("Chart-Fenster geöffnet – zum Beenden schließen.")
        plt.show()


if __name__ == "__main__":
    main()
