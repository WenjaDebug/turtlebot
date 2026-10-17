"""Bollinger-Band-Mean-Reversion mit ATR-Filter auf 5 Aktien, verglichen mit SPY (12 Monate).

Regeln:
- Kauf: Schlusskurs unter dem unteren Bollinger Band (20 Tage, 2 Std.-Abw.), aber nur,
  wenn ATR(14) höchstens 1,5 × seinem 50-Tage-Durchschnitt ist (keine Käufe im Crash)
- Verkauf: Schlusskurs über der Mittellinie (20-Tage-Durchschnitt) oder dem oberen Band
- Kapital: 10.000 $, je 2.000 $ pro Aktie; pro Kauf höchstens 1.000 $ inkl. Gebühr

Orders werden zum Schlusskurs des Signaltages ausgeführt, damit die 1.000-$-Grenze exakt
eingehalten wird. Es wird ein Vorlauf geladen, damit alle Indikatoren ab Tag 1 Werte haben.

Aufruf: python backtest_bollinger.py [--no-show]
"""

import argparse
import datetime as dt

import backtrader as bt
import matplotlib
import matplotlib.ticker as mticker
import pandas as pd

from backtest_rsi_vs_sma import GRID, SURFACE, TEXT, TEXT_MUTED
from backtest_sma_crossover import COMMISSION, load_data

TICKERS = ["AAPL", "MSFT", "JPM", "XOM", "KO"]
BENCHMARK = "SPY"
TOTAL_CASH = 10_000.0
MAX_POSITION = 1_000.0
ATR_FILTER = 1.5  # "deutlich über dem Durchschnitt" = mehr als 50 % darüber
WARMUP_DAYS = 200  # Kalendertage Vorlauf für BB(20) und SMA(50) des ATR(14)
STRATEGY_PLOT = "backtest_bollinger_vs_spy.png"
SPY_PLOT = "spy_12_monate.png"

COLOR_STRATEGY = "#2a78d6"  # kategorialer Slot 1
COLOR_SPY = "#52514e"  # neutrale Referenz


class BollingerMeanReversion(bt.Strategy):
    params = dict(period=20, devfactor=2.0, atr_period=14, atr_avg=50,
                  atr_max=ATR_FILTER, max_position=MAX_POSITION, trade_start=None)

    def __init__(self):
        self.bb = bt.ind.BollingerBands(period=self.p.period, devfactor=self.p.devfactor)
        self.atr = bt.ind.ATR(period=self.p.atr_period)
        self.atr_avg = bt.ind.SMA(self.atr, period=self.p.atr_avg)
        self.values = {}  # Depotwert pro Handelstag ab trade_start

    def next(self):
        date = self.data.datetime.date(0)
        if date < self.p.trade_start:
            return
        self.values[date] = self.broker.getvalue()

        close = self.data.close[0]
        if not self.position:
            calm = self.atr[0] <= self.p.atr_max * self.atr_avg[0]
            if close < self.bb.bot[0] and calm:
                size = int(self.p.max_position / (close * (1 + COMMISSION)))
                if size:
                    self.buy(size=size)
        elif close > self.bb.mid[0] or close > self.bb.top[0]:
            self.close()


def run(df, trade_start, cash):
    cerebro = bt.Cerebro(stdstats=False)
    cerebro.adddata(bt.feeds.PandasData(dataname=df))
    cerebro.addstrategy(BollingerMeanReversion, trade_start=trade_start)
    cerebro.broker.setcash(cash)
    cerebro.broker.setcommission(commission=COMMISSION)
    cerebro.broker.set_coc(True)  # Ausführung zum Schlusskurs des Signaltages
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    strat = cerebro.run()[0]

    trades = strat.analyzers.trades.get_analysis()
    equity = pd.Series(strat.values)
    equity.index = pd.to_datetime(equity.index)
    return equity, trades.get("total", {}).get("total", 0), trades.get("won", {}).get("total", 0)


def max_drawdown(equity):
    return (equity / equity.cummax() - 1).min()


def style_axes(ax, index):
    ax.set_facecolor(SURFACE)
    # Links bündig am Startdatum, rechts Platz für die Endwert-Labels
    ax.set_xlim(index[0], index[-1] + (index[-1] - index[0]) * 0.2)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.tick_params(length=0, colors=TEXT_MUTED)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)


def end_labels(fig, ax, items):
    """Endwert-Labels rechts neben den Linien, bei Überlappung vertikal auseinandergeschoben."""
    lo, hi = ax.get_ylim()
    height_pt = ax.bbox.height * 72 / fig.dpi
    gap = (hi - lo) * 0.07
    prev = None
    for date, value, text in sorted(items, key=lambda i: i[1]):
        y = value if prev is None else max(value, prev + gap)
        prev = y
        ax.annotate(text, (date, value), xytext=(8, (y - value) / (hi - lo) * height_pt),
                    textcoords="offset points", va="center", fontsize=10, color=TEXT,
                    annotation_clip=False)


