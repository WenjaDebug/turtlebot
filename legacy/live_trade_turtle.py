"""
live_trade_turtle.py
--------------------
Turtle-Breakout-Strategie live auf dem IBKR-PAPER-Konto über TWS/ib_async
(Spezifikation: turtle_breakout_live_prompt_v2.md).

Testwoche 05.–11.10.2026 lief mit TEST_MODE (stündliche Intraday-Scans); seit 12.10. Produktionsmodus.

Der echte Lauf nutzt Client-ID 11, jede DRY-RUN-Instanz eine eigene (21–29); Sperrdateien unter
.cache/live/locks/ verhindern, dass zwei Instanzen dieselbe Client-ID verwenden.

Ablauf pro Handelstag (Zeiten US/Eastern):
  08:00  Universum neu aufbauen: S&P 500/400/600 + Nasdaq Composite -> Liquidität -> Market Cap (yfinance)
  09:00  Abgleich mit TWS, Stop-Orders auf das aktuelle 10-Tage-Tief nachziehen (nur nach oben)
  Scan   TEST_MODE: stündlich 10:30–15:30, Kauf sofort per Market-Order
         Produktion: 15:45 ET (kurz vor Börsenschluss, MOC-Annahmeschluss ca. 15:50 ET), Kauf per
         Market-on-Close zum heutigen Schlusskurs; zu knapp vor Schluss verpasst -> automatisch
         Market-on-Open am nächsten Morgen
Jeder Kauf geht mit einer angehängten Stop-Order (GTC) am 10-Tage-Tief raus. Ordertyp, Sizing und
Risiko-Checks: siehe KONFIGURATION.

Signal-Logik: strategies/signals.py (dieselbe Funktion wie im Backtest), Daten: data_sources.py.

Voraussetzungen wie bei day_trade_test_bot.py: TWS im Paper-Modus, API aktiv, Port 7497,
"Read-Only API" aus.

Aufruf:
  python live_trade_turtle.py --dry-run          alles wie echt, aber keine Orders (empfohlen zuerst)
  python live_trade_turtle.py --dry-run --once   Abgleich + ein Scan sofort, dann Ende
  python live_trade_turtle.py                    Paper-Trading, läuft bis Ctrl+C
  python live_trade_turtle.py --report           Zusammenfassung aus Journal und Log
  python live_trade_turtle.py --max-entries 20   Tageslimit neuer Käufe (Standard 10, auch per
                                                 Umgebungsvariable TURTLE_MAX_ENTRIES_PER_DAY=20)
"""

import argparse
import csv
import datetime as dt
import fcntl
import json
import logging
import math
import os
import pathlib
import resource
import subprocess
import threading
import time
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from ib_async import IB, ExecutionFilter, Forex, MarketOrder, Order, Stock, StopOrder

from data_sources import liquid_tickers, load_live_universe, load_market_caps, load_prices
from strategies.portfolio import select_entries
from strategies.signals import turtle_breakout

# ---------------------------------------------------------------------------
# KONFIGURATION
# ---------------------------------------------------------------------------
HOST = "127.0.0.1"
PORT = 7497              # TWS Paper. Das Skript verweigert jeden anderen Port.
CLIENT_ID = 11           # nur für den echten Lauf: nur dieselbe Client-ID darf die eigenen Stop-Orders ändern
DRY_RUN_CLIENT_IDS = range(21, 30)  # DRY-RUN-Instanzen nehmen die erste freie davon, nie die 11
ORDER_REF = "TURTLE"     # Kennzeichnung aller Orders von Block 1
ORDER_REF_B2 = "TURTLE2" # Kennzeichnung aller Orders von Block 2 (so bleibt die Zuordnung auch in TWS sichtbar)
ORDER_REFS = {ORDER_REF, ORDER_REF_B2}

TEST_MODE = False        # True nur für die Testwoche 05.–11.10.2026 (stündliche Intraday-Scans)

# scan_grace_min: so lange nach der Scan-Zeit wird ein verpasster oder fehlgeschlagener Scan noch
# nachgeholt. Nach Börsenschluss reicht die Zeit bis zur Market-on-Open-Order am nächsten Morgen.
PRODUCTION = dict(entry_period=20, volume_factor=1.5, scan_times=["15:45"], scan_grace_min=360)
TEST = dict(entry_period=10, volume_factor=1.3,
            scan_times=["10:30", "11:30", "12:30", "13:30", "14:30", "15:30"], scan_grace_min=45)
PARAMS = TEST if TEST_MODE else PRODUCTION
EXIT_PERIOD = 10                 # Exit: Stop am tiefsten Tief der letzten 10 Handelstage
VOLUME_PERIOD = 20
RANK_BY = "breakout_strength"

POSITION_PCT = 0.015             # 1,5 % des Kontowerts pro neuer Position
MAX_POSITIONS = 20               # Slots von Block 1

# Block 2: zweiter, unabhängiger Slot-Pool aus demselben Scan. Gleiche Parameter wie Block 1 (Signal,
# Positionsgrösse, Stop, Tageslimit), einziger Unterschied: nur Signale mit Breakout-Strength >= 10 %
# (Filter-Backtest backtest_turtle_filters.py, 2016–2026). Block 1 wählt zuerst; Block 2 bekommt keine
# Ticker, die Block 1 hält oder am selben Tag gekauft hat. Abschalten: BLOCK2_ENABLED = False oder
# Umgebungsvariable TURTLE_BLOCK2=0 – bestehende Block-2-Positionen werden dann weiter per Stop verwaltet.
BLOCK2_ENABLED = os.environ.get("TURTLE_BLOCK2", "1") != "0"
BLOCK2_MAX_POSITIONS = 20
BLOCK2_MIN_STRENGTH = 0.10
MIN_MARKET_CAP = 500e6
MIN_DOLLAR_VOLUME = 5e6          # 20-Tage-Ø von Close × Volumen
MIN_PRICE = 1.0                  # technische Untergrenze (Tick-Grösse 0,01 $)

MIN_CUSHION = 0.25               # Überschussliquidität nach der Order >= 25 % des Kontowerts
# Kill-Switch: max. neue Käufe pro Tag. Ohne Codeänderung anpassbar über die Umgebungsvariable
# TURTLE_MAX_ENTRIES_PER_DAY oder die Option --max-entries (Option hat Vorrang).
MAX_NEW_ENTRIES_PER_DAY = int(os.environ.get("TURTLE_MAX_ENTRIES_PER_DAY", "10"))
DAILY_LOSS_LIMIT_PCT = 0.02      # Kill-Switch: ab -2 % Tages-P&L keine neuen Käufe

UNIVERSE_TIME = "08:00"
RECONCILE_TIME = "09:00"
JOB_TIMEOUT_MIN = 45             # Watchdog: Universe-Aufbau und Kursdownload höchstens so lange
HISTORY_DAYS = 75               # Kalendertage Kurshistorie für Donchian-20 und Volumen-Schnitt

# Typischer kumulierter Anteil des Tagesvolumens (US-Aktien, U-Profil mit Schlussauktion), grobe Näherung.
# Damit wird das bisherige Intraday-Volumen auf den ganzen Tag hochgerechnet.
INTRADAY_VOLUME_PROFILE = {"09:30": 0.00, "10:00": 0.12, "10:30": 0.19, "11:00": 0.25, "11:30": 0.30,
                           "12:00": 0.35, "12:30": 0.39, "13:00": 0.43, "13:30": 0.47, "14:00": 0.52,
                           "14:30": 0.57, "15:00": 0.63, "15:30": 0.72, "16:00": 1.00}

NY = ZoneInfo("America/New_York")
SESSION_OPEN, SESSION_CLOSE = dt.time(9, 30), dt.time(16, 0)
MOC_CUTOFF = dt.time(15, 50)      # NYSE/Nasdaq-Annahmeschluss fuer neue Market-on-Close-Orders

