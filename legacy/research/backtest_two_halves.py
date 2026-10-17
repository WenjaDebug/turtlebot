"""Backtest Zwei-Strategien-Portfolio (10.000 $): 2-Tages-Verlierer-Reversal + gehebelter Turtle-Breakout.

Hälfte 1 (5.000 $, ohne Hebel): nach Börsenschluss die 3 Titel mit dem negativsten 2-Tages-Return
  (nicht bereits gehalten) bestimmen, Kauf zum Open am nächsten Handelstag. Exit, was zuerst eintritt:
  +5 % Take-Profit, -5 % Stop-Loss, Zeit-Stop zum Close nach 10 Handelstagen. Max. 5 Positionen.
Hälfte 2 (5.000 $ Eigenkapital, 2x Hebel = 10.000 $ Kaufkraft): Donchian-20-Breakout mit Volumen
  > 1,5 × 20-Tage-Schnitt, Exit unter 10-Tage-Tief; nur Market Cap > 500 Mio $. Signal zum Close,
  Ausführung zum Open am nächsten Handelstag. Max. 5 Positionen. Zinsen (5 % p.a.) separat ausgewiesen.
Benchmark: 5.000 $ Buy & Hold SPY.

Universum: aktuelle Konstituenten S&P 500 + Nasdaq-100 (statisch, siehe Einschränkungen im Output).
Signal-Logik: strategies/signals.py (wiederverwendbar im Live-Loop).

Aufruf: python backtest_two_halves.py [--no-show]
"""

import argparse
import datetime as dt

import matplotlib
import matplotlib.ticker as mticker
import pandas as pd

from backtest_rsi_vs_sma import GRID, SURFACE, TEXT, TEXT_MUTED
from data_sources import load_market_caps, load_prices, load_universe
from strategies.portfolio import select_entries
from strategies.signals import biggest_losers, turtle_breakout, two_day_returns

TODAY = dt.date.today()
WINDOW_START = pd.Timestamp(TODAY.replace(year=TODAY.year - 1))
WINDOW_END = pd.Timestamp(TODAY)  # exklusiv; der laufende Tag wird nicht verwendet
HISTORY_START = WINDOW_START - pd.Timedelta(days=63)  # Vorlauf für Donchian/Volumen (20 Tage)
BENCHMARK = "SPY"

HALF_CAPITAL = 5_000.0
MAX_POSITIONS = 5
LOSER_PICKS = 3
TAKE_PROFIT, STOP_LOSS, MAX_HOLD_DAYS = 0.05, 0.05, 10
LEVERAGE = 2.0
H2_MAX_POSITION = HALF_CAPITAL * LEVERAGE / MAX_POSITIONS  # 2.000 $ (1.000 $ Eigenkapital + 1.000 $ Hebel)
LOAN_RATE = 0.05
MIN_MARKET_CAP = 500e6

# TODO v2: Transaktionskosten und Slippage (in dieser Version bewusst 0)
COMMISSION_PER_ORDER = 0.0  # USD pro Order
SLIPPAGE = 0.0  # Anteil des Preises, z. B. 0.0005 = 5 Basispunkte

PLOT_FILE = "backtest_two_halves.png"
TRADES_FILE = "backtest_two_halves_trades.csv"
COLORS = {"H1": "#2a78d6", "H2": "#eb6834", "SPY": "#52514e"}
NAMES = {"H1": "Hälfte 1: 2-Tages-Verlierer-Reversal", "H2": "Hälfte 2: Turtle-Breakout (2x Hebel)",
         "SPY": "Benchmark: S&P 500 (SPY)"}


def buy(positions, cash, ticker, price, day_idx, day, max_size=None):
    """Kauft gleichgewichtet: verfügbares Cash / freie Slots (optional gedeckelt). Gibt das neue Cash zurück."""
    budget = cash / (MAX_POSITIONS - len(positions))
    if max_size is not None:
        budget = min(budget, max_size)
    fill = price * (1 + SLIPPAGE)
    shares = (budget - COMMISSION_PER_ORDER) / fill  # Bruchstücke erlaubt
    positions[ticker] = dict(ticker=ticker, entry_date=day, entry_idx=day_idx, entry_price=fill,
                             shares=shares)
    return cash - budget


