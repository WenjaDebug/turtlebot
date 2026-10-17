"""Backtest Cross-Asset Time-Series-Momentum ("Trend-Following über mehrere Anlageklassen").

- Universum: 7 ETFs (Aktien, Anleihen, Rohstoffe), tägliche Adjusted Closes via yfinance
- Signal: 12-Monats-Return (252 Handelstage) > 0 → Long, sonst Flat (Cash, 0 % Zins)
- Rebalancing: monatlich am letzten Handelstag, Ausführung zum Schlusskurs dieses Tages
- Sizing: Inverse-Volatility (60 Tage), Portfolio auf 10 % Vol-Target skaliert,
  max. 35 % pro Asset, kein Hebel (Summe der Gewichte max. 100 %)
- Kosten: 0,05 % auf jeden umgeschichteten Betrag
- Benchmark: SPY Buy & Hold über denselben Zeitraum

Aufruf: python backtest_trend_following.py
"""

import pathlib
import time

import matplotlib

matplotlib.use("Agg")  # nur PNGs speichern, kein Fenster öffnen
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
import yfinance as yf

# ---------------------------------------------------------------------------
# 1) EINSTELLUNGEN – alle Parameter der Strategie an einer Stelle
# ---------------------------------------------------------------------------
UNIVERSE = {
    "SPY": "Aktien USA",
    "EFA": "Aktien Industrieländer ex-US",
    "EEM": "Aktien Emerging Markets",
    "TLT": "US-Staatsanleihen lang",
    "IEF": "US-Staatsanleihen mittel",
    "GLD": "Gold",
    "DBC": "Rohstoffkorb",
}
BENCHMARK = "SPY"

DOWNLOAD_START = "2003-01-01"  # Vorlauf, damit die 252-Tage-Signale ab Backteststart gültig sind
BACKTEST_START = "2006-01-01"  # erster Rebalancing-Termin ist der letzte Handelstag im Jan. 2006

LOOKBACK_SIGNAL = 252   # Handelstage für das 12-Monats-Momentum
LOOKBACK_VOL = 60       # Handelstage für die Volatilitätsschätzung
VOL_TARGET = 0.10       # Ziel-Volatilität des Gesamtportfolios p.a.
MAX_WEIGHT = 0.35       # max. Kapitalanteil pro Einzelasset
MAX_GROSS = 1.00        # max. Summe aller Gewichte (1.0 = kein Hebel)
COST_RATE = 0.0005      # 0,05 % Transaktionskosten pro gehandeltem Betrag
RISK_FREE = 0.02        # risikofreier Zins für die Sharpe Ratio
TRADING_DAYS = 252      # Handelstage pro Jahr (für Annualisierung)

CACHE_DIR = pathlib.Path(".cache/yfinance")
CHART_DIR = pathlib.Path("charts_trend_following")

# Farben im selben Stil wie die übrigen Backtests im Projekt
TEXT, TEXT_MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
COLORS = {"Strategie": "#2a78d6", "SPY Buy & Hold": "#8a8984"}
LONG_COLOR, FLAT_COLOR = "#2a78d6", "#d9d8d3"


# ---------------------------------------------------------------------------
# 2) DATEN LADEN
# ---------------------------------------------------------------------------
def load_prices():
    """Lädt die bereinigten Schlusskurse aller ETFs als Tabelle (Zeilen = Tage, Spalten = Ticker).

    Ergebnis wird lokal zwischengespeichert, damit Yahoo nicht bei jedem Lauf neu gefragt wird.
    Ticker, die später an die Börse kamen (z.B. DBC), haben vor ihrem Listing einfach NaN.
    """
    today = pd.Timestamp.today().normalize()
    path = CACHE_DIR / f"trend_following_{DOWNLOAD_START}_{today:%Y%m%d}.pkl"
    if path.exists():
        return pd.read_pickle(path)

    closes = {}
    for ticker in UNIVERSE:
        # auto_adjust=True → Kurse sind um Dividenden und Splits bereinigt ("adjusted close")
        df = yf.Ticker(ticker).history(start=DOWNLOAD_START, end=today, interval="1d",
                                       auto_adjust=True, timeout=15)
        if df.empty:
            raise SystemExit(f"Keine Daten für {ticker} erhalten.")
        df.index = df.index.tz_localize(None)
        closes[ticker] = df["Close"]
        time.sleep(0.5)  # kurze Pause gegen Rate-Limits

    prices = pd.DataFrame(closes)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    prices.to_pickle(path)
    return prices