def plot_charts(combined, spy_equity, spy_pct, trade_start, today):
    import matplotlib.pyplot as plt

    period = f"{trade_start:%d.%m.%Y} – {today:%d.%m.%Y}"

    # Chart 1: kombinierter Depotwert vs. 10.000 $ in SPY
    fig1, ax = plt.subplots(figsize=(12, 6), facecolor=SURFACE)
    style_axes(ax, combined.index)
    ax.axhline(TOTAL_CASH, color=TEXT_MUTED, linewidth=0.8, zorder=1)
    for eq, color, name in ((spy_equity, COLOR_SPY, "10.000 $ in SPY (Buy & Hold)"),
                            (combined, COLOR_STRATEGY, "Bollinger-Strategie, 5 Aktien kombiniert")):
        ax.plot(eq.index, eq.values, color=color, linewidth=2, label=name)
        ax.plot(eq.index[-1], eq.iloc[-1], "o", color=color, markersize=6)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f} $"))
    ax.legend(loc="upper left", frameon=False, fontsize=11)
    ax.set_title(f"Depotwert, Start 10.000 $ ({period})", loc="left",
                 fontsize=14, fontweight="bold", color=TEXT)
    fig1.tight_layout()
    end_labels(fig1, ax, [(eq.index[-1], eq.iloc[-1], f"{eq.iloc[-1]:,.0f} $ ({eq.iloc[-1] / TOTAL_CASH - 1:+.1%})")
                          for eq in (combined, spy_equity)])
    fig1.savefig(STRATEGY_PLOT, dpi=120, facecolor=SURFACE)

    # Chart 2: SPY selbst in Prozent
    fig2, ax = plt.subplots(figsize=(12, 5), facecolor=SURFACE)
    style_axes(ax, spy_pct.index)
    ax.axhline(0, color=TEXT_MUTED, linewidth=0.8, zorder=1)
    ax.plot(spy_pct.index, spy_pct.values, color=COLOR_SPY, linewidth=2)
    ax.plot(spy_pct.index[-1], spy_pct.iloc[-1], "o", color=COLOR_SPY, markersize=6)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:+.0%}" if v else "0 %"))
    ax.set_title(f"S&P 500 (SPY): Veränderung in Prozent ({period})", loc="left",
                 fontsize=14, fontweight="bold", color=TEXT)
    fig2.tight_layout()
    end_labels(fig2, ax, [(spy_pct.index[-1], spy_pct.iloc[-1], f"{spy_pct.iloc[-1]:+.1%}")])
    fig2.savefig(SPY_PLOT, dpi=120, facecolor=SURFACE)

    print(f"\nCharts gespeichert: {STRATEGY_PLOT}, {SPY_PLOT}")
    return plt


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-show", action="store_true",
                        help="keine interaktiven Fenster, nur PNGs speichern")
    args = parser.parse_args()
    if args.no_show:
        matplotlib.use("Agg")

    today = dt.date.today()
    trade_start = today.replace(year=today.year - 1)
    load_start = (trade_start - dt.timedelta(days=WARMUP_DAYS)).isoformat()
    cash_per_stock = TOTAL_CASH / len(TICKERS)

    rows, curves = [], []
    for ticker in TICKERS:
        equity, n_trades, n_won = run(load_data(ticker, start=load_start), trade_start, cash_per_stock)
        curves.append(equity)
        rows.append({"Aktie": ticker, "Trades": n_trades, "Gewinner": n_won,
                     "Endwert": equity.iloc[-1], "Rendite": equity.iloc[-1] / cash_per_stock - 1,
                     "Max DD": max_drawdown(equity)})

    combined = pd.concat(curves, axis=1).ffill().sum(axis=1)
    spy = load_data(BENCHMARK, start=load_start)["Close"]
    spy = spy[spy.index.date >= trade_start]
    spy_pct = spy / spy.iloc[0] - 1
    spy_equity = TOTAL_CASH * (1 + spy_pct)

    rows.append({"Aktie": "Depot gesamt", "Trades": sum(r["Trades"] for r in rows),
                 "Gewinner": sum(r["Gewinner"] for r in rows), "Endwert": combined.iloc[-1],
                 "Rendite": combined.iloc[-1] / TOTAL_CASH - 1, "Max DD": max_drawdown(combined)})
    rows.append({"Aktie": "SPY Buy & Hold", "Trades": 0, "Gewinner": 0,
                 "Endwert": spy_equity.iloc[-1], "Rendite": spy_pct.iloc[-1],
                 "Max DD": max_drawdown(spy_equity)})

    table = pd.DataFrame(rows).set_index("Aktie")
    print(f"Zeitraum: {trade_start} bis {today}, {TOTAL_CASH:,.0f} $ Startkapital "
          f"({cash_per_stock:,.0f} $ pro Aktie, max. {MAX_POSITION:,.0f} $ pro Kauf), "
          f"Gebühr {COMMISSION:.1%} pro Order\n")
    print(table.to_string(formatters={"Endwert": "{:,.2f} $".format, "Rendite": "{:+.2%}".format,
                                      "Max DD": "{:.2%}".format}))

    plt = plot_charts(combined, spy_equity, spy_pct, trade_start, today)
    if not args.no_show:
        print("Chart-Fenster geöffnet – zum Beenden schließen.")
        plt.show()


if __name__ == "__main__":
    main()