def sell(positions, trades, ticker, price, day, reason):
    """Schließt eine Position. Gibt den Erlös zurück."""
    trade = positions.pop(ticker)
    fill = price * (1 - SLIPPAGE)
    trade.update(exit_date=day, exit_price=fill, reason=reason)
    trades.append(trade)
    return trade["shares"] * fill - COMMISSION_PER_ORDER


def finish(positions, trades, equity, closes):
    """Offene Positionen zum letzten Close bewerten und Trade-Liste aufbereiten."""
    last_day = closes.index[-1]
    for ticker, trade in positions.items():
        trade.update(exit_date=pd.NaT, exit_price=closes.at[last_day, ticker], reason="offen")
        trades.append(trade)
    trades = pd.DataFrame(trades).drop(columns="entry_idx")
    trades["return"] = trades["exit_price"] / trades["entry_price"] - 1
    return pd.Series(equity), trades


def bracket_exit(trade, bar, days_held):
    """Take-Profit / Stop-Loss / Zeit-Stop auf Tagesbalken. Gibt (Preis, Grund) oder None zurück.

    Die Reihenfolge von Hoch und Tief innerhalb des Tages ist unbekannt: Werden beide Schwellen
    am selben Tag berührt, gilt konservativ der Stop-Loss. Eröffnet der Kurs bereits jenseits einer
    Schwelle (Gap), wird zum Open ausgeführt.
    """
    tp = trade["entry_price"] * (1 + TAKE_PROFIT)
    sl = trade["entry_price"] * (1 - STOP_LOSS)
    if bar["Open"] <= sl:
        return bar["Open"], "Stop-Loss"
    if bar["Open"] >= tp:
        return bar["Open"], "Take-Profit"
    if bar["Low"] <= sl:
        return sl, "Stop-Loss"
    if bar["High"] >= tp:
        return tp, "Take-Profit"
    if days_held >= MAX_HOLD_DAYS:
        return bar["Close"], "Zeit-Stop"
    return None


def run_half1(data, dates):
    opens, highs, lows, closes = (pd.DataFrame({t: df[c] for t, df in data.items()})
                                  for c in ("Open", "High", "Low", "Close"))
    returns_2d = two_day_returns(closes)
    signal_day = closes.index[closes.index < dates[0]][-1]
    picks = biggest_losers(returns_2d.loc[signal_day], n=LOSER_PICKS)

    cash, positions, trades, equity = HALF_CAPITAL, {}, [], {}
    for i, day in enumerate(dates):
        # Open: gestern bestimmte Verlierer kaufen
        for ticker in picks:
            if len(positions) < MAX_POSITIONS and pd.notna(opens.at[day, ticker]):
                cash = buy(positions, cash, ticker, opens.at[day, ticker], i, day)

        # Im Tagesverlauf / zum Close: TP, SL, Zeit-Stop (auch am Kauftag selbst)
        for ticker in list(positions):
            if pd.isna(closes.at[day, ticker]):
                continue
            bar = {"Open": opens.at[day, ticker], "High": highs.at[day, ticker],
                   "Low": lows.at[day, ticker], "Close": closes.at[day, ticker]}
            hit = bracket_exit(positions[ticker], bar, i - positions[ticker]["entry_idx"])
            if hit:
                cash += sell(positions, trades, ticker, hit[0], day, hit[1])

        # Nach Börsenschluss: Picks für morgen (nur so viele, wie Slots frei sind)
        free = MAX_POSITIONS - len(positions)
        picks = biggest_losers(returns_2d.loc[day], held=positions, n=LOSER_PICKS)[:free]

        equity[day] = cash + sum(p["shares"] * closes[t].loc[:day].ffill().iloc[-1]
                                 for t, p in positions.items())
    return finish(positions, trades, equity, closes.loc[dates].ffill())


