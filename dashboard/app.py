# app.py
from datetime import datetime

import pandas as pd
import requests
import streamlit as st
import plotly.graph_objects as go

from claude_chat import ask_claude, ClaudeChatError
from equity_snapshot import run_equity_snapshot

API_BASE = "http://localhost:8000"

st.set_page_config(page_title="Turtle Trading Dashboard", page_icon="🐢", layout="wide")

STRATEGIE_BLOCKS = [
    ("block1", "Block 1 — Basisstrategie", "#2563EB", "#EFF6FF"),
    ("block2", "Block 2 — gefiltert", "#7C3AED", "#F5F3FF"),
]

STRATEGY_INFO = {
    "block1": """
Der Bot kauft, wenn eine Aktie über ihr **20-Tage-Hoch** ausbricht — und dabei deutlich mehr
gehandelt wird als sonst (mind. das 1,5-fache des üblichen Volumens). Das ist das klassische
"Turtle-Breakout"-Prinzip: ein neuer Höchststand mit starkem Volumen gilt als Signal für einen
beginnenden Trend.

- **Positionsgrösse:** 1,5 % des Kontowerts pro Kauf
- **Stop-Loss:** wird am **10-Tage-Tief** gesetzt und danach nur nach oben nachgezogen (nie gesenkt)
- **Max. Positionen:** 20 gleichzeitig
- **Universum:** nur liquide Aktien (Marktkapitalisierung ≥ 500 Mio. $, genug Handelsvolumen)
""",
    "block2": """
Läuft mit **exakt derselben Logik wie Block 1** (gleicher Scan, gleiche Positionsgrösse, gleicher
Stop) — mit einem zusätzlichen Qualitätsfilter: Es wird nur gekauft, wenn die
**Breakout-Strength ≥ 10 %** beträgt, also der Ausbruch über das 20-Tage-Hoch deutlich ausfällt,
nicht nur knapp.

- Eigener, unabhängiger Positions-Pool (ebenfalls max. 20)
- Bekommt keine Aktien, die Block 1 bereits hält oder am selben Tag gekauft hat
- Soll zeigen, ob strengere Signal-Qualität die Performance verbessert
""",
}

# ---------------------------------------------------------------------------
# Styling
# ---------------------------------------------------------------------------
st.markdown("""
<style>
    .block-container { padding-top: 1.8rem; padding-bottom: 3rem; }
    h1, h2, h3 { letter-spacing: -0.01em; }
    .dash-subtitle { color: #64748B; font-size: 0.95rem; margin-top: -0.6rem; }

    .section-header {
        display: flex; align-items: center; gap: 0.5rem;
        margin: 1.2rem 0 0.6rem 0;
    }
    .section-header .bar {
        width: 5px; height: 1.3rem; border-radius: 3px; background: #2563EB;
    }
    .section-header span { font-size: 1.05rem; font-weight: 600; color: #0F172A; }

    .kpi-card {
        border-radius: 12px; padding: 1rem 1.2rem; border: 1px solid #E2E8F0;
        background: #FFFFFF;
    }
    .kpi-label { font-size: 0.82rem; color: #64748B; font-weight: 500; }
    .kpi-value { font-size: 1.6rem; font-weight: 700; margin-top: 0.15rem; }

    .strategy-card {
        border-radius: 12px; padding: 1rem 1.3rem; margin-bottom: 1rem;
        border-left: 5px solid var(--accent); background: var(--accent-bg);
    }
    .strategy-card h5 { margin: 0 0 0.4rem 0; color: var(--accent); }

    .chat-header {
        border-radius: 14px; padding: 1.3rem 1.6rem; margin-bottom: 1.2rem;
        background: linear-gradient(135deg, #4F46E5 0%, #7C3AED 100%); color: white;
    }
    .chat-header h2 { color: white; margin: 0; }
    .chat-header p { color: #E0E7FF; margin: 0.3rem 0 0 0; font-size: 0.92rem; }
</style>
""", unsafe_allow_html=True)


def section_header(text):
    st.markdown(f'<div class="section-header"><div class="bar"></div><span>{text}</span></div>',
                unsafe_allow_html=True)


def kpi_card(label, value, accent="#0F172A"):
    st.markdown(
        f'<div class="kpi-card"><div class="kpi-label">{label}</div>'
        f'<div class="kpi-value" style="color:{accent}">{value}</div></div>',
        unsafe_allow_html=True,
    )


def color_pnl(val):
    if not isinstance(val, (int, float)):
        return ""
    if val > 0:
        return "color: #166534; background-color: #DCFCE7; font-weight: 600;"
    if val < 0:
        return "color: #991B1B; background-color: #FEE2E2; font-weight: 600;"
    return ""


