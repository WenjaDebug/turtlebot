from ib_async import IB, Stock

ib = IB()
ib.connect('127.0.0.1', 7497, clientId=3)
ib.reqMarketDataType(3)  # 3 = verzögerte Daten

contract = Stock('SPY', 'SMART', 'USD', primaryExchange='ARCA')
ib.qualifyContracts(contract)

ticker = ib.reqMktData(contract, '', False, False)
for i in range(10):
    ib.sleep(2)
    print(f"Type={ticker.marketDataType} Bid={ticker.bid} Ask={ticker.ask} Last={ticker.last}")

ib.cancelMktData(contract)
ib.disconnect()

