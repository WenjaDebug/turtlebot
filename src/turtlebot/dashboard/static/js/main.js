// turtlebot desk: wires the live snapshot stream to the UI.

import { getJSON, postJSON, subscribe } from "./api.js";
import { equitySeries, ready, renderEquity, renderStopHistogram } from "./charts.js";
import {
  ago, dateShort, esc, isNum, miniMarkdown, money, pct, price, qty, ratio, time, tone,
} from "./format.js";

const $ = (sel) => document.querySelector(sel);
const state = {
  version: null,
  overview: null,
  positions: [],
  events: [],
  equity: null,
  recon: null,
  range: 0,
  book: "open",
  blockFilter: 0,
  feedFilter: "all",
  trades: [],
  signals: [],
  sessionId: null,
};
try { state.sessionId = sessionStorage.getItem("chat-session"); } catch { /* storage unavailable */ }

const signedPct = (v) => pct(v, { signed: true });

// ------------------------------------------------------------------ header & status
function setPill(id, stateName, text) {
  const pill = $(id);
  pill.dataset.state = stateName;
  pill.querySelector(".pill__text").textContent = text;
}

function renderStatus(o) {
  const s = o.status;
  if (s.demo) setPill("#pill-engine", "idle", "Demo data");
  else if (s.running) setPill("#pill-engine", "good", `Engine live · ${s.mode}`);
  else setPill("#pill-engine", "idle", s.heartbeat ? `Engine idle · seen ${ago(s.heartbeat)}` : "Engine never ran");

  const r = s.last_reconciliation;
  if (!r) setPill("#pill-recon", "idle", "No reconciliation yet");
  else {
    const st = r.outcome === "ok" ? (r.critical ? "warn" : "good") : r.outcome === "degraded" ? "warn" : "bad";
    const label = r.outcome === "ok" ? "Reconciled" : `Reconciliation ${r.outcome}`;
    setPill("#pill-recon", st, `${label} ${time(r.ts)} · ${r.discrepancies} diff${r.discrepancies === 1 ? "" : "s"}`);
  }
  setPill("#pill-trading", s.trading_enabled ? "warn" : "idle", s.trading_enabled ? "Trading enabled" : "Trading off");
  renderBanner(s, r);
}

function renderBanner(s, r) {
  const banner = $("#banner");
  const alerts = s.alerts || [];
  if (alerts.length) {
    const bad = alerts.filter((a) => a.severity !== "warn");
    const warn = alerts.filter((a) => a.severity === "warn");
    banner.hidden = false;
    banner.dataset.level = bad.length ? "bad" : "warn";
    const title = bad.length ? "Stop orders need attention" : "Check these positions";
    const rows = [...bad, ...warn].slice(0, 8);
    banner.innerHTML = `<strong>${title}</strong> · ${bad.length} stop issue(s), ${warn.length} warning(s)
      <ul class="alerts">${rows.map((a) => `<li>${esc(a.message)}</li>`).join("")}</ul>`;
    return;
  }
  delete banner.dataset.level;
  if (s.demo) {
    banner.hidden = false;
    banner.innerHTML = "<strong>Demo mode.</strong> All figures are synthetic and generated for illustration.";
  } else if (r && r.outcome !== "ok") {
    banner.hidden = false;
    banner.dataset.level = "warn";
    banner.innerHTML = `<strong>Reconciliation ${esc(r.outcome)}.</strong> ${esc(r.message || "")} Stops were left untouched.`;
  } else banner.hidden = true;
}

// ------------------------------------------------------------------ hero & KPIs
function setKpi(key, value, sub, cls = "") {
  const v = $(`[data-k="${key}"]`);
  v.textContent = value;
  v.className = `kpi__value num ${cls}`;
  if (sub !== undefined) $(`[data-s="${key}"]`).textContent = sub;
}

