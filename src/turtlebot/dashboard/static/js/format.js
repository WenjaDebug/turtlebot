// Number/date formatting and safe HTML helpers.

const nf = (opts) => new Intl.NumberFormat("en-US", opts);
const money0 = nf({ maximumFractionDigits: 0 });
const money2 = nf({ minimumFractionDigits: 2, maximumFractionDigits: 2 });
const pct1 = nf({ style: "percent", minimumFractionDigits: 1, maximumFractionDigits: 1 });
const dec2 = nf({ minimumFractionDigits: 2, maximumFractionDigits: 2 });

export const isNum = (v) => typeof v === "number" && Number.isFinite(v);
const MINUS = "−";
const sign = (v) => (v > 0 ? "+" : v < 0 ? MINUS : "");

export function money(v, { digits = 0, signed = false, currency = "" } = {}) {
  if (!isNum(v)) return "—";
  const body = (digits ? money2 : money0).format(Math.abs(v));
  const prefix = signed ? sign(v) : v < 0 ? MINUS : "";
  return `${prefix}${body}${currency ? ` ${currency}` : ""}`;
}

export function pct(v, { signed = false } = {}) {
  if (!isNum(v)) return "—";
  return `${signed ? sign(v) : v < 0 ? MINUS : ""}${pct1.format(Math.abs(v))}`;
}

export const ratio = (v) => (isNum(v) ? dec2.format(v) : "—");
export const price = (v) => (isNum(v) ? money2.format(v) : "—");
export const qty = (v) => (isNum(v) ? money0.format(v) : "—");

/** CSS class for a signed value (paired with an explicit +/− sign in the text). */
export const tone = (v) => (!isNum(v) || v === 0 ? "" : v > 0 ? "pos" : "neg");

export function time(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: "America/New_York" });
}

export function dateShort(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString("en-GB", { day: "2-digit", month: "short", timeZone: "America/New_York" });
}

export function ago(iso) {
  if (!iso) return "never";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400 * 2) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);

/** Minimal, escape-first markdown for chat answers: paragraphs, lists, **bold**, `code`. */
export function miniMarkdown(text) {
  const inline = (s) =>
    esc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  const blocks = String(text).trim().split(/\n{2,}/);
  return blocks
    .map((block) => {
      const lines = block.split("\n");
      if (lines.every((l) => /^\s*[-*•] /.test(l))) {
        return `<ul>${lines.map((l) => `<li>${inline(l.replace(/^\s*[-*•] /, ""))}</li>`).join("")}</ul>`;
      }
      return `<p>${lines.map(inline).join("<br>")}</p>`;
    })
    .join("");
}
