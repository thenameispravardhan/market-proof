// Display-only "un-leveraged" (1×) quantity.
//
// The risk engine sizes positions against buying power =
// equity × INTRADAY_LEVERAGE (Fyers MIS ~5×), so a single position's
// raw share count can be up to `leverage`× what the base capital alone
// would buy. Operators read those raw counts as "too big" against the
// portfolio value, so the read-only trade/position tables show the 1×
// equivalent: qty ÷ leverage.
//
// IMPORTANT: this scales ONLY the displayed share count. P&L, entry,
// exit and every realised/unrealised number stay at full size — so the
// shown qty × price-move will NOT reconcile with the shown P&L. Nothing
// about the actual orders, fills, sizing, or accounting changes; this is
// purely how the number is printed. Leverage ≤ 1 (or missing) is a no-op.

export function displayQty(
  qty: number | null | undefined,
  leverage: number | null | undefined,
): number {
  if (qty == null || Number.isNaN(qty)) return 0;
  const lev = leverage && leverage > 1 ? leverage : 1;
  // Round to a whole share count; Math.round preserves the sign so
  // short positions (negative qty) stay negative.
  return Math.round(qty / lev);
}

// Whether the 1× display transform is actually active (leverage > 1).
// Used to show a small "(1×)" hint on the Qty column header so the
// scaled number isn't silently misread as the raw fill size.
export function isLeveraged(leverage: number | null | undefined): boolean {
  return !!leverage && leverage > 1;
}