LIVE_DIR = pathlib.Path(".cache/live")
LOCK_DIR = LIVE_DIR / "locks"    # eine Sperrdatei pro Client-ID, damit keine zwei Instanzen dieselbe nutzen
STATE_FILE = pathlib.Path("live_turtle_state.json")
JOURNAL_FILE = pathlib.Path("live_turtle_journal.csv")
LOG_FILE = pathlib.Path("live_trade_turtle.log")
JOURNAL_FIELDS = ["time", "event", "symbol", "qty", "price", "stop", "strength", "details"]

# TWS-Meldungen, die nur informieren (Datenfarm-Verbindungen u. Ä.)
INFO_CODES = {1104, 1106, 2104, 2106, 2107, 2108, 2119, 2158, 10349}

# TWS-Meldungen, die anzeigen, dass eine Order-ID bei TWS nicht (mehr) gültig ist (schon storniert,
# ausgeführt oder unbekannt): 103 Doppelt vorhandene Order-ID, 10147/10148 Cancel/Modify auf eine nicht
# (mehr) existierende Order. Reparatur vom 17.10.2026: vorher liess eine einzelne solche "Geister-Order"
# (lokal noch als offen gecacht, bei TWS längst nicht mehr gültig) reconcile() jeden Zyklus erneut daran
# scheitern -- betraf am 17.10. praktisch den gesamten Stop-Order-Bestand. Siehe turtle_trades().
DEAD_ORDER_CODES = {103, 10147, 10148}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE)])
log = logging.getLogger("turtle")
logging.getLogger("ib_async").setLevel(logging.WARNING)
logging.getLogger("ib_async.wrapper").setLevel(logging.CRITICAL)  # TWS-Fehler loggt on_error()
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

FILE_LIMIT_TARGET = 10240        # macOS-Terminals erlauben standardmässig nur 256 offene Dateien
JOB_RETRY_MIN = 10               # fehlgeschlagenen Job nach so vielen Minuten wiederholen ...
JOB_MAX_ATTEMPTS = 3             # ... höchstens so oft pro Tag
NOTIFY_MAX_LINES = 4             # macOS kürzt lange Benachrichtigungen; Rest als "+N weitere"
FILL_WAIT_SEC = 30               # nach einem Scan so lange auf Ausführungen der neuen Käufe warten

ib = IB()
# ib_async abonniert bei TWS-Meldung 1102 ("Verbindung IBKR–TWS wiederhergestellt") jedes Mal die
# Kontozusammenfassung neu, ohne das vorherige Abo zu kündigen. TWS erlaubt nur 2 gleichzeitige Abos,
# danach kommt Fehler 322. Der Bot braucht die Zusammenfassung nicht (Kontowerte kommen aus
# reqAccountUpdates, das TWS über 1102 hinweg weiterliefert), deshalb wird dieser Automatismus abgeschaltet.
ib.errorEvent -= ib._onError
DRY_RUN = False
client_id = CLIENT_ID            # in main() für DRY-RUN-Instanzen ersetzt
_lock_handle = None              # offene Sperrdatei; das Betriebssystem gibt sie beim Prozessende frei
THREADED_DOWNLOADS = True        # wird in raise_file_limit() abgeschaltet, falls das Limit niedrig bleibt


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------
def now_ny():
    return dt.datetime.now(NY)


def at(hhmm, day=None):
    h, m = map(int, hhmm.split(":"))
    return dt.datetime.combine(day or now_ny().date(), dt.time(h, m), tzinfo=NY)


def is_trading_day(day):
    return day.weekday() < 5  # US-Feiertage fallen in die Testwoche nicht


def to_ib(ticker):
    return ticker.replace("-", " ")  # yfinance BRK-B -> IB "BRK B"


def from_ib(symbol):
    return symbol.replace(" ", "-")


def last_sell_fill(symbol):
    """Letzte SELL-Ausfuehrung fuer dieses Symbol aus den TWS-Exekutionen -- fuer Exits, die reconcile()
    erst nachtraeglich erkennt (z. B. Stop ausgeloest, waehrend das Skript offline war), damit der
    echte Verkaufspreis ins Journal kommt statt leer zu bleiben."""
    try:
        fills = ib.reqExecutions(ExecutionFilter(symbol=to_ib(symbol), side="SELL"))
    except Exception as exc:
        log.warning(f"{symbol}: Exekutionen konnten nicht abgefragt werden ({exc})")
        return None, None
    if not fills:
        return None, None
    latest = max(fills, key=lambda f: f.execution.time)
    return latest.execution.price, latest.execution.shares


def journal(event, symbol="", qty="", price="", stop="", strength="", details="", block=None):
    """Eine Zeile ins Journal. Positionsbezogene Ereignisse bekommen 'block=1|2' in details: explizit
    übergeben (Kauf) oder aus dem Zustand der Position gelesen."""
    if block is None and event != "SIGNAL" and symbol in state["positions"]:
        block = block_of(state["positions"][symbol])
    if block is not None:
        details = f"{details}, block={block}" if details else f"block={block}"
    new = not JOURNAL_FILE.exists()
    with JOURNAL_FILE.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
        if new:
            writer.writeheader()
        writer.writerow(dict(time=now_ny().isoformat(timespec="seconds"), event=event, symbol=symbol,
                             qty=qty, price=_round(price), stop=_round(stop),
                             strength=f"{strength:.4f}" if isinstance(strength, float) else strength,
                             details=details))


def _round(value):
    return round(value, 4) if isinstance(value, float) else value


def volume_fraction(now):
    """Bis `now` typischerweise gehandelter Anteil des Tagesvolumens (linear zwischen den Stützstellen)."""
    minutes = [(at(k) - at("09:30")).total_seconds() / 60 for k in INTRADAY_VOLUME_PROFILE]
    elapsed = (now - at("09:30")).total_seconds() / 60
    fraction = float(pd.Series(list(INTRADAY_VOLUME_PROFILE.values()), index=minutes)
                     .reindex(sorted(set(minutes) | {elapsed})).interpolate("index").loc[elapsed])
    return min(max(fraction, 0.05), 1.0)


_new_fills = []  # seit der letzten Benachrichtigung ausgeführte Orders: (Kauf/Verkauf, Stk, Ticker, Preis)
_dead_order_ids = set()  # Order-IDs, die TWS als ungültig gemeldet hat (DEAD_ORDER_CODES, siehe on_error)


def notify(title, message):
    """macOS-Benachrichtigung. Text wird als Argument übergeben, damit Anführungszeichen nichts kaputt machen."""
    try:
        subprocess.run(["osascript", "-e", "on run argv",
                        "-e", "display notification (item 1 of argv) with title (item 2 of argv)",
                        "-e", "end run", message, title],
                       check=True, timeout=10, capture_output=True)
    except Exception as exc:
        log.warning(f"Benachrichtigung fehlgeschlagen: {exc}")


def notify_fills():
    """Eine Benachrichtigung für alle Ausführungen seit der letzten; ohne neue Ausführungen keine."""
    if not _new_fills:
        return
    lines = [f"{side} {qty:.0f} {symbol} @ {price:.2f}" if price else f"{side} {symbol}"
             for side, qty, symbol, price in _new_fills]
    message = " · ".join(lines[:NOTIFY_MAX_LINES])
    if len(lines) > NOTIFY_MAX_LINES:
        message += f" · +{len(lines) - NOTIFY_MAX_LINES} weitere"
    title = f"Turtle: {len(lines)} Order{'s' if len(lines) > 1 else ''} ausgeführt"
    notify(title, message)
    log.info(f"Benachrichtigung: {title}: {' · '.join(lines)}")
    _new_fills.clear()


