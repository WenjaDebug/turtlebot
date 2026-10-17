"""Signal-Logik der beiden Teilportfolios: reine Pandas-Funktionen ohne Backtest- oder Broker-Abhängigkeit.

Eingabe: Tagesdaten als DataFrame mit Spalten Open, High, Low, Close, Volume, aufsteigend nach Datum
(Daten von ib_async vorher mit standardize_ohlcv() umformen).

Ausgabe: DataFrame mit einer Zeile pro Tag
  entry  - Kaufsignal an diesem Tag (Ausführung zum Schlusskurs)
  exit   - Verkaufssignal an diesem Tag (Ausführung zum Schlusskurs)
  valid  - ein wegen voller Slots noch nicht ausgeführtes Kaufsignal darf heute nachgeholt werden
  score  - Rangfolge, wenn mehr Kandidaten als freie Slots vorhanden sind (höher = zuerst)

Backtest: Funktion einmal auf die ganze Historie anwenden und Tag für Tag lesen.
Live: Funktion auf die aktuelle Historie anwenden und die letzte Zeile lesen, z. B.
    sig = golden_triangle(standardize_ohlcv(util.df(bars))).iloc[-1]
"""

import pandas as pd


def standardize_ohlcv(df):
    """Bringt Daten aus ib_async (util.df(bars): Spalten date/open/high/...) ins erwartete Format."""
    df = df.rename(columns=str.capitalize)
    if "Date" in df.columns:
        df = df.set_index(pd.to_datetime(df.pop("Date")))
    return df[["Open", "High", "Low", "Close", "Volume"]].sort_index()


def golden_triangle(df, fast=50, slow=200):
    """Teilportfolio A: SMA-Crossover. Entry = Golden Cross, Exit = Death Cross.

    Ein verpasstes Signal (alle Slots belegt) bleibt gültig, solange SMA50 über SMA200 liegt.
    """
    sma_fast = df["Close"].rolling(fast).mean()
    sma_slow = df["Close"].rolling(slow).mean()
    above = sma_fast > sma_slow
    was_above = above.shift(1, fill_value=False)
    ready = sma_slow.notna() & sma_slow.shift(1).notna()  # Kreuzung nur mit zwei echten SMA-Werten
    return pd.DataFrame({
        "entry": above & ~was_above & ready,
        "exit": ~above & was_above & ready,
        "valid": above,
        "score": sma_fast / sma_slow - 1,
        "sma_fast": sma_fast,
        "sma_slow": sma_slow,
    }, index=df.index)


def two_day_returns(closes):
    """Kumulierte 2-Tages-Rendite (Close vor 2 Tagen -> Close heute) für ein Datum × Ticker-DataFrame."""
    return closes / closes.shift(2) - 1


def biggest_losers(two_day_row, held=(), n=3):
    """Die n Titel mit dem negativsten 2-Tages-Return, die nicht bereits gehalten werden.

    two_day_row: eine Zeile aus two_day_returns() (Ticker -> Rendite), nach Börsenschluss berechnet.
    """
    return two_day_row.drop(labels=list(held), errors="ignore").dropna().nsmallest(n).index.tolist()


def turtle_breakout(df, entry_period=20, exit_period=10, volume_period=20, volume_factor=1.5,
                    rank_by="volume_ratio"):
    """Teilportfolio B: Donchian-Breakout mit Volumenbestätigung.

    Entry: Schlusskurs über dem höchsten Hoch der letzten 20 Tage (ohne heute) UND
           Volumen > 1,5 × Durchschnittsvolumen der letzten 20 Tage (ohne heute).
    Exit:  Schlusskurs unter dem tiefsten Tief der letzten 10 Tage (ohne heute).
    Ein Breakout gilt nur am Signaltag; ist dann kein Slot frei, wird er übersprungen.

    rank_by: Spalte, nach der bei mehr Signalen als freien Slots sortiert wird. "volume_ratio"
    ist der Stand der bisherigen Backtests; "breakout_strength" (Schluss relativ zum Donchian-Hoch)
    war in analyze_turtle.py über 20 Start-Tage robuster und wird live verwendet.
    """
    donchian_high = df["High"].rolling(entry_period).max().shift(1)
    donchian_low = df["Low"].rolling(exit_period).min().shift(1)
    volume_ratio = df["Volume"] / df["Volume"].rolling(volume_period).mean().shift(1)
    breakout_strength = df["Close"] / donchian_high - 1
    entry = (df["Close"] > donchian_high) & (volume_ratio > volume_factor)
    result = pd.DataFrame({
        "entry": entry,
        "exit": df["Close"] < donchian_low,
        "valid": entry,
        "donchian_high": donchian_high,
        "donchian_low": donchian_low,
        "volume_ratio": volume_ratio,
        "breakout_strength": breakout_strength,
    }, index=df.index)
    result["score"] = result[rank_by]
    return result