# ---------------------------------------------------------------------------
# 3) SIGNALE UND POSITIONSGRÖSSEN
# ---------------------------------------------------------------------------
def month_end_dates(index):
    """Letzter Handelstag jedes Monats (= Rebalancing-Termine)."""
    s = pd.Series(index, index=index)
    ends = pd.DatetimeIndex(s.groupby([index.year, index.month]).last().values)
    # Der laufende Monat ist noch nicht vorbei → sein bisher letzter Tag ist kein Monatsende
    if (ends[-1] + pd.offsets.BDay(1)).month == ends[-1].month:
        ends = ends[:-1]
    return ends


def target_weights(day, prices, returns):
    """Berechnet die Zielgewichte aller Assets für einen Rebalancing-Tag.

    Es werden nur Daten bis einschliesslich `day` verwendet (kein Blick in die Zukunft).
    Rückgabe: (Gewichte als Series, Signale als Series mit True=Long / False=Flat / NaN=noch nicht handelbar)
    """
    hist_prices = prices.loc[:day]
    hist_returns = returns.loc[:day]

    weights = pd.Series(0.0, index=prices.columns)
    signals = pd.Series(np.nan, index=prices.columns, dtype=object)

    # --- Signal: 12-Monats-Return pro Asset ---
    for ticker in prices.columns:
        p = hist_prices[ticker].dropna()
        if len(p) <= LOOKBACK_SIGNAL:
            continue  # Asset hat noch keine 12 Monate Historie → wird noch nicht gehandelt
        momentum = p.iloc[-1] / p.iloc[-1 - LOOKBACK_SIGNAL] - 1
        signals[ticker] = bool(momentum > 0)

    longs = [t for t in prices.columns if signals[t] is True]
    if not longs:
        return weights, signals  # alles Flat → 100 % Cash

    # --- Inverse-Volatility: jedes Long-Asset bekommt Gewicht ~ 1 / eigene Vola ---
    recent = hist_returns[longs].iloc[-LOOKBACK_VOL:]
    vols = recent.std() * np.sqrt(TRADING_DAYS)        # annualisierte 60-Tage-Vola
    raw = (1 / vols) / (1 / vols).sum()                  # summiert sich auf 100 %

    # --- Portfolio auf 10 % Vol-Target skalieren ---
    # Die Portfolio-Vola hängt auch von den Korrelationen ab, daher die Kovarianzmatrix.
    cov = recent.cov() * TRADING_DAYS
    port_vol = np.sqrt(raw @ cov @ raw)
    scaled = raw * (VOL_TARGET / port_vol)

    # Kein Hebel: wenn das Vol-Target mehr als 100 % Investitionsgrad verlangen würde,
    # wird auf 100 % gedeckelt (Cash ist mit 0 % verzinst, Kredit ist nicht vorgesehen).
    if scaled.sum() > MAX_GROSS:
        scaled *= MAX_GROSS / scaled.sum()

    # Harte Obergrenze pro Asset; der abgeschnittene Teil bleibt als Cash liegen.
    scaled = scaled.clip(upper=MAX_WEIGHT)

    weights[longs] = scaled
    return weights, signals


