"""Entfernt einzelne fehlerhafte Order-Zeilen aus der Dashboard-DB anhand ihrer id.
Aufruf: python remove_order.py 21 22
"""
import sqlite3
import sys

from db import DB_PATH


def main():
    ids = [int(x) for x in sys.argv[1:]]
    if not ids:
        print("Bitte mindestens eine id angeben, z.B.: python remove_order.py 21 22")
        return
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    for i in ids:
        row = cur.execute("SELECT symbol, action, timestamp FROM orders WHERE id = ?", (i,)).fetchone()
        if row is None:
            print(f"id {i}: nicht gefunden.")
            continue
        cur.execute("DELETE FROM orders WHERE id = ?", (i,))
        print(f"id {i} geloescht: {row[0]} {row[1]} @ {row[2]}")
    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
