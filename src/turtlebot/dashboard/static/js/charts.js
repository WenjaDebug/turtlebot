// Plotly charts themed from CSS tokens. One y-axis per panel (no dual axes): equity and its
// drawdown are two stacked panels sharing the x-axis.

import { isNum, money, pct } from "./format.js";

const token = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function theme() {
  return {
    ink: token("--ink"),
    ink2: token("--ink-2"),
    ink3: token("--ink-3"),
    rule: token("--rule"),
    surface: token("--surface"),
    bg: token("--bg"),
    b1: token("--b1"),
    b2: token("--b2"),
    bad: token("--bad-mark"),
    num: token("--font-num"),
    ui: token("--font-ui"),
  };
}

const CONFIG = { displayModeBar: false, responsive: true };

function axis(t, extra = {}) {
  return {
    gridcolor: t.rule,
    zeroline: false,
    linecolor: t.rule,
    tickfont: { family: t.num, size: 11, color: t.ink3 },
    showspikes: false,
    ...extra,
  };
}

function baseLayout(t) {
  return {
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: { family: t.ui, color: t.ink2, size: 12 },
    margin: { l: 64, r: 12, t: 8, b: 28 },
    hoverlabel: { bgcolor: t.surface, bordercolor: t.rule, font: { family: t.num, color: t.ink, size: 12 } },
    showlegend: false,
  };
}

/**
 * Equity (top, 70 %) and drawdown (bottom, 30 %).
 * @param {HTMLElement} el
 * @param {{points: {ts: string, equity: number, drawdown: number}[], source: string, currency: string, drawdown_unit: string}} data
 * @param {number} lastN  0 = all points
 */
export function renderEquity(el, data, lastN = 0) {
  if (!window.Plotly) return;
  const t = theme();
  let pts = data.points;
  if (lastN > 0) {
    const days = new Set();
    pts = [...pts].reverse().filter((p) => {
      days.add(p.ts.slice(0, 10));
      return days.size <= lastN;
    }).reverse();
  }
  if (!pts.length) {
    Plotly.purge(el);
    el.innerHTML = '<p class="empty">No equity history yet — the engine records account snapshots while it runs.</p>';
    return;
  }
  el.innerHTML = "";
  const x = pts.map((p) => p.ts);
  const relative = data.drawdown_unit === "fraction";
  const fmtDD = (v) => (relative ? pct(v, { signed: true }) : money(v, { signed: true }));
  const label = data.source === "account" ? `Net liquidation (${data.currency})` : "Cumulative realised P&L (USD)";

  const equity = {
    x,
    y: pts.map((p) => p.equity),
    type: "scatter",
    mode: "lines",
    line: { color: t.ink, width: 2 },
    name: label,
    customdata: pts.map((p) => money(p.equity, { digits: 0 })),
    hovertemplate: "%{customdata}<extra></extra>",
  };
  const dd = {
    x,
    y: pts.map((p) => p.drawdown),
    type: "scatter",
    mode: "lines",
    line: { color: t.bad, width: 1.5 },
    fill: "tozeroy",
    fillcolor: hexA(t.bad, 0.14),
    xaxis: "x",
    yaxis: "y2",
    name: "Drawdown",
    customdata: pts.map((p) => fmtDD(p.drawdown)),
    hovertemplate: "Drawdown %{customdata}<extra></extra>",
  };
  const layout = {
    ...baseLayout(t),
    margin: { l: 64, r: 12, t: 22, b: 40 },
    hovermode: "x unified",
    xaxis: axis(t, { type: "date", showgrid: false, anchor: "y2" }),
    yaxis: axis(t, { domain: [0.36, 0.97], tickformat: ",.0f" }),
    yaxis2: axis(t, { domain: [0, 0.24], tickformat: relative ? ".0%" : ",.0f", rangemode: "tozero" }),
    annotations: [
      annot(t, label, 1.0),
      annot(t, "Drawdown", 0.25),
    ],
  };
  Plotly.react(el, [equity, dd], layout, CONFIG);
}

function annot(t, text, y) {
  return {
    text, xref: "paper", yref: "paper", x: 0, y, xanchor: "left", yanchor: "bottom",
    showarrow: false, font: { size: 11, color: t.ink3, family: t.ui },
  };
}

/** Realised vs unrealised P&L per block, horizontal bars, colour = block, direct labels. */
export function renderBlocks(el, blocks) {
  if (!window.Plotly) return;
  const t = theme();
  const cats = ["Unrealised", "Realised"];
  const traces = blocks.map((b) => {
    const color = b.block === 1 ? t.b1 : t.b2;
    const vals = [b.unrealized_pnl, b.realized_pnl];
    return {
      type: "bar",
      orientation: "h",
      name: `Block ${b.block}`,
      y: cats,
      x: vals,
      marker: { color, line: { color: t.bg, width: 2 } },
      text: vals.map((v) => `B${b.block} ${money(v, { signed: true })}`),
      textposition: "outside",
      textfont: { family: t.num, size: 11, color: t.ink2 },
      cliponaxis: false,
      hovertemplate: `Block ${b.block} · %{y}: %{text}<extra></extra>`,
    };
  });
  const layout = {
    ...baseLayout(t),
    barmode: "group",
    bargap: 0.35,
    bargroupgap: 0.12,
    margin: { l: 84, r: 84, t: 4, b: 22 },
    // Symmetric range around zero leaves room for outside labels of negative bars.
    xaxis: axis(t, { tickformat: ",.0f", zeroline: true, zerolinecolor: t.ink3, range: symmetric(blocks) }),
    yaxis: axis(t, { showgrid: false, autorange: "reversed", tickfont: { family: t.ui, size: 12, color: t.ink2 } }),
  };
  Plotly.react(el, traces, layout, CONFIG);
}

function symmetric(blocks) {
  const vals = blocks.flatMap((b) => [b.unrealized_pnl, b.realized_pnl]).filter(isNum);
  const max = Math.max(1, ...vals.map(Math.abs)) * 1.45;
  return vals.some((v) => v < 0) ? [-max, max] : [0, max];
}

function hexA(hex, a) {
  const m = hex.replace("#", "").match(/.{2}/g);
  if (!m) return hex;
  const [r, g, b] = m.map((h) => parseInt(h, 16));
  return `rgba(${r},${g},${b},${a})`;
}

export const ready = () =>
  new Promise((resolve) => {
    if (window.Plotly) return resolve();
    const check = setInterval(() => {
      if (window.Plotly) {
        clearInterval(check);
        resolve();
      }
    }, 50);
    setTimeout(() => { clearInterval(check); resolve(); }, 8000);
  });

export { isNum };
