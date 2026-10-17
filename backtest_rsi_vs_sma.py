"""Vergleich RSI-Mean-Reversion vs. SMA-50/200-Crossover auf mehreren Aktien über 5 Jahre.

RSI-Strategie: Kauf, wenn RSI(14) unter 30 fällt; Verkauf, wenn RSI über 70 steigt
oder spätestens nach 20 Handelstagen.

Es wird 1 Jahr zusätzlicher Vorlauf geladen, damit SMA 200 und RSI zu Beginn des
5-Jahres-Zeitraums schon Werte haben. Gehandelt wird erst ab dem Startdatum.

Aufruf: python backtest_rsi_vs_sma.py [--no-show]

Speichert die Equity-Kurven als backtest_rsi_vs_sma.png und öffnet sie in einem
Chart-Fenster (außer mit --no-show).
"""

import argparse
import datetime as dt

import backtrader as bt
import matplotlib
import pandas as pd

from backtest_sma_crossover import COMMISSION, START_CASH, SmaCross, load_data

TICKERS = ["AAPL", "MSFT", "JPM", "XOM", "KO"]
YEARS = 5
WARMUP_YEARS = 1
PLOT_FILE = "backtest_rsi_vs_sma.png"

# Farben: kategoriale Slots 1/2 für die Strategien, neutrales Grau für die Buy-&-Hold-Referenz
COLORS = {"RSI": "#2a78d6", "SMA": "#eb6834", "Buy & Hold": "#8a8984"}
TEXT, TEXT_MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"


class RsiMeanReversion(bt.Strategy):
    params = dict(period=14, lower=30, upper=70, max_hold=20, trade_start=None)

    def __init__(self):
        self.rsi = bt.ind.RSI_Safe(period=self.p.period)
        self.order = None
        self.entry_bar = None

    def next(self):
        if self.order or self.data.datetime.date(0) < self.p.trade_start:
            return
        if not self.position:
            # RSI fällt unter 30 (Kreuzung von oben)
            if self.rsi[0] < self.p.lower <= self.rsi[-1]:
                size = int(self.broker.getcash() * 0.95 / self.data.close[0])
                self.order = self.buy(size=size)
        elif self.rsi[0] > self.p.upper or len(self) - self.entry_bar >= self.p.max_hold:
            self.order = self.close()

    def notify_order(self, order):
        if order.status in (order.Submitted, order.Accepted):
            return
        if order.status == order.Completed and order.isbuy():
            self.entry_bar = len(self)
        self.order = None


class SmaCrossFrom(SmaCross):
    """SMA-50/200-Crossover von vorhin, aber erst ab trade_start handelnd und ohne Order-Log."""

    params = dict(trade_start=None)

    def next(self):
        if self.data.datetime.date(0) >= self.p.trade_start:
            super().next()

    def notify_order(self, order):
        pass


def run(df, strategy, trade_start):
    cerebro = bt.Cerebro(stdstats=False)
    cerebro.adddata(bt.feeds.PandasData(dataname=df))
    cerebro.addstrategy(strategy, trade_start=trade_start)
    cerebro.broker.setcash(START_CASH)
    cerebro.broker.setcommission(commission=COMMISSION)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    cerebro.addanalyzer(bt.analyzers.TimeReturn, _name="daily", timeframe=bt.TimeFrame.Days)
    strat = cerebro.run()[0]

    daily = pd.Series(strat.analyzers.daily.get_analysis())
    equity = START_CASH * (1 + daily[daily.index.date >= trade_start]).cumprod()

    trades = strat.analyzers.trades.get_analysis()
    return {
        "trades": trades.get("total", {}).get("total", 0),
        "won": trades.get("won", {}).get("total", 0),
        "return": cerebro.broker.getvalue() / START_CASH - 1,
        "max_dd": strat.analyzers.drawdown.get_analysis().max.drawdown / 100,
        "equity": equity,
    }


