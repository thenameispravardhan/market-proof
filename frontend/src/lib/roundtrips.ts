// Shared round-trip reconstruction used by both the Trade History page
// and the dashboard P&L chart, so the two views compute realised P&L
// from the exact same source of truth (they used to disagree because
// the chart summed raw per-trade pnl while Trade History paired legs
// into FIFO round-trips).
//
// The entry leg (BUY, no realised P&L) and the exit leg (SELL carrying
// realised P&L) of the same position pair into one round-trip. A
// position that hasn't been closed yet shows as OPEN with no exit/P&L.

import type { BrokerAccount, Trade } from "../types";

export interface RoundTrip {
  key: string;
  symbol: string;
  isPaper: boolean;
  accountName: string;
  side: string; // BUY (long) / SELL (short) — the opening side
  quantity: number;
  entryPrice: number | null;
  exitPrice: number | null;
  entryTime: string | null;
  exitTime: string | null;
  pnl: number | null;
  open: boolean;
  // The trade rows behind this row: [entry] or [entry, exit]. Lets the
  // history page delete a round-trip without re-deriving the pairing.
  tradeIds: number[];
}

export function acctMeta(
  t: Trade | undefined,
  byId: Map<number, BrokerAccount>
): { isPaper: boolean; accountName: string } {
  const acct = t?.broker_account_id != null ? byId.get(t.broker_account_id) : undefined;
  const isPaper = acct
    ? acct.paper_mode
    : (t?.broker_order_id?.startsWith("PAPER-") ?? true);
  const accountName = acct && !acct.paper_mode ? acct.name : isPaper ? "Paper" : "Fyers";
  return { isPaper, accountName };
}

// Pair entry + exit legs into round-trips via FIFO, keyed on SYMBOL alone.
// We can't key on signal_id or account: auto-pipeline exit legs (the SELL
// the trade manager issues) carry neither the original signal_id nor the
// account, so symbol is the only reliable join. A BUY opens a lot; a later
// opposite-side SELL closes it (and vice-versa for shorts). Leftover lots
// stay OPEN.
export function buildRoundTrips(
  trades: Trade[],
  byId: Map<number, BrokerAccount>
): RoundTrip[] {
  const filled = trades.filter((t) => t.status === "filled");
  const groups = new Map<string, Trade[]>();
  for (const t of filled) {
    (groups.get(t.symbol) ?? groups.set(t.symbol, []).get(t.symbol)!).push(t);
  }
  const out: RoundTrip[] = [];
  for (const [, legs] of groups) {
    legs.sort((a, b) => (a.created_at ?? "").localeCompare(b.created_at ?? ""));
    const openLots: { side: string; qty: number; price: number; t: Trade }[] = [];
    let seq = 0;
    for (const t of legs) {
      let qty = Math.abs(t.quantity || 0);
      const exitQtyTotal = Math.abs(t.quantity || 0) || 1;
      while (qty > 0 && openLots.length > 0 && openLots[0].side !== t.side) {
        const lot = openLots[0];
        const matched = Math.min(qty, lot.qty);
        const long = lot.side === "BUY";
        const entryP = lot.price;
        const exitP = t.price ?? 0;
        const { isPaper, accountName } = acctMeta(lot.t, byId);
        // Prefer the broker's realised P&L on the exit leg (scaled to the
        // matched quantity); fall back to entry/exit price math.
        const pnl =
          t.pnl != null
            ? (t.pnl * matched) / exitQtyTotal
            : (long ? exitP - entryP : entryP - exitP) * matched;
        out.push({
          key: `r-${lot.t.id}-${t.id}-${seq++}`,
          symbol: t.symbol,
          isPaper,
          accountName,
          side: lot.side,
          quantity: matched,
          entryPrice: entryP,
          exitPrice: exitP,
          entryTime: lot.t.executed_at ?? lot.t.created_at ?? null,
          exitTime: t.executed_at ?? t.created_at ?? null,
          pnl,
          open: false,
          tradeIds: [lot.t.id, t.id],
        });
        lot.qty -= matched;
        qty -= matched;
        if (lot.qty === 0) openLots.shift();
      }
      if (qty > 0) openLots.push({ side: t.side, qty, price: t.price ?? 0, t });
    }
    for (const lot of openLots) {
      const { isPaper, accountName } = acctMeta(lot.t, byId);
      out.push({
        key: `o-${lot.t.id}`,
        symbol: lot.t.symbol,
        isPaper,
        accountName,
        side: lot.side,
        quantity: lot.qty,
        entryPrice: lot.price,
        exitPrice: null,
        entryTime: lot.t.executed_at ?? lot.t.created_at ?? null,
        exitTime: lot.t.executed_at ?? lot.t.created_at ?? null,
        pnl: null,
        open: true,
        tradeIds: [lot.t.id],
      });
    }
  }
  // Most recently closed first; open positions sink to the bottom.
  out.sort((a, b) => {
    if (a.open !== b.open) return a.open ? 1 : -1;
    return (b.exitTime ?? "").localeCompare(a.exitTime ?? "");
  });
  return out;
}
