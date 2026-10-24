// footprint — order-flow analytics for the chart, as pure functions.
//
// The recorder stores, per minute, buy (lifted the ask) and sell (hit the bid)
// volume at every traded price. A footprint bar groups those into rows of N
// ticks and reads them the way ATAS / Sierra / TradingView do:
//   * diagonal imbalance — buying at a row against selling one row BELOW it
//     (an aggressive buyer lifts the ask, which sits a tick above the bid the
//     seller hits); selling at a row against buying one row ABOVE it. A side
//     wins at `ratio` x (300% by default) the other, with at least `minVol`;
//   * stacked imbalances — `stacked` or more in a row on one side: a zone the
//     chart extends to the right until price trades back into it;
//   * POC (the row with the most volume) and the value area (the rows around
//     it holding `vaPct`% of the bar's volume);
//   * an unfinished auction — both sides traded at the bar's extreme row: the
//     auction never found the price where one side stopped.
// Plus the plumbing: mapping the server's base-resolution rows onto the chart's
// candles (a 4-minute or weekly chart is built from 1-minute / daily rows) and
// the index-vs-future basis.

export type FootprintMode = "bidask" | "delta" | "volume" | "heatmap";
export type ProfileScope = "visible" | "session" | "composite";
export type ProfileType = "volume" | "delta" | "tpo";
export type StatRow = "vol" | "delta" | "dpct" | "cvd" | "dmin" | "dmax" | "trades" | "oi";

export interface FootprintSettings {
  mode: FootprintMode;
  /** Ticks per row; 0 = auto (rows at least 14 px tall). */
  rowTicks: number;
  /** Diagonal imbalance ratio (3 = 300%); 0 = off. */
  imbalance: number;
  /** An imbalance needs at least this much volume on the winning side. */
  imbalanceMin: number;
  /** Consecutive imbalances that make a zone; 0 = off. */
  stacked: number;
  poc: boolean;
  va: boolean;
  vaPct: number;
  stats: StatRow[];
  prints: boolean;
  unfinished: boolean;
  /** Volume profile of the visible bars on the pane's right edge, with its POC line. */
  profile: boolean;
  /** visible = one profile of the bars on screen at the right edge; session = one per day, from its first bar;
   *  composite = one of the last `compositeDays` sessions at the right edge. */
  profileScope: ProfileScope;
  compositeDays: number;
  /** volume = buy + sell per row; delta = buy − sell per row (coloured by side); tpo = time at price
   *  (candles whose range covered the row — from the candles alone, no ticks needed). */
  profileType: ProfileType;
  /** Developing POC / VAH / VAL: where each session's POC and value area stood after every bar. */
  devLines: boolean;
  /** How many of the latest candles get their footprint loaded (more = more to scroll back through, heavier). */
  historyBars: number;
  /** Absorption and delta-divergence markers. */
  signals: boolean;
  /** Each bar's POC extended to the right until price trades back through it (only the untested ones). */
  npoc: boolean;
  /** Numbers in the cells; off = colour only. */
  numbers: boolean;
}

export const DEFAULT_FOOTPRINT: FootprintSettings = {
  mode: "bidask",
  rowTicks: 0,
  imbalance: 3,
  imbalanceMin: 0,
  stacked: 3,
  poc: true,
  va: true,
  vaPct: 70,
  stats: ["vol", "delta", "dpct", "cvd"],
  prints: true,
  unfinished: true,
  profile: true,
  profileScope: "visible",
  compositeDays: 5,
  profileType: "volume",
  devLines: false,
  historyBars: 400,
  signals: true,
  npoc: false,
  numbers: true,
};

export const STAT_LABEL: Record<StatRow, string> = {
  vol: "Volume", delta: "Delta", dpct: "Delta %", cvd: "CVD", dmin: "Min Δ", dmax: "Max Δ", trades: "Trades", oi: "OI Δ",
};
/** Height of one statistics-table row at the pane's foot, in px. */
export const STAT_ROW_PX = 14;

export function fpSettings(raw: Partial<FootprintSettings> | undefined): FootprintSettings {
  const s = { ...DEFAULT_FOOTPRINT, ...(raw ?? {}) };
  return {
    ...s,
    mode: (["bidask", "delta", "volume", "heatmap"] as const).includes(s.mode) ? s.mode : DEFAULT_FOOTPRINT.mode,
    profileScope: s.profileScope === "session" || s.profileScope === "composite" ? s.profileScope : "visible",
    compositeDays: Math.min(30, Math.max(1, Math.round(Number(s.compositeDays) || DEFAULT_FOOTPRINT.compositeDays))),
    historyBars: Math.min(3000, Math.max(100, Math.round(Number(s.historyBars) || DEFAULT_FOOTPRINT.historyBars))),
    profileType: s.profileType === "delta" || s.profileType === "tpo" ? s.profileType : "volume",
    stats: Array.isArray(s.stats) ? s.stats.filter((x) => x in STAT_LABEL) : DEFAULT_FOOTPRINT.stats,
  };
}