# ---------------------------------------------------------------------------
# 4) BACKTEST-SCHLEIFE
# ---------------------------------------------------------------------------
def run_backtest(prices):
    """Simuliert die Strategie Tag für Tag.

    Zwischen zwei Rebalancing-Terminen werden die Stückzahlen gehalten, d.h. die Gewichte
    "driften" mit den Kursen. Am Monatsende wird zum Schlusskurs auf die neuen Zielgewichte
    umgeschichtet und die Transaktionskosten werden vom Kapital abgezogen.
    """
    returns = prices.pct_change(fill_method=None)
    rebalance_days = month_end_dates(prices.index)
    rebalance_days = rebalance_days[rebalance_days >= BACKTEST_START]
    days = prices.index[prices.index >= rebalance_days[0]]

    cash = 1.0                                           # Startkapital (normiert), anfangs alles Cash
    holdings = pd.Series(0.0, index=prices.columns)      # investierter Betrag pro Asset
    equity_curve, weight_log, signal_log, cost_log = {}, {}, {}, {}

    for i, day in enumerate(days):
        # a) Wertentwicklung des heutigen Tages auf die gehaltenen Positionen anwenden
        #    (Cash bleibt unverändert, da mit 0 % verzinst)
        if i > 0:
            holdings *= 1 + returns.loc[day].fillna(0)
        equity = cash + holdings.sum()

        # b) Am Monatsende: neue Zielgewichte berechnen und umschichten
        if day in rebalance_days:
            weights, signals = target_weights(day, prices, returns)
            target = weights * equity
            turnover = (target - holdings).abs().sum()   # gesamter gehandelter Betrag
            cost = turnover * COST_RATE
            equity -= cost
            holdings = weights * equity                   # Positionen nach Kostenabzug
            cash = equity - holdings.sum()                # Rest liegt als Cash
            weight_log[day], signal_log[day], cost_log[day] = weights, signals, cost

        equity_curve[day] = equity

    return (pd.Series(equity_curve), pd.DataFrame(weight_log).T,
            pd.DataFrame(signal_log).T, pd.Series(cost_log))


# ---------------------------------------------------------------------------
# 5) KENNZAHLEN
# ---------------------------------------------------------------------------
def drawdown_series(equity):
    """Prozentualer Abstand zum bisherigen Höchststand (0 = neues Hoch, -0.2 = 20 % darunter)."""
    return equity / equity.cummax() - 1


def max_drawdown_duration(equity):
    """Längste Phase unter Wasser: vom alten Hoch bis zum Wiedererreichen (in Kalendertagen).

    Gibt (Dauer in Tagen, Start, Ende, erholt ja/nein) zurück.
    """
    dd = drawdown_series(equity)
    longest, best = pd.Timedelta(0), (None, None, True)
    start = None
    for day, value in dd.items():
        if value < 0 and start is None:
            start = day                        # neue Unterwasser-Phase beginnt
        elif value == 0 and start is not None:
            if day - start > longest:
                longest, best = day - start, (start, day, True)
            start = None
    if start is not None and dd.index[-1] - start > longest:  # noch nicht erholt
        longest, best = dd.index[-1] - start, (start, dd.index[-1], False)
    return longest.days, *best


def metrics(equity):
    """Alle Kennzahlen für eine Equity-Kurve als Dictionary."""
    daily = equity.pct_change().dropna()
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1
    vol = daily.std() * np.sqrt(TRADING_DAYS)
    # Sharpe: annualisierte Überrendite über dem risikofreien Zins geteilt durch die Vola
    sharpe = (daily.mean() * TRADING_DAYS - RISK_FREE) / vol
    dd = drawdown_series(equity)
    mdd = dd.min()
    dd_days, dd_start, dd_end, recovered = max_drawdown_duration(equity)
    return {
        "CAGR": cagr,
        "Volatilität p.a.": vol,
        "Sharpe Ratio (rf 2 %)": sharpe,
        "Max Drawdown": mdd,
        "Max-DD Tiefpunkt": dd.idxmin(),
        "Längste DD-Phase (Tage)": dd_days,
        "Längste DD-Phase": f"{dd_start:%m/%Y} – {dd_end:%m/%Y}" + ("" if recovered else " (offen)"),
        "Calmar Ratio": cagr / abs(mdd),
        "Endwert (Start 100)": 100 * equity.iloc[-1] / equity.iloc[0],
    }


# ---------------------------------------------------------------------------
# 6) CHARTS
# ---------------------------------------------------------------------------
def style_axis(ax, title):
    """Einheitliches, ruhiges Layout für alle Charts."""
    ax.set_facecolor(SURFACE)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.tick_params(length=0, colors=TEXT_MUTED)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.set_title(title, loc="left", fontsize=14, fontweight="bold", color=TEXT, pad=12)


