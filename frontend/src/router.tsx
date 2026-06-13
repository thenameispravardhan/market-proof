// Hash-based router: the active tab is determined by window.location.hash.
// e.g. "#/dashboard", "#/prompts", "#/rules", etc.
// Default tab: "dashboard".

import { useState, useEffect } from "react";

export type TabKey =
  | "dashboard"
  | "trades"
  | "prompts"
  | "rules"
  | "strategies"
  | "accounts"
  | "notifications"
  | "webhooks"
  | "backtest"
  | "settings";

const VALID_TABS = new Set<TabKey>([
  "dashboard",
  "trades",
  "prompts",
  "rules",
  "strategies",
  "accounts",
  "notifications",
  "webhooks",
  "backtest",
  "settings",
]);

function parseHash(): TabKey {
  const hash = window.location.hash.replace(/^#\/?/, "");
  return VALID_TABS.has(hash as TabKey) ? (hash as TabKey) : "dashboard";
}

export function useRouter(): [TabKey, (tab: TabKey) => void] {
  const [tab, setTab] = useState<TabKey>(parseHash);

  useEffect(() => {
    const handler = () => setTab(parseHash());
    window.addEventListener("hashchange", handler);
    return () => window.removeEventListener("hashchange", handler);
  }, []);

  const navigate = (t: TabKey) => {
    window.location.hash = `/${t}`;
  };

  return [tab, navigate];
}
