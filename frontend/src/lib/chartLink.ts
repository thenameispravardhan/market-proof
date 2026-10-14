// Link a symbol to the Trade page (#/trade?symbol=…&at=…) in a new tab, with
// the chart centred on a time. Timeframe: Settings → "Symbol-link chart timeframe".

import type { InstrumentHit } from "../types";

export const CHART_LINK_IV_KEY = "chart:linkIv";

/** A trade the link was opened for: drawn on the chart as one arrow per leg
 *  (the entry, and the exit once it's closed). Times are epoch seconds. */
export interface ChartEntry {
  side: "BUY" | "SELL";
  qty: number;
  price: number;
  at?: number;
  exitPrice?: number;
  exitAt?: number;
}

export function chartLinkIv(): string {
  try {
    return localStorage.getItem(CHART_LINK_IV_KEY) || "5";
  } catch {
    return "5";
  }
}

/** ISO time (naive = UTC, as the API sends it) → epoch seconds. */
function epoch(iso: string | null | undefined): number | undefined {
  if (!iso) return undefined;
  const s = iso.trim();
  const t = Date.parse(/Z$|[+-]\d{2}:?\d{2}$/.test(s) ? s : `${s}Z`);
  return Number.isNaN(t) ? undefined : Math.floor(t / 1000);
}

/** `at`: ISO time. A bare name stays bare: the Trade page resolves it to its
 *  real listing (BSE-only scrips, -BE series), where forcing "NSE:X-EQ" made
 *  Fyers answer "Invalid symbol". */
export function chartHref(
  symbol: string,
  at?: string | null,
  trade?: { side: "BUY" | "SELL"; qty: number; price: number; exitPrice?: number | null; exitTime?: string | null } | null,
): string {
  const q = new URLSearchParams({ symbol: symbol.trim().toUpperCase(), iv: chartLinkIv() });
  const t = epoch(at);
  if (t) q.set("at", String(t));
  if (trade && trade.price > 0) {
    q.set("side", trade.side);
    q.set("qty", String(trade.qty));
    q.set("px", String(trade.price));
    const xt = epoch(trade.exitTime);
    if (xt && trade.exitPrice) {
      q.set("xpx", String(trade.exitPrice));
      q.set("xt", String(xt));
    }
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
  const at = Number(q.get("at")) || undefined;
  const px = Number(q.get("px"));
  return {
    hit: { symbol, short_name: name, exchange: symbol.includes(":") ? symbol.split(":")[0] : "", segment: "", instrument_type: "", lot_size: 1, tick_size: 0.05, expiry: null, strike: null, underlying: null, display: name } as InstrumentHit,
    iv: q.get("iv"),
    at,
    entry: px > 0 ? {
      side: q.get("side") === "SELL" ? "SELL" : "BUY", qty: Number(q.get("qty")) || 0, price: px, at,
      exitPrice: Number(q.get("xpx")) || undefined, exitAt: Number(q.get("xt")) || undefined,
    } : undefined,
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