def run_half2(data, dates, max_size=H2_MAX_POSITION):
    signals = {t: turtle_breakout(df) for t, df in data.items()}
    opens = pd.DataFrame({t: df["Open"] for t, df in data.items()})
    closes = pd.DataFrame({t: df["Close"] for t, df in data.items()})
    entries, exits, scores = (pd.DataFrame({t: s[c] for t, s in signals.items()})
                              for c in ("entry", "exit", "score"))
    loan = HALF_CAPITAL * (LEVERAGE - 1)

    def plan(day, positions):
        """Nach Börsenschluss: welche Positionen morgen verkaufen, welche Titel morgen kaufen."""
        to_sell = [t for t in positions if exits.at[day, t]]
        candidates = {t: scores.at[day, t] for t in entries.columns[entries.loc[day].fillna(False).astype(bool)]
                      if t not in positions}
        return to_sell, select_entries(candidates, MAX_POSITIONS - len(positions) + len(to_sell))

    signal_day = closes.index[closes.index < dates[0]][-1]
    cash, positions, trades, equity = HALF_CAPITAL + loan, {}, [], {}
    to_sell, to_buy = plan(signal_day, positions)
    for i, day in enumerate(dates):
        # Open: zuerst Exits, dann Entries mit dem frei gewordenen Kapital
        for ticker in to_sell:
            price = opens.at[day, ticker]
            if pd.isna(price):
                price = closes[ticker].loc[:day].ffill().iloc[-1]
            cash += sell(positions, trades, ticker, price, day, "10-Tage-Tief")
        for ticker in to_buy:
            if len(positions) < MAX_POSITIONS and pd.notna(opens.at[day, ticker]):
                cash = buy(positions, cash, ticker, opens.at[day, ticker], i, day, max_size)

        to_sell, to_buy = plan(day, positions)
        market_value = sum(p["shares"] * closes[t].loc[:day].ffill().iloc[-1] for t, p in positions.items())
        equity[day] = cash + market_value - loan  # Eigenkapital: Hebel-P&L wirkt 1:1 auf die 5.000 $
    return finish(positions, trades, equity, closes.loc[dates].ffill())


def trade_stats(trades):
    closed = trades[trades["reason"] != "offen"]
    winners, losers = closed[closed["return"] > 0], closed[closed["return"] <= 0]
    stats = {
        "Trades insgesamt": f"{len(trades)} ({len(closed)} geschlossen, {len(trades) - len(closed)} offen)",
        "Gewinn- / Verlust-Trades": f"{len(winners)} / {len(losers)}",
        "Ø Rendite Gewinn-Trades": f"{winners['return'].mean():+.2%}" if len(winners) else "–",
        "Ø Rendite Verlust-Trades": f"{losers['return'].mean():+.2%}" if len(losers) else "–",
    }
    reasons = closed["reason"].value_counts().to_dict()
    stats["Exit-Gründe"] = ", ".join(f"{k}: {v}" for k, v in reasons.items())
    return stats


