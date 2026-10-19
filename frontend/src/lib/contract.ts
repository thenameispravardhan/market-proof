// Rupees per 1-point move per 1 qty. Indian instruments: 1 (qty is in the quoted unit; MCX and currency lots
// are counted that way too: GOLD 1 lot = 100 x 10 g). Crypto: prices are USDT and a paper contract is a fixed
// slice of a coin, booked in rupees at the server's USD/INR (GET /api/market/fx) — the same rule the paper
// engine uses, so the P&L on screen matches the book.

let fx: { usd_inr: number; units: Record<string, number> } | null = null;
let loading: Promise<void> | null = null;

export function loadFx(): Promise<void> {
  if (!loading) loading = fetch("/api/market/fx")
    .then((r) => r.json())
    .then((j) => { if (j && typeof j.usd_inr === "number") fx = { usd_inr: j.usd_inr, units: j.units ?? {} }; })
    .catch(() => { loading = null; });                     // retried on the next crypto lookup
  return loading;
}

export function pointValue(symbol: string | null | undefined): number {
  const s = (symbol ?? "").toUpperCase();
  if (!s.startsWith("CRYPTO:")) return 1;
  if (!fx) { void loadFx(); return 0; }                    // 0 until the rate is in: never a wrong rupee figure
  return (fx.units[s.slice(7)] ?? 1) * fx.usd_inr;
}

/** Test hook. */
export function setFx(v: { usd_inr: number; units: Record<string, number> } | null): void {
  fx = v;
  loading = null;
}
