// Link a symbol to the Trade page (#/trade?symbol=…&at=…) in a new tab, with
// the chart centred on a time. Timeframe: Settings → "Symbol-link chart timeframe".

import type { InstrumentHit } from "../types";

export const CHART_LINK_IV_KEY = "chart:linkIv";

/** A trade the link was opened for: drawn on the chart as its entry line. */
export interface ChartEntry {
  side: "BUY" | "SELL";
  qty: number;
  price: number;
}

export function chartLinkIv(): string {
  try {
    return localStorage.getItem(CHART_LINK_IV_KEY) || "5";
  } catch {
    return "5";
  }
}

/** `at`: ISO time (naive = UTC, as the API sends it). A bare name stays bare:
 *  the Trade page resolves it to its real listing (BSE-only scrips, -BE
 *  series), where forcing "NSE:X-EQ" made Fyers answer "Invalid symbol". */
export function chartHref(symbol: string, at?: string | null, entry?: ChartEntry | null): string {
  const q = new URLSearchParams({ symbol: symbol.trim().toUpperCase(), iv: chartLinkIv() });
  if (at) {
    const s = /Z$|[+-]\d{2}:?\d{2}$/.test(at.trim()) ? at.trim() : `${at.trim()}Z`;
    const t = Date.parse(s);
    if (!Number.isNaN(t)) q.set("at", String(Math.floor(t / 1000)));
  }
  if (entry && entry.price > 0) {
    q.set("side", entry.side);
    q.set("qty", String(entry.qty));
    q.set("px", String(entry.price));
  }
  return `${window.location.pathname}#/trade?${q.toString()}`;
}

/** What a chartHref link asked the Trade page for, or null. A bare name
 *  comes back as `hit.symbol` without an exchange: pass it to resolveBareHit. */
export function chartLinkParams(): { hit: InstrumentHit; iv: string | null; at: number | undefined; entry: ChartEntry | undefined } | null {
  const q = new URLSearchParams(window.location.hash.split("?")[1] ?? "");
  const symbol = (q.get("symbol") ?? "").trim();
  if (!symbol || q.get("popout") === "1") return null;
  const name = symbol.split(":").pop()!.replace(/-EQ$/, "");
  const px = Number(q.get("px"));
  return {
    hit: { symbol, short_name: name, exchange: symbol.includes(":") ? symbol.split(":")[0] : "", segment: "", instrument_type: "", lot_size: 1, tick_size: 0.05, expiry: null, strike: null, underlying: null, display: name } as InstrumentHit,
    iv: q.get("iv"),
    at: Number(q.get("at")) || undefined,
    entry: px > 0 ? { side: q.get("side") === "SELL" ? "SELL" : "BUY", qty: Number(q.get("qty")) || 0, price: px } : undefined,
  };
}

/** A bare name ("JSTL") → its real listing, by the same rule as the backend's
 *  resolve_fyers_symbol (the one the bot priced and traded with): exact NSE
 *  match, else any exact match, else the first hit. Falls back to NSE EQ. */
export async function resolveBareHit(name: string): Promise<InstrumentHit> {
  const n = name.trim().toUpperCase();
  try {
    const r = await fetch(`/api/search/symbols?q=${encodeURIComponent(n)}&segment=EQ&limit=8`);
    const hits: InstrumentHit[] = r.ok ? (await r.json()).hits ?? [] : [];
    const exact = hits.filter((h) => h.short_name.toUpperCase() === n);
    const pick = exact.find((h) => h.exchange === "NSE") ?? exact[0] ?? hits[0];
    if (pick) return pick;
  } catch { /* offline: fall through */ }
  return { symbol: `NSE:${n}-EQ`, short_name: n, exchange: "NSE", segment: "", instrument_type: "", lot_size: 1, tick_size: 0.05, expiry: null, strike: null, underlying: null, display: n } as InstrumentHit;
}
