# equity_snapshot.py
"""Holt aktuelle Kurse (yfinance) fuer alle offenen Positionen und schreibt einen
Equity-Snapshot pro Block in die Dashboard-DB. Liest nur -- schreibt nicht in Bot-Dateien.

Hinweis zur Interpretation: 'Equity' hier = Marktwert der offenen Positionen (qty * aktueller
Kurs), 'P&L' = unrealisierter Gewinn/Verlust ggue. Einstiegspreis. Das ist NICHT die vollstaendige
Kontobilanz (Cash, realisierte Gewinne/Verluste aus geschlossenen Trades fehlen) -- fuer den
vollen Kontostand bräuchte es eine direkte IBKR-Abfrage."""

import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import yfinance as yf

from db import log_snapshot, init_db

BOT_DIR = Path(__file__).parent.parent
STATE_JSON = BOT_DIR / "live_turtle_state.json"


def load_open_positions():
    if not STATE_JSON.exists():
        return []
    data = json.loads(STATE_JSON.read_text())
    return [
        {"symbol": sym, **pos}
        for sym, pos in data.get("positions", {}).items()
        if pos.get("status") == "open"
    ]


def fetch_current_prices(symbols):
    if not symbols:
        return {}
    data = yf.download(symbols, period="1d", progress=False, group_by="ticker", threads=True)
    prices = {}
    for sym in symbols:
        try:
            if len(symbols) == 1:
                prices[sym] = float(data["Close"].iloc[-1])
            else:
                prices[sym] = float(data[sym]["Close"].iloc[-1])
        except Exception as e:
            print(f"Warnung: Kein Kurs fuer {sym} ({e})")
    return prices


def get_portfolio_overview():
    """Aktuelle offene Positionen mit Live-Kurs und unrealisiertem P&L -- fuer die
    Portfolio-Uebersicht im Dashboard. Loggt NICHTS in die DB (im Unterschied zu
    run_equity_snapshot), reine Lese-Abfrage fuer die Anzeige."""
    positions = load_open_positions()
    if not positions:
        return []

    symbols = sorted({p["symbol"] for p in positions})
    prices = fetch_current_prices(symbols)

    overview = []
    for p in positions:
        price = prices.get(p["symbol"])
        entry = p.get("entry_price")
        qty = p.get("qty", 0)
        market_value = qty * price if price is not None else None
        pnl = (price - entry) * qty if price is not None and entry is not None else None
        pnl_pct = ((price - entry) / entry * 100) if price is not None and entry else None
        overview.append({
            "symbol": p["symbol"],
            "block": p.get("block", 1),
            "qty": qty,
            "entry_price": entry,
            "current_price": price,
            "market_value": round(market_value, 2) if market_value is not None else None,
            "pnl": round(pnl, 2) if pnl is not None else None,
            "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
            "stop": p.get("stop"),
        })
    overview.sort(key=lambda r: (r["block"], r["symbol"]))
    return overview


def run_equity_snapshot():
    """Fuehrt den Snapshot aus und gibt eine Liste von Ergebnis-Dicts zurueck (fuer UI-Anzeige)."""
    positions = load_open_positions()
    if not positions:
        return []

    symbols = sorted({p["symbol"] for p in positions})
    prices = fetch_current_prices(symbols)

    per_block = defaultdict(lambda: {"equity": 0.0, "pnl": 0.0, "open_positions": 0})
    missing = []
    for p in positions:
        price = prices.get(p["symbol"])
        if price is None:
            missing.append(p["symbol"])
            continue
        block = p.get("block", 1)
        qty = p.get("qty", 0)
        entry = p.get("entry_price", price)
        per_block[block]["equity"] += qty * price
        per_block[block]["pnl"] += (price - entry) * qty
        per_block[block]["open_positions"] += 1

    today = date.today().isoformat()
    results = []
    for block, vals in per_block.items():
        log_snapshot(
            date=today,
            strategie_block=f"block{block}",
            equity=round(vals["equity"], 2),
            open_positions=vals["open_positions"],
            pnl=round(vals["pnl"], 2),
        )
        results.append({"block": block, **vals})

    if missing:
        results.append({"missing_symbols": missing})
    return results


def main():
    results = run_equity_snapshot()
    if not results:
        print("Keine offenen Positionen gefunden.")
        return
    for r in results:
        if "missing_symbols" in r:
            print(f"Warnung: keine Kurse fuer {', '.join(r['missing_symbols'])}")
            continue
        print(f"Block {r['block']}: {r['open_positions']} Positionen, "
              f"Marktwert ${r['equity']:,.2f}, unrealisiert ${r['pnl']:+,.2f}")


if __name__ == "__main__":
    init_db()
    main()
