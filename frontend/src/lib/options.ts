// Option symbol helpers shared by the option chain and the scalper.
//
// Fyers option symbols are `<EXCH>:<ROOT><expiry code><strike><CE|PE>`, e.g.
// NSE:NIFTY25O1425000CE, NSE:BANKNIFTY25OCT55000PE, NSE:M&M25OCT3200CE. The
// ROOT is the F&O name, which for an index is NOT its cash symbol or its
// scrip-master short name (NIFTY50-INDEX trades as NIFTY, NIFTYBANK-INDEX
// as BANKNIFTY).

import type { InstrumentHit } from "../types";

const INDEX_ROOTS: Record<string, string> = {
  NIFTY50: "NIFTY",
  NIFTY: "NIFTY",
  NIFTYBANK: "BANKNIFTY",
  BANKNIFTY: "BANKNIFTY",
  FINNIFTY: "FINNIFTY",
  NIFTYFIN: "FINNIFTY",
  MIDCPNIFTY: "MIDCPNIFTY",
  NIFTYMIDSELECT: "MIDCPNIFTY",
  NIFTYNXT50: "NIFTYNXT50",
  SENSEX: "SENSEX",
  BANKEX: "BANKEX",
};

/** The option root of an underlying, e.g. `NSE:NIFTY50-INDEX` -> `NIFTY`,
 *  `NSE:BAJAJ-AUTO-EQ` -> `BAJAJ-AUTO`. */
export function optionRoot(h: Pick<InstrumentHit, "symbol" | "short_name">): string {
  const body = (h.symbol.includes(":") ? h.symbol.split(":")[1] : h.symbol).toUpperCase();
  if (body.endsWith("-INDEX")) {
    const name = body.slice(0, -"-INDEX".length);
    return INDEX_ROOTS[name] ?? INDEX_ROOTS[h.short_name.toUpperCase()] ?? name;
  }
  if (body.endsWith("-EQ")) return body.slice(0, -"-EQ".length);
  return h.short_name.toUpperCase();
}

/** True when `symbol` is an option (CE/PE) on `root` listed on `exchange`. */
export function isOptionOf(symbol: string, exchange: string, root: string): boolean {
  const prefix = `${exchange}:${root}`.toUpperCase();
  const s = symbol.toUpperCase();
  return s.startsWith(prefix) && /^\d/.test(s.slice(prefix.length)) && /\d(CE|PE)$/.test(s);
}

/** A chain expiry label as `YYYY-MM-DD`. Fyers labels read `DD-MM-YYYY`;
 *  the static master's are already ISO. Anything else gives null. */
export function expiryIso(label: string | null | undefined): string | null {
  if (!label) return null;
  const s = label.trim();
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s;
  const m = /^(\d{2})-(\d{2})-(\d{4})$/.exec(s);
  return m ? `${m[3]}-${m[2]}-${m[1]}` : null;
}
