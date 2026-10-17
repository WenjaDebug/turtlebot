// turtlebot desk: wires the live snapshot stream to the UI.

import { getJSON, postJSON, subscribe } from "./api.js";
import { ready, renderBlocks, renderEquity } from "./charts.js";
import {
  ago, dateShort, esc, isNum, miniMarkdown, money, pct, price, qty, ratio, time, tone,
} from "./format.js";

const $ = (sel) => document.querySelector(sel);
const state = {
  version: null,
  equity: null,
  range: 0,
  book: "trades",
  trades: [],
  signals: [],
  seenEventId: 0,
  blocks: null,
  activityOpen: false,
  sessionId: sessionStorage.getItem("chat-session"),
};

// ------------------------------------------------------------------ header & status
function setPill(id, stateName, text) {
  const pill = $(id);
  pill.dataset.state = stateName;
  pill.querySelector(".pill__text").textContent = text;
}

function renderStatus(o) {
  const s = o.status;
  if (s.demo) setPill("#pill-engine", "idle", "Demo data");
  else if (s.running) setPill("#pill-engine", "good", `Engine running · ${s.mode}`);
  else setPill("#pill-engine", "idle", s.heartbeat ? `Engine paused · last seen ${ago(s.heartbeat)}` : "Engine never ran");

  const r = s.last_reconciliation;
  if (!r) setPill("#pill-recon", "idle", "No reconciliation yet");
  else {
    const st = r.outcome === "ok" ? (r.critical ? "warn" : "good") : r.outcome === "degraded" ? "warn" : "bad";
    const issues = r.discrepancies ? ` · ${r.discrepancies} note${r.discrepancies > 1 ? "s" : ""}` : "";
    setPill("#pill-recon", st, `Reconciliation ${r.outcome} ${time(r.ts)}${issues}`);
  }

  const banner = $("#banner");
  if (s.demo) {
    banner.hidden = false;
    banner.innerHTML = "<strong>Demo mode.</strong> All figures are synthetic and generated for illustration.";
  } else if (r && r.outcome !== "ok") {
    banner.hidden = false;
    banner.innerHTML = `<strong>Reconciliation ${esc(r.outcome)}.</strong> ${esc(r.message || "")} Stops were left untouched — check the Activity tab.`;
  } else banner.hidden = true;
}

function renderHero(o) {
  const a = o.account;
  const m = o.metrics;
  $("#netliq").textContent = a ? money(a.net_liquidation, { currency: a.currency }) : "—";
  const day = $("#daypnl");
  day.textContent = a && isNum(a.daily_pnl) ? `${money(a.daily_pnl, { signed: true })} today` : "— today";
  day.className = `num ${tone(a?.daily_pnl)}`;
  const tot = $("#totalret");
  tot.textContent = isNum(m.total_return) ? `${pct(m.total_return, { signed: true })} since start` : "— since start";
  tot.className = `num ${tone(m.total_return)}`;
  $("#asof").textContent = a ? `as of ${time(a.ts)} ET` : "";

  const k = {
    sharpe: ratio(m.sharpe),
    sortino: ratio(m.sortino),
    max_drawdown: pct(m.max_drawdown),
    current_drawdown: pct(m.current_drawdown),
    win_rate: isNum(m.trades.win_rate) ? `${pct(m.trades.win_rate)}` : "—",
    profit_factor: ratio(m.trades.profit_factor),
    exposure_usd: money(m.exposure_usd),
    open_risk_usd: money(m.open_risk_usd),
  };
  for (const [key, val] of Object.entries(k)) $(`[data-k="${key}"]`).textContent = val;
  $('[data-k="win_rate"]').title = `${m.trades.trades} closed trades`;
}

// ------------------------------------------------------------------ positions
function cushionCell(p) {
  const d = p.stop_distance_pct;
  if (!isNum(d)) return '<span class="muted">—</span>';
  const level = d < 0.03 ? "tight" : d < 0.06 ? "close" : "ok";
  const width = Math.max(2, Math.min(100, (d / 0.25) * 100));
  const label = level === "tight" ? "tight" : level === "close" ? "near" : "";
  return `<div class="cushion" data-level="${level}" title="${pct(d)} above the stop">
      <div class="cushion__track"><div class="cushion__fill" style="width:${width}%"></div></div>
      <span class="cushion__val num">${pct(d)}</span>
    </div>${label ? `<span class="visually-hidden">${label}</span>` : ""}`;
}

