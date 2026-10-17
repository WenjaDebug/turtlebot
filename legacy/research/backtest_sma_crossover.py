"""Backtest einer SMA-Crossover-Strategie (50 vs. 200 Tage) mit backtrader.

Aufruf: python backtest_sma_crossover.py [TICKER] [--no-show]   (Standard: AAPL)

Ohne --no-show öffnet sich zusätzlich zum gespeicherten PNG ein interaktives Chart-Fenster.
"""

import argparse

import backtrader as bt
import backtrader.plot  # setzt auf macOS das GUI-Backend "MacOSX"
import matplotlib
import yfinance as yf

PERIOD = "2y"
START_CASH = 10_000.0
COMMISSION = 0.001  # 0,1 % pro Order


class SmaCross(bt.Strategy):
    params = dict(fast=50, slow=200)

    def __init__(self):
        sma_fast = bt.ind.SMA(period=self.p.fast)
        sma_slow = bt.ind.SMA(period=self.p.slow)
        self.crossover = bt.ind.CrossOver(sma_fast, sma_slow)

    def next(self):
        if not self.position and self.crossover > 0:
            # Golden Cross: mit ~95 % des Kapitals kaufen (Puffer für Gebühren/Gap)
            size = int(self.broker.getcash() * 0.95 / self.data.close[0])
            self.buy(size=size)
        elif self.position and self.crossover < 0:
            # Death Cross: Position schließen
            self.close()

    def notify_order(self, order):
        if order.status == order.Completed:
            side = "KAUF" if order.isbuy() else "VERKAUF"
            print(f"{self.data.datetime.date(0)}  {side:7s} {order.executed.size:>5.0f} "
                  f"@ {order.executed.price:8.2f}")


def load_data(ticker, period=PERIOD, start=None):
    span = {"start": start} if start else {"period": period}
    df = yf.Ticker(ticker).history(**span, interval="1d", auto_adjust=True, timeout=15)
    if df.empty:
        raise SystemExit(f"Keine Daten für {ticker} erhalten.")
    df.index = df.index.tz_localize(None)  # backtrader erwartet naive Timestamps
    return df[["Open", "High", "Low", "Close", "Volume"]]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ticker", nargs="?", default="AAPL")
    parser.add_argument("--no-show", action="store_true",
                        help="kein interaktives Fenster, nur PNG speichern")
    args = parser.parse_args()
    ticker = args.ticker.upper()
    plot_file = f"backtest_sma_crossover_{ticker}.png"
    if args.no_show:
        matplotlib.use("Agg")  # überschreibt das GUI-Backend von backtrader.plot

    df = load_data(ticker)
    print(f"{ticker}: {len(df)} Handelstage von {df.index[0].date()} bis {df.index[-1].date()}\n")

    cerebro = bt.Cerebro()
    cerebro.adddata(bt.feeds.PandasData(dataname=df), name=ticker)
    cerebro.addstrategy(SmaCross)
    cerebro.broker.setcash(START_CASH)
    cerebro.broker.setcommission(commission=COMMISSION)

    cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe",
                        timeframe=bt.TimeFrame.Days, annualize=True, riskfreerate=0.0)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")

    strat = cerebro.run()[0]

    end_value = cerebro.broker.getvalue()
    total_return = end_value / START_CASH - 1
    buy_hold = df["Close"].iloc[-1] / df["Close"].iloc[0] - 1
    sharpe = strat.analyzers.sharpe.get_analysis().get("sharperatio")
    dd = strat.analyzers.drawdown.get_analysis()
    trades = strat.analyzers.trades.get_analysis()
    n_closed = trades.get("total", {}).get("closed", 0)
    n_open = trades.get("total", {}).get("open", 0)
    n_won = trades.get("won", {}).get("total", 0)

    print("\n===== Performance =====")
    print(f"Startkapital:         {START_CASH:>12,.2f} $")
    print(f"Endkapital:           {end_value:>12,.2f} $")
    print(f"Gesamtrendite:        {total_return:>12.2%}")
    print(f"{f'Buy & Hold ({ticker}):':<22}{buy_hold:>12.2%}")
    print(f"Sharpe Ratio (ann.):  {sharpe:>12.2f}" if sharpe is not None
          else "Sharpe Ratio (ann.):          n/a")
    print(f"Max. Drawdown:        {dd.max.drawdown:>12.2f} %")
    print(f"Trades geschlossen:   {n_closed:>12d}  (davon gewonnen: {n_won})")
    print(f"Trades offen:         {n_open:>12d}")

    # Statt cerebro.plot() direkt den Plotter nutzen: erst speichern, dann (blockierend) anzeigen
    plotter = bt.plot.Plot(style="candlestick", volume=False)
    fig = plotter.plot(strat, iplot=False)[0]
    fig.set_size_inches(14, 9)
    fig.savefig(plot_file, dpi=120, bbox_inches="tight")
    print(f"\nPlot gespeichert: {plot_file}")
    if not args.no_show:
        print("Chart-Fenster geöffnet – zum Beenden schließen.")
        plotter.show()


if __name__ == "__main__":
    main()
