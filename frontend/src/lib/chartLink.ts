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

type LinkTrade = { side: "BUY" | "SELL"; qty: number; price: number; exitPrice?: number | null; exitTime?: string | null };

/** `at`: ISO time. A bare name stays bare: the Trade page resolves it to its
 *  real listing (BSE-only scrips, -BE series), where forcing "NSE:X-EQ" made
 *  Fyers answer "Invalid symbol". `opts.pair` opens a second chart beside the
 *  first (2-chart layout): an Algo Lab option trade, underlying left, option
 *  right with the trade's arrows. `opts.iv` overrides the Settings timeframe. */
export function chartHref(
  symbol: string,
  at?: string | null,
  trade?: LinkTrade | null,
  opts?: { iv?: string; pair?: { symbol: string; trade?: LinkTrade | null } },
): string {
  const q = new URLSearchParams({ symbol: symbol.trim().toUpperCase(), iv: opts?.iv ?? chartLinkIv() });
  const t = epoch(at);
  if (t) q.set("at", String(t));
  const put = (tr: LinkTrade | null | undefined, k: string) => {
    if (!tr || !(tr.price > 0)) return;
    q.set(`side${k}`, tr.side);
    q.set(`qty${k}`, String(tr.qty));
    q.set(`px${k}`, String(tr.price));
    const xt = epoch(tr.exitTime);
    if (xt && tr.exitPrice) {
      q.set(`xpx${k}`, String(tr.exitPrice));
      q.set(`xt${k}`, String(xt));
    }
  };
  put(trade, "");
  if (opts?.pair) {
    q.set("sym2", opts.pair.symbol.trim().toUpperCase());
    put(opts.pair.trade, "2");
  }
  return `${window.location.pathname}#/trade?${q.toString()}`;
}

/** What a chartHref link asked the Trade page for, or null. A bare name
 *  comes back as `hit.symbol` without an exchange: pass it to resolveBareHit. */
export function chartLinkParams(): {
  hit: InstrumentHit; iv: string | null; at: number | undefined; entry: ChartEntry | undefined;
  hit2?: InstrumentHit; entry2?: ChartEntry;
} | null {
  const q = new URLSearchParams(window.location.hash.split("?")[1] ?? "");
  const symbol = (q.get("symbol") ?? "").trim();
  if (!symbol || q.get("popout") === "1") return null;
  const at = Number(q.get("at")) || undefined;
  // lot 0 = unknown: the ticket blocks orders until the real lot loads (a
  // derivative opened from a link must never trade with a guessed lot of 1).
  const hitOf = (s: string, lot: number): InstrumentHit => {
    const name = s.split(":").pop()!.replace(/-EQ$/, "");
    return { symbol: s, short_name: name, exchange: s.includes(":") ? s.split(":")[0] : "", segment: "", instrument_type: "", lot_size: lot, tick_size: 0.05, expiry: null, strike: null, underlying: null, display: name } as InstrumentHit;
  };
  const entryOf = (k: string): ChartEntry | undefined => {
    const px = Number(q.get(`px${k}`));
    return px > 0 ? {
      side: q.get(`side${k}`) === "SELL" ? "SELL" : "BUY", qty: Number(q.get(`qty${k}`)) || 0, price: px, at,
      exitPrice: Number(q.get(`xpx${k}`)) || undefined, exitAt: Number(q.get(`xt${k}`)) || undefined,
    } : undefined;
  };
  const sym2 = (q.get("sym2") ?? "").trim();
  return {
    hit: hitOf(symbol, 1),
    iv: q.get("iv"),
    at,
    entry: entryOf(""),
    ...(sym2 ? { hit2: hitOf(sym2, 0), entry2: entryOf("2") } : {}),
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