/** A heat-map colour for `t` in 0..1: cold blue through green and yellow to hot red. */
export function heatColor(t: number, a = 1): string {
  const x = Math.min(1, Math.max(0, t));
  const hue = 230 - 230 * x;                  // 230 (blue) -> 0 (red)
  return `hsla(${Math.round(hue)}, 85%, ${Math.round(38 + 14 * x)}%, ${a})`;
}

/** Naked POCs: each bar's POC row [lo, lo + step) that no LATER bar has traded through.
 *  `bars` are oldest first: [high, low, pocRowLow | null]. Returns [bar index, pocRowLow]. */
export function nakedPocs(bars: [number, number, number | null][], step: number): [number, number][] {
  const out: [number, number][] = [];
  for (let i = 0; i < bars.length; i++) {
    const p = bars[i][2];
    if (p == null) continue;
    let hit = false;
    for (let j = i + 1; j < bars.length && !hit; j++) hit = bars[j][1] < p + step && bars[j][0] >= p;
    if (!hit) out.push([i, p]);
  }
  return out;
}

export interface FpRow {
  p: number;
  buy: number;
  sell: number;
}

export interface FpBarStats {
  vol: number;
  delta: number;
  trades: number;
  dlo: number;
  dhi: number;
  oi: number | null;
  oiChg: number | null;
  cvd: number;
}

export interface Analysis {
  rows: FpRow[];
  total: number;
  delta: number;
  maxVol: number;
  maxSide: number;
  maxAbsDelta: number;
  poc: number | null;
  vah: number | null;
  val: number | null;
  buyImb: Set<number>;
  sellImb: Set<number>;
  zones: { lo: number; hi: number; side: 1 | -1 }[];
  unfinishedHigh: boolean;
  unfinishedLow: boolean;
}

const round6 = (x: number) => Math.round(x * 1e6) / 1e6;

/** The price grid's tick: the smallest gap between traded prices, as a multiple of 0.05 (NSE's tick). */
export function inferTick(prices: number[], fallback = 0.05): number {
  const ps = [...new Set(prices.map(round6))].sort((a, b) => a - b);
  let best = Infinity;
  for (let i = 1; i < ps.length; i++) {
    const d = round6(ps[i] - ps[i - 1]);
    if (d > 0 && d < best) best = d;
  }
  if (!Number.isFinite(best)) return fallback;
  return best < fallback ? fallback : round6(Math.max(fallback, Math.round(best / fallback) * fallback));
}

const NICE = [1, 2, 4, 5, 10, 20, 25, 40, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000];

/** A row height in price: `rowTicks` ticks, or (auto) the fewest nice tick
 *  multiples that keep a row at least `minPx` tall. */
export function rowStep(tick: number, pxPerPrice: number, rowTicks = 0, minPx = 14): number {
  if (rowTicks > 0) return round6(tick * rowTicks);
  if (!(pxPerPrice > 0)) return tick;
  const k = NICE.find((m) => m * tick * pxPerPrice >= minPx) ?? NICE[NICE.length - 1];
  return round6(tick * k);
}

/** Group [price, buy, sell] levels into rows of `step` (each row = its low edge), lowest first. */
export function groupRows(levels: [number, number, number][], step: number): FpRow[] {
  const rows = new Map<number, FpRow>();
  for (const [price, buy, sell] of levels) {
    const p = round6(Math.floor(price / step + 1e-7) * step);
    const r = rows.get(p) ?? { p, buy: 0, sell: 0 };
    r.buy += buy;
    r.sell += sell;
    rows.set(p, r);
  }
  return [...rows.values()].sort((a, b) => a.p - b.p);
}

/** Time at price: for each candle, +1 on every `step` row its range covered (as buy, so a profile's
 *  POC / value area read it like volume). From the candles alone — no ticks needed. */
export function tpoRows(candles: { high: number; low: number }[], step: number): FpRow[] {
  const rows = new Map<number, FpRow>();
  for (const c of candles) {
    if (!(c.high >= c.low)) continue;
    const a = Math.round(Math.floor(c.low / step + 1e-7)), b = Math.round(Math.floor(c.high / step + 1e-7));
    for (let k = a; k <= b; k++) {
      const p = round6(k * step);
      const r = rows.get(p) ?? { p, buy: 0, sell: 0 };
      r.buy += 1;
      rows.set(p, r);
    }
  }
  return [...rows.values()].sort((x, y) => x.p - y.p);
}