def raise_file_limit():
    """Dateilimit des Prozesses anheben (yfinance braucht beim parallelen Download ~300–500 Handles).

    Bleibt das Limit trotzdem niedrig, wird ohne Threads geladen (~25 Handles, aber ca. 4x langsamer).
    """
    global THREADED_DOWNLOADS
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = FILE_LIMIT_TARGET if hard == resource.RLIM_INFINITY else min(hard, FILE_LIMIT_TARGET)
    if soft < target:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        except (ValueError, OSError) as exc:
            log.warning(f"Dateilimit konnte nicht angehoben werden ({exc})")
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    THREADED_DOWNLOADS = soft >= 2048
    log.info(f"Dateilimit: {soft} offene Dateien"
             + ("" if THREADED_DOWNLOADS else " -> Kursdaten werden ohne Threads geladen (langsamer)"))


def run_blocking(fn, *args, timeout_min=JOB_TIMEOUT_MIN, **kwargs):
    """Langsame Datenabfragen in einem Thread ausführen und dabei die TWS-Verbindung bedienen.

    Watchdog: Nach `timeout_min` Minuten wird TimeoutError ausgelöst. Python kann einen hängenden Thread
    nicht abbrechen; er läuft als Daemon-Thread im Hintergrund weiter, blockiert aber nichts mehr
    (jeder Aufruf bekommt seinen eigenen Thread) und endet spätestens mit dem Prozess.
    """
    result = {}

    def target():
        try:
            result["value"] = fn(*args, **kwargs)
        except BaseException as exc:
            result["error"] = exc

    worker = threading.Thread(target=target, name=fn.__name__, daemon=True)
    worker.start()
    deadline = time.time() + timeout_min * 60
    while worker.is_alive():
        if time.time() > deadline:
            raise TimeoutError(f"{fn.__name__} nach {timeout_min} min abgebrochen (Watchdog)")
        ib.sleep(2) if ib.isConnected() else time.sleep(2)
    if "error" in result:
        raise result["error"]
    return result["value"]


# ---------------------------------------------------------------------------
# Zustand (Backup zu TWS; TWS bleibt die Wahrheit für Positionen und Orders)
# ---------------------------------------------------------------------------
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"positions": {}, "days": {}}


def save_state():
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(STATE_FILE)


def today_state():
    """Tageszähler: erledigte Jobs, neue Käufe (je Block), heute schon gehandelte Ticker (beide Blöcke)."""
    key = now_ny().date().isoformat()
    day = state["days"].setdefault(key, {"done": [], "entries": 0, "traded": []})
    day.setdefault("entries_b2", 0)
    return day


def block_of(pos):
    """Block einer Position; Positionen ohne Feld (alle Käufe vor Block 2) gehören zu Block 1."""
    return pos.get("block", 1)


def block_positions(block):
    return {s: p for s, p in state["positions"].items() if block_of(p) == block}


def entries_key(block):
    return "entries" if block == 1 else "entries_b2"


state = {"positions": {}, "days": {}}  # wird in main() geladen


# ---------------------------------------------------------------------------
# Konto, Risiko
# ---------------------------------------------------------------------------
def account_id():
    return ib.managedAccounts()[0]


def account_value(tag):
    """Wert in der Basiswährung des Kontos (None, falls nicht verfügbar)."""
    for v in ib.accountValues(account_id()):
        if v.tag == tag and v.currency not in ("", "BASE"):
            return float(v.value), v.currency
    return None, None


_fx_cache = {}


def usd_per_base():
    """Umrechnung Basiswährung -> USD (1.0 bei einem USD-Konto), stündlich aktualisiert.

    Quelle: TWS-Forex-Kurs (IDEALPRO, Midpoint), Fallback yfinance.
    """
    _, base = account_value("NetLiquidation")
    if base == "USD":
        return 1.0
    hour = now_ny().strftime("%Y%m%d%H")
    if _fx_cache.get("hour") == hour:
        return _fx_cache["rate"]
    rate = None
    try:
        bars = ib.reqHistoricalData(Forex(f"USD{base}"), endDateTime="", durationStr="1 D",
                                    barSizeSetting="1 hour", whatToShow="MIDPOINT", useRTH=False)
        if bars:
            rate = 1.0 / bars[-1].close  # USDCHF = CHF pro USD -> USD pro CHF
    except Exception as exc:
        log.warning(f"Forex-Kurs USD{base} aus TWS nicht verfügbar ({exc})")
    if rate is None:
        try:
            rate = 1.0 / float(yf.Ticker(f"USD{base}=X").history(period="5d")["Close"].iloc[-1])
            log.info(f"Wechselkurs aus yfinance: 1 {base} = {rate:.4f} USD")
        except Exception as exc:
            # Weder TWS noch yfinance verfügbar: lieber den letzten bekannten Kurs weiterverwenden als
            # abzustürzen (ein einzelner Netzwerk-Hänger darf nicht den ganzen Lauf beenden).
            if "rate" in _fx_cache:
                log.warning(f"Wechselkurs auch über yfinance nicht verfügbar ({exc}) -> "
                            f"verwende letzten bekannten Kurs ({_fx_cache['rate']:.4f}) weiter")
                return _fx_cache["rate"]
            log.error(f"Wechselkurs USD{base} nirgends verfügbar ({exc}), kein Fallback vorhanden")
            raise
    _fx_cache.update(hour=hour, rate=rate)
    return rate


def kill_switch_active(block=1):
    """Tagesverlust gilt fürs ganze Konto, das Tageslimit neuer Käufe je Block."""
    netliq, _ = account_value("NetLiquidation")
    pnl = ib.pnl(account_id())
    daily = pnl[0].dailyPnL if pnl else None
    if daily is not None and not math.isnan(daily) and netliq and daily <= -DAILY_LOSS_LIMIT_PCT * netliq:
        log.warning(f"KILL-SWITCH: Tages-P&L {daily:,.0f} <= -{DAILY_LOSS_LIMIT_PCT:.0%} des Kontowerts")
        return True
    if today_state()[entries_key(block)] >= MAX_NEW_ENTRIES_PER_DAY:
        log.info(f"Block {block}: Tageslimit von {MAX_NEW_ENTRIES_PER_DAY} neuen Käufen erreicht")
        return True
    return False


def margin_ok(contract, order, price):
    """Prüft per whatIf, ob nach der Order noch genügend Überschussliquidität bleibt."""
    netliq, _ = account_value("NetLiquidation")
    if netliq is None:
        log.warning(f"{contract.symbol}: Kontowert (NetLiquidation) nicht verfügbar -> "
                    "Order vorsichtshalber abgelehnt")
        return False
    try:
        what_if = ib.whatIfOrder(contract, order)
        if not hasattr(what_if, "initMarginAfter"):  # TWS hat die Prüf-Order abgelehnt
            raise ValueError("kein whatIf-Ergebnis von TWS")
        excess_after = float(what_if.equityWithLoanAfter) - float(what_if.initMarginAfter)
        if excess_after > 1e300:  # TWS meldet 'nicht gesetzt' als Double.MAX
            raise ValueError("whatIf ohne Margin-Werte")
    except Exception as exc:
        log.warning(f"whatIf für {contract.symbol} fehlgeschlagen ({exc}); Fallback auf AvailableFunds")
        available, _ = account_value("AvailableFunds")
        excess_after = (available or 0) - order.totalQuantity * price / usd_per_base()
    ok = excess_after >= MIN_CUSHION * netliq
    if not ok:
        log.warning(f"MARGIN: {contract.symbol} abgelehnt, Überschuss danach {excess_after:,.0f} "
                    f"< {MIN_CUSHION:.0%} von {netliq:,.0f}")
    return ok


# ---------------------------------------------------------------------------
# Universum
# ---------------------------------------------------------------------------
def eligible_path(day):
    return LIVE_DIR / f"eligible_{day:%Y%m%d}.json"


def _eligible_tickers(day):
    """Läuft im Hintergrund-Thread (siehe run_blocking): alle langsamen Schritte des Universe-Aufbaus."""
    universe = load_live_universe(day)["ticker"].tolist()
    start = pd.Timestamp(day - dt.timedelta(days=HISTORY_DAYS))
    prices = load_prices(universe, start, pd.Timestamp(day), threads=THREADED_DOWNLOADS)
    liquid = [t for t in liquid_tickers(prices, MIN_DOLLAR_VOLUME, VOLUME_PERIOD)
              if prices[t]["Close"].iloc[-1] >= MIN_PRICE]
    caps = load_market_caps(liquid, day)
    unknown = [t for t in liquid if caps.get(t) is None]
    eligible = sorted(t for t in liquid if (caps.get(t) or 0) > MIN_MARKET_CAP)
    summary = dict(universe=len(universe), with_prices=len(prices), liquid=len(liquid),
                   market_cap_unknown=len(unknown), eligible=len(eligible))
    return eligible, summary


