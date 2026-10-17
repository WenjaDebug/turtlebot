from equity_snapshot import get_portfolio_overview
import traceback
try:
    r = get_portfolio_overview()
    print("OK", len(r), "Positionen")
except Exception:
    traceback.print_exc()