def plot_equity(curves, trade_start, today):
    """Small Multiples: pro Aktie Depotwert von RSI, SMA und Buy & Hold (Start 10.000 $)."""
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker

    plt.rcParams.update({"font.size": 10, "text.color": TEXT, "axes.labelcolor": TEXT_MUTED,
                         "xtick.color": TEXT_MUTED, "ytick.color": TEXT_MUTED})
    fig, axes = plt.subplots(3, 2, figsize=(14, 11), facecolor=SURFACE)
    for ax, (ticker, series) in zip(axes.flat, curves.items()):
        ax.set_facecolor(SURFACE)
        for name, eq in series.items():
            is_ref = name == "Buy & Hold"
            ax.plot(eq.index, eq.values, color=COLORS[name], linewidth=1.5 if is_ref else 2,
                    zorder=2 if is_ref else 3, label=name)
            ax.plot(eq.index[-1], eq.iloc[-1], "o", color=COLORS[name], markersize=5, zorder=4)
        ax.axhline(START_CASH, color=TEXT_MUTED, linewidth=0.8, zorder=1)
        ax.margins(x=0.12)

        # Direkte Labels am Linienende (Endrendite in Textfarbe), bei Überlappung auseinanderschieben
        lo, hi = ax.get_ylim()
        min_gap = (hi - lo) * 0.055
        ends = sorted((eq.iloc[-1], eq.index[-1]) for eq in series.values())
        label_ys = [ends[0][0]]
        for value, _ in ends[1:]:
            label_ys.append(max(value, label_ys[-1] + min_gap))
        for (value, date), y in zip(ends, label_ys):
            ax.annotate(f"{value / START_CASH - 1:+.0%}", (date, value), xytext=(8, 0),
                        textcoords="offset points", va="center", fontsize=9, color=TEXT,
                        annotation_clip=False)
            if y != value:  # verschobenes Label: Textposition in Datenkoordinaten setzen
                ax.texts[-1].xyann = (8, (ax.transData.transform((0, y))[1]
                                          - ax.transData.transform((0, value))[1])
                                      * 72 / fig.dpi)
        ax.set_title(ticker, loc="left", fontweight="bold", fontsize=12)
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v / 1000:g}k $"))
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.tick_params(length=0)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(GRID)

    # Letztes Feld: Legende statt leerem Plot
    legend_ax = axes.flat[-1]
    legend_ax.axis("off")
    handles, labels = axes.flat[0].get_legend_handles_labels()
    names = {"RSI": "RSI(14) Mean Reversion", "SMA": "SMA 50/200 Crossover",
             "Buy & Hold": "Buy & Hold"}
    legend_ax.legend(handles, [names[l] for l in labels], loc="center left", frameon=False,
                     fontsize=12, handlelength=3)

    fig.suptitle(f"Depotwert, Start 10.000 $ ({trade_start:%d.%m.%Y} – {today:%d.%m.%Y})",
                 x=0.06, ha="left", fontsize=15, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(PLOT_FILE, dpi=120, facecolor=SURFACE)
    print(f"\nPlot gespeichert: {PLOT_FILE}")
    return plt


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-show", action="store_true",
                        help="kein interaktives Fenster, nur PNG speichern")
    args = parser.parse_args()
    if args.no_show:
        matplotlib.use("Agg")

    today = dt.date.today()
    trade_start = today.replace(year=today.year - YEARS)
    load_start = today.replace(year=today.year - YEARS - WARMUP_YEARS)

    rows, curves = [], {}
    for ticker in TICKERS:
        df = load_data(ticker, start=load_start.isoformat())
        in_window = df.loc[df.index.date >= trade_start, "Close"]
        rsi = run(df, RsiMeanReversion, trade_start)
        sma = run(df, SmaCrossFrom, trade_start)
        rows.append({
            "Aktie": ticker,
            "RSI Trades": rsi["trades"],
            "RSI Gewinner": rsi["won"],
            "RSI Rendite": rsi["return"],
            "RSI Max DD": rsi["max_dd"],
            "SMA Trades": sma["trades"],
            "SMA Rendite": sma["return"],
            "SMA Max DD": sma["max_dd"],
            "Buy & Hold": in_window.iloc[-1] / in_window.iloc[0] - 1,
        })
        curves[ticker] = {"RSI": rsi["equity"], "SMA": sma["equity"],
                          "Buy & Hold": START_CASH * in_window / in_window.iloc[0]}
        print(f"{ticker}: fertig")

    table = pd.DataFrame(rows).set_index("Aktie")
    pct = [c for c in table.columns if "Rendite" in c or "DD" in c or c == "Buy & Hold"]
    print(f"\nZeitraum: {trade_start} bis {today}, Startkapital {START_CASH:,.0f} $, "
          f"Gebühr {COMMISSION:.1%} pro Order\n")
    print(table.to_string(formatters={c: "{:+.1%}".format if "DD" not in c else "{:.1%}".format
                                      for c in pct}))

    plt = plot_equity(curves, trade_start, today)
    if not args.no_show:
        print("Chart-Fenster geöffnet – zum Beenden schließen.")
        plt.show()


if __name__ == "__main__":
    main()