def fetch_json(path, params=None, default=None):
    try:
        r = requests.get(f"{API_BASE}{path}", params=params, timeout=8)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        st.error(f"Backend nicht erreichbar ({path}): {e}")
        return default if default is not None else []


@st.cache_data(ttl=60)
def cached_portfolio():
    return fetch_json("/portfolio")


# ---------------------------------------------------------------------------
# Sidebar — Navigation & Aktionen
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### 🐢 Turtle Dashboard")
    page = st.radio("Ansicht", ["📊 Dashboard", "💬 Chat mit Claude"], label_visibility="collapsed")

    st.divider()
    st.subheader("⚙️ Aktionen")

    if st.button("🔄 Portfolio aktualisieren", width="stretch"):
        cached_portfolio.clear()
        st.rerun()
    st.caption("Live-Kurse der offenen Positionen (gecacht für 60s).")

    st.write("")
    if st.button("📈 Equity-Snapshot speichern", width="stretch"):
        with st.spinner("Hole aktuelle Kurse ..."):
            try:
                results = run_equity_snapshot()
            except Exception as e:
                st.error(f"Fehler: {e}")
                results = None
        if results is not None:
            if not results:
                st.info("Keine offenen Positionen gefunden.")
            else:
                for r in results:
                    if "missing_symbols" in r:
                        st.warning(f"Keine Kurse für: {', '.join(r['missing_symbols'])}")
                    else:
                        st.success(f"Block {r['block']}: ${r['equity']:,.2f} (P&L {r['pnl']:+,.2f})")
        cached_portfolio.clear()
    st.caption("Schreibt einen Punkt in die Equity-Kurve. Mehrmals täglich sinnvoll.")

# ---------------------------------------------------------------------------
# Chat-Ansicht
# ---------------------------------------------------------------------------
if page == "💬 Chat mit Claude":
    st.markdown(
        '<div class="chat-header"><h2>💬 Chat mit Claude über deinen Bot</h2>'
        '<p>Läuft über deine lokale Claude-Code-Session und dein Claude-Abo — keine separate '
        'API-Abrechnung.</p></div>',
        unsafe_allow_html=True,
    )

    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []
    if "claude_session_id" not in st.session_state:
        st.session_state.claude_session_id = None

    for msg in st.session_state.chat_history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    user_input = st.chat_input("Frag etwas zu deinem Portfolio, Signalen oder Trades ...")
    if user_input:
        st.session_state.chat_history.append({"role": "user", "content": user_input})
        with st.chat_message("user"):
            st.markdown(user_input)

        with st.chat_message("assistant"):
            with st.spinner("Denke nach ..."):
                try:
                    answer, new_session_id = ask_claude(user_input, st.session_state.claude_session_id)
                    st.session_state.claude_session_id = new_session_id
                except ClaudeChatError as e:
                    answer = f"Fehler: {e}"
            st.markdown(answer)
        st.session_state.chat_history.append({"role": "assistant", "content": answer})

    st.stop()

# ---------------------------------------------------------------------------
# Dashboard-Ansicht — Kopfbereich
# ---------------------------------------------------------------------------
st.title("🐢 Turtle Trading Dashboard")
st.markdown(
    f'<div class="dash-subtitle">Live-Übersicht über Block 1 &amp; Block 2 · Stand '
    f'{datetime.now().strftime("%d.%m.%Y %H:%M")}</div>',
    unsafe_allow_html=True,
)
st.write("")

# ---------------------------------------------------------------------------
# Portfolio-Übersicht (aktueller Stand, über beide Blöcke)
# ---------------------------------------------------------------------------
section_header("📋 Portfolio — aktuell offene Positionen")

portfolio = cached_portfolio()

if not portfolio:
    st.info("Keine offenen Positionen gefunden (oder Kurse gerade nicht abrufbar).")