def line_chart(series_by_name, title, filename, y_format, baseline=None, log_y=False,
               fill=False, legend_loc="upper left"):
    """Liniendiagramm für Strategie vs. SPY.

    fill=True füllt die Fläche zwischen Linie und Null (für den Underwater-Chart).
    """
    fig, ax = plt.subplots(figsize=(12, 6), facecolor=SURFACE)
    style_axis(ax, title)
    for name, s in series_by_name.items():
        color, front = COLORS[name], name == "Strategie"
        if fill:
            ax.fill_between(s.index, s.values, 0, color=color, alpha=0.35 if front else 0.25,
                            linewidth=0, zorder=3 if front else 2)
        ax.plot(s.index, s.values, color=color, linewidth=1.2 if fill else 1.8, label=name,
                zorder=4 if front else 2)
    if log_y:
        ax.set_yscale("log")
        ax.yaxis.set_minor_formatter(mticker.NullFormatter())
    if baseline is not None:
        ax.axhline(baseline, color=TEXT_MUTED, linewidth=0.8, zorder=1)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(y_format))
    ax.legend(loc=legend_loc, frameon=False, fontsize=11)
    ax.margins(x=0.01)
    fig.tight_layout()
    fig.savefig(CHART_DIR / filename, dpi=120, facecolor=SURFACE)
    plt.close(fig)


def chart_long_flat(signals):
    """Balkendiagramm: Anteil der Monate Long bzw. Flat pro Asset (ab Handelbarkeit)."""
    rows = []
    for ticker in signals.columns:
        s = signals[ticker].dropna().astype(bool)          # nur Monate, in denen das Asset handelbar war
        # Länge der Long-Phasen: aufeinanderfolgende Long-Monate zählen
        runs = s.ne(s.shift()).cumsum()
        long_runs = s.groupby(runs).agg(["first", "size"])
        avg_long = long_runs.loc[long_runs["first"], "size"].mean()
        rows.append((ticker, s.sum(), (~s).sum(), avg_long))
    stats = pd.DataFrame(rows, columns=["ticker", "long", "flat", "avg_long"]).set_index("ticker")
    total = stats["long"] + stats["flat"]

    fig, ax = plt.subplots(figsize=(12, 5.5), facecolor=SURFACE)
    style_axis(ax, "Wie oft war jedes Asset Long bzw. Flat? (Anteil der Monate)")
    ax.grid(False)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)  # Gitterlinien hinter die Balken legen
    y = np.arange(len(stats))[::-1]
    ax.barh(y, stats["long"] / total, color=LONG_COLOR, height=0.6, label="Long")
    ax.barh(y, stats["flat"] / total, left=stats["long"] / total, color=FLAT_COLOR,
            height=0.6, label="Flat (Cash)")
    for yi, (ticker, row) in zip(y, stats.iterrows()):
        share = row["long"] / (row["long"] + row["flat"])
        ax.text(share - 0.01, yi, f"{share:.0%}  ({row['long']:.0f} Mon.)", ha="right",
                va="center", color="white", fontsize=10, fontweight="bold")
        ax.text(1.01, yi, f"Ø Long-Phase {row['avg_long']:.1f} Mon.", va="center",
                color=TEXT_MUTED, fontsize=9)
    ax.set_yticks(y, [f"{t}  {UNIVERSE[t]}" for t in stats.index])
    ax.xaxis.set_major_formatter(mticker.PercentFormatter(1.0))
    ax.set_xlim(0, 1.22)
    ax.set_xticks(np.linspace(0, 1, 6))
    ax.spines["bottom"].set_visible(False)
    ax.legend(loc="upper left", bbox_to_anchor=(0.0, -0.08), ncol=2, frameon=False, fontsize=10)
    fig.tight_layout()
    fig.savefig(CHART_DIR / "4_long_flat_pro_asset.png", dpi=120, facecolor=SURFACE)
    plt.close(fig)


