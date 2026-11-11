// REST client and a self-healing WebSocket subscription. A static export (GitHub Pages demo)
// sets <html data-static="data">: requests then read frozen JSON files and nothing is pushed.

const BASE = "/api/v1";
const STATIC = document.documentElement.dataset.static || "";

/** True when the page is a static export without a server behind it. */
export const isStatic = Boolean(STATIC);

const url = (path) => (STATIC ? `${STATIC}/${path.replace(/^\//, "").split("?")[0]}.json` : `${BASE}${path}`);

export async function getJSON(path) {
  const res = await fetch(url(path), { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return res.json();
}

export async function postJSON(path, body) {
  if (STATIC) throw new Error("the assistant runs locally with Claude Code and is not part of the static demo");
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail ? String(data.detail) : `HTTP ${res.status}`);
  return data;
}

/**
 * Subscribe to snapshot pushes. Reconnects with exponential backoff (1 s … 30 s).
 * @param {(snapshot: object) => void} onSnapshot
 * @param {(state: "open"|"closed"|"connecting") => void} onState
 */
export function subscribe(onSnapshot, onState) {
  if (STATIC) {
    onState("static");
    getJSON("/stream").then(onSnapshot, (err) => { console.error(err); onState("closed"); });
    return;
  }
  let delay = 1000;
  let ws;
  const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${BASE}/stream`;

  const connect = () => {
    onState("connecting");
    ws = new WebSocket(url);
    ws.onopen = () => {
      delay = 1000;
      onState("open");
    };
    ws.onmessage = (ev) => {
      try {
        onSnapshot(JSON.parse(ev.data));
      } catch (err) {
        console.error("bad snapshot", err);
      }
    };
    ws.onclose = () => {
      onState("closed");
      setTimeout(connect, delay);
      delay = Math.min(delay * 2, 30000);
    };
  };
  connect();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && ws?.readyState === WebSocket.OPEN) ws.send("refresh");
  });
}
