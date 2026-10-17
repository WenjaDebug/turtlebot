# chat_tools.py
"""Tool-Funktionen, die Claude im Chat-Panel aufrufen kann, um echte Daten ueber den
Trading-Bot zu bekommen. Liest nur -- schreibt nirgendwo in Bot- oder State-Dateien."""

import json
from pathlib import Path

from db import get_signals_today, get_open_positions as db_get_orders

BOT_DIR = Path(__file__).parent.parent
STATE_JSON = BOT_DIR / "live_turtle_state.json"


def _load_state():
    if not STATE_JSON.exists():
        return {}
    return json.loads(STATE_JSON.read_text())


def get_portfolio_overview(block: str | None = None) -> dict:
    """Aktuelle offene Positionen direkt aus dem Live-State des Bots (Quelle der Wahrheit,
    nicht aus dem Journal-Log). block: '1', '2' oder None fuer alle."""
    state = _load_state()
    positions = state.get("positions", {})
    result = []
    for symbol, pos in positions.items():
        if pos.get("status") != "open":
            continue
        if block is not None and str(pos.get("block")) != str(block):
            continue
        result.append({
            "symbol": symbol,
            "block": pos.get("block"),
            "qty": pos.get("qty"),
            "entry_price": pos.get("entry_price"),
            "stop": pos.get("stop"),
            "entry_date": pos.get("entry_date"),
        })
    return {"open_positions": result, "count": len(result)}


def get_recent_signals(limit: int = 20) -> dict:
    """Heutige Turtle-Breakout-Signale (Kandidaten-Pool, vor Block-Filterung)."""
    signals = get_signals_today()
    return {"signals": signals[:limit], "count": len(signals)}


def get_recent_trades(block: str | None = None, limit: int = 20) -> dict:
    """Zuletzt ausgefuehrte Trades (Kaeufe) aus dem synchronisierten Journal-Log."""
    strategie_block = f"block{block}" if block else None
    trades = db_get_orders(strategie_block)
    return {"trades": trades[:limit], "count": len(trades)}


TOOL_DEFINITIONS = [
    {
        "name": "get_portfolio_overview",
        "description": "Gibt die aktuell offenen Positionen des Trading-Bots zurueck (Symbol, Block, Menge, "
                        "Einstiegspreis, Stop-Loss, Einstiegsdatum). Quelle: Live-State des Bots, also der "
                        "aktuell genaueste Stand.",
        "input_schema": {
            "type": "object",
            "properties": {
                "block": {"type": "string", "description": "'1' fuer Block 1 (Turtle-Breakout), '2' fuer "
                                                            "Block 2 (gefiltert), leer lassen fuer alle."}
            },
        },
    },
    {
        "name": "get_recent_signals",
        "description": "Gibt die heutigen Turtle-Breakout-Signale zurueck (Kandidaten-Pool, bevor sie auf "
                        "Block 1 oder 2 gefiltert wurden) -- Symbol, Breakout-Strength, Zeitpunkt.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max. Anzahl Ergebnisse, Standard 20."}
            },
        },
    },
    {
        "name": "get_recent_trades",
        "description": "Gibt die zuletzt ausgefuehrten Trades (Kaeufe) zurueck, optional gefiltert nach Block.",
        "input_schema": {
            "type": "object",
            "properties": {
                "block": {"type": "string", "description": "'1' oder '2', leer lassen fuer alle."},
                "limit": {"type": "integer", "description": "Max. Anzahl Ergebnisse, Standard 20."},
            },
        },
    },
]

TOOL_FUNCTIONS = {
    "get_portfolio_overview": get_portfolio_overview,
    "get_recent_signals": get_recent_signals,
    "get_recent_trades": get_recent_trades,
}
