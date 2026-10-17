# backend.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from db import get_open_positions, get_signals_today, get_equity_curve
from equity_snapshot import get_portfolio_overview

app = FastAPI(title="Trading Bot Dashboard API")

# Erlaubt dem Streamlit-Frontend (anderer lokaler Port) den Zugriff
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {"status": "ok", "message": "Trading Bot Dashboard API läuft"}


@app.get("/portfolio")
def portfolio():
    """Aktuelle offene Positionen mit Live-Kurs -- Momentaufnahme direkt aus dem Bot-State,
    nicht aus der synchronisierten Trade-Historie."""
    return get_portfolio_overview()


@app.get("/positions")
def positions(strategie_block: str | None = None):
    return get_open_positions(strategie_block)


@app.get("/signals/today")
def signals_today(strategie_block: str | None = None):
    return get_signals_today(strategie_block)


@app.get("/pnl/{strategie_block}")
def pnl(strategie_block: str):
    return get_equity_curve(strategie_block)
