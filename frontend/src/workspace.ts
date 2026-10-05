// Workspace = one browser tab's Trade-page view, identified in the URL the way
// TradingView puts the layout id in /chart/<id>/:  #/trade?ws=<id>.
//
// - Refresh keeps the URL → same id → same view (symbol, scalper, layout...).
// - Another tab has its own id → its own view; nothing is shared but the
//   "last used" seed a brand-new tab starts from.
// - A duplicated tab (or a pasted URL) arrives with an id another live tab
//   already holds: it forks to a fresh id, copying that state, so the two
//   tabs diverge from then on (live tabs answer a BroadcastChannel ping).

const ID_RE = /^[a-z0-9]{4,16}$/;
const INDEX_KEY = "ws:index";          // id -> last-used ms, for pruning
const KEEP = 20;

let current = "";

export function wsId(): string {
  return current;
}

function fromHash(): string | null {
  const q = window.location.hash.split("?")[1] ?? "";
  const v = new URLSearchParams(q).get("ws");
  return v && ID_RE.test(v) ? v : null;
}

/** Put ?ws=<id> on the current hash without adding a history entry. */
export function withWs(hash: string): string {
  const [path, q = ""] = hash.replace(/^#/, "").split("?");
  const p = new URLSearchParams(q);
  p.set("ws", current);
  return `#${path || "/"}?${p.toString()}`;
}

function newId(): string {
  return Math.random().toString(36).slice(2, 10).replace(/[^a-z0-9]/g, "x").padEnd(6, "x");
}

function copyState(from: string, to: string): void {
  try {
    const suffix = `@${from}`;
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k && k.endsWith(suffix)) localStorage.setItem(k.slice(0, -suffix.length) + `@${to}`, localStorage.getItem(k) ?? "");
    }
  } catch { /* storage off */ }
}

function touchIndex(): void {
  try {
    const idx = JSON.parse(localStorage.getItem(INDEX_KEY) ?? "{}") as Record<string, number>;
    idx[current] = Date.now();
    const ids = Object.keys(idx).sort((a, b) => idx[b] - idx[a]);
    for (const old of ids.slice(KEEP)) {           // forget long-closed tabs' views
      delete idx[old];
      const suffix = `@${old}`;
      const drop: string[] = [];
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (k && k.endsWith(suffix)) drop.push(k);
      }
      drop.forEach((k) => localStorage.removeItem(k));
    }
    localStorage.setItem(INDEX_KEY, JSON.stringify(idx));
  } catch { /* storage off */ }
}

/** Resolve this tab's workspace id before the app renders. */
export async function initWorkspace(): Promise<void> {
  let id = fromHash();
  if (id && typeof BroadcastChannel !== "undefined") {
    // Is another LIVE tab already using this id? (A refresh's old page is gone.)
    const ch = new BroadcastChannel("tradebot-ws");
    const taken = await new Promise<boolean>((resolve) => {
      const t = setTimeout(() => resolve(false), 120);
      ch.onmessage = (e) => {
        if (e.data?.type === "here" && e.data.id === id) {
          clearTimeout(t);
          resolve(true);
        }
      };
      ch.postMessage({ type: "who", id });
    });
    ch.close();
    if (taken) {
      const fresh = newId();
      copyState(id, fresh);
      id = fresh;
    }
  }
  current = id ?? newId();
  window.history.replaceState(null, "", withWs(window.location.hash || "#/dashboard"));
  touchIndex();
  if (typeof BroadcastChannel !== "undefined") {
    const live = new BroadcastChannel("tradebot-ws");
    live.onmessage = (e) => {
      if (e.data?.type === "who" && e.data.id === current) live.postMessage({ type: "here", id: current });
    };
  }
}
