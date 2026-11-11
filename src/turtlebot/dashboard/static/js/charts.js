// Plotly charts themed from CSS tokens. Every chart has a visible reference line (start value,
// zero) so the series never float without an anchor.

import { isNum, money, pct } from "./format.js";

const token = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function theme() {
  return {
    ink: token("--ink"),
    ink2: token("--ink-2"),
    ink3: token("--ink-3"),
    rule: token("--rule"),
    rule2: token("--rule-2"),
    card: token("--card"),
    well: token("--well"),
    pos: token("--pos"),
    neg: token("--neg"),
    bench: token("--bench"),
    b1: token("--b1"),
    num: token("--font-num"),
    ui: token("--font-ui"),
  };
}

const CONFIG = { displayModeBar: false, responsive: true };

function axis(t, extra = {}) {
  return {
    gridcolor: t.rule,
    griddash: "dot",
    zeroline: false,
    showline: false,
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
    margin: { l: 44, r: 8, t: 4, b: 24 },
    hoverlabel: { bgcolor: t.card, bordercolor: t.rule2, font: { family: t.num, color: t.ink, size: 12 } },
    showlegend: false,
  };
}

/** Keep the last ``lastN`` trading days (0 = everything). */
function lastDays(points, lastN) {
  if (!(lastN > 0)) return points;
  const days = new Set();
  return [...points].reverse().filter((p) => {
    days.add(p.ts.slice(0, 10));
    return days.size <= lastN;
  }).reverse();
}

/**
 * Equity series prepared for display. With a benchmark both curves are rebased to 100 at the
 * first point of the window; without one the raw equity is shown.
 */
export function equitySeries(data, lastN = 0) {
  const pts = lastDays(data.points, lastN);
  const bench = new Map((data.benchmark || []).map((b) => [b.ts, b.close]));
  const base = pts[0]?.equity;
  const benchBase = pts.length ? bench.get(pts[0].ts) : undefined;
  const rebased = data.source === "account" && isNum(base) && base > 0;
  const withBench = rebased && isNum(benchBase) && benchBase > 0;
  return {
    pts,
    rebased,
    withBench,
    equity: pts.map((p) => (rebased ? (p.equity / base) * 100 : p.equity)),
    benchmark: withBench ? pts.map((p) => (bench.has(p.ts) ? (bench.get(p.ts) / benchBase) * 100 : null)) : [],
  };
}

/**
 * Equity vs benchmark (top), project phases (ribbon) and drawdown (bottom).
 * @param {HTMLElement} el
 * @param {object} data  EquityView from the API
 * @param {number} lastN 0 = all points
 */