/** A developing profile: after each bar (its rows, oldest first), the POC and value area of
 *  everything so far. */
export function developing(perBar: FpRow[][], step: number, vaPct: number): { poc: number | null; vah: number | null; val: number | null }[] {
  const acc = new Map<number, FpRow>();
  return perBar.map((rows) => {
    for (const r of rows) {
      const cur = acc.get(r.p) ?? { p: r.p, buy: 0, sell: 0 };
      cur.buy += r.buy;
      cur.sell += r.sell;
      acc.set(r.p, cur);
    }
    const a = analyze([...acc.values()].sort((x, y) => x.p - y.p), step, { imbalance: 0, imbalanceMin: 0, stacked: 0, vaPct });
    return { poc: a.poc, vah: a.vah, val: a.val };
  });
}

/** Everything a footprint bar shows (see the module comment). Rows are contiguous
 *  in price for the diagonal comparison: a missing row counts as zero volume. */
export function analyze(rows: FpRow[], step: number, s: Pick<FootprintSettings, "imbalance" | "imbalanceMin" | "stacked" | "vaPct">): Analysis {
  const out: Analysis = {
    rows, total: 0, delta: 0, maxVol: 0, maxSide: 0, maxAbsDelta: 0, poc: null, vah: null, val: null,
    buyImb: new Set(), sellImb: new Set(), zones: [], unfinishedHigh: false, unfinishedLow: false,
  };
  if (!rows.length) return out;
  const at = new Map(rows.map((r) => [r.p, r]));
  let pocVol = -1;
  for (const r of rows) {
    const v = r.buy + r.sell;
    out.total += v;
    out.delta += r.buy - r.sell;
    out.maxVol = Math.max(out.maxVol, v);
    out.maxSide = Math.max(out.maxSide, r.buy, r.sell);
    out.maxAbsDelta = Math.max(out.maxAbsDelta, Math.abs(r.buy - r.sell));
    if (v > pocVol) {
      pocVol = v;
      out.poc = r.p;
    }
  }
  // value area: grow from the POC towards the heavier neighbour until vaPct% is inside
  const idx = rows.findIndex((r) => r.p === out.poc);
  let lo = idx, hi = idx, inside = pocVol;
  const target = (out.total * s.vaPct) / 100;
  while (inside < target && (lo > 0 || hi < rows.length - 1)) {
    const up = hi < rows.length - 1 ? rows[hi + 1].buy + rows[hi + 1].sell : -1;
    const dn = lo > 0 ? rows[lo - 1].buy + rows[lo - 1].sell : -1;
    if (up >= dn) inside += rows[++hi].buy + rows[hi].sell;
    else inside += rows[--lo].buy + rows[lo].sell;
  }
  out.val = rows[lo].p;
  out.vah = rows[hi].p;
  const top = rows[rows.length - 1], bottom = rows[0];
  out.unfinishedHigh = top.buy > 0 && top.sell > 0;
  out.unfinishedLow = bottom.buy > 0 && bottom.sell > 0;
  if (s.imbalance > 0) {
    for (const r of rows) {
      // the extreme rows have no neighbour on that side; a gap inside the bar is zero volume
      if (r !== bottom && r.buy > 0 && r.buy >= s.imbalanceMin && r.buy >= s.imbalance * (at.get(round6(r.p - step))?.sell ?? 0)) out.buyImb.add(r.p);
      if (r !== top && r.sell > 0 && r.sell >= s.imbalanceMin && r.sell >= s.imbalance * (at.get(round6(r.p + step))?.buy ?? 0)) out.sellImb.add(r.p);
    }
    if (s.stacked > 1) {
      for (const [set, side] of [[out.buyImb, 1], [out.sellImb, -1]] as const) {
        let run: number[] = [];
        const flush = () => {
          if (run.length >= s.stacked) out.zones.push({ lo: run[0], hi: round6(run[run.length - 1] + step), side });
          run = [];
        };
        for (const r of rows) {
          if (set.has(r.p) && (!run.length || round6(run[run.length - 1] + step) === r.p)) run.push(r.p);
          else {
            flush();
            if (set.has(r.p)) run.push(r.p);
          }
        }
        flush();
      }
    }
  }
  return out;
}

