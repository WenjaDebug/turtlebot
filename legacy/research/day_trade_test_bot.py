"""
day_trade_test_bot.py
----------------------
Zweck: EIN TAGES-TEST, um die autonome Order-Ausführung über IBKR/TWS (Paper
Trading Account) end-to-end zu verifizieren — NICHT um eine ausgefeilte
Strategie zu backtesten. Die Strategie (EMA9/EMA21-Crossover auf 1-Min-Bars)
ist bewusst simpel und reagiert schnell, damit heute mit hoher Wahrscheinlichkeit
ein paar Trades ausgelöst werden und du sehen kannst, ob Verbindung, Order-
Versand, Fills, Logging und Risiko-Limits wie erwartet funktionieren.

Voraussetzungen:
- TWS läuft lokal, eingeloggt im PAPER TRADING Modus (nicht Live!)
- API aktiviert: File > Global Configuration > API > Settings
    - "Enable ActiveX and Socket Clients" angehakt
    - "Read-Only API" AUSgeschaltet (sonst können keine Orders geschickt werden)
    - "Bypass Order Precautions for API Orders" angehakt (sonst blockieren
      Bestätigungs-Popups jede einzelne Order)
    - Socket Port für TWS Paper = 7497 (IB Gateway Paper = 4002)
    - Trusted IP 127.0.0.1 eingetragen
- Python venv aktiv, ib_async installiert (pip install ib_async)

Vor dem ersten Lauf unbedingt die Variablen im Abschnitt "KONFIGURATION"
anpassen und mit einer kleinen Stückzahl testen.
"""

import logging
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
from ib_async import IB, Stock, util

# ---------------------------------------------------------------------------
# KONFIGURATION — hier anpassen
# ---------------------------------------------------------------------------
HOST = "127.0.0.1"
PORT = 7497              # TWS Paper = 7497 | IB Gateway Paper = 4002
CLIENT_ID = 7            # beliebig, aber eindeutig pro gleichzeitiger Verbindung

SYMBOL = "SPY"           # liquide Wahl für den ersten Test; z.B. QQQ, AAPL, NVDA
EXCHANGE = "SMART"
PRIMARY_EXCHANGE = "ARCA"  # Hinweis zur eindeutigen Contract-Identifikation
CURRENCY = "USD"

BAR_SIZE = "1 min"
EMA_FAST = 9
EMA_SLOW = 21

QTY = 1                  # Stückzahl pro Trade — klein halten für den Test
STOP_LOSS_PCT = 0.003    # 0.3% Stop-Loss ab Einstieg
TAKE_PROFIT_PCT = 0.006  # 0.6% Take-Profit ab Einstieg

MAX_TRADES_PER_DAY = 5
DAILY_LOSS_LIMIT = -150.0   # Kill-Switch: Bot stoppt, wenn Tages-PnL darunter fällt (Paper-$)

NY_TZ = ZoneInfo("America/New_York")
NO_NEW_ENTRIES_AFTER = dtime(15, 50)   # ab dann keine neuen Einstiege mehr
FLATTEN_AT = dtime(15, 55)             # alles glattstellen vor Handelsschluss

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("day_trade_test_bot.log")],
)
log = logging.getLogger("daytest")

# ---------------------------------------------------------------------------
# STATE
# ---------------------------------------------------------------------------
state = {
    "position": 0,          # aktuelle Stückzahl (long), 0 = flat
    "trades_today": 0,
    "stopped": False,
}

ib = IB()


def now_ny():
    return datetime.now(NY_TZ)


def flatten_all(contract):
    # Market-Order gegen jede offene Position in diesem Contract schicken
    positions = [p for p in ib.positions() if p.contract.conId == contract.conId]
    for p in positions:
        if p.position != 0:
            from ib_async import MarketOrder
            action = "SELL" if p.position > 0 else "BUY"
            o = MarketOrder(action, abs(p.position))
            ib.placeOrder(contract, o)
            log.info(f"FLATTEN: {action} {abs(p.position)} {contract.symbol}")
    state["position"] = 0


def place_bracket(contract, action, qty, ref_price):
    # Nutzt die eingebaute bracketOrder()-Hilfsfunktion von ib_async, die
    # parentId-Verknüpfung und transmit-Reihenfolge korrekt setzt (das von
    # Hand nachzubauen war der Bug, der vorhin zu drei unverknüpften
    # Einzelorders geführt hat).
    if action == "BUY":
        sl_price = round(ref_price * (1 - STOP_LOSS_PCT), 2)
        tp_price = round(ref_price * (1 + TAKE_PROFIT_PCT), 2)
        close_action = "SELL"
    else:
        sl_price = round(ref_price * (1 + STOP_LOSS_PCT), 2)
        tp_price = round(ref_price * (1 - TAKE_PROFIT_PCT), 2)
        close_action = "BUY"

    bracket = ib.bracketOrder(
        action,
        qty,
        limitPrice=ref_price,
        takeProfitPrice=tp_price,
        stopLossPrice=sl_price,
    )
    # Einstieg soll ein Market-Order sein, nicht Limit (Default von bracketOrder)
    bracket.parent.orderType = "MKT"
    bracket.parent.lmtPrice = 0.0

    trades = [ib.placeOrder(contract, o) for o in bracket]

    log.info(
        f"ORDER: {action} {qty} {contract.symbol} @MKT | "
        f"TP={tp_price} SL={sl_price} (ref={ref_price})"
    )
    return trades