def plot(curves, interest):
    import matplotlib.pyplot as plt

    fig, (ax, text_ax) = plt.subplots(2, 1, figsize=(12, 7.8), facecolor=SURFACE,
                                      gridspec_kw={"height_ratios": [5, 1.1]})
    ax.set_facecolor(SURFACE)
    first, last = curves["SPY"].index[0], curves["SPY"].index[-1]
    ax.set_xlim(first, last + (last - first) * 0.2)
    ax.axhline(HALF_CAPITAL, color=TEXT_MUTED, linewidth=0.8, zorder=1)
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

    text_ax.axis("off")
    for i, (key, eq) in enumerate(curves.items()):
        y = 0.85 - i * 0.33
        text_ax.plot([0.0, 0.03], [y, y], color=COLORS[key], linewidth=3, transform=text_ax.transAxes)
        note = f"   →  {interest:,.0f} USD (noch zu zahlende Zinsen)" if key == "H2" else ""
        text_ax.text(0.045, y, f"{NAMES[key]}:  {eq.iloc[-1]:,.2f} $  "
                     f"({eq.iloc[-1] / HALF_CAPITAL - 1:+.2%}){note}",
                     transform=text_ax.transAxes, va="center", fontsize=11, color=TEXT)
    fig.tight_layout()

    lo, hi = ax.get_ylim()
    height_pt = ax.bbox.height * 72 / fig.dpi
    prev = None
    for key, eq in sorted(curves.items(), key=lambda kv: kv[1].iloc[-1]):
        value = eq.iloc[-1]
        y = value if prev is None else max(value, prev + (hi - lo) * 0.07)
        prev = y
        label = f"{value:,.0f} $" + (f"\n{interest:,.0f} $ Zinsen (zu zahlen)" if key == "H2" else "")
        ax.annotate(label, (eq.index[-1], value),
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

    universe = load_universe(TODAY)
    tickers = universe["ticker"].tolist()
    print(f"Universum: {len(tickers)} Titel (S&P 500 + Nasdaq-100, Stand {TODAY})")
    prices = load_prices(tickers + [BENCHMARK], HISTORY_START, WINDOW_END)
    spy = prices.pop(BENCHMARK)
    missing = sorted(set(tickers) - set(prices))
    print(f"Kursdaten: {len(prices)} Titel" + (f", fehlend: {', '.join(missing)}" if missing else ""))

    caps = load_market_caps(tickers, TODAY)
    unknown = sorted(t for t, c in caps.items() if c is None)
    small = sorted(t for t, c in caps.items() if c is not None and c <= MIN_MARKET_CAP)
    eligible = {t: df for t, df in prices.items() if caps.get(t) and caps[t] > MIN_MARKET_CAP}
    print(f"Market-Cap-Filter (> 500 Mio $): {len(eligible)} erfüllt, {len(small)} darunter"
          + (f" ({', '.join(small)})" if small else "")
          + f", {len(unknown)} ohne Angabe" + (f" ({', '.join(unknown)}) – ausgeschlossen" if unknown else ""))

    dates = spy.index[(spy.index >= WINDOW_START) & (spy.index < WINDOW_END)]
    equity_1, trades_1 = run_half1(prices, dates)
    equity_2, trades_2 = run_half2(eligible, dates)
    equity_2_uncapped, _ = run_half2(eligible, dates, max_size=None)  # Sensitivität: ohne 2.000-$-Deckel
    equity_spy = HALF_CAPITAL * spy["Close"].loc[dates] / spy["Close"].loc[dates[0]]
    curves = {"H1": equity_1, "H2": equity_2, "SPY": equity_spy}
    interest = -HALF_CAPITAL * (LEVERAGE - 1) * LOAN_RATE  # 5.000 $ Kredit × 5 % × 12 Monate

    print(f"\nZeitraum: {dates[0].date()} bis {dates[-1].date()} ({len(dates)} Handelstage)")
    print("\n===== Endwerte (Start je 5.000 $) =====")
    for key, eq in curves.items():
        print(f"{NAMES[key]:<42}{eq.iloc[-1]:>12,.2f} $  ({eq.iloc[-1] / HALF_CAPITAL - 1:+.2%})")
        if key == "H2":
            print(f"{'':<42}{interest:>12,.2f} $  (noch zu zahlende Zinsen, nicht in der Kurve)")
            print(f"{'':<42}{eq.iloc[-1] + interest:>12,.2f} $  (Endwert nach Zinsen)")
            print(f"{'':<42}({equity_2_uncapped.iloc[-1]:,.2f} $ zum Vergleich, wenn Gewinne ohne "
                  f"2.000-$-Deckel reinvestiert würden)")

    for key, trades in (("H1", trades_1), ("H2", trades_2)):
        print(f"\n===== {NAMES[key]} =====")
        for label, value in trade_stats(trades).items():
            print(f"{label:<28}{value}")
    print("Hinweis Hälfte 2: Renditen pro Trade auf Positionsebene (ohne Hebel); "
          "auf das Eigenkapital wirkt jede Position doppelt.")

    pd.concat([trades_1.assign(half="H1"), trades_2.assign(half="H2")]).to_csv(TRADES_FILE, index=False)
    print(f"\nTrade-Liste gespeichert: {TRADES_FILE}")

    print("\n===== Bekannte Einschränkungen =====")
    print("- Survivorship-Bias: Universum = heutige Index-Konstituenten; Titel, die im Zeitraum aus den\n"
          "  Indizes gefallen sind, fehlen, später aufgenommene sind von Anfang an enthalten.")
    print("- Market Cap ist eine Momentaufnahme von heute (yfinance .info), kein historischer Verlauf.")
    print("- Keine Transaktionskosten und keine Slippage (Parameter für v2 vorbereitet).")
    print("- Cash ohne Verzinsung; Hebel ohne Margin-Calls modelliert; Zinsen nur separat ausgewiesen.")
    print("- Tagesdaten: Reihenfolge von Hoch/Tief im Tag unbekannt -> bei TP und SL am selben Tag gilt SL.")

    plt = plot(curves, interest)
    if not args.no_show:
        print("Chart-Fenster geöffnet – zum Beenden schließen.")
        plt.show()


if __name__ == "__main__":
    main()
