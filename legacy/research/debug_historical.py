from ib_async import IB, Stock

ib = IB()
ib.connect('127.0.0.1', 7497, clientId=2)

contract = Stock('SPY', 'SMART', 'USD', primaryExchange='ARCA')
qualified = ib.qualifyContracts(contract)
print("Qualified:", qualified)

bars = ib.reqHistoricalData(
    contract,
    endDateTime='',
    durationStr='1 D',
    barSizeSetting='1 min',
    whatToShow='TRADES',
    useRTH=True,
    keepUpToDate=False,
)
print("Anzahl Bars:", len(bars))
if bars:
    print("Letzter Bar:", bars[-1])

ib.disconnect()