def check_kill_switch(account):
    pnl = ib.pnl(account)
    if pnl and pnl[0].dailyPnL is not None and pnl[0].dailyPnL <= DAILY_LOSS_LIMIT:
        log.warning(f"KILL-SWITCH ausgelöst: Tages-PnL {pnl[0].dailyPnL} <= Limit {DAILY_LOSS_LIMIT}")
        return True
    return False


def fetch_bars(contract):
    # Holt frisch die letzten Bars per einmaliger historischer Abfrage (Polling),
    # statt eine Live-Streaming-Subscription vorauszusetzen, die der Account
    # evtl. nicht hat.
    bars = ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr="2 D",
        barSizeSetting=BAR_SIZE,
        whatToShow="TRADES",
        useRTH=True,
        keepUpToDate=False,
    )
    return util.df(bars)


def check_and_trade(contract, last_seen_bar_time):
    if state["stopped"]:
        return last_seen_bar_time

    df = fetch_bars(contract)
    if df is None or len(df) < EMA_SLOW + 2:
        return last_seen_bar_time

    latest_bar_time = df["date"].iloc[-1]
    if latest_bar_time == last_seen_bar_time:
        return last_seen_bar_time  # noch kein neuer abgeschlossener Bar

    df["ema_fast"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()

    prev_fast, prev_slow = df["ema_fast"].iloc[-2], df["ema_slow"].iloc[-2]
    last_fast, last_slow = df["ema_fast"].iloc[-1], df["ema_slow"].iloc[-1]
    last_close = df["close"].iloc[-1]

    crossed_up = prev_fast <= prev_slow and last_fast > last_slow
    crossed_down = prev_fast >= prev_slow and last_fast < last_slow

    nowtime = now_ny().time()

    # Kill-switch / Zeit-Checks zuerst
    account = ib.managedAccounts()[0]
    if check_kill_switch(account):
        state["stopped"] = True
        flatten_all(contract)
        return

    if nowtime >= FLATTEN_AT:
        if state["position"] != 0:
            flatten_all(contract)
        state["stopped"] = True
        return latest_bar_time

    if state["trades_today"] >= MAX_TRADES_PER_DAY:
        return latest_bar_time  # Tageslimit erreicht, keine neuen Einstiege mehr

    if nowtime >= NO_NEW_ENTRIES_AFTER:
        return latest_bar_time  # zu spät für neue Einstiege

    # Signal-Logik (long-only für den ersten Test)
    if crossed_up and state["position"] == 0:
        place_bracket(contract, "BUY", QTY, last_close)
        state["position"] = QTY
        state["trades_today"] += 1
        log.info(f"Signal: EMA{EMA_FAST} kreuzt EMA{EMA_SLOW} nach oben @ {last_close}")

    elif crossed_down and state["position"] > 0:
        flatten_all(contract)
        state["trades_today"] += 1
        log.info(f"Signal: EMA{EMA_FAST} kreuzt EMA{EMA_SLOW} nach unten @ {last_close} -> Exit")
    else:
        log.info(f"Bar-Update: close={last_close:.2f} EMA9={last_fast:.2f} EMA21={last_slow:.2f}")

    return latest_bar_time


def main():
    log.info("Verbinde mit TWS (Paper Trading erwartet)...")
    ib.connect(HOST, PORT, clientId=CLIENT_ID)

    accounts = ib.managedAccounts()
    log.info(f"Verbunden. Accounts: {accounts}")
    if not any(a.startswith("D") for a in accounts):
        log.warning(
            "ACHTUNG: Kein Account mit 'D'-Präfix gefunden — das ist normalerweise "
            "das Kennzeichen für IBKR-Paper-Accounts. Prüfe, ob TWS wirklich im "
            "Paper-Modus läuft, bevor du weitermachst!"
        )

    account = accounts[0]
    ib.reqPnL(account)

    contract = Stock(SYMBOL, EXCHANGE, CURRENCY, primaryExchange=PRIMARY_EXCHANGE)
    ib.qualifyContracts(contract)

    log.info(f"Bot läuft (Polling-Modus). Symbol={SYMBOL} EMA{EMA_FAST}/{EMA_SLOW} auf {BAR_SIZE}-Bars.")
    log.info(f"Max Trades/Tag={MAX_TRADES_PER_DAY} | Daily-Loss-Limit={DAILY_LOSS_LIMIT}")
    log.info("Stoppe mit Ctrl+C zum sofortigen Beenden (Positionen bleiben dann offen!).")

    last_seen_bar_time = None
    try:
        while not state["stopped"]:
            last_seen_bar_time = check_and_trade(contract, last_seen_bar_time)
            ib.sleep(30)  # alle 30 Sekunden neu abfragen
    except KeyboardInterrupt:
        log.info("Manueller Stopp (Ctrl+C). Verbindung wird getrennt.")
    finally:
        ib.disconnect()


if __name__ == "__main__":
    main()