def previous_universe(day):
    """Jüngstes gespeichertes Universum vor `day` als (Datum, Ticker) oder (None, None)."""
    for path in sorted(LIVE_DIR.glob("eligible_*.json"), reverse=True):
        saved = dt.datetime.strptime(path.stem.split("_")[1], "%Y%m%d").date()
        if saved < day:
            return saved, json.loads(path.read_text())["tickers"]
    return None, None


def build_universe():
    """Konstituenten -> Kursdaten -> Liquiditätsfilter -> Market-Cap-Filter (einmal pro Tag).

    Hängt der Aufbau länger als JOB_TIMEOUT_MIN, wird mit dem Universum des letzten Handelstags
    weitergearbeitet, statt den ganzen Tag ausfallen zu lassen.
    """
    day = now_ny().date()
    path = eligible_path(day)
    if path.exists():
        return json.loads(path.read_text())["tickers"]
    log.info(f"Universum wird neu aufgebaut (dauert ca. 20–30 Minuten, Abbruch nach {JOB_TIMEOUT_MIN} min) ...")
    try:
        eligible, summary = run_blocking(_eligible_tickers, day)
    except TimeoutError as exc:
        saved, eligible = previous_universe(day)
        if eligible is None:
            raise  # kein Ersatz vorhanden -> run_job wiederholt den Aufbau
        log.warning(f"{exc} -> verwende Universum vom {saved} ({len(eligible)} Titel)")
        journal("UNIVERSE", details=f"Timeout, Universum vom {saved} verwendet ({len(eligible)} Titel)")
        notify("Turtle: Universe-Aufbau abgebrochen", f"Watchdog nach {JOB_TIMEOUT_MIN} min – "
               f"verwende Universum vom {saved}")
        return eligible  # nicht als heutiges speichern, damit der nächste Lauf neu aufbaut
    log.info(f"Universum: {summary}")
    journal("UNIVERSE", details=json.dumps(summary))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"summary": summary, "tickers": eligible}))
    return eligible


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------
def turtle_trades(symbol=None):
    """Alle offenen TURTLE/TURTLE2-Orders. Orders, deren ID TWS zuletzt als ungültig gemeldet hat
    (DEAD_ORDER_CODES), werden ausgefiltert -- sonst gilt eine solche "Geister-Order" lokal weiter als
    offen, obwohl TWS sie längst nicht mehr kennt, und jeder Versuch, sie zu ändern oder zu stornieren,
    scheitert erneut (Fund vom 17.10.2026, siehe DEAD_ORDER_CODES)."""
    trades = [t for t in ib.openTrades()
              if t.order.orderRef in ORDER_REFS and t.order.orderId not in _dead_order_ids]
    return [t for t in trades if symbol is None or from_ib(t.contract.symbol) == symbol]


def stop_trade(symbol):
    return next((t for t in turtle_trades(symbol)
                 if t.order.action == "SELL" and t.order.orderType == "STP"), None)


def place(contract, order, what):
    if DRY_RUN:
        log.info(f"DRY-RUN: würde senden: {what}")
        return None
    log.info(f"ORDER: {what}")
    return ib.placeOrder(contract, order)


def cancel(trade, why):
    if DRY_RUN:
        log.info(f"DRY-RUN: würde stornieren: {trade.contract.symbol} {trade.order.orderType} ({why})")
        return
    log.info(f"STORNO: {trade.contract.symbol} {trade.order.orderType} #{trade.order.orderId} ({why})")
    ib.cancelOrder(trade.order)
    journal("CANCEL", from_ib(trade.contract.symbol), details=why)


def qualify(ticker):
    contract = Stock(to_ib(ticker), "SMART", "USD")
    try:
        qualified = ib.qualifyContracts(contract)
    except Exception as exc:
        log.warning(f"{ticker}: Contract nicht eindeutig ({exc})")
        return None
    return qualified[0] if qualified else None


def enter(candidate, mode, block=1):
    """Kauf mit angehängter Stop-Order für Block 1 oder 2. `mode`: 'moc' (Market-on-Close, heutiger
    Schluss), 'open' (Market-on-Open, nächster Morgen) oder 'day' (sofort, nur TEST_MODE intraday).
    Gibt True zurück, wenn eine Order gesendet wurde."""
    ticker, price, stop = candidate["ticker"], candidate["price"], round(candidate["stop"], 2)
    if not stop < price:
        log.info(f"{ticker}: übersprungen, Stop {stop} nicht unter Kurs {price}")
        return False
    contract = qualify(ticker)
    if contract is None:
        journal("SKIP", ticker, details="Contract nicht gefunden", block=block)
        return False

    netliq, _ = account_value("NetLiquidation")
    budget_usd = netliq * POSITION_PCT * usd_per_base()
    qty = math.floor(budget_usd / price)
    if qty < 1:
        return False

    ref = ORDER_REF if block == 1 else ORDER_REF_B2
    if mode == "moc":
        # Market-on-Close: eTradeOnly/firmQuoteOnly muessen explizit False sein, sonst lehnt TWS Order()-
        # Objekte ausserhalb der MarketOrder/StopOrder-Hilfskonstruktoren ab (bekannte ib_async-Eigenheit).
        parent = Order(action="BUY", totalQuantity=qty, orderType="MOC", orderRef=ref, tif="DAY",
                      transmit=False, eTradeOnly=False, firmQuoteOnly=False)
        order_desc, tif_label = "MOC (heutiger Schlusskurs)", "MOC"
    else:
        tif = "OPG" if mode == "open" else "DAY"  # 'open': Market-on-Open naechster Morgen; 'day': sofort
        parent = MarketOrder("BUY", qty, orderRef=ref, tif=tif, transmit=False)
        order_desc, tif_label = f"@MKT ({tif})", tif
    # TIF/Orderart explizit setzen, sonst verwirft TWS die Prüf-Order wegen der Order-Voreinstellungen
    # (Fehler 10349)
    if not margin_ok(contract, MarketOrder("BUY", qty, tif="DAY"), price):
        journal("SKIP", ticker, qty, price, stop, candidate["strength"], "Margin-Puffer zu klein", block=block)
        return False
    parent.orderId = ib.client.getReqId()
    stop_order = StopOrder("SELL", qty, stop, orderRef=ref, tif="GTC",
                           parentId=parent.orderId, transmit=True, orderId=ib.client.getReqId())

    what = (f"Block {block}: BUY {qty} {ticker} {order_desc} ≈ {qty * price:,.0f} $ | Stop {stop} "
            f"| Strength {candidate['strength']:+.2%} Vol-Ratio {candidate['volume_ratio']:.2f}")
    place(contract, parent, what)
    place(contract, stop_order, f"  -> Stop SELL {qty} {ticker} @ {stop} GTC")
    journal("ORDER", ticker, qty, price, stop, candidate["strength"],
            f"{tif_label}, vol_ratio={candidate['volume_ratio']:.2f}" + (", DRY-RUN" if DRY_RUN else ""),
            block=block)
    if not DRY_RUN:
        state["positions"][ticker] = dict(status="pending", qty=qty, signal_price=price, stop=stop,
                                          parent_id=parent.orderId, stop_id=stop_order.orderId,
                                          signal_time=now_ny().isoformat(timespec="seconds"), block=block)
    day = today_state()
    day[entries_key(block)] += 1
    day["traded"].append(ticker)
    save_state()
    return True


