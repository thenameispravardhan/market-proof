// Link a symbol to the Trade page (#/trade?symbol=…&at=…) in a new tab, with
// the chart centred on a time. Timeframe: Settings → "Symbol-link chart timeframe".

import type { InstrumentHit } from "../types";

export const CHART_LINK_IV_KEY = "chart:linkIv";

export function chartLinkIv(): string {
  try {
    return localStorage.getItem(CHART_LINK_IV_KEY) || "5";
  } catch {
    return "5";
  }
}

/** `at`: ISO time (naive = UTC, as the API sends it). */
export function chartHref(symbol: string, at?: string | null): string {
  // ponytail: bare names (announcements) map to NSE equity; a BSE-only
  // scrip would need an instrument-master lookup here.
  const sym = symbol.includes(":") ? symbol.toUpperCase() : `NSE:${symbol.trim().toUpperCase()}-EQ`;
  const q = new URLSearchParams({ symbol: sym, iv: chartLinkIv() });
  if (at) {
    const s = /Z$|[+-]\d{2}:?\d{2}$/.test(at.trim()) ? at.trim() : `${at.trim()}Z`;
    const t = Date.parse(s);
    if (!Number.isNaN(t)) q.set("at", String(Math.floor(t / 1000)));
  }
  return `${window.location.pathname}#/trade?${q.toString()}`;
}

/** What a chartHref link asked the Trade page for, or null. */
export function chartLinkParams(): { hit: InstrumentHit; iv: string | null; at: number | undefined } | null {
  const q = new URLSearchParams(window.location.hash.split("?")[1] ?? "");
  const symbol = (q.get("symbol") ?? "").trim();
  if (!symbol || q.get("popout") === "1") return null;
  const name = symbol.split(":").pop()!.replace(/-EQ$/, "");
  return {
    hit: { symbol, short_name: name, exchange: symbol.split(":")[0], segment: "", instrument_type: "", lot_size: 1, tick_size: 0.05, expiry: null, strike: null, underlying: null, display: name } as InstrumentHit,
    iv: q.get("iv"),
    at: Number(q.get("at")) || undefined,
  };
}
