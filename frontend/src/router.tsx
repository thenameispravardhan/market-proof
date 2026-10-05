// Hash-based router: the active tab is determined by window.location.hash.
// e.g. "#/dashboard", "#/prompts", "#/rules", etc.
// Default tab: "dashboard".

import { useState, useEffect } from "react";
import { withWs, wsId } from "./workspace";

export type TabKey =
  | "dashboard"
  | "trade"
  | "exits"
  | "timing"
  | "trades"
  | "outcomes"
  | "dataset"
  | "model"
  | "prompts"
  | "rules"
  | "strategies"
  | "algo"
  | "accounts"
  | "notifications"
  | "settings";

const VALID_TABS = new Set<TabKey>([
  "dashboard",
  "trade",
  "exits",
  "timing",
  "trades",
  "outcomes",
  "dataset",
  "model",
  "prompts",
  "rules",
  "strategies",
  "algo",
  "accounts",
  "notifications",
  "settings",
]);

// #/tab wins; else a path URL (/trade); else this browser tab's last page
// (sessionStorage is per tab, so several tabs each keep their own page across refresh).
function parseHash(): TabKey {
  const hash = window.location.hash.replace(/^#\/?/, "").split("?")[0];
  const path = window.location.pathname.replace(/^\/|\/$/g, "");
  let last: string | null = null;
  try { last = sessionStorage.getItem("route:last"); } catch { /* storage off */ }
  const t = [hash, path, last].find((x) => x && VALID_TABS.has(x as TabKey)) as TabKey | undefined;
  return t ?? "dashboard";
}

export function useRouter(): [TabKey, (tab: TabKey) => void] {
  const [tab, setTab] = useState<TabKey>(parseHash);

  useEffect(() => {
    const handler = () => setTab(parseHash());
    if (!window.location.hash.split("?")[0].replace(/^#\/?/, "")) window.history.replaceState(null, "", withWs(`#/${parseHash()}`));
    window.addEventListener("hashchange", handler);
    return () => window.removeEventListener("hashchange", handler);
  }, []);

  const navigate = (t: TabKey) => {
    window.location.hash = withWs(`#/${t}`).slice(1);   // keep this tab's workspace id in the URL
  };

  useEffect(() => {
    try { sessionStorage.setItem("route:last", tab); } catch { /* storage off */ }
  }, [tab]);

  return [tab, navigate];
}

/** useState that survives a refresh in THIS browser tab only (sessionStorage),
 *  so two tabs can sit on different sub-pages without fighting. */
export function useSessionState<T>(key: string, init: T): [T, (v: T) => void] {
  const [v, setV] = useState<T>(() => {
    try {
      const raw = sessionStorage.getItem(key);
      return raw == null ? init : (JSON.parse(raw) as T);
    } catch {
      return init;
    }
  });
  const set = (n: T) => {
    setV(n);
    try { sessionStorage.setItem(key, JSON.stringify(n)); } catch { /* storage off */ }
  };
  return [v, set];
}

// Trade-page state that must stay with ITS browser tab across a refresh (tab 1
// NIFTY chart, tab 2 BANKNIFTY chart, tab 3 SENSEX scalper never take each
// other's view). Read: this tab's copy first, else the last-used one (a new
// tab starts from where you left off). Write: both. Other keys pass through.
const PER_TAB = /^(trade:(last|scalper|layout|cells|chainBase|dock|dockSizes|bottomTab|bottomOpen|sync|splits)|chart:prefs)$/;

// Stored under the tab's workspace id from the URL (#/trade?ws=<id>, see
// workspace.ts); the plain key is the "last used" seed for a brand-new tab.
const nsKey = (k: string) => `${k}@${wsId()}`;

export const tabLocal = {
  getItem(k: string): string | null {
    try {
      if (PER_TAB.test(k) && wsId()) {
        const v = localStorage.getItem(nsKey(k)) ?? sessionStorage.getItem(k);   // sessionStorage: pre-workspace tabs
        if (v != null) return v;
      }
      return localStorage.getItem(k);
    } catch {
      return null;
    }
  },
  setItem(k: string, v: string): void {
    localStorage.setItem(k, v);
    if (PER_TAB.test(k) && wsId()) localStorage.setItem(nsKey(k), v);
  },
  removeItem(k: string): void {
    localStorage.removeItem(k);
    if (wsId()) localStorage.removeItem(nsKey(k));
  },
};
