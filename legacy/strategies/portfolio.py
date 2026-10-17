"""Sizing-, Slot- und Kommissionsregeln: gemeinsam genutzt von Backtest und Live-Loop."""

import math

SUBPORTFOLIO_CAPITAL = 5_000.0
MAX_POSITIONS = 5
POSITION_SIZE = SUBPORTFOLIO_CAPITAL / MAX_POSITIONS  # 20 % vom Startkapital, kein Compounding


def ibkr_commission(shares, price):
    """IBKR Fixed (US-Aktien): 0,005 $ pro Aktie, mindestens 1 $, höchstens 1 % des Handelswerts."""
    return max(1.0, min(0.005 * shares, 0.01 * shares * price))


def position_shares(price, budget=POSITION_SIZE):
    """Ganze Aktien, sodass Kaufpreis + Kommission das Budget nicht überschreiten."""
    shares = math.floor(budget / price)
    while shares > 0 and shares * price + ibkr_commission(shares, price) > budget:
        shares -= 1
    return shares


def select_entries(candidates, free_slots):
    """Wählt aus {ticker: score} die besten Kandidaten für die freien Slots (höchster Score zuerst)."""
    ranked = sorted(candidates.items(), key=lambda item: item[1], reverse=True)
    return [ticker for ticker, _ in ranked[:max(free_slots, 0)]]