function renderPositions(rows) {
  const open = rows.filter((r) => r.status === "open").length;
  const pending = rows.length - open;
  $("#pos-count").textContent = `${open}${pending ? ` + ${pending} pending` : ""}`;
  const body = $("#positions tbody");
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="8" class="empty">No open positions.</td></tr>';
    return;
  }
  body.innerHTML = rows
    .map((p) => {
      const tags = [
        p.status === "pending" ? '<span class="tag">pending</span>' : "",
        p.missing_cycles ? `<span class="tag tag--bad" title="Not reported by the broker in ${p.missing_cycles} reconciliation(s)">unconfirmed</span>` : "",
        p.quote_stale && p.status === "open" ? '<span class="tag tag--warn" title="No live quote, valued at entry">no quote</span>' : "",
      ].join("");
      const pnl = isNum(p.unrealized_pnl)
        ? `<span class="${tone(p.unrealized_pnl)}">${pct(p.unrealized_pct, { signed: true })}</span> <span class="muted">${money(p.unrealized_pnl, { signed: true })}</span>`
        : '<span class="muted">—</span>';
      return `<tr>
        <td><span class="sym" data-block="${p.block}" title="Block ${p.block}">${esc(p.symbol)}</span>${tags}</td>
        <td class="r num">${qty(p.qty)}</td>
        <td class="r num">${price(p.entry_price)}</td>
        <td class="r num">${price(p.last_price)}</td>
        <td class="r num">${pnl}</td>
        <td>${cushionCell(p)}</td>
        <td class="r num">${price(p.stop)}</td>
        <td class="r num hide-sm">${p.days_held ?? "—"}</td>
      </tr>`;
    })
    .join("");
}

// ------------------------------------------------------------------ blocks
function renderBlockCards(blocks) {
  $("#blocks").innerHTML = blocks
    .map((b) => {
      const slots = Array.from({ length: b.max_positions }, (_, i) =>
        i < b.open_positions ? '<i class="on"></i>' : i < b.open_positions + b.pending_positions ? '<i class="pending"></i>' : "<i></i>",
      ).join("");
      const filter = b.min_strength != null ? `breakout ≥ ${pct(b.min_strength)}` : "no strength filter";
      return `<article class="block" data-block="${b.block}">
        <div class="block__title">Block ${b.block} · ${esc(b.label)}</div>
        <div class="block__sub">${filter}${b.enabled ? "" : " · new entries disabled"}</div>
        <dl class="block__rows">
          <dt>Unrealised</dt><dd class="num ${tone(b.unrealized_pnl)}">${money(b.unrealized_pnl, { signed: true })}</dd>
          <dt>Realised</dt><dd class="num ${tone(b.realized_pnl)}">${money(b.realized_pnl, { signed: true })}</dd>
          <dt>Closed trades</dt><dd class="num">${b.closed_trades}${isNum(b.win_rate) ? ` · ${pct(b.win_rate)} won` : ""}</dd>
          <dt>Slots</dt><dd class="num">${b.open_positions}/${b.max_positions}</dd>
        </dl>
        <div class="slots" aria-label="${b.open_positions} of ${b.max_positions} slots used">${slots}</div>
      </article>`;
    })
    .join("");
  renderBlocks($("#chart-blocks"), blocks);
}

// ------------------------------------------------------------------ book (trades / signals)
function renderBook() {
  const table = $("#book");
  if (state.book === "trades") {
    const rows = state.trades;
    table.innerHTML = `<thead><tr><th>Symbol</th><th class="r">Entry</th><th class="r">Exit</th><th class="r">Return</th><th class="r">P&amp;L</th><th class="r">Closed</th></tr></thead>
      <tbody>${
        rows.length
          ? rows.map((t) => `<tr>
              <td><span class="sym" data-block="${t.block}">${esc(t.symbol)}</span></td>
              <td class="r num">${price(t.entry_price)}</td>
              <td class="r num">${isNum(t.exit_price) ? price(t.exit_price) : '<span class="muted" title="Exit price not recorded by the legacy bot">n/a</span>'}</td>
              <td class="r num ${tone(t.return_pct)}">${pct(t.return_pct, { signed: true })}</td>
              <td class="r num ${tone(t.pnl)}">${money(t.pnl, { signed: true })}</td>
              <td class="r num muted">${dateShort(t.closed_at)}</td></tr>`).join("")
          : '<tr><td colspan="6" class="empty">No closed trades yet.</td></tr>'
      }</tbody>`;
  } else {
    const rows = state.signals;
    table.innerHTML = `<thead><tr><th>Symbol</th><th class="r">Strength</th><th class="r">Volume ×</th><th class="r">Close</th><th class="r">Initial stop</th><th class="r">Block 2</th></tr></thead>
      <tbody>${
        rows.length
          ? rows.map((s) => `<tr>
              <td><strong>${esc(s.symbol)}</strong></td>
              <td class="r num">${pct(s.strength, { signed: true })}</td>
              <td class="r num">${ratio(s.volume_ratio)}</td>
              <td class="r num">${price(s.price)}</td>
              <td class="r num">${price(s.stop)}</td>
              <td class="r">${s.strength >= 0.1 ? "eligible" : '<span class="muted">—</span>'}</td></tr>`).join("")
          : '<tr><td colspan="6" class="empty">No signals in the latest scan.</td></tr>'
      }</tbody>`;
  }
}