# ---------------------------------------------------------------------------
# TWS-Events
# ---------------------------------------------------------------------------
def on_order_status(trade):
    if trade.order.orderRef not in ORDER_REFS:
        return
    symbol, status = from_ib(trade.contract.symbol), trade.orderStatus.status
    pos = state["positions"].get(symbol)
    if status == "Filled":
        price, qty = trade.orderStatus.avgFillPrice, trade.orderStatus.filled
        if trade.order.action == "BUY" and pos:
            pos.update(status="open", qty=qty, entry_price=price,
                       entry_date=now_ny().date().isoformat())
            slip = price / pos["signal_price"] - 1
            log.info(f"FILL: BUY {qty:.0f} {symbol} @ {price:.2f} (Signalkurs {pos['signal_price']:.2f}, "
                     f"Abweichung {slip:+.2%})")
            journal("FILL", symbol, qty, price, pos["stop"], details=f"slippage={slip:+.4f}")
            _new_fills.append(("Kauf", qty, symbol, price))
        elif trade.order.action == "SELL":
            entry = pos.get("entry_price") if pos else None
            ret = f"{price / entry - 1:+.2%}" if entry else "unbekannt"
            log.info(f"EXIT: SELL {qty:.0f} {symbol} @ {price:.2f} ({trade.order.orderType}), Rendite {ret}")
            journal("EXIT", symbol, qty, price, details=f"{trade.order.orderType}, return={ret}")
            _new_fills.append(("Verkauf", qty, symbol, price))
            state["positions"].pop(symbol, None)
            today_state()["traded"].append(symbol)
        save_state()
    elif status in ("Cancelled", "ApiCancelled", "Inactive"):
        log.warning(f"Order {status}: {symbol} {trade.order.action} {trade.order.orderType} "
                    f"#{trade.order.orderId} {[e.message for e in trade.log if e.message][-1:]}")
        if trade.order.action == "BUY" and pos and pos["status"] == "pending":
            journal("CANCEL", symbol, details=f"Kauf {status}")
            state["positions"].pop(symbol, None)
            save_state()


def on_error(req_id, code, message, contract):
    level = logging.INFO if code in INFO_CODES else logging.WARNING
    log.log(level, f"TWS {code} (reqId {req_id}): {message}")
    if code in DEAD_ORDER_CODES and req_id > 0:
        _dead_order_ids.add(req_id)


# ---------------------------------------------------------------------------
# Abgleich mit TWS
# ---------------------------------------------------------------------------
def recent_lows(tickers, include_today):
    """Tiefstes Tief der letzten EXIT_PERIOD abgeschlossenen Handelstage je Ticker."""
    day = now_ny().date()
    start = pd.Timestamp(day - dt.timedelta(days=HISTORY_DAYS))
    prices = run_blocking(load_prices, tickers, start, pd.Timestamp(day + dt.timedelta(days=1)),
                          cache=False, threads=THREADED_DOWNLOADS)
    lows = {}
    for t, df in prices.items():
        completed = df if include_today else df[df.index.date < day]
        lows[t] = float(completed["Low"].tail(EXIT_PERIOD).min())
    return lows


def reconcile(update_stops=False):
    """TWS ist die Wahrheit: Zustand an Positionen und offene Orders anpassen, Lücken schliessen."""
    # reqOpenOrders() (nur eigene Client-ID) statt reqAllOpenOrders() (alle Clients, laut ib_async-
    # Doku nicht zuverlässig synchron gehalten) -- plus kurz warten, bis der Snapshot tatsächlich
    # verarbeitet ist, bevor turtle_trades()/openTrades() darauf zugreifen.
    ib.reqOpenOrders()
    ib.sleep(1)
    held = {from_ib(p.contract.symbol): p for p in ib.positions(account_id()) if p.position != 0}
    open_turtle = turtle_trades()

    # Sicherheitsnetz gegen die Race Condition vom 12.10.2026: Der Zustand kennt offene Positionen, aber
    # TWS meldet gerade 0 Positionen zurueck -> vermutlich noch nicht synchronisiert (z. B. kurz nach einem
    # Reconnect), kein echter Massen-Exit. Lieber einen Abgleich aussetzen als faelschlich alle Stop-Orders
    # zu stornieren; der naechste faellige Abgleich greift wieder normal.
    tracked_open = {s for s, p in state["positions"].items() if p["status"] == "open"}
    if tracked_open and not held:
        log.warning(f"Abgleich übersprungen: TWS meldet 0 Positionen, Zustand kennt {len(tracked_open)} "
                    f"offene ({', '.join(sorted(tracked_open))}) -> vermutlich noch nicht synchronisiert")
        return

    # Zustand verloren? Positionen mit TURTLE-Stop übernehmen
    for t in open_turtle:
        symbol = from_ib(t.contract.symbol)
        if symbol not in state["positions"] and symbol in held and t.order.action == "SELL":
            log.warning(f"{symbol}: Position mit TURTLE-Stop gefunden, aber nicht im Zustand -> übernommen")
            state["positions"][symbol] = dict(status="open", qty=held[symbol].position,
                                              entry_price=held[symbol].avgCost, stop=t.order.auxPrice,
                                              signal_price=held[symbol].avgCost,
                                              block=2 if t.order.orderRef == ORDER_REF_B2 else 1)

    for symbol, pos in list(state["positions"].items()):
        try:
            parent_open = any(t.order.action == "BUY" for t in turtle_trades(symbol))
            if pos["status"] == "pending":
                if symbol in held:
                    pos.update(status="open", qty=held[symbol].position, entry_price=held[symbol].avgCost)
                    log.info(f"{symbol}: Kauf wurde ausgeführt (beim Abgleich erkannt)")
                elif not parent_open:
                    log.warning(f"{symbol}: Kauf weder offen noch ausgeführt -> aus Zustand entfernt")
                    journal("CANCEL", symbol, details="Kauf nicht mehr offen")
                    state["positions"].pop(symbol)
                continue
            if symbol not in held:  # Stop ausgelöst oder manuell geschlossen, während das Skript offline war
                price, filled_qty = last_sell_fill(symbol)
                qty = filled_qty if filled_qty is not None else state["positions"][symbol].get("qty", 0)
                info = f"(Verkauf @ {price})" if price is not None else "(Verkaufspreis nicht ermittelbar)"
                log.info(f"{symbol}: Position nicht mehr vorhanden -> geschlossen {info}")
                journal("EXIT", symbol, qty=qty, price=price, details="beim Abgleich erkannt (Stop oder manuell)")
                _new_fills.append(("Verkauf", qty, symbol, price))
                for t in turtle_trades(symbol):
                    try:
                        cancel(t, "Position geschlossen, Restorder verwaist")
                    except Exception:
                        log.exception(f"{symbol}: Stornieren der Restorder fehlgeschlagen")
                state["positions"].pop(symbol)
        except Exception:
            # Wie bei der Stop-Update-Schleife unten: ein Problem bei EINEM Symbol darf den Abgleich
            # für die übrigen Positionen nicht blockieren.
            log.exception(f"{symbol}: Positions-Abgleich fehlgeschlagen, weiter mit den übrigen Positionen")

    # Verwaiste TURTLE-Verkaufsorders ohne Position stornieren (sonst droht ein Short)
    for t in turtle_trades():
        symbol = from_ib(t.contract.symbol)
        if t.order.action == "SELL" and symbol not in held and symbol not in state["positions"]:
            try:
                cancel(t, "verwaiste Stop-Order ohne Position")
            except Exception:
                log.exception(f"{symbol}: Stornieren der verwaisten Stop-Order fehlgeschlagen")

    open_positions = [s for s, p in state["positions"].items() if p["status"] == "open"]
    needs_stop = [s for s in open_positions if stop_trade(s) is None]
    if open_positions and (update_stops or needs_stop):
        lows = recent_lows(open_positions, include_today=False)
        for symbol in open_positions:
            # Jedes Symbol einzeln absichern: Ein Problem bei EINER Position (z. B. eine von TWS
            # abgelehnte Order) darf nie den Stop-Abgleich für alle ANDEREN Positionen blockieren
            # (Vorfall Okt. 2026: eine einzelne kaputte Order hat reconcile() komplett abgebrochen und
            # damit die Stop-Pflege für den ganzen Rest des Portfolios lahmgelegt).
            try:
                pos, trade = state["positions"][symbol], stop_trade(symbol)
                qty = abs(held[symbol].position)
                new_stop = round(lows.get(symbol, pos["stop"]), 2)
                ref = ORDER_REF_B2 if block_of(pos) == 2 else ORDER_REF
                if trade is None:
                    contract = qualify(symbol)
                    if contract:
                        stop = max(new_stop, pos.get("stop", 0))
                        place(contract, StopOrder("SELL", qty, stop, orderRef=ref, tif="GTC"),
                              f"fehlende Stop-Order ergänzt: SELL {qty:.0f} {symbol} @ {stop}")
                        pos["stop"] = stop
                        journal("STOP_NEW", symbol, qty, stop=stop)
                    continue
                changed = []
                if update_stops and new_stop > trade.order.auxPrice:
                    changed.append(f"Stop {trade.order.auxPrice} -> {new_stop}")
                    trade.order.auxPrice = new_stop
                if trade.order.totalQuantity != qty:
                    changed.append(f"Menge {trade.order.totalQuantity:.0f} -> {qty:.0f}")
                    trade.order.totalQuantity = qty
                if changed:
                    order_id = trade.order.orderId
                    place(trade.contract, trade.order, f"Stop {symbol} angepasst: {', '.join(changed)}")
                    ib.sleep(1)  # kurz auf eine mögliche TWS-Ablehnung warten (on_error setzt _dead_order_ids)
                    if order_id in _dead_order_ids:
                        # TWS hat die Modify-Anfrage abgelehnt (Order dort nicht mehr gültig, z. B. Error
                        # 103) -- die Position NIE ungeschützt lassen: sofort eine frische Ersatz-Stop-
                        # Order platzieren, statt es beim nächsten Zyklus wieder an derselben toten ID
                        # scheitern zu lassen (Fund vom 17.10.2026).
                        log.warning(f"{symbol}: Stop-Update (Order #{order_id}) von TWS abgelehnt -> "
                                    "ersetze durch neue Stop-Order")
                        contract = qualify(symbol)
                        if contract:
                            stop = trade.order.auxPrice
                            place(contract, StopOrder("SELL", qty, stop, orderRef=ref, tif="GTC"),
                                  f"Ersatz-Stop nach abgelehntem Update: SELL {qty:.0f} {symbol} @ {stop}")
                            pos["stop"] = stop
                            journal("STOP_NEW", symbol, qty, stop=stop,
                                    details=f"Ersatz für von TWS abgelehnte Order #{order_id}")
                    else:
                        pos["stop"] = trade.order.auxPrice
                        journal("STOP_UPDATE", symbol, qty, stop=trade.order.auxPrice,
                                details="; ".join(changed))
            except Exception:
                log.exception(f"{symbol}: Stop-Abgleich fehlgeschlagen, weiter mit den übrigen Positionen")
            save_state()  # nach jedem Symbol sichern, damit bei einem späteren Fehler nichts verloren geht

    others = [s for s in held if s not in state["positions"]]
    if others:
        log.info(f"Nicht von dieser Strategie (werden ignoriert): {', '.join(sorted(others))}")
    n_open = sum(p["status"] == "open" for p in state["positions"].values())
    n_pending = sum(p["status"] == "pending" for p in state["positions"].values())
    per_block = " / ".join(f"Block {b}: {len(block_positions(b))}" for b in (1, 2))
    log.info(f"Abgleich: {n_open} offene, {n_pending} ausstehende Positionen ({per_block}), "
             f"{len(turtle_trades())} offene TURTLE-Orders")
    if update_stops:
        state["last_stop_update"] = now_ny().isoformat(timespec="seconds")
    save_state()