export function renderEquity(el, data, lastN = 0) {
  if (!window.Plotly) return null;
  const t = theme();
  const s = equitySeries(data, lastN);
  if (!s.pts.length) {
    Plotly.purge(el);
    el.innerHTML = '<p class="empty">No equity history yet. The engine records account snapshots while it runs.</p>';
    return s;
  }
  clearPlaceholder(el);
  // Plotly does not parse UTC offsets; plot the exchange-local wall time instead.
  const x = s.pts.map((p) => p.ts.slice(0, 16).replace("T", " "));
  const relative = data.drawdown_unit === "fraction";
  const fmtDD = (v) => (relative ? pct(v, { signed: true }) : money(v, { signed: true }));
  const fmtEq = (v) => (s.rebased ? v.toFixed(2) : money(v));
  const hasPhases = (data.phases || []).length > 0;

  const traces = [];
  if (s.withBench) {
    traces.push({
      x, y: s.benchmark, type: "scatter", mode: "lines", connectgaps: true,
      line: { color: t.bench, width: 1.5, shape: "spline", smoothing: 0.4 },
      name: data.benchmark_symbol, hovertemplate: `${data.benchmark_symbol} %{y:.2f}<extra></extra>`,
    });
  }
  traces.push({
    x, y: s.equity, type: "scatter", mode: "lines",
    line: { color: t.pos, width: 2.5, shape: "spline", smoothing: 0.4 },
    name: "Bot",
    customdata: s.pts.map((p) => money(p.equity)),
    hovertemplate: s.rebased ? "Bot %{y:.2f} · %{customdata}<extra></extra>" : "%{customdata}<extra></extra>",
  });
  traces.push({
    x, y: s.pts.map((p) => p.drawdown), type: "scatter", mode: "lines",
    line: { color: t.neg, width: 1.5 },
    fill: "tozeroy", fillcolor: hexA(t.neg, 0.18),
    xaxis: "x", yaxis: "y2", name: "Drawdown",
    customdata: s.pts.map((p) => fmtDD(p.drawdown)),
    hovertemplate: "Drawdown %{customdata}<extra></extra>",
  });

  const top = [0.42, 1];
  const ribbon = [0.305, 0.355];
  const bottom = [0, hasPhases ? 0.22 : 0.3];
  const reference = s.rebased ? 100 : 0;
  const shapes = [];
  // Reference lines (start value / zero P&L, zero drawdown) as traces so they sit above the grid.
  const ref = (y, axisName) => ({
    x: [x[0], x.at(-1)], y: [y, y], type: "scatter", mode: "lines", yaxis: axisName,
    line: { color: t.ink3, width: 1 }, hoverinfo: "skip",
  });
  traces.unshift(ref(reference, "y"), ref(0, "y2"));
  const annotations = [
    label(t, "DRAWDOWN", 0, bottom[1] + 0.005, "left"),
    label(t, `max ${fmtDD(Math.min(...s.pts.map((p) => p.drawdown)))} · now ${fmtDD(s.pts.at(-1).drawdown)}`, 1, bottom[1] + 0.005, "right"),
  ];
  if (hasPhases) {
    const first = x[0];
    const last = x.at(-1);
    const fills = [t.rule2, hexA(t.b1, 0.28), hexA(t.pos, 0.24)];
    const span = Date.parse(last) - Date.parse(first) || 1;
    const plotWidth = Math.max(100, el.clientWidth - 56);
    data.phases.forEach((ph, i) => {
      const x0 = ph.start > first.slice(0, 10) ? ph.start : first;
      const x1 = ph.end && ph.end < last.slice(0, 10) ? ph.end : last;
      if (x0 >= x1) return;
      shapes.push({
        type: "rect", xref: "x", yref: "paper", x0, x1, y0: ribbon[0], y1: ribbon[1],
        fillcolor: fills[Math.max(0, fills.length - data.phases.length + i)] || t.rule2,
        line: { width: 1, color: t.card }, layer: "below",
      });
      const px = ((Date.parse(x1) - Date.parse(x0)) / span) * plotWidth;
      if (px < ph.label.length * 6.5 + 12) return; // label would not fit its band
      annotations.push({
        text: ph.label.toUpperCase(), xref: "x", yref: "paper", x: x0, y: (ribbon[0] + ribbon[1]) / 2,
        xanchor: "left", yanchor: "middle", xshift: 6, showarrow: false,
        font: { family: t.ui, size: 10, color: t.ink2 },
      });
    });
  }
  const layout = {
    ...baseLayout(t),
    margin: { l: 48, r: 8, t: 6, b: 26 },
    hovermode: "x unified",
    xaxis: axis(t, { type: "date", showgrid: false, anchor: "y2", tickformat: "%d %b", range: [x[0], x.at(-1)] }),
    yaxis: axis(t, { domain: top, tickformat: s.rebased ? ".0f" : ",.0f", nticks: 6 }),
    yaxis2: axis(t, { domain: bottom, tickformat: relative ? ".0%" : ",.0f", nticks: 3, rangemode: "tozero" }),
    shapes,
    annotations,
  };
  Plotly.react(el, traces, layout, CONFIG);
  return s;
}

/** Remove an "empty" placeholder without destroying an existing Plotly plot. */
function clearPlaceholder(el) {
  const placeholder = el.querySelector(":scope > .empty");
  if (placeholder) placeholder.remove();
}

function label(t, text, x, y, anchor) {
  return {
    text, xref: "paper", yref: "paper", x, y, xanchor: anchor, yanchor: "bottom", showarrow: false,
    font: { size: 11, color: t.ink3, family: anchor === "left" ? t.ui : t.num },
  };
}

export const STOP_BUCKETS = [
  [0, 0.02, "<2"], [0.02, 0.04, "2–4"], [0.04, 0.06, "4–6"], [0.06, 0.08, "6–8"],
  [0.08, 0.1, "8–10"], [0.1, 0.15, "10–15"], [0.15, Infinity, ">15"],
];

/** Histogram of the distance between last price and stop over open positions. */
export function renderStopHistogram(el, positions) {
  if (!window.Plotly) return;
  const t = theme();
  const d = positions.map((p) => p.stop_distance_pct).filter(isNum);
  if (!d.length) {
    Plotly.purge(el);
    el.innerHTML = '<p class="empty">No open positions with a live quote.</p>';
    return;
  }
  clearPlaceholder(el);
  const counts = STOP_BUCKETS.map(([lo, hi]) => d.filter((v) => v >= lo && v < hi).length);
  const colors = STOP_BUCKETS.map(([lo]) => (lo < 0.02 ? t.neg : lo < 0.06 ? token("--warn") : t.b1));
  const layout = {
    ...baseLayout(t),
    margin: { l: 4, r: 4, t: 16, b: 22 },
    bargap: 0.28,
    xaxis: axis(t, { showgrid: false, fixedrange: true, tickangle: 0, tickfont: { family: t.num, size: 10, color: t.ink3 } }),
    yaxis: axis(t, { visible: false, fixedrange: true, rangemode: "tozero", range: [0, Math.max(...counts) * 1.25 || 1] }),
    shapes: [{ type: "line", xref: "paper", x0: 0, x1: 1, yref: "y", y0: 0, y1: 0, line: { color: t.ink3, width: 1 } }],
  };
  Plotly.react(el, [{
    type: "bar", x: STOP_BUCKETS.map((b) => b[2]), y: counts,
    marker: { color: colors, cornerradius: 4 },
    text: counts.map(String), textposition: "outside", cliponaxis: false,
    textfont: { family: t.num, size: 11, color: t.ink2 },
    hovertemplate: "%{x} %: %{y} position(s)<extra></extra>",
  }], layout, CONFIG);
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