function renderHero() {
  const o = state.overview;
  if (!o) return;
  const a = o.account;
  const m = o.metrics;
  $("#netliq").textContent = !a ? "—"
    : a.currency === "USD" ? `$${money(a.net_liquidation)}` : money(a.net_liquidation, { currency: a.currency });
  const day = $("#daypnl");
  day.textContent = a && isNum(a.daily_pnl) ? `${money(a.daily_pnl, { signed: true })} today` : "— today";
  day.className = `num ${tone(a?.daily_pnl)}`;
  const first = state.equity?.points?.[0]?.ts;
  const tot = $("#totalret");
  tot.textContent = isNum(m.total_return) ? `${signedPct(m.total_return)} since ${first ? dateShort(first) : "start"}` : "";
  tot.className = `num ${tone(m.total_return)}`;

  // vs benchmark over the full history
  const s = state.equity ? equitySeries(state.equity, 0) : null;
  if (s?.withBench) {
    const bot = s.equity.at(-1) / 100 - 1;
    const bench = s.benchmark.filter(isNum).at(-1) / 100 - 1;
    const diff = (bot - bench) * 100;
    setKpi("vs_bench", `${diff >= 0 ? "+" : "−"}${Math.abs(diff).toFixed(1)} pp`,
      `Bot ${signedPct(bot)} · ${state.equity.benchmark_symbol} ${signedPct(bench)}`, tone(diff));
  } else {
    setKpi("vs_bench", "—", state.equity?.source === "account" ? "benchmark unavailable" : "needs account history");
  }

  const nl = a?.net_liquidation_usd;
  const open = state.positions.filter((p) => p.status === "open");
  const expo = isNum(nl) && nl > 0 ? m.exposure_usd / nl : null;
  setKpi("exposure", isNum(expo) ? pct(expo) : `$${money(m.exposure_usd)}`, `$${money(m.exposure_usd)} · ${open.length} pos.`);
  $("#exposure-meter").style.width = `${Math.min(100, Math.max(0, (expo ?? 0) * 100))}%`;

  const live = open.filter((p) => p.stop_confirmed && !p.stop_alert).length;
  const riskPct = isNum(nl) && nl > 0 ? m.open_risk_usd / nl : null;
  setKpi("open_risk", `$${money(m.open_risk_usd)}`, `${isNum(riskPct) ? `${pct(riskPct)} of equity · ` : ""}${live} stops live`);
  setKpi("max_dd", pct(m.max_drawdown), `Now ${pct(m.current_drawdown)} from peak`, m.max_drawdown < 0 ? "neg" : "");
  setKpi("pf", ratio(m.trades.profit_factor),
    `Win rate ${isNum(m.trades.win_rate) ? pct(m.trades.win_rate) : "—"} · ${m.trades.trades} trades`);
  setKpi("sharpe", ratio(m.sharpe), `Sortino ${ratio(m.sortino)} · vol ${isNum(m.volatility) ? pct(m.volatility) : "—"}`);
  renderSpark();
}