def last_due_stop_update():
    """Zeitpunkt des letzten fälligen 09:00-Stop-Abgleichs (heute oder letzter Handelstag davor)."""
    day = now_ny().date()
    if not (is_trading_day(day) and now_ny() >= at(RECONCILE_TIME)):
        day -= dt.timedelta(days=1)
        while not is_trading_day(day):
            day -= dt.timedelta(days=1)
    return at(RECONCILE_TIME, day)


def catch_up_stop_update():
    """Stop-Abgleich sofort nachholen, falls der letzte fällige 09:00-Lauf verpasst wurde
    (Absturz, Rechner aus, hängender Job). Stops werden dabei wie immer nur nach oben gezogen."""
    due = last_due_stop_update()
    last = state.get("last_stop_update")
    if last and dt.datetime.fromisoformat(last) >= due:
        return
    log.info(f"Stop-Abgleich vom {due:%d.%m. %H:%M} ET wurde verpasst (letzter: {last or 'unbekannt'}) "
             "-> wird jetzt nachgeholt")
    reconcile(update_stops=True)
    if due.date() == now_ny().date():
        mark_done("reconcile")


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------
def allocate(candidates, mode, block, max_slots):
    """Freie Slots eines Blocks mit den stärksten Kandidaten füllen. Ausgeschlossen sind Ticker, die einer
    der beiden Blöcke hält oder die heute schon gehandelt wurden (auch Käufe von Block 1 in diesem Scan)."""
    day = today_state()
    taken = set(state["positions"]) | set(day["traded"])
    free = max_slots - len(block_positions(block))
    pool = {c["ticker"]: c for c in candidates if c["ticker"] not in taken}
    chosen = select_entries({t: c["strength"] for t, c in pool.items()}, free)
    log.info(f"Block {block}: freie Slots {free}, neue Kandidaten {len(pool)}, ausgewählt: {chosen or '-'}")
    for ticker in chosen:
        if kill_switch_active(block):
            break
        enter(pool[ticker], mode, block)


