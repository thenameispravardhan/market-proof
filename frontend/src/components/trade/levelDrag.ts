/** Pure helpers for dragging a position's stop-loss / target on the chart. */

/** Pixels the pointer must travel before a press on a tag counts as a drag
 *  (a plain click must never move, or create, a live exit). */
export const LEVEL_DRAG_SLOP = 3;

/** Snap a dragged price to the instrument's tick (NSE: 0.05), so the
 *  broker doesn't reject a level like 123.47. */
export function snapToTick(price: number, tick: number): number {
  const t = tick > 0 ? tick : 0.05;
  const decimals = Math.max(0, Math.min(6, Math.ceil(-Math.log10(t) - 1e-9)));
  return Number((Math.round(price / t) * t).toFixed(decimals));
}

/** Why a new stop-loss / target can't be saved, or null when it can.
 *  A level on the wrong side of the market would exit on the next tick. */
export function levelProblem(which: "sl" | "tp", price: number, ltp: number | null, long: boolean, fmt: (v: number) => string = (v) => v.toFixed(2)): string | null {
  if (!(price > 0)) return `${which === "sl" ? "Stop-loss" : "Target"} must be above zero — not changed`;
  if (ltp == null) return null;
  const below = which === "sl" ? long : !long;
  if (below ? price >= ltp : price <= ltp) {
    return `${which === "sl" ? "Stop-loss" : "Target"} must be ${below ? "below" : "above"} the LTP ${fmt(ltp)} for a ${long ? "long" : "short"} — not changed`;
  }
  return null;
}
