// Hash-based router: the active tab is determined by window.location.hash.
// e.g. "#/dashboard", "#/prompts", "#/rules", etc.
// Default tab: "dashboard".

import { useState, useEffect } from "react";

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
    if (!window.location.hash) window.history.replaceState(null, "", `#/${parseHash()}`);
    window.addEventListener("hashchange", handler);
    return () => window.removeEventListener("hashchange", handler);
  }, []);

  const navigate = (t: TabKey) => {
    window.location.hash = `/${t}`;
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
