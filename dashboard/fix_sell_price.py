"""Traegt einen nachtraeglich ermittelten Verkaufspreis in die Dashboard-DB ein.

Aktualisiert den juengsten SELL-Order-Eintrag eines Symbols, der noch keinen Preis hat
(z.B. weil der Bot ihn beim Exit nicht geloggt hat -- siehe fetch_exit_price.py, um den
echten Preis bei TWS abzufragen).

Aufruf:
    python fix_sell_price.py FCEL 18.42 860
    python fix_sell_price.py STRL 575.10 31
"""

import sqlite3
import sys

from db import DB_PATH


def main():
    if len(sys.argv) != 4:
        print("Aufruf: python fix_sell_price.py SYMBOL PREIS MENGE")
        return

    symbol, price, size = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    row = cur.execute(
        """SELECT id FROM orders WHERE symbol = ? AND action = 'SELL' AND price IS NULL
           ORDER BY timestamp DESC LIMIT 1""",
        (symbol,),
    ).fetchone()

    if row is None:
        print(f"Kein SELL-Eintrag ohne Preis fuer {symbol} gefunden -- nichts zu tun.")
        conn.close()
        return

    cur.execute("UPDATE orders SET price = ?, size = ? WHERE id = ?", (price, size, row[0]))
    conn.commit()
    conn.close()
    print(f"{symbol}: SELL-Eintrag (id={row[0]}) auf Preis {price}, Menge {size} aktualisiert.")


if __name__ == "__main__":
    main()
