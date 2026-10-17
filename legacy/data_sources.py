"""Datenbeschaffung mit lokalem Cache: Index-Universum (Wikipedia, nasdaqtrader.com), Tagesdaten und
Market Cap (yfinance)."""

import gc
import io
import json
import pathlib
import time

import pandas as pd
import requests
import yfinance as yf

CACHE_DIR = pathlib.Path(".cache")
HEADERS = {"User-Agent": "Mozilla/5.0 (backtest research script)"}
WIKI = {
    "S&P 500": ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "Symbol"),
    "Nasdaq-100": ("https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies", "Ticker"),
}
LIVE_WIKI = {
    "S&P 500": WIKI["S&P 500"],
    "S&P 400": ("https://en.wikipedia.org/wiki/List_of_S%26P_400_companies", "Symbol"),
    "S&P 600": ("https://en.wikipedia.org/wiki/List_of_S%26P_600_companies", "Symbol"),
}
NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
# Keine Stammaktien: Optionsscheine, Units, Bezugsrechte, Vorzugsaktien, Anleihen
NON_COMMON = r"\b(?:Warrants?|Units?|Rights?|Preferred|Notes|Debentures)\b"
# SPACs (Mantelgesellschaften): "... Acquisition Corp.", "Acquisition Company", "Acquisitions II Corp",
# "KRAKacquisition Corp", "Acquisition I" usw.
SPAC = (r"acquisitions?\d*\b(?:\s+[\w&.]+){0,3}?\s+(?:Corp(?:oration)?|Company|Co|Inc|Ltd|Limited)\b"
        r"|\bacquisitions?\s+[IVX]+\b|\bblank check\b")


def _wiki_constituents(sources):
    frames = []
    for index, (url, column) in sources.items():
        html = requests.get(url, headers=HEADERS, timeout=20).text
        table = next(t for t in pd.read_html(io.StringIO(html), flavor="lxml") if column in t.columns)
        frames.append(pd.DataFrame({"ticker": table[column].astype(str).str.strip(), "index": index}))
    return pd.concat(frames)


def _combine(frames):
    return (pd.concat(frames)
            .assign(ticker=lambda d: d["ticker"].str.replace(".", "-", regex=False))  # BRK.B -> BRK-B
            .groupby("ticker", as_index=False)["index"].agg(" + ".join))


def load_universe(as_of):
    """Aktuelle Konstituenten S&P 500 + Nasdaq-100 als statische Liste (einmal pro Tag gespeichert)."""
    path = CACHE_DIR / f"universe_{as_of:%Y%m%d}.csv"
    if path.exists():
        return pd.read_csv(path)
    universe = _combine([_wiki_constituents(WIKI)])
    path.parent.mkdir(parents=True, exist_ok=True)
    universe.to_csv(path, index=False)
    return universe


def load_nasdaq_listed(exclude_spacs=True):
    """Alle an der Nasdaq gelisteten Stammaktien (offizielle Symbolliste, ohne ETFs und Test-Titel,
    standardmässig auch ohne SPACs)."""
    text = requests.get(NASDAQ_LISTED, headers=HEADERS, timeout=30).text
    table = pd.read_csv(io.StringIO(text), sep="|")
    table = table[~table["Symbol"].astype(str).str.startswith("File Creation Time")]
    common = ((table["Test Issue"] == "N") & (table["ETF"] == "N") & (table["Financial Status"] == "N")
              & table["Symbol"].astype(str).str.fullmatch(r"[A-Z]+")
              & ~table["Security Name"].astype(str).str.contains(NON_COMMON, case=False, regex=True))
    if exclude_spacs:
        spacs = common & table["Security Name"].astype(str).str.contains(SPAC, case=False, regex=True)
        print(f"  Nasdaq-Liste: {spacs.sum()} SPACs ausgeschlossen")
        common &= ~spacs
    return pd.DataFrame({"ticker": table.loc[common, "Symbol"], "index": "Nasdaq Composite"})


def load_live_universe(as_of):
    """Live-Universum: S&P 500 + S&P 400 + S&P 600 + Nasdaq Composite, einmal pro Tag neu aufgebaut."""
    path = CACHE_DIR / "live" / f"universe_{as_of:%Y%m%d}.csv"
    if path.exists():
        return pd.read_csv(path)
    universe = _combine([_wiki_constituents(LIVE_WIKI), load_nasdaq_listed()])
    path.parent.mkdir(parents=True, exist_ok=True)
    universe.to_csv(path, index=False)
    return universe


