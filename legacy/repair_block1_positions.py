"""
repair_block1_positions.py
---------------------------
Einmaliges Reparaturskript fuer den Vorfall vom 12.10.2026: reconcile() hat beim Reconnect
faelschlich alle 20 Block-1-Stop-Orders storniert, weil TWS direkt nach dem Connect noch keine
Positionsdaten geliefert hatte (Race Condition, inzwischen in live_trade_turtle.py gepatcht).

Was dieses Skript macht:
  1. Verbindet sich separat mit TWS (eigene Client-ID 50, NICHT 11 - der Live-Bot muss dafuer
     NICHT laufen, aber besser erst stoppen, damit sich nichts in die Quere kommt).
  2. Liest die echten Positionen (Menge, Einstandskurs) direkt aus TWS fuer die 20 bekannten
     Block-1-Ticker.
  3. Berechnet pro Ticker das 10-Tage-Tief (wie reconcile() es normalerweise tut) und platziert
     dafuer eine neue GTC-Stop-Verkaufsorder mit orderRef=TURTLE.
  4. Schreibt alle 20 Positionen wieder in live_turtle_state.json.

Aufruf (im trading-bot-Ordner, mit aktiviertem venv):
  venv/bin/python repair_block1_positions.py
"""
import live_trade_turtle as ltt

BLOCK1_TICKERS = ["XP", "PTC", "PCVX", "INTR", "RXO", "STNE", "MTUS", "PAX", "SPCX", "SHOP",
                  "OPCH", "FCEL", "TLN", "CEG", "CIEN", "HTZ", "VST", "APOG", "AAOI", "STRL"]

ltt.client_id = 50          # eigene Client-ID, kollidiert nicht mit dem Live-Bot (11)
ltt.connect()
ltt.state.update(ltt.load_state())   # bestehenden Tageszustand (days, last_stop_update) uebernehmen

held = {ltt.from_ib(p.contract.symbol): p for p in ltt.ib.positions(ltt.account_id()) if p.position != 0}
missing = [t for t in BLOCK1_TICKERS if t not in held]
if missing:
    print(f"WARNUNG: in TWS nicht gefunden (werden uebersprungen): {missing}")

found = [t for t in BLOCK1_TICKERS if t in held]
print(f"Gefunden in TWS: {len(found)} von {len(BLOCK1_TICKERS)} Tickern. Berechne 10-Tage-Tiefs ...")
lows = ltt.recent_lows(found, include_today=False)

for t in found:
    pos = held[t]
    qty = pos.position
    stop = round(lows[t], 2)
    contract = ltt.qualify(t)
    if contract is None:
        print(f"  {t}: Contract nicht gefunden, uebersprungen")
        continue
    order = ltt.StopOrder("SELL", qty, stop, orderRef=ltt.ORDER_REF, tif="GTC")
    ltt.ib.placeOrder(contract, order)
    print(f"  {t}: STOP SELL {qty:.0f} @ {stop} gesendet (Einstand {pos.avgCost:.2f})")
    ltt.state["positions"][t] = dict(status="open", qty=qty, entry_price=pos.avgCost, stop=stop,
                                     signal_price=pos.avgCost,
                                     entry_date=ltt.now_ny().date().isoformat(), block=1)

ltt.save_state()
print(f"\nFertig: {len(found)} Positionen in live_turtle_state.json gespeichert, "
      f"{len(found)} neue Stop-Orders gesendet.")
ltt.ib.sleep(2)
ltt.ib.disconnect()
