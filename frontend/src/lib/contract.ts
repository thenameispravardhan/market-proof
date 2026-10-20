// Rupees per 1-point move per 1 qty. Indian instruments: 1 (qty is in the quoted unit; MCX and currency lots
// are counted that way too: GOLD 1 lot = 100 x 10 g). Crypto: prices are USDT and a paper contract is a fixed
// slice of a coin, booked in rupees at the server's USD/INR (GET /api/market/fx) — the same rule the paper
// engine uses, so the P&L on screen matches the book. NSE's cross-currency pairs (EURUSD, GBPUSD, USDJPY) move
// in USD / JPY per unit, so they convert the same way (`cross`: rupees per point).

type Fx = { usd_inr: number; units: Record<string, number>; cross?: Record<string, number> };
let fx: Fx | null = null;
let loading: Promise<void> | null = null;
const CROSS = /^(?:NSE|BSE):(EURUSD|GBPUSD|USDJPY)/;

export function loadFx(): Promise<void> {
  if (!loading) loading = fetch("/api/market/fx")
    .then((r) => r.json())
    .then((j) => { if (j && typeof j.usd_inr === "number") fx = { usd_inr: j.usd_inr, units: j.units ?? {}, cross: j.cross ?? {} }; })
    .catch(() => { loading = null; });                     // retried on the next lookup
  return loading;
}

export function pointValue(symbol: string | null | undefined): number {
  const s = (symbol ?? "").toUpperCase();
  const cross = CROSS.exec(s);
  if (!cross && !s.startsWith("CRYPTO:")) return 1;
  if (!fx) { void loadFx(); return 0; }                    // 0 until the rate is in: never a wrong rupee figure
  if (cross) return fx.cross?.[cross[1]] ?? 0;
  return (fx.units[s.slice(7)] ?? 1) * fx.usd_inr;
}

/** Test hook. */
export function setFx(v: Fx | null): void {
  fx = v;
  loading = null;
}
