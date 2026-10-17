# sync.py
"""Synchronisiert die Live-Journal-CSV und den State-JSON des Trading-Bots in die Dashboard-DB.
Laeuft unabhaengig vom Bot -- liest nur, schreibt nie in die Bot-Dateien."""

import csv
import json
import re
import sys
import time
from pathlib import Path

from db import log_order, log_signal, init_db

BOT_DIR = Path(__file__).parent.parent
JOURNAL_CSV = BOT_DIR / "live_turtle_journal.csv"
STATE_JSON = BOT_DIR / "live_turtle_state.json"
SYNC_STATE_FILE = Path(__file__).parent / ".sync_state.json"

BLOCK_RE = re.compile(r"block=(\d+)")

# Ereignisse, die zeigen, dass eine Position NACH einem EXIT-Eintrag noch (wieder) aktiv war --
# dann war der EXIT ein Reconcile-Fehlalarm, kein echter Verkauf.
SUPERSEDING_EVENTS = {"FILL", "STOP_NEW", "STOP_UPDATE"}


def load_sync_state():
    if SYNC_STATE_FILE.exists():
        return json.loads(SYNC_STATE_FILE.read_text())
    return {"last_row": 0}


def save_sync_state(state):
    SYNC_STATE_FILE.write_text(json.dumps(state))


def load_bot_positions():
    """symbol -> block, aus dem aktuellen State des Bots (fuer Zeilen ohne Block-Angabe)."""
    if not STATE_JSON.exists():
        return {}
    data = json.loads(STATE_JSON.read_text())
    return {sym: pos.get("block", 1) for sym, pos in data.get("positions", {}).items()}


def resolve_block(details, symbol, positions_by_symbol):
    match = BLOCK_RE.search(details or "")
    if match:
        return int(match.group(1))
    return positions_by_symbol.get(symbol, 1)


def to_float_or_none(value):
    if value is None or value == "":
        return None
    return float(value)


def is_exit_superseded(symbol, exit_ts, all_rows):
    """True, wenn nach diesem EXIT noch FILL/STOP_NEW/STOP_UPDATE fuer dasselbe Symbol kam --
    dann war die Position nach dem EXIT nachweislich noch offen, der EXIT also ein Fehlalarm."""
    for other in all_rows:
        if other["symbol"] == symbol and other["event"] in SUPERSEDING_EVENTS and other["time"] > exit_ts:
            return True
    return False


def sync_once():
    if not JOURNAL_CSV.exists():
        print(f"Journal-Datei nicht gefunden: {JOURNAL_CSV}")
        return 0

    sync_state = load_sync_state()
    # Aktuell offene Positionen laut Bot -- zusaetzliches Sicherheitsnetz: ein EXIT ohne Preis wird
    # nie als echter Verkauf gewertet, wenn das Symbol aktuell weiterhin offen ist.
    positions_by_symbol = load_bot_positions()

    with JOURNAL_CSV.open(newline="") as f:
        rows = list(csv.DictReader(f))

    new_rows = rows[sync_state["last_row"]:]
    processed = 0

    for row in new_rows:
        event = row["event"]
        symbol = row["symbol"]
        details = row.get("details", "")
        ts = row.get("time") or None  # echter Zeitpunkt aus der Journal-Zeile, nicht "jetzt"

        if event == "SIGNAL":
            strength = to_float_or_none(row.get("strength"))
            log_signal(symbol, strategie_block="pool", signal_strength=strength, executed=False, timestamp=ts)
            processed += 1

        elif event == "FILL":
            block = resolve_block(details, symbol, positions_by_symbol)
            qty = to_float_or_none(row.get("qty"))
            price = to_float_or_none(row.get("price"))
            log_order(symbol, strategie_block=f"block{block}", action="BUY", price=price, size=qty, timestamp=ts)
            processed += 1

        elif event == "EXIT":
            price = to_float_or_none(row.get("price"))
            qty = to_float_or_none(row.get("qty"))
            if price is None or qty is None:
                # Kein Preis/Menge geloggt (reiner Reconcile-Fund). Zwei Pruefungen, ob das ein
                # echter Verkauf war oder ein Fehlalarm:
                if symbol in positions_by_symbol:
                    # Aktuell weiterhin offen laut Bot -> eindeutig Fehlalarm.
                    continue
                if is_exit_superseded(symbol, ts, rows):
                    # Nach diesem EXIT gab es noch FILL/STOP_NEW/STOP_UPDATE fuer dasselbe Symbol ->
                    # die Position war danach nachweislich noch offen, dieser EXIT war ein Fehlalarm
                    # (zB der verwaiste Reconcile-Bug vom 12.10., der am 13.10. per Stop-Update
                    # ueberschrieben wurde).
                    continue
                # Weder aktuell offen noch durch spaetere Aktivitaet widerlegt -> echter Verkauf,
                # nur Preis/Menge sind unbekannt (vom Bot nicht protokolliert).
            block = resolve_block(details, symbol, positions_by_symbol)
            log_order(symbol, strategie_block=f"block{block}", action="SELL", price=price, size=qty, timestamp=ts)
            processed += 1

        # SKIP, CANCEL, ORDER, STOP_NEW, STOP_UPDATE, UNIVERSE: aktuell nicht im Dashboard gebraucht

    sync_state["last_row"] = len(rows)
    save_sync_state(sync_state)
    return processed


def sync_loop(interval_sec=30):
    print(f"Sync laeuft alle {interval_sec}s. Mit Ctrl+C beenden.")
    while True:
        n = sync_once()
        if n:
            print(f"{n} neue Eintraege synchronisiert.")
        time.sleep(interval_sec)


if __name__ == "__main__":
    init_db()
    if "--loop" in sys.argv:
        sync_loop()
    else:
        n = sync_once()
        print(f"Einmaliger Sync abgeschlossen: {n} neue Eintraege.")