else:
    total_value = sum(p["market_value"] for p in portfolio if p.get("market_value") is not None)
    total_pnl = sum(p["pnl"] for p in portfolio if p.get("pnl") is not None)
    pnl_accent = "#16A34A" if total_pnl >= 0 else "#DC2626"

    k1, k2, k3 = st.columns(3)
    with k1:
        kpi_card("Offene Positionen", str(len(portfolio)))
    with k2:
        kpi_card("Marktwert gesamt", f"${total_value:,.2f}")
    with k3:
        kpi_card("Unrealisierter P&L", f"${total_pnl:+,.2f}", accent=pnl_accent)

    st.write("")

    df = pd.DataFrame(portfolio)
    df["block"] = df["block"].map(lambda b: f"Block {b}")
    df = df.rename(columns={
        "symbol": "Symbol", "block": "Block", "qty": "Stk.",
        "entry_price": "Einstieg", "current_price": "Aktuell",
        "market_value": "Marktwert", "pnl": "P&L", "pnl_pct": "P&L %", "stop": "Stop",
    })
    df = df[["Symbol", "Block", "Stk.", "Einstieg", "Aktuell", "Marktwert", "P&L", "P&L %", "Stop"]]

    styled = (
        df.style
        .format({
            "Einstieg": "${:,.2f}", "Aktuell": "${:,.2f}", "Marktwert": "${:,.2f}",
            "P&L": "${:+,.2f}", "P&L %": "{:+.2f}%", "Stop": "${:,.2f}",
        }, na_rep="–")
        .map(color_pnl, subset=["P&L", "P&L %"])
    )
    st.dataframe(styled, hide_index=True, width="stretch")
    st.caption(
        "Marktwert = Stückzahl × aktueller Kurs, P&L = unrealisiert gegenüber dem Einstiegspreis. "
        "Keine vollständige Kontobilanz (Cash & realisierte Gewinne fehlen)."
    )

st.divider()

# ---------------------------------------------------------------------------
# Block-Tabs
# ---------------------------------------------------------------------------
tab_labels = [label for _, label, _, _ in STRATEGIE_BLOCKS]
tabs = st.tabs(tab_labels)

for tab, (block_id, label, accent, accent_bg) in zip(tabs, STRATEGIE_BLOCKS):
    with tab:
        st.markdown(
            f'<div class="strategy-card" style="--accent:{accent}; --accent-bg:{accent_bg};">'
            f'<h5>📖 Strategie</h5>{STRATEGY_INFO[block_id]}</div>',
            unsafe_allow_html=True,
        )

        col1, col2 = st.columns([2, 1])

        with col1:
            st.markdown("##### Equity-Verlauf")
            data = fetch_json(f"/pnl/{block_id}")
            if data:
                timestamps = [d.get("timestamp", d.get("date")) for d in data]
                equity = [d["equity"] for d in data]
                fig = go.Figure()
                fig.add_trace(go.Scatter(
                    x=timestamps, y=equity, mode="lines+markers", name="Equity",
                    line=dict(color=accent, width=2), marker=dict(size=5),
                    fill="tozeroy", fillcolor=accent_bg,
                ))
                fig.update_layout(
                    height=340, margin=dict(l=10, r=10, t=10, b=10),
                    plot_bgcolor="white", paper_bgcolor="white",
                    xaxis=dict(showgrid=False), yaxis=dict(showgrid=True, gridcolor="#E2E8F0"),
                )
                st.plotly_chart(fig, width="stretch")
            else:
                st.info("Noch keine Equity-Daten für diesen Block — Snapshot in der Sidebar speichern.")

        with col2:
            st.markdown("##### Status")
            if data:
                latest = data[-1]
                pnl_color = "#16A34A" if latest["pnl"] >= 0 else "#DC2626"
                kpi_card("Equity", f"${latest['equity']:,.2f}", accent=accent)
                st.write("")
                kpi_card("P&L", f"${latest['pnl']:+,.2f}", accent=pnl_color)
            else:
                kpi_card("Equity", "–")
                st.write("")
                kpi_card("P&L", "–")

        st.divider()

        st.markdown("##### Trade-Log (Käufe & Verkäufe)")
        positions = fetch_json("/positions", params={"strategie_block": block_id})
        if positions:
            pdf = pd.DataFrame(positions)
            if "action" in pdf.columns:
                def action_color(val):
                    if val == "BUY":
                        return "color: #166534; background-color: #DCFCE7; font-weight: 600;"
                    if val == "SELL":
                        return "color: #991B1B; background-color: #FEE2E2; font-weight: 600;"
                    return ""
                styled_pdf = pdf.style.map(action_color, subset=["action"])
                st.dataframe(styled_pdf, hide_index=True, width="stretch")
            else:
                st.dataframe(pdf, hide_index=True, width="stretch")
        else:
            st.info("Noch keine Trades für diesen Block synchronisiert.")

        st.markdown("##### Heutige Signale (Signal-Pool, vor Block-Filterung)")
        st.caption("Gilt für beide Blöcke gleichermaßen — Block 2 filtert zusätzlich auf Mindest-Strength.")
        signals = fetch_json("/signals/today")
        if signals:
            st.dataframe(signals, hide_index=True, width="stretch")
        else:
            st.info("Noch keine Signale heute.")