/** The bar's rows laid out to fill its candle exactly, like ATAS / TradingView:
 *  the row holding the low sits on the candle's low, the row holding the high
 *  on its high, the rest split the span evenly. A bar that traded one price
 *  gets one row of `flatPx` around it. Returns [top, bottom] in pixels for a
 *  row's price (its low edge on the step grid). */
export function cellLayout(low: number, high: number, step: number, yHigh: number, yLow: number, flatPx: number):
  { n: number; rowPx: number; box: (p: number) => [number, number]; rowOf: (price: number) => number } {
  const rowOf = (price: number) => round6(Math.floor(Math.min(high, Math.max(low, price)) / step + 1e-7) * step);
  const rLo = rowOf(low), rHi = rowOf(high);
  const n = Math.max(1, Math.round((rHi - rLo) / step) + 1);
  let top = Math.min(yHigh, yLow), bot = Math.max(yHigh, yLow);
  if (bot - top < n) {                        // a flat (or sub-pixel) candle: rows of their own size around it
    const mid = (top + bot) / 2, h = Math.max(bot - top, n * Math.max(1, flatPx));
    top = mid - h / 2;
    bot = mid + h / 2;
  }
  const rowPx = (bot - top) / n;
  return {
    n, rowPx, rowOf,
    box: (p: number) => {
      const k = Math.min(n - 1, Math.max(0, Math.round((p - rLo) / step)));
      return [bot - (k + 1) * rowPx, bot - k * rowPx];
    },
  };
}

export interface BarSignals {
  /** Heavy aggressive buying at the high, and the bar still closed in its lower part: sellers absorbed it. */
  absorbHigh: boolean;
  /** Heavy aggressive selling at the low, closed in its upper part: buyers absorbed it. */
  absorbLow: boolean;
  /** A higher high on negative delta. */
  divBear: boolean;
  /** A lower low on positive delta. */
  divBull: boolean;
}

/** What a footprint bar says beyond its numbers. Absorption: the extreme row
 *  holds at least `heavy` x the bar's median row volume, its aggressor pushed
 *  into the extreme, and the close sits in the opposite 40% of the range.
 *  Divergence: price made a new extreme past the previous bar while the bar's
 *  delta went the other way. */
export function barSignals(a: Analysis, bar: { open: number; high: number; low: number; close: number },
  prev: { high: number; low: number } | null, heavy = 2.5): BarSignals {
  const out: BarSignals = { absorbHigh: false, absorbLow: false, divBear: false, divBull: false };
  if (!a.rows.length || a.total <= 0) return out;
  const range = bar.high - bar.low;
  if (a.rows.length >= 3 && range > 0) {
    const vols = a.rows.map((r) => r.buy + r.sell).filter((v) => v > 0).sort((x, y) => x - y);
    const med = vols[Math.floor(vols.length / 2)] ?? 0;
    const top = a.rows[a.rows.length - 1], bottom = a.rows[0];
    const pos = (bar.close - bar.low) / range;
    out.absorbHigh = top.buy + top.sell >= heavy * med && top.buy > top.sell && pos <= 0.4;
    out.absorbLow = bottom.buy + bottom.sell >= heavy * med && bottom.sell > bottom.buy && pos >= 0.6;
  }
  if (prev) {
    out.divBear = bar.high > prev.high && a.delta < 0;
    out.divBull = bar.low < prev.low && a.delta > 0;
  }
  return out;
}

// ---- server rows -> chart candles ----------------------------------------------

/** One chart candle's order flow, summed from the server's base-resolution bars. */
export interface FlowBar {
  buy: number;
  sell: number;
  delta: number;
  trades: number;
  unk: number;
  oi: number | null;
  close: number | null;
  /** Lowest / highest running delta inside the candle (from its start). */
  dlo: number;
  dhi: number;
  /** The order book's total pending buy / sell quantity, averaged over the candle (null: none recorded). */
  bookBid: number | null;
  bookAsk: number | null;
}

/** Index of the candle holding chart time `t` (the last one starting at or before it), or -1. */
export function candleAt(times: number[], t: number): number {
  let lo = 0, hi = times.length - 1, hit = -1;
  while (lo <= hi) {
    const m = (lo + hi) >> 1;
    if (times[m] <= t) {
      hit = m;
      lo = m + 1;
    } else hi = m - 1;
  }
  return hit;
}

/** Fold /api/algo/ticks/flow bars ([t, buy, sell, delta, trades, unk, oi, close, dlo, dhi, book_bid,
 *  book_ask], epoch t) into the chart's candles (chart time = epoch + `shift`). */
