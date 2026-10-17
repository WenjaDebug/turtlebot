"""Fragt bei TWS die echten SELL-Ausfuehrungen fuer bestimmte Symbole ab (reqExecutions).
Dient dazu, Verkaufspreise nachzutragen, die reconcile() frueher nicht geloggt hat.

Voraussetzung: TWS laeuft (Paper-Modus, Port 7497, API aktiv). Nutzt eine eigene Client-ID (51),
damit es sich nicht mit dem laufenden Bot (Client-ID 11) ueberschneidet -- kann also auch parallel
zum laufenden Bot ausgefuehrt werden.

Aufruf:
    python fetch_exit_price.py FCEL STRL
"""

import sys

from ib_async import IB, ExecutionFilter

PORT = 7497
CLIENT_ID = 51


def to_ib(ticker):
    return ticker.replace("-", " ")


def from_ib(symbol):
    return symbol.replace(" ", "-")


def main():
    symbols = sys.argv[1:]
    if not symbols:
        print("Bitte mindestens ein Symbol angeben, z.B.: python fetch_exit_price.py FCEL STRL")
        return

    ib = IB()
    try:
        ib.connect("127.0.0.1", PORT, clientId=CLIENT_ID)
    except Exception as exc:
        print(f"Verbindung zu TWS fehlgeschlagen: {exc}")
        print("Ist TWS im Paper-Modus geoeffnet und die API aktiv (Port 7497)?")
        return

    try:
        # Ohne Filter abfragen (kein side=, kein symbol=), damit wir sehen, was TWS ueberhaupt
        # noch an Exekutionen kennt -- manche Paper-Konten verlieren die Historie nach einem Reset.
        fills = ib.reqExecutions(ExecutionFilter())
        print(f"TWS liefert insgesamt {len(fills)} Exekution(en) zurueck.")

        if not fills:
            print("TWS kennt gar keine Exekutionen mehr -- vermutlich hat das Paper-Konto ueber Nacht")
            print("zurueckgesetzt. Dann muessen wir die Preise ueber den Account-Management-Bericht")
            print("(Activity Statement) auf der IBKR-Webseite nachschauen statt ueber die API.")
            return

        print("\nAlle gefundenen Exekutionen:")
        for f in sorted(fills, key=lambda x: x.execution.time):
            symbol = from_ib(f.contract.symbol)
            print(f"  {symbol}: {f.execution.side} {f.execution.shares:.0f} Stk @ {f.execution.price} "
                  f"(Zeit: {f.execution.time})")

        wanted = {to_ib(s) for s in symbols}
        matches = [f for f in fills if f.contract.symbol in wanted]
        if matches:
            print("\nGefiltert auf gesuchte Symbole:")
            for f in sorted(matches, key=lambda x: x.execution.time):
                symbol = from_ib(f.contract.symbol)
                print(f"  {symbol}: {f.execution.shares:.0f} Stk @ {f.execution.price} "
                      f"(Zeit: {f.execution.time})")
        else:
            print(f"\nKeine der gesuchten Symbole ({', '.join(symbols)}) unter den obigen Exekutionen.")
    finally:
        ib.disconnect()


if __name__ == "__main__":
    main()