def scan(eligible):
    now = now_ny()
    in_session = SESSION_OPEN <= now.time() < SESSION_CLOSE
    if not in_session:
        mode = "open"             # nach Schluss: Kauf per Market-on-Open am naechsten Morgen
    elif TEST_MODE:
        mode = "day"               # Testwoche: sofortiger Intraday-Kauf
    elif now.time() < MOC_CUTOFF:
        mode = "moc"               # Produktion, vor Schluss: Kauf per Market-on-Close zum heutigen Schluss
    else:
        log.warning(f"Scan: zu knapp vor Börsenschluss ({now:%H:%M} ET) für eine neue MOC-Order "
                    f"(Annahmeschluss {MOC_CUTOFF:%H:%M} ET) -> Kauf stattdessen per Market-on-Open morgen")
        mode = "open"
    log.info(f"Scan über {len(eligible)} Titel (Lookback {PARAMS['entry_period']}, "
             f"Volumen > {PARAMS['volume_factor']}x, "
             f"{'MOC vor Schluss' if mode == 'moc' else 'intraday' if mode == 'day' else 'Kauf naechster Morgen'}) ...")

    start = pd.Timestamp(now.date() - dt.timedelta(days=HISTORY_DAYS))
    prices = run_blocking(load_prices, eligible, start, pd.Timestamp(now.date() + dt.timedelta(days=1)),
                          cache=False, threads=THREADED_DOWNLOADS)
    if not prices:
        log.warning("Scan: keine Kursdaten erhalten")
        return
    ref_date = pd.Series([df.index[-1].date() for df in prices.values()]).mode()[0]
    if in_session and ref_date != now.date():
        log.warning(f"Scan: Kursdaten enden am {ref_date}, nicht heute -> kein Scan")
        return
    session_fraction = volume_fraction(now) if in_session else 1.0

    candidates, stale = [], 0
    for ticker, df in prices.items():
        if df.index[-1].date() != ref_date:
            stale += 1
            continue
        df = df.astype({"Volume": float})  # Kopie; float, damit die Hochrechnung nicht an int64 scheitert
        df.iloc[-1, df.columns.get_loc("Volume")] /= session_fraction  # auf ganzen Tag hochgerechnet
        sig = turtle_breakout(df, entry_period=PARAMS["entry_period"], exit_period=EXIT_PERIOD,
                              volume_period=VOLUME_PERIOD, volume_factor=PARAMS["volume_factor"],
                              rank_by=RANK_BY).iloc[-1]
        if sig["entry"]:
            # 'moc'/'open': heutiges Tief ist (fast) final -> inklusive Stop; 'day': laufender Tag noch
            # nicht abgeschlossen -> Donchian-Tief ohne den heutigen Tag
            stop = df["Low"].tail(EXIT_PERIOD).min() if mode != "day" else sig["donchian_low"]
            candidates.append(dict(ticker=ticker, price=float(df["Close"].iloc[-1]), stop=float(stop),
                                   strength=float(sig["breakout_strength"]),
                                   volume_ratio=float(sig["volume_ratio"])))
    log.info(f"Scan ({ref_date}): {len(prices)} Titel mit Daten, {stale} ohne aktuellen Kurs, "
             f"{len(candidates)} Signale")
    for c in sorted(candidates, key=lambda c: -c["strength"]):
        journal("SIGNAL", c["ticker"], price=c["price"], stop=c["stop"], strength=c["strength"],
                details=f"vol_ratio={c['volume_ratio']:.2f}")

    # Vor der Slot-Zuteilung abgleichen: Ausgestoppte oder manuell geschlossene Positionen seit dem
    # letzten Abgleich werden hier erkannt, damit frei gewordene Slots noch in diesem Scan neu belegt
    # werden koennen. Sonst rechnet allocate() mit veraltetem Zustand und laesst frische Slots bis zum
    # naechsten Scan liegen (Vorfall 13.10.2026: FCEL/STRL ausgestoppt, Block 1 sah die freien Slots
    # trotz 8 Signalen nicht mehr, weil reconcile() bisher erst NACH der Zuteilung lief).
    reconcile()

    allocate(candidates, mode, block=1, max_slots=MAX_POSITIONS)
    if BLOCK2_ENABLED:
        strong = [c for c in candidates if c["strength"] >= BLOCK2_MIN_STRENGTH]
        allocate(strong, mode, block=2, max_slots=BLOCK2_MAX_POSITIONS)

    # Intraday-Käufe werden meist in Sekunden ausgeführt; kurz warten, damit sie in die Benachrichtigung kommen
    deadline = time.time() + FILL_WAIT_SEC
    while (mode == "day" and time.time() < deadline
           and any(p["status"] == "pending" for p in state["positions"].values())):
        ib.sleep(1)
    reconcile()
    notify_fills()


# ---------------------------------------------------------------------------
# Hauptschleife
# ---------------------------------------------------------------------------
def acquire_client_id(dry_run):
    """Sperrdatei pro Client-ID: Der echte Lauf belegt CLIENT_ID (11), jede DRY-RUN-Instanz die erste
    freie aus DRY_RUN_CLIENT_IDS. So kann ein Testlauf nie die Verbindung des echten Laufs übernehmen."""
    global _lock_handle
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    for cid in (DRY_RUN_CLIENT_IDS if dry_run else [CLIENT_ID]):
        handle = (LOCK_DIR / f"client_{cid}.lock").open("w")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            continue
        handle.write(f"pid={os.getpid()} dry_run={dry_run} since={now_ny().isoformat(timespec='seconds')}\n")
        handle.flush()
        _lock_handle = handle
        return cid
    if dry_run:
        raise SystemExit(f"ABBRUCH: alle DRY-RUN-Client-IDs {DRY_RUN_CLIENT_IDS.start}–"
                         f"{DRY_RUN_CLIENT_IDS.stop - 1} sind belegt.")
    raise SystemExit(f"ABBRUCH: Client-ID {CLIENT_ID} ist schon von einem laufenden Live-Bot belegt "
                     f"(Sperrdatei {LOCK_DIR / f'client_{CLIENT_ID}.lock'}).")


def connect():
    ib.connect(HOST, PORT, clientId=client_id, timeout=20)
    accounts = ib.managedAccounts()
    if PORT != 7497 or not accounts or not all(a.startswith("D") for a in accounts):
        ib.disconnect()
        raise SystemExit(f"ABBRUCH: kein Paper-Konto (Port {PORT}, Accounts {accounts}). "
                         "Dieses Skript handelt nur auf dem Paper-Konto.")
    ib.reqPnL(accounts[0])
    # Positionsdaten explizit anfordern und auf den Sync warten: ib.positions() liefert direkt nach dem
    # (Re-)Connect oft noch eine leere/unvollstaendige Liste, weil TWS den Positions-Snapshot erst mit
    # Verzoegerung ueber die API schickt. reconcile() wuerde das sonst als "alle Positionen geschlossen"
    # lesen und faelschlich alle Stop-Orders stornieren (Vorfall 12.10.2026: 20 Positionen, Stops weg).
    ib.reqPositions()
    for _ in range(10):
        ib.sleep(1)
        if ib.positions(accounts[0]):
            break
    else:
        log.info("Keine Positionen nach Sync-Wartezeit (vermutlich tatsaechlich 0 Positionen im Konto)")
    netliq, base = account_value("NetLiquidation")
    fx = usd_per_base()
    log.info(f"Verbunden mit Paper-Konto {accounts[0]} (Client-ID {client_id}): Kontowert {netliq:,.0f} {base} "
             f"(≈ {netliq * fx:,.0f} USD, 1 {base} = {fx:.4f} USD), "
             f"Positionsgrösse ≈ {netliq * fx * POSITION_PCT:,.0f} USD"
             + (" | DRY-RUN: es werden keine Orders gesendet" if DRY_RUN else ""))


def job_due(name, when, grace_min=None):
    now, day = now_ny(), today_state()
    if name in day["done"] or now < when:
        return False
    if grace_min is not None and now > when + dt.timedelta(minutes=grace_min):
        day["done"].append(name)  # verpasst (z. B. Skript lief nicht) -> nicht mehr nachholen
        log.info(f"Job {name} verpasst, wird übersprungen")
        return False
    return True


def mark_done(name):
    today_state()["done"].append(name)
    save_state()


_job_failures = {}  # Job -> (Fehlversuche, frühester nächster Versuch)


def run_job(name, fn):
    """Job ausführen; ein Fehler beendet nicht das Skript, sondern löst Benachrichtigung + Wiederholung aus."""
    attempts, next_try = _job_failures.get(name, (0, None))
    if next_try and now_ny() < next_try:
        return
    try:
        fn()
    except Exception as exc:
        attempts += 1
        log.exception(f"Job {name} fehlgeschlagen (Versuch {attempts}/{JOB_MAX_ATTEMPTS})")
        reason = f"{type(exc).__name__}: {exc}"[:150]
        if attempts >= JOB_MAX_ATTEMPTS:
            mark_done(name)  # für heute aufgeben
            _job_failures.pop(name, None)
            notify(f"Turtle: Job {name} endgültig fehlgeschlagen", f"{reason} – heute kein weiterer Versuch")
        else:
            _job_failures[name] = (attempts, now_ny() + dt.timedelta(minutes=JOB_RETRY_MIN))
            notify(f"Turtle: Fehler in Job {name}", f"{reason} – neuer Versuch in {JOB_RETRY_MIN} min")
        return
    _job_failures.pop(name, None)
    mark_done(name)


