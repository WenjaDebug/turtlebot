# db.py
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = Path(__file__).parent / "trading_dashboard.db"
NY = ZoneInfo("America/New_York")  # der Bot zeitstempelt alles in NY-Zeit -- "heute" muss dieselbe sein


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            strategie_block TEXT NOT NULL,
            action TEXT NOT NULL,       -- z.B. 'BUY' / 'SELL'
            price REAL,
            size REAL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            strategie_block TEXT NOT NULL,
            signal_strength REAL,
            executed INTEGER NOT NULL DEFAULT 0  -- 0 = nicht ausgeführt, 1 = ausgeführt
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_snapshot (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            date TEXT NOT NULL,
            strategie_block TEXT NOT NULL,
            equity REAL,
            open_positions INTEGER,
            pnl REAL
        )
    """)

    # Migration: alte Tabellenversion (ein Eintrag pro Tag, kein Zeitstempel) auf mehrere
    # Eintraege pro Tag umstellen, ohne orders/signals anzufassen.
    cols = [r[1] for r in cur.execute("PRAGMA table_info(daily_snapshot)").fetchall()]
    if "timestamp" not in cols:
        cur.execute("ALTER TABLE daily_snapshot RENAME TO daily_snapshot_old")
        cur.execute("""
            CREATE TABLE daily_snapshot (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                date TEXT NOT NULL,
                strategie_block TEXT NOT NULL,
                equity REAL,
                open_positions INTEGER,
                pnl REAL
            )
        """)
        cur.execute("""
            INSERT INTO daily_snapshot (timestamp, date, strategie_block, equity, open_positions, pnl)
            SELECT date || 'T12:00:00', date, strategie_block, equity, open_positions, pnl
            FROM daily_snapshot_old
        """)
        cur.execute("DROP TABLE daily_snapshot_old")

    conn.commit()
    conn.close()


def log_order(symbol: str, strategie_block: str, action: str, price: float | None, size: float | None,
              timestamp: str | None = None):
    conn = get_connection()
    conn.execute(
        """INSERT INTO orders (timestamp, symbol, strategie_block, action, price, size)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (timestamp or datetime.now().isoformat(timespec="seconds"), symbol, strategie_block, action, price, size),
    )
    conn.commit()
    conn.close()


def log_signal(symbol: str, strategie_block: str, signal_strength: float | None, executed: bool = False,
               timestamp: str | None = None):
    conn = get_connection()
    conn.execute(
        """INSERT INTO signals (timestamp, symbol, strategie_block, signal_strength, executed)
           VALUES (?, ?, ?, ?, ?)""",
        (timestamp or datetime.now().isoformat(timespec="seconds"), symbol, strategie_block, signal_strength, int(executed)),
    )
    conn.commit()
    conn.close()


def log_snapshot(date: str, strategie_block: str, equity: float, open_positions: int, pnl: float,
                  timestamp: str | None = None):
    conn = get_connection()
    conn.execute(
        """INSERT INTO daily_snapshot (timestamp, date, strategie_block, equity, open_positions, pnl)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (timestamp or datetime.now().isoformat(timespec="seconds"), date, strategie_block, equity,
         open_positions, pnl),
    )
    conn.commit()
    conn.close()


def get_open_positions(strategie_block: str | None = None):
    conn = get_connection()
    if strategie_block:
        rows = conn.execute(
            "SELECT * FROM orders WHERE strategie_block = ? ORDER BY timestamp DESC",
            (strategie_block,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM orders ORDER BY timestamp DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_signals_today(strategie_block: str | None = None):
    # NY-Datum, nicht lokales CH-Datum: die Journal-Zeitstempel stehen in NY-Zeit (now_ny() im Bot), die
    # Tagesgrenze liegt also 6-7 Std. versetzt zur lokalen Zeit -- vorher zeigte das Dashboard z.B. frueh
    # morgens (CH) faelschlich "keine Signale heute" oder die Signale vom falschen Tag (Fix 17.10.2026).
    today = datetime.now(NY).date().isoformat()
    conn = get_connection()
    query = "SELECT * FROM signals WHERE date(timestamp) = ?"
    params = [today]
    if strategie_block:
        query += " AND strategie_block = ?"
        params.append(strategie_block)
    rows = conn.execute(query + " ORDER BY timestamp DESC", params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_equity_curve(strategie_block: str):
    conn = get_connection()
    rows = conn.execute(
        "SELECT timestamp, date, equity, pnl FROM daily_snapshot WHERE strategie_block = ? ORDER BY timestamp ASC",
        (strategie_block,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# Migration/Schema beim Import automatisch sicherstellen, egal ob db.py direkt
# ausgefuehrt oder nur importiert wird (z.B. von backend.py oder app.py).
init_db()

if __name__ == "__main__":
    print(f"DB initialisiert unter: {DB_PATH}")