function renderSpark() {
  const pts = state.equity?.points || [];
  const svg = $("#spark");
  if (pts.length < 2) {
    svg.innerHTML = "";
    return;
  }
  const ys = pts.map((p) => p.equity);
  const lo = Math.min(...ys);
  const hi = Math.max(...ys);
  const span = hi - lo || 1;
  const X = (i) => (i / (pts.length - 1)) * 120;
  const Y = (v) => 52 - ((v - lo) / span) * 48;
  const line = ys.map((v, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join("");
  svg.innerHTML = `<defs><linearGradient id="spark-fill" x1="0" x2="0" y1="0" y2="1">
      <stop offset="0" stop-color="var(--pos)" stop-opacity=".35"/><stop offset="1" stop-color="var(--pos)" stop-opacity="0"/></linearGradient></defs>
    <line class="spark__base" x1="0" x2="120" y1="${Y(ys[0]).toFixed(1)}" y2="${Y(ys[0]).toFixed(1)}"/>
    <path class="spark__area" d="${line}L120,56L0,56Z"/>
    <path class="spark__line" d="${line}"/>`;
}

// ------------------------------------------------------------------ equity
function renderEquityPanel() {
  const eq = state.equity;
  if (!eq) return;
  const s = renderEquity($("#chart-equity"), eq, state.range);
  const sym = eq.benchmark_symbol || "benchmark";
  $("#equity-vs").textContent = s?.withBench ? `vs ${sym === "SPY" ? "S&P 500" : sym}` : "";
  const notes = {
    account: s?.withBench
      ? `Rebased to 100 at the window start · ${sym} total return, daily close`
      : "Net liquidation from account snapshots",
    realized: "No account snapshots yet. Showing the cumulative realised P&L of closed trades.",
    none: "No history yet",
  };
  $("#equity-note").textContent = notes[eq.source] || "";
  const legend = [];
  if (s?.pts.length) {
    const fmt = (v) => (s.rebased ? v.toFixed(1) : money(v));
    legend.push(`<li><i></i>Bot ${fmt(s.equity.at(-1))}</li>`);
    if (s.withBench) legend.push(`<li><i class="dashed"></i>${esc(sym)} ${fmt(s.benchmark.filter(isNum).at(-1))}</li>`);
  }
  $("#equity-legend").innerHTML = legend.join("");
}

// ------------------------------------------------------------------ blocks
function slotTone(p) {
  if (p.status !== "open") return "pending";
  const r = p.unrealized_pct;
  if (!isNum(r)) return "up1";
  if (r >= 0.05) return "up2";
  if (r >= 0) return "up1";
  return r <= -0.04 ? "dn2" : "dn1";
}

function renderBlocks() {
  const blocks = state.overview?.blocks || [];
  const used = blocks.reduce((n, b) => n + b.open_positions + b.pending_positions, 0);
  const total = blocks.reduce((n, b) => n + b.max_positions, 0);
  $("#slots-used").textContent = `${used} / ${total} used`;
  $("#blocks").innerHTML = blocks.map((b) => {
    const mine = state.positions
      .filter((p) => p.block === b.block)
      .sort((x, y) => (y.unrealized_pct ?? 0) - (x.unrealized_pct ?? 0));
    const slots = Array.from({ length: b.max_positions }, (_, i) => {
      const p = mine[i];
      return p
        ? `<i data-tone="${slotTone(p)}" title="${esc(p.symbol)} ${p.status === "open" ? signedPct(p.unrealized_pct) : "pending"}"></i>`
        : "<i></i>";
    }).join("");
    const filter = b.min_strength != null ? `breakout strength ≥ ${pct(b.min_strength)}` : "no strength filter";
    return `<article class="block" data-block="${b.block}">
      <div class="block__head"><span class="dot"></span><span class="block__title">Block ${b.block} · ${esc(b.label)}</span>
        <span class="block__slots num">${b.open_positions + b.pending_positions}/${b.max_positions} slots</span></div>
      <div class="block__sub">${filter}${b.enabled ? "" : " · new entries disabled"}</div>
      <div class="slots" aria-label="${b.open_positions} of ${b.max_positions} slots used">${slots}</div>
      <div class="block__stats">
        <span>open <b class="${tone(b.unrealized_pnl)}">${money(b.unrealized_pnl, { signed: true })}</b></span>
        <span>realised <b class="${tone(b.realized_pnl)}">${money(b.realized_pnl, { signed: true })}</b></span>
        <span>${b.closed_trades} closed${isNum(b.win_rate) ? ` · ${pct(b.win_rate)} won` : ""}</span>
      </div>
    </article>`;
  }).join("");
}

// ------------------------------------------------------------------ book
function cushionLevel(d) {
  if (!isNum(d)) return "";
  return d < 0.03 ? "tight" : d < 0.06 ? "close" : "ok";
}

function cushionCell(p) {
  const d = p.stop_distance_pct;
  if (!isNum(d)) return '<span class="muted">—</span>';
  const width = Math.max(3, Math.min(100, (d / 0.2) * 100));
  return `<div class="cushion" data-level="${cushionLevel(d)}" title="Last price is ${pct(d)} above the stop">
      <div class="cushion__track"><div class="cushion__fill" style="width:${width}%"></div></div>
      <span class="cushion__val num">${pct(d)}</span></div>`;
}

const byBlock = (rows) => (state.blockFilter ? rows.filter((r) => r.block === state.blockFilter) : rows);
const blk = (b) => `<span class="blk" data-block="${b}">B${b}</span>`;

function renderBook() {
  const table = $("#book");
  const sub = $("#book-sub");
  if (state.book === "open") {
    const rows = byBlock(state.positions).slice().sort(
      (a, b) => (a.stop_distance_pct ?? 9) - (b.stop_distance_pct ?? 9),
    );
    const open = state.positions.filter((p) => p.status === "open").length;
    const pending = state.positions.length - open;
    sub.textContent = `${open} open${pending ? ` · ${pending} pending` : ""} · sorted by cushion to stop`;
    table.innerHTML = `<thead><tr><th>Symbol</th><th>Blk</th><th class="r">Qty</th><th class="r">Entry</th><th class="r">Last</th>
      <th class="r">P&amp;L</th><th class="r">Stop</th><th>Cushion to stop</th><th class="r">Days</th></tr></thead><tbody>${
      rows.length
        ? rows.map((p) => {
          const tags = [
            p.status === "pending" ? '<span class="tag">pending</span>' : "",
            p.missing_cycles ? `<span class="tag tag--bad" title="Not reported by the broker in ${p.missing_cycles} reconciliation(s)">unconfirmed</span>` : "",
            p.quote_stale && p.status === "open" ? '<span class="tag tag--warn" title="No live quote, valued at entry">no quote</span>' : "",
            p.stop_alert ? `<span class="tag tag--bad">${esc(p.stop_alert)}</span>` : "",
            !p.stop_confirmed && !p.stop_alert ? '<span class="tag tag--warn" title="Stop not yet compared with TWS">stop unverified</span>' : "",
            ...(p.warnings || []).map((w) => `<span class="tag tag--warn">${esc(w)}</span>`),
          ].join("");
          const pnl = isNum(p.unrealized_pnl)
            ? `<span class="${tone(p.unrealized_pnl)}">${money(p.unrealized_pnl, { signed: true })}</span> <span class="muted">${signedPct(p.unrealized_pct)}</span>`
            : '<span class="muted">—</span>';
          return `<tr data-level="${cushionLevel(p.stop_distance_pct)}">
            <td><span class="sym">${esc(p.symbol)}</span>${tags}</td><td>${blk(p.block)}</td>
            <td class="r num">${qty(p.qty)}</td><td class="r num muted">${price(p.entry_price)}</td>
            <td class="r num">${price(p.last_price)}</td><td class="r num">${pnl}</td>
            <td class="r num muted">${price(p.stop)}</td><td>${cushionCell(p)}</td>
            <td class="r num muted">${p.days_held ?? "—"}</td></tr>`;
        }).join("")
        : '<tr><td colspan="9" class="empty">No open positions.</td></tr>'
    }</tbody>`;
  } else if (state.book === "trades") {
    const rows = byBlock(state.trades);
    const wins = rows.filter((t) => isNum(t.pnl) && t.pnl > 0).length;
    sub.textContent = `${rows.length} closed trade(s) · ${wins} winner(s)`;
    table.innerHTML = `<thead><tr><th>Symbol</th><th>Blk</th><th class="r">Entry</th><th class="r">Exit</th><th class="r">Return</th>
      <th class="r">P&amp;L</th><th class="r">Closed</th><th>Reason</th></tr></thead><tbody>${
      rows.length
        ? rows.map((t) => `<tr>
            <td><span class="sym">${esc(t.symbol)}</span></td><td>${blk(t.block)}</td>
            <td class="r num muted">${price(t.entry_price)}</td>
            <td class="r num">${isNum(t.exit_price) ? price(t.exit_price) : '<span class="muted" title="Exit price unknown, see import-executions">n/a</span>'}</td>
            <td class="r num ${tone(t.return_pct)}">${signedPct(t.return_pct)}</td>
            <td class="r num ${tone(t.pnl)}">${money(t.pnl, { signed: true })}</td>
            <td class="r num muted">${dateShort(t.closed_at)}</td>
            <td class="muted">${esc(t.reason || "")}</td></tr>`).join("")
        : '<tr><td colspan="8" class="empty">No closed trades yet.</td></tr>'
    }</tbody>`;
  } else {
    const rows = state.signals;
    sub.textContent = rows.length ? `Latest scan ${dateShort(rows[0].scan_date)} · ${rows.length} breakout(s)` : "No signals";
    table.innerHTML = `<thead><tr><th>Symbol</th><th class="r">Strength</th><th class="r">Volume ×</th><th class="r">Close</th>
      <th class="r">Initial stop</th><th>Eligible for</th></tr></thead><tbody>${
      rows.length
        ? rows.map((s) => `<tr><td><span class="sym">${esc(s.symbol)}</span></td>
            <td class="r num">${signedPct(s.strength)}</td><td class="r num">${ratio(s.volume_ratio)}</td>
            <td class="r num">${price(s.price)}</td><td class="r num muted">${price(s.stop)}</td>
            <td>${blk(1)}${s.strength >= 0.1 ? ` ${blk(2)}` : ""}</td></tr>`).join("")
        : '<tr><td colspan="6" class="empty">No signals in the latest scan.</td></tr>'
    }</tbody>`;
  }
}

// ------------------------------------------------------------------ risk
function renderRisk() {
  const open = state.positions.filter((p) => p.status === "open");
  renderStopHistogram($("#chart-hist"), open);

  const risky = open.filter((p) => isNum(p.risk_to_stop) && p.risk_to_stop > 0)
    .sort((a, b) => b.risk_to_stop - a.risk_to_stop).slice(0, 5);
  const max = risky[0]?.risk_to_stop || 1;
  $("#risk-bars").innerHTML = risky.length
    ? risky.map((p) => `<li data-block="${p.block}"><span class="sym">${esc(p.symbol)}</span>
        <span class="track"><span class="fill" style="display:block;width:${(p.risk_to_stop / max) * 100}%"></span></span>
        <span class="val num">$${money(p.risk_to_stop)}</span></li>`).join("")
    : '<li class="empty">No open risk.</li>';

  const s = state.overview?.status;
  const guards = [];
  const r = state.recon;
  if (r) {
    guards.push([r.outcome === "ok" ? "good" : "warn",
      `Reconciliation ${r.outcome} · ${r.n_local} tracked / ${r.n_broker} at broker · ${r.n_actions} action(s)`]);
  }
  const unverified = open.filter((p) => !p.stop_confirmed || p.stop_alert).length;
  guards.push(unverified ? ["warn", `${unverified} stop(s) not yet confirmed at the broker`] : ["good", `All ${open.length} stops confirmed at the broker`]);
  const missing = state.positions.filter((p) => p.missing_cycles > 0).length;
  if (missing) guards.push(["warn", `${missing} position(s) missing at the broker · closed only after confirmation`]);
  const bad = (s?.alerts || []).filter((a) => a.severity !== "warn").length;
  const warn = (s?.alerts || []).length - bad;
  if (bad) guards.push(["bad", `${bad} stop order problem(s) · see banner`]);
  else if (warn) guards.push(["warn", `${warn} price data / stop distance warning(s)`]);
  const stale = open.filter((p) => p.quote_stale).length;
  if (stale) guards.push(["warn", `${stale} position(s) without a live quote`]);
  $("#guards").innerHTML = guards.map(([lvl, text]) => `<li data-level="${lvl}">${esc(text)}</li>`).join("");
}

// ------------------------------------------------------------------ activity
const KIND_LABEL = {
  ORDER: "Order", FILL: "Fill", PARTIAL_FILL: "Partial fill", EXIT: "Exit", PARTIAL_EXIT: "Partial exit",
  CANCEL: "Cancel", SKIP: "Skipped", STOP_NEW: "New stop", STOP_UPDATE: "Stop trailed",
  STOP_LOST: "Stop lost", ADOPT: "Adopted", UNIVERSE: "Universe", SIGNAL: "Signal",
  STOP_REPLACE_FAILED: "Stop replace failed", FOREIGN_ORDER: "Foreign order",
  EXIT_PRICE_BACKFILL: "Exit price", MANUAL_CLOSE: "Manual close",
};
const ORDER_KINDS = new Set(["ORDER", "FILL", "PARTIAL_FILL", "EXIT", "PARTIAL_EXIT", "CANCEL", "SKIP", "MANUAL_CLOSE"]);

function eventTone(e) {
  const d = e.details || {};
  if (["STOP_LOST", "STOP_REPLACE_FAILED", "FOREIGN_ORDER"].includes(e.kind)) return "bad";
  if (e.kind === "EXIT" || e.kind === "PARTIAL_EXIT") return isNum(d.return) ? (d.return >= 0 ? "good" : "bad") : "warn";
  if (e.kind === "SKIP" || e.kind === "ADOPT") return "warn";
  if (["ORDER", "FILL", "PARTIAL_FILL", "STOP_NEW", "STOP_UPDATE"].includes(e.kind)) return e.block === 2 ? "b2" : "b1";
  return "";
}

function describe(e) {
  const d = e.details || {};
  const bits = [];
  if (isNum(e.qty)) bits.push(qty(e.qty));
  if (e.symbol) bits.push(`<strong>${esc(e.symbol)}</strong>`);
  if (isNum(e.price)) bits.push(`@ ${price(e.price)}`);
  if (isNum(e.stop) && e.kind.startsWith("STOP")) bits.push(`stop ${price(e.stop)}`);
  const extra = d.change || d.reason || d.legacy || d.error
    || (isNum(d.client_id) ? `client id ${d.client_id}` : "")
    || (isNum(d.return) ? `return ${signedPct(d.return)}` : "")
    || (d.size ? `${d.size} eligible tickers` : "");
  return `${bits.join(" ")}${extra ? ` <span class="muted">· ${esc(extra)}</span>` : ""}`;
}

function renderFeed() {
  const f = state.feedFilter;
  const rows = state.events.filter((e) =>
    f === "all" ? true : f === "orders" ? ORDER_KINDS.has(e.kind) : e.kind.startsWith("STOP"));
  $("#feed").innerHTML = rows.length
    ? rows.slice(0, 40).map((e) => `<li data-tone="${eventTone(e)}"><time>${time(e.ts)}</time><span class="dot"></span>
        <div><span class="kind">${KIND_LABEL[e.kind] || esc(e.kind)}</span>${describe(e)}</div></li>`).join("")
    : '<li class="empty">No activity yet.</li>';
}

function renderRecon() {
  const r = state.recon;
  $("#recon").innerHTML = r
    ? `<strong>Last reconciliation</strong> <span class="num muted">${dateShort(r.ts)} ${time(r.ts)}</span> · ${esc(r.outcome)}
       ${r.discrepancies.length ? `<ul>${r.discrepancies.slice(0, 4).map((d) => `<li>${esc(d.symbol || "*")}: ${esc(d.kind.replaceAll("_", " "))} → ${esc(d.resolution)}</li>`).join("")}</ul>` : ""}`
    : '<span class="muted">No reconciliation recorded yet.</span>';
}

// ------------------------------------------------------------------ data refresh
async function refreshSlow() {
  const [equity, trades, signals, recon] = await Promise.all([
    getJSON("/equity"), getJSON("/trades?limit=200"), getJSON("/signals"),
    getJSON("/reconciliations?limit=1").catch(() => []),
  ]);
  state.equity = equity;
  state.trades = trades;
  state.signals = signals;
  state.recon = recon[0] || null;
  renderEquityPanel();
  renderHero();
  renderBook();
  renderRecon();
  renderRisk();
}

function onSnapshot(snap) {
  state.overview = snap.overview;
  state.positions = snap.positions;
  state.events = snap.events;
  renderStatus(snap.overview);
  renderHero();
  renderBlocks();
  if (state.book === "open") renderBook();
  renderRisk();
  renderFeed();
  if (snap.data_version !== state.version) {
    state.version = snap.data_version;
    refreshSlow().catch((err) => console.error(err));
  }
}

function onStream(s) {
  if (s === "open") setPill("#pill-stream", "good", "Live");
  else if (s === "connecting") setPill("#pill-stream", "idle", "Connecting…");
  else setPill("#pill-stream", "bad", "Offline · retrying");
}

// ------------------------------------------------------------------ chat
function addMessage(role, html) {
  const li = document.createElement("li");
  li.className = `msg msg--${role}`;
  li.innerHTML = html;
  $("#messages").append(li);
  li.scrollIntoView({ block: "end", behavior: "smooth" });
  return li;
}

async function ask(text) {
  const question = text.trim();
  if (!question) return;
  $("#suggestions").hidden = true;
  addMessage("user", esc(question));
  const pending = addMessage("bot", '<span class="typing" aria-label="Thinking"><i></i><i></i><i></i></span>');
  const send = $(".send");
  send.disabled = true;
  try {
    const res = await postJSON("/chat", { message: question, session_id: state.sessionId });
    state.sessionId = res.session_id;
    try { if (res.session_id) sessionStorage.setItem("chat-session", res.session_id); } catch { /* ignore */ }
    pending.innerHTML = miniMarkdown(res.answer || "(empty answer)");
  } catch (err) {
    pending.classList.add("msg--error");
    pending.textContent = `Could not answer: ${err.message}`;
  } finally {
    send.disabled = false;
  }
}

function initChat() {
  const form = $("#composer");
  const input = $("#prompt");
  const autosize = () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 140)}px`;
  };
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      form.requestSubmit();
    }
  });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value;
    input.value = "";
    autosize();
    ask(text);
  });
  $("#suggestions").addEventListener("click", (e) => {
    if (e.target instanceof HTMLButtonElement) ask(e.target.textContent);
  });
}

// ------------------------------------------------------------------ controls
function tabs(container, attr, onSelect) {
  container.addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    container.querySelectorAll("button").forEach((b) => b.setAttribute("aria-selected", String(b === btn)));
    onSelect(btn.dataset[attr]);
  });
}

function marketStatus(now) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(now).map((p) => [p.type, p.value]));
  const minutes = Number(parts.hour) * 60 + Number(parts.minute);
  const weekday = !["Sat", "Sun"].includes(parts.weekday);
  const open = 9 * 60 + 30;
  const close = 16 * 60;
  if (weekday && minutes >= open && minutes < close) {
    const left = close - minutes;
    return `NYSE open · closes in ${Math.floor(left / 60)}h ${String(left % 60).padStart(2, "0")}m`;
  }
  return weekday && minutes < open ? "NYSE pre-market · opens 09:30 ET" : "NYSE closed";
}

function initControls() {
  tabs($("#range"), "range", (v) => {
    state.range = Number(v);
    renderEquityPanel();
  });
  tabs($("#book-tabs"), "tab", (v) => {
    state.book = v;
    renderBook();
  });
  tabs($("#block-filter"), "block", (v) => {
    state.blockFilter = Number(v);
    renderBook();
  });
  tabs($("#feed-filter"), "filter", (v) => {
    state.feedFilter = v;
    renderFeed();
  });

  let mode = "dark";
  try { mode = localStorage.getItem("theme") || "dark"; } catch { /* storage unavailable */ }
  const apply = () => {
    document.documentElement.dataset.theme = mode;
    $("#theme-toggle").title = `Theme: ${mode}`;
    renderEquityPanel();
    renderRisk();
  };
  apply();
  $("#theme-toggle").addEventListener("click", () => {
    mode = mode === "dark" ? "light" : "dark";
    try { localStorage.setItem("theme", mode); } catch { /* ignore */ }
    apply();
  });

  const tick = () => {
    const now = new Date();
    $("#clock").textContent = `${now.toLocaleTimeString("en-GB", { timeZone: "America/New_York" })} ET`;
    $("#market").textContent = marketStatus(now);
  };
  tick();
  setInterval(tick, 1000);
}

// ------------------------------------------------------------------ boot
initControls();
initChat();
await ready();
subscribe(onSnapshot, onStream);
