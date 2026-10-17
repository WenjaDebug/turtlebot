// REST client and a self-healing WebSocket subscription.

const BASE = "/api/v1";

export async function getJSON(path) {
  const res = await fetch(`${BASE}${path}`, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return res.json();
}

export async function postJSON(path, body) {
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