def make_charts(strategy, spy):
    """Erzeugt die drei Zeitreihen-Charts (Equity, Underwater, rollierende Vola)."""
    CHART_DIR.mkdir(exist_ok=True)
    plt.rcParams.update({"font.size": 10, "text.color": TEXT,
                         "xtick.color": TEXT_MUTED, "ytick.color": TEXT_MUTED})
    start = f"{strategy.index[0]:%m/%Y}"

    # 1) Equity-Kurven, beide auf 100 normiert (logarithmische Achse: gleiche %-Bewegung = gleiche Höhe)
    eq = {"Strategie": 100 * strategy / strategy.iloc[0], "SPY Buy & Hold": 100 * spy / spy.iloc[0]}
    line_chart(eq, f"Equity-Kurve: Trend-Following vs. SPY Buy & Hold (Start {start} = 100, log. Skala)",
               "1_equity_kurve.png", lambda v, _: f"{v:,.0f}", baseline=100, log_y=True)

    # 2) Underwater-Chart
    dd = {name: drawdown_series(s) for name, s in eq.items()}
    line_chart(dd, "Underwater-Chart: Abstand zum bisherigen Höchststand", "2_underwater.png",
               lambda v, _: f"{v:.0%}", baseline=0, fill=True, legend_loc="lower right")

    # 3) Rollierende 12-Monats-Volatilität (252 Handelstage)
    roll = {name: s.pct_change().rolling(TRADING_DAYS).std().mul(np.sqrt(TRADING_DAYS)).dropna()
            for name, s in eq.items()}
    line_chart(roll, "Rollierende 12-Monats-Volatilität (annualisiert)", "3_rollierende_vola.png",
               lambda v, _: f"{v:.0%}", baseline=VOL_TARGET)


# ---------------------------------------------------------------------------
# 7) HAUPTPROGRAMM
# ---------------------------------------------------------------------------
def main():
    prices = load_prices()
    print("Datenverfügbarkeit (erster Kurs pro Ticker):")
    for ticker in prices.columns:
        first = prices[ticker].first_valid_index()
        print(f"  {ticker:<4} {UNIVERSE[ticker]:<30} ab {first:%d.%m.%Y}")

    strategy, weights, signals, costs = run_backtest(prices)

    # Benchmark über exakt denselben Zeitraum
    spy = prices[BENCHMARK].loc[strategy.index[0]:strategy.index[-1]]

    # Kennzahlen-Tabelle nebeneinander
    table = pd.DataFrame({"Strategie": metrics(strategy), "SPY Buy & Hold": metrics(spy)})
    monthly_strat = strategy.resample("ME").last().pct_change().dropna()
    monthly_spy = spy.resample("ME").last().pct_change().dropna()
    correlation = monthly_strat.corr(monthly_spy)

    formats = {
        "CAGR": "{:+.2%}", "Volatilität p.a.": "{:.2%}", "Sharpe Ratio (rf 2 %)": "{:.2f}",
        "Max Drawdown": "{:.2%}", "Max-DD Tiefpunkt": "{:%d.%m.%Y}",
        "Längste DD-Phase (Tage)": "{:,}", "Längste DD-Phase": "{}",
        "Calmar Ratio": "{:.2f}", "Endwert (Start 100)": "{:,.1f}",
    }
    pretty = table.apply(lambda col: [formats[k].format(v) for k, v in col.items()])

    make_charts(strategy, spy)
    chart_long_flat(signals)

    # --- Zusammenfassung im Terminal ---
    print(f"\nBacktest-Zeitraum: {strategy.index[0]:%d.%m.%Y} – {strategy.index[-1]:%d.%m.%Y} "
          f"({len(weights)} Rebalancings)")
    print("\n===== Kennzahlen: Trend-Following vs. SPY Buy & Hold =====")
    print(pretty.to_string())
    print(f"\nKorrelation der Monatsrenditen Strategie vs. SPY: {correlation:.2f}")

    invested = weights.sum(axis=1)
    print(f"\nØ Investitionsgrad: {invested.mean():.0%}  "
          f"(min {invested.min():.0%}, max {invested.max():.0%}); "
          f"Monate komplett in Cash: {(invested == 0).sum()}")
    print(f"Transaktionskosten gesamt: {costs.sum():.2%} des Startkapitals")
    print("\nAktuelle Positionierung (letztes Rebalancing "
          f"{weights.index[-1]:%d.%m.%Y}):")
    for ticker, w in weights.iloc[-1].items():
        print(f"  {ticker:<4} {'Long' if w > 0 else 'Flat':<5} {w:6.1%}")

    print(f"\nCharts gespeichert in: {CHART_DIR.resolve()}/")
    for png in sorted(CHART_DIR.glob("*.png")):
        print(f"  {png.name}")


if __name__ == "__main__":
    main()
