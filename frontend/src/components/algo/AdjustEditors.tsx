// Option adjustments on a candle's close (guide §5.1, §6.4): a leg re-entering
// after its own stop, a tested leg rolled out to a lower delta, and a futures
// delta hedge on the whole position. Absent = off: these editors leave the
// keys out of the spec until a value is set.

import { NumInput } from "./NumInput";

export type RollCfg = { above: number; to: number; max: number };
export type HedgeCfg = { above: number; max: number };
type Adjustable = { action: string; reentries?: number; roll?: RollCfg };

export const DEFAULT_ROLL: RollCfg = { above: 0.45, to: 0.25, max: 1 };
export const DEFAULT_HEDGE: HedgeCfg = { above: 0.5, max: 10 };

/** The leg with `reentries` set to n (0-5), or the key removed for 0 / empty. */
export function withReentries<T extends Adjustable>(lg: T, n: number | null): T {
  const { reentries: _old, ...rest } = lg;
  const k = Math.max(0, Math.min(5, Math.floor(n ?? 0)));
  return (k ? { ...rest, reentries: k } : rest) as T;
}

/** The leg with its roll patched (kept valid: 0 < to < above < 1, max 1-5), or removed with null. */
export function withRoll<T extends Adjustable>(lg: T, patch: Partial<RollCfg> | null): T {
  const { roll: old, ...rest } = lg;
  if (patch === null) return rest as T;
  const r = { ...DEFAULT_ROLL, ...old, ...patch };
  const above = Math.min(0.99, Math.max(0.02, r.above));
  const to = Math.min(above - 0.01, Math.max(0.01, r.to));
  return { ...rest, roll: { above: +above.toFixed(2), to: +to.toFixed(2), max: Math.max(1, Math.min(5, Math.floor(r.max))) } } as T;
}

export function LegAdjustEditor<T extends Adjustable>({ lg, onChange }: { lg: T; onChange: (lg: T) => void }) {
  return (
    <>
      <label className="meta" data-testid="leg-reentries"
        title="after this leg's own stop (SL %) closes it, open the same leg again on the next candle close — strike re-picked at the current spot — up to this many times per position (0-5). A target never re-enters">
        re-enter<NumInput width={40} allowEmpty placeholder="off" value={lg.reentries ?? null} onChange={(n) => onChange(withReentries(lg, n))} />×
      </label>
      <label className="meta" data-testid="leg-roll"
        title="when this leg's |delta| (per unit) rises above the first number on a candle close, close it and open the same leg at the strike nearest the second |delta| — at most the third number of times per position">
        <input type="checkbox" checked={!!lg.roll} onChange={(e) => onChange(withRoll(lg, e.target.checked ? {} : null))} /> roll
      </label>
      {lg.roll && (
        <span className="meta" data-testid="leg-roll-fields">
          |Δ| &gt;<NumInput width={48} value={lg.roll.above} onChange={(n) => onChange(withRoll(lg, { above: n ?? DEFAULT_ROLL.above }))} />
          → <NumInput width={48} value={lg.roll.to} onChange={(n) => onChange(withRoll(lg, { to: n ?? DEFAULT_ROLL.to }))} />
          max<NumInput width={32} value={lg.roll.max} onChange={(n) => onChange(withRoll(lg, { max: n ?? 1 }))} />
        </span>
      )}
    </>
  );
}

export function HedgeEditor({ hedge, onChange }: { hedge: HedgeCfg | undefined; onChange: (h: HedgeCfg | undefined) => void }) {
  return (
    <div className="algo-operand" style={{ gap: 8, marginTop: 8 }} data-testid="hedge-editor">
      <label className="meta" title="on each candle's close, when the position's net delta per set passes this many lots, add a futures leg of −(total delta) lots (rounded) to bring it back near neutral">
        <input type="checkbox" checked={!!hedge} onChange={(e) => onChange(e.target.checked ? DEFAULT_HEDGE : undefined)} /> delta-hedge with futures
      </label>
      {hedge && (
        <>
          <label className="meta">when |delta| &gt;<NumInput width={44} value={hedge.above} onChange={(n) => onChange({ ...hedge, above: Math.max(0.01, n ?? DEFAULT_HEDGE.above) })} /> lots per set</label>
          <label className="meta">at most <NumInput width={36} value={hedge.max} onChange={(n) => onChange({ ...hedge, max: Math.max(1, Math.min(50, Math.floor(n ?? 10))) })} /> hedges per position</label>
          <span className="meta">re-entries and rolls stop at the entry window's end; the hedge runs until square-off</span>
        </>
      )}
    </div>
  );
}