def run(once):
    connect()
    ib.orderStatusEvent += on_order_status
    ib.errorEvent += on_error
    ib.disconnectedEvent += lambda: log.warning("TWS-Verbindung getrennt")
    mode = "TEST_MODE" if TEST_MODE else "Produktion"
    log.info(f"{mode}: Lookback {PARAMS['entry_period']}, Volumen > {PARAMS['volume_factor']}x, "
             f"Scans {PARAMS['scan_times']} ET, {POSITION_PCT:.1%} pro Position, max. {MAX_POSITIONS} Slots, "
             f"max. {MAX_NEW_ENTRIES_PER_DAY} neue Käufe pro Tag und Block")
    log.info(f"Block 2: " + (f"aktiv, max. {BLOCK2_MAX_POSITIONS} Slots, nur Breakout-Strength >= "
                             f"{BLOCK2_MIN_STRENGTH:.0%}, Orders mit Ref {ORDER_REF_B2}" if BLOCK2_ENABLED
                             else "deaktiviert (bestehende Positionen werden weiter per Stop verwaltet)"))
    reconcile()
    catch_up_stop_update()

    if once:
        scan(build_universe())
        return

    eligible = None

    def universe_job():
        nonlocal eligible
        eligible = build_universe()

    def scan_job():
        nonlocal eligible
        eligible = eligible or build_universe()
        scan(eligible)

    last_alert = None
    # Ohne diese Eskalation blieb ein Verbindungsausfall komplett unbemerkt: die Hauptschleife loggte
    # nur eine WARNING pro Minute und benachrichtigte NIE (Vorfall 10./17.10.2026: TWS über Nacht/
    # Wochenende getrennt, 13–14 Std. lang ohne jede Meldung an macOS). Jetzt: erste Benachrichtigung
    # nach DISCONNECT_ALERT_AFTER_MIN, danach alle DISCONNECT_ALERT_REPEAT_MIN erneut, solange getrennt.
    disconnected_since = None
    last_disconnect_alert = None
    DISCONNECT_ALERT_AFTER_MIN = 5
    DISCONNECT_ALERT_REPEAT_MIN = 30
    while True:
        try:
            if not ib.isConnected():
                if disconnected_since is None:
                    disconnected_since = now_ny()
                down_min = (now_ny() - disconnected_since).total_seconds() / 60
                log.warning(f"Verbindung zu TWS verloren (seit {down_min:.0f} min), neuer Versuch in 60 s ...")
                if (down_min >= DISCONNECT_ALERT_AFTER_MIN
                        and (last_disconnect_alert is None
                             or time.time() - last_disconnect_alert > DISCONNECT_ALERT_REPEAT_MIN * 60)):
                    notify("Turtle: Keine Verbindung zu TWS",
                           f"Seit {down_min:.0f} min getrennt -- bitte TWS prüfen/neu einloggen. "
                           "Offene Positionen bleiben durch ihre Stop-Orders bei TWS geschützt.")
                    last_disconnect_alert = time.time()
                time.sleep(60)
                try:
                    connect()
                    reconcile()
                    catch_up_stop_update()
                    if disconnected_since is not None:
                        log.info(f"Verbindung wiederhergestellt nach {down_min:.0f} min")
                        if down_min >= DISCONNECT_ALERT_AFTER_MIN:
                            notify("Turtle: Verbindung wiederhergestellt",
                                   f"Nach {down_min:.0f} min wieder verbunden")
                    disconnected_since = None
                    last_disconnect_alert = None
                except Exception as exc:
                    log.warning(f"Verbindung fehlgeschlagen: {exc}")
                continue

            now = now_ny()
            if is_trading_day(now.date()):
                if job_due("universe", at(UNIVERSE_TIME)) and now.time() < SESSION_CLOSE:
                    run_job("universe", universe_job)
                if job_due("reconcile", at(RECONCILE_TIME)):
                    run_job("reconcile", lambda: reconcile(update_stops=True))
                for hhmm in PARAMS["scan_times"]:
                    if job_due(f"scan {hhmm}", at(hhmm), PARAMS["scan_grace_min"]):
                        run_job(f"scan {hhmm}", scan_job)
            ib.sleep(30)
        except Exception as exc:
            # z. B. beim nächtlichen TWS-Neustart: nicht beenden, sondern loggen und weitermachen
            log.exception("Fehler in der Hauptschleife – weiter in 60 s")
            if last_alert is None or time.time() - last_alert > 3600:
                notify("Turtle: Fehler in der Hauptschleife",
                       f"{type(exc).__name__}: {exc}"[:150] + " – Skript läuft weiter")
                last_alert = time.time()
            time.sleep(60)


def report():
    if not JOURNAL_FILE.exists():
        raise SystemExit("Noch kein Journal vorhanden.")
    j = pd.read_csv(JOURNAL_FILE, parse_dates=["time"])
    j["day"] = j["time"].astype(str).str[:10]
    print("===== Ereignisse pro Tag =====")
    print(j.pivot_table(index="day", columns="event", values="symbol", aggfunc="count", fill_value=0)
          .to_string())
    fills = j[j["event"] == "FILL"]
    if len(fills):
        slip = fills["details"].str.extract(r"slippage=([-+\d.]+)")[0].astype(float)
        print(f"\nFills: {len(fills)}, Ø Abweichung Fill vs. Signalkurs {slip.mean():+.2%} "
              f"(min {slip.min():+.2%}, max {slip.max():+.2%})")
    exits = j[j["event"] == "EXIT"]
    if len(exits):
        print("\n===== Exits =====")
        print(exits[["time", "symbol", "qty", "price", "details"]].to_string(index=False))
    if LOG_FILE.exists():
        lines = LOG_FILE.read_text().splitlines()
        warnings = [l for l in lines if " WARNING " in l or " ERROR " in l]
        print(f"\nLog: {len(warnings)} Warnungen/Fehler" + (", letzte 10:" if warnings else ""))
        for line in warnings[-10:]:
            print("  " + line)
    positions = load_state()["positions"]
    print(f"\n===== Aktueller Zustand: {len(positions)} Positionen =====")
    for symbol, p in sorted(positions.items(), key=lambda kv: (block_of(kv[1]), kv[0])):
        print(f"  Block {block_of(p)}  {symbol:6s} {p['status']:8s} {p['qty']:>6.0f} Stk  "
              f"Einstieg {p.get('entry_price', '-')}  Stop {p['stop']}")


def main():
    global DRY_RUN, STATE_FILE, JOURNAL_FILE, MAX_NEW_ENTRIES_PER_DAY, client_id
    parser = argparse.ArgumentParser(description="Turtle-Breakout live auf dem IBKR-Paper-Konto")
    parser.add_argument("--dry-run", action="store_true", help="keine Orders senden, nur loggen")
    parser.add_argument("--once", action="store_true", help="Abgleich + ein Scan sofort, dann Ende")
    parser.add_argument("--report", action="store_true", help="Zusammenfassung aus Journal und Log")
    parser.add_argument("--max-entries", type=int, default=None,
                        help="max. neue Käufe pro Tag (sonst TURTLE_MAX_ENTRIES_PER_DAY bzw. 10)")
    args = parser.parse_args()
    if args.report:
        report()
        return
    DRY_RUN = args.dry_run
    if args.max_entries is not None:
        MAX_NEW_ENTRIES_PER_DAY = args.max_entries
    if DRY_RUN:  # eigener Zustand und eigenes Journal, damit der Testlauf den echten Betrieb nicht beeinflusst
        STATE_FILE = STATE_FILE.with_name("live_turtle_state_dryrun.json")
        JOURNAL_FILE = JOURNAL_FILE.with_name("live_turtle_journal_dryrun.csv")
    client_id = acquire_client_id(DRY_RUN)
    state.update(load_state())
    raise_file_limit()
    try:
        run(args.once)
    except KeyboardInterrupt:
        log.info("Manueller Stopp (Ctrl+C). Offene Positionen bleiben durch ihre Stop-Orders geschützt.")
    except (Exception, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code in (None, 0):
            raise
        # Traceback ins Log (bisher nur im Terminal) und sofort Bescheid geben
        log.exception("ABSTURZ: Skript beendet")
        notify("Turtle: Skript abgestürzt",
               f"{type(exc).__name__}: {exc}"[:150] + " – Stops in TWS bleiben aktiv, bitte neu starten")
        raise SystemExit(1)
    finally:
        save_state()
        if ib.isConnected():
            ib.disconnect()


if __name__ == "__main__":
    main()