// ------------------------------------------------------------------ activity
const KIND_LABEL = {
  ORDER: "Order", FILL: "Fill", PARTIAL_FILL: "Partial fill", EXIT: "Exit", PARTIAL_EXIT: "Partial exit",
  CANCEL: "Cancel", SKIP: "Skipped", STOP_NEW: "New stop", STOP_UPDATE: "Stop trailed",
  STOP_LOST: "Stop lost", ADOPT: "Adopted", UNIVERSE: "Universe",
};

function describe(e) {
  const d = e.details || {};
  const bits = [];
  if (isNum(e.qty)) bits.push(qty(e.qty));
  if (e.symbol) bits.push(`<strong>${esc(e.symbol)}</strong>`);
  if (isNum(e.price)) bits.push(`@ ${price(e.price)}`);
  if (isNum(e.stop) && e.kind.startsWith("STOP")) bits.push(`stop ${price(e.stop)}`);
  const extra = d.change || d.reason || d.legacy || (isNum(d.return) ? `return ${pct(d.return, { signed: true })}` : "") ||
    (d.size ? `${d.size} eligible tickers` : "");
  return `${bits.join(" ")}${extra ? ` <span class="muted">· ${esc(extra)}</span>` : ""}`;
}

function renderFeed(events) {
  $("#feed").innerHTML = events.length
    ? events.map((e) => `<li><time>${time(e.ts)}</time><div><span class="kind">${KIND_LABEL[e.kind] || esc(e.kind)}</span><span class="detail">${describe(e)}</span></div></li>`).join("")
    : '<li class="empty">No activity yet.</li>';
  const newest = events[0]?.id || 0;
  const badge = $("#activity-badge");
  const unseen = events.filter((e) => e.id > state.seenEventId).length;
  if (state.activityOpen || !state.seenEventId) state.seenEventId = newest;
  else if (unseen) {
    badge.hidden = false;
    badge.textContent = unseen;
  }
}

async function renderRecon() {
  const runs = await getJSON("/reconciliations?limit=1").catch(() => []);
  const r = runs[0];
  $("#recon").innerHTML = r
    ? `<h3><span>Last reconciliation</span><span class="num muted">${dateShort(r.ts)} ${time(r.ts)}</span></h3>
       <div>${esc(r.outcome)} · ${r.n_local} tracked / ${r.n_broker} at broker · ${r.n_actions} action(s)</div>
       ${r.discrepancies.length ? `<ul>${r.discrepancies.slice(0, 6).map((d) => `<li>${esc(d.symbol || "*")}: ${esc(d.kind.replaceAll("_", " "))} → ${esc(d.resolution)}</li>`).join("")}</ul>` : ""}`
    : '<span class="muted">No reconciliation recorded yet.</span>';
}

// ------------------------------------------------------------------ data refresh
async function refreshSlow() {
  const [equity, trades, signals] = await Promise.all([
    getJSON("/equity"), getJSON("/trades?limit=200"), getJSON("/signals"),
  ]);
  state.equity = equity;
  state.trades = trades;
  state.signals = signals;
  const note = { account: "", realized: "No account snapshots yet — showing cumulative realised P&L of closed trades.", none: "" };
  $("#equity-note").textContent = note[equity.source] || "";
  renderEquity($("#chart-equity"), equity, state.range);
  renderBook();
  renderRecon();
}

function onSnapshot(snap) {
  renderStatus(snap.overview);
  renderHero(snap.overview);
  renderPositions(snap.positions);
  state.blocks = snap.overview.blocks;
  renderBlockCards(snap.overview.blocks);
  renderFeed(snap.events);
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
    if (res.session_id) sessionStorage.setItem("chat-session", res.session_id);
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

function initControls() {
  tabs($("#range"), "range", (v) => {
    state.range = Number(v);
    if (state.equity) renderEquity($("#chart-equity"), state.equity, state.range);
  });
  tabs($("#book-tabs"), "tab", (v) => {
    state.book = v;
    renderBook();
  });
  tabs($("#rail-tabs"), "pane", (v) => {
    state.activityOpen = v === "activity";
    $("#pane-chat").hidden = v !== "chat";
    $("#pane-activity").hidden = v !== "activity";
    if (state.activityOpen) $("#activity-badge").hidden = true;
  });

  const modes = ["auto", "light", "dark"];
  let mode = localStorage.getItem("theme") || "auto";
  const apply = () => {
    if (mode === "auto") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = mode;
    $("#theme-toggle").title = `Theme: ${mode}`;
    if (state.equity) renderEquity($("#chart-equity"), state.equity, state.range);
    if (state.blocks) renderBlocks($("#chart-blocks"), state.blocks);
  };
  try { apply(); } catch { /* storage unavailable */ }
  $("#theme-toggle").addEventListener("click", () => {
    mode = modes[(modes.indexOf(mode) + 1) % modes.length];
    try { localStorage.setItem("theme", mode); } catch { /* ignore */ }
    apply();
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", apply);

  const tick = () => {
    $("#clock").textContent = `${new Date().toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: "America/New_York" })} ET`;
  };
  tick();
  setInterval(tick, 15000);
}

// ------------------------------------------------------------------ boot
initControls();
initChat();
await ready();
subscribe(onSnapshot, onStream);