def load_index_details(as_of):
    """Sektor (GICS bzw. ICB für reine Nasdaq-100-Titel) und S&P-500-Aufnahmedatum je Ticker."""
    path = CACHE_DIR / f"index_details_{as_of:%Y%m%d}.csv"
    if path.exists():
        return pd.read_csv(path, parse_dates=["sp500_added"])

    def fetch(index):
        url, _ = WIKI[index]
        html = requests.get(url, headers=HEADERS, timeout=20).text
        return pd.read_html(io.StringIO(html), flavor="lxml")[0]

    sp = fetch("S&P 500")
    nd = fetch("Nasdaq-100")
    details = pd.concat([
        pd.DataFrame({"ticker": sp["Symbol"], "sector": sp["GICS Sector"],
                      "sp500_added": pd.to_datetime(sp["Date added"], errors="coerce")}),
        pd.DataFrame({"ticker": nd["Ticker"], "sector": nd.filter(like="ICB Industry").iloc[:, 0],
                      "sp500_added": pd.NaT}),
    ])
    details["ticker"] = details["ticker"].astype(str).str.strip().str.replace(".", "-", regex=False)
    details = details.drop_duplicates("ticker")  # S&P-Eintrag (GICS) hat Vorrang
    path.parent.mkdir(parents=True, exist_ok=True)
    details.to_csv(path, index=False)
    return details


def _download(tickers, start, end, batch_size, threads=True):
    """Lädt Tages-OHLCV in Batches; liefert (ticker, DataFrame)-Paare.

    yfinance hält pro Ticker Verbindungen und Cache-Datenbank-Handles offen, bis Python den Speicher
    freigibt (gemessen: bis ~490 offene Dateien bei 50er-Batches). gc.collect() nach jedem Batch
    begrenzt das; threads=False braucht nur ~25 offene Dateien, ist aber ca. 4x langsamer.
    """
    loaded = 0
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        raw = yf.download(batch, start=start, end=end, interval="1d", auto_adjust=False,
                          group_by="ticker", threads=threads, progress=False, timeout=30)
        for ticker in batch:
            if ticker not in raw.columns.get_level_values(0):
                continue
            df = raw[ticker][["Open", "High", "Low", "Close", "Volume"]].dropna(how="all")
            if not df.empty:
                df.index = pd.DatetimeIndex(df.index).tz_localize(None)
                loaded += 1
                yield ticker, df
        del raw
        gc.collect()
        print(f"  Kursdaten: {min(i + batch_size, len(tickers))}/{len(tickers)} geladen")
        time.sleep(2)
    missing = len(tickers) - loaded
    if tickers and missing > 0.1 * len(tickers):
        # yfinance meldet Fehler (z. B. "unable to open database file") nur als "Failed downloads"
        print(f"  WARNUNG: {missing} von {len(tickers)} Tickern ohne Kursdaten – Download-Problem?")


def load_prices(tickers, start, end, batch_size=50, cache=True, threads=True):
    """Tages-OHLCV (Split-bereinigt) für viele Ticker, in Batches geladen und pro Ticker gecacht.

    cache=False lädt immer frisch (z. B. für Intraday-Scans, bei denen der heutige Balken noch läuft).
    """
    if not cache:
        return dict(_download(list(tickers), start, end, batch_size, threads))
    folder = CACHE_DIR / f"prices_{start:%Y%m%d}_{end:%Y%m%d}"
    folder.mkdir(parents=True, exist_ok=True)
    missing = [t for t in tickers if not (folder / f"{t}.pkl").exists()]
    for ticker, df in _download(missing, start, end, batch_size, threads):
        df.to_pickle(folder / f"{ticker}.pkl")
    return {t: pd.read_pickle(folder / f"{t}.pkl") for t in tickers if (folder / f"{t}.pkl").exists()}


def liquid_tickers(prices, min_dollar_volume, window=20):
    """Ticker, deren durchschnittliches Tages-Dollarvolumen (Close × Volumen) über `window` Tage
    über min_dollar_volume liegt."""
    return [t for t, df in prices.items()
            if len(df) >= window and (df["Close"] * df["Volume"]).tail(window).mean() > min_dollar_volume]


def load_market_caps(tickers, as_of):
    """Market Cap als Momentaufnahme von heute (yfinance .info), gecacht. None = nicht verfügbar."""
    path = CACHE_DIR / f"market_caps_{as_of:%Y%m%d}.json"
    caps = json.loads(path.read_text()) if path.exists() else {}
    todo = [t for t in tickers if t not in caps]
    for n, ticker in enumerate(todo, 1):
        try:
            caps[ticker] = yf.Ticker(ticker).info.get("marketCap")
        except Exception as exc:  # Rate-Limit o. Ä.: als unbekannt loggen, nicht abbrechen
            print(f"  Market Cap {ticker}: Fehler ({type(exc).__name__})")
            caps[ticker] = None
        if n % 50 == 0 or n == len(todo):
            print(f"  Market Caps: {n}/{len(todo)} geladen")
            path.write_text(json.dumps(caps))
        time.sleep(0.3)
    return {t: caps.get(t) for t in tickers}