export function flowByCandle(times: number[], bars: (number | null)[][], shift: number): Map<number, FlowBar> {
  const out = new Map<number, FlowBar>();
  const bookN = new Map<number, number>();          // sub-bars that carried book totals, per candle
  for (const b of bars) {
    const i = candleAt(times, (b[0] ?? 0) + shift);
    if (i < 0) continue;
    const key = times[i];
    const buy = b[1] ?? 0, sell = b[2] ?? 0, delta = b[3] ?? buy - sell;
    const trades = b[4] ?? 0, unk = b[5] ?? 0, oi = b[6] ?? null, close = b[7] ?? null;
    const dlo = b[8] ?? Math.min(0, delta), dhi = b[9] ?? Math.max(0, delta);
    const bid = b[10] ?? null, ask = b[11] ?? null;
    const cur = out.get(key);
    if (!cur) {
      out.set(key, { buy, sell, delta, trades, unk, oi, close, dlo, dhi, bookBid: bid, bookAsk: ask });
      if (bid != null && ask != null) bookN.set(key, 1);
      continue;
    }
    if (bid != null && ask != null) {                 // a running mean over the sub-bars that have the book
      const n = (bookN.get(key) ?? 0) + 1;
      bookN.set(key, n);
      cur.bookBid = (cur.bookBid ?? 0) + (bid - (cur.bookBid ?? 0)) / n;
      cur.bookAsk = (cur.bookAsk ?? 0) + (ask - (cur.bookAsk ?? 0)) / n;
    }
    // a later sub-bar: its running extremes start from where the candle's delta stood
    cur.dlo = Math.min(cur.dlo, cur.delta + dlo);
    cur.dhi = Math.max(cur.dhi, cur.delta + dhi);
    cur.buy += buy;
    cur.sell += sell;
    cur.delta += delta;
    cur.trades += trades;
    cur.unk += unk;
    if (oi != null) cur.oi = oi;
    if (close != null) cur.close = close;
  }
  return out;
}

/** Footprint rows ([t, price, buy, sell]) per chart candle: price -> [buy, sell]. */
export function footprintByCandle(times: number[], rows: number[][], shift: number): Map<number, Map<number, [number, number]>> {
  const out = new Map<number, Map<number, [number, number]>>();
  for (const [t, p, b, s] of rows) {
    const i = candleAt(times, t + shift);
    if (i < 0) continue;
    const m = out.get(times[i]) ?? new Map<number, [number, number]>();
    const cur = m.get(p);
    if (cur) {
      cur[0] += b;
      cur[1] += s;
    } else m.set(p, [b, s]);
    out.set(times[i], m);
  }
  return out;
}

/** Session-cumulative delta per candle (restarts each day, chart time). */
export function sessionCvd(times: number[], flow: Map<number, FlowBar>): Map<number, number> {
  const out = new Map<number, number>();
  let day = -1, run = 0;
  for (const t of times) {
    const d = Math.floor(t / 86400);
    if (d !== day) {
      day = d;
      run = 0;
    }
    const f = flow.get(t);
    if (f) run += f.delta;
    out.set(t, run);
  }
  return out;
}

/** An index's footprint is its future's, at futures prices: the futures-minus-index basis
 *  per session (chart day) that lays it onto the index candles — the median over the
 *  session's candles of (the footprint's traded-range midpoint − the candle's midpoint).
 *  Taken from the footprint itself, so it needs no flow bars and can't disagree with what
 *  is drawn: a basis off by 40 points pushed every bar's prints above the candle, and the
 *  renderer piled them into the top rows. Snapped to `tick`. */
export function footprintBasis(candles: { time: number; high: number; low: number }[],
  fp: Map<number, { fp: [number, number, number][] }>, tick = 0.05): Map<number, number> {
  const per = new Map<number, number[]>();
  for (const c of candles) {
    const rows = fp.get(c.time)?.fp;
    if (!rows?.length || !(c.high >= c.low)) continue;
    let lo = Infinity, hi = -Infinity;
    for (const [p, b, s] of rows) {
      if (b + s <= 0) continue;
      lo = Math.min(lo, p);
      hi = Math.max(hi, p);
    }
    if (!Number.isFinite(lo)) continue;
    const d = Math.floor(c.time / 86400);
    per.set(d, [...(per.get(d) ?? []), (lo + hi) / 2 - (c.high + c.low) / 2]);
  }
  const out = new Map<number, number>();
  for (const [d, xs] of per) {
    xs.sort((a, b) => a - b);
    const m = xs.length % 2 ? xs[(xs.length - 1) / 2] : (xs[xs.length / 2 - 1] + xs[xs.length / 2]) / 2;
    out.set(d, Math.round(m / tick) * tick);
  }
  return out;
}

