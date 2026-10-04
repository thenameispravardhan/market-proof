// indicatorMore — the rest of the TradingView built-in library and the
// Fyers desk set that indicatorCatalog / indicatorExtras don't cover:
// adaptive and windowed moving averages, MA crosses, %B / width, the
// classic oscillators (CMO, Connors RSI, Fisher, KST, TSI, UO …), trend
// tools (Vortex, Alligator, fractals, Zig Zag, ASI), volume flows (A/D,
// Klinger, PVT …), volatility estimators, two-symbol statistics, open
// interest, candlestick patterns, auto Fibonacci and RSI divergences.
// Same IndicatorDef shape; indicatorCatalog appends these.

import { atr, bollinger, ema, movingAverage, rsi, sma, smma, type MaType, type OhlcvCandle } from "../../lib/indicators";
import { calendarBucket, intervalGroup, intervalSeconds } from "./chartData";
import type { ComputeCtx, IndicatorDef, InputDef, InputValue, Mark, PlotDef } from "./indicatorCatalog";

type Arr = (number | null)[];

const num = (v: InputValue | undefined, d: number) => (typeof v === "number" && Number.isFinite(v) ? v : d);
const str = (v: InputValue | undefined, d: string) => (typeof v === "string" && v ? v : d);
const bool = (v: InputValue | undefined, d: boolean) => (typeof v === "boolean" ? v : d);
const MA_TYPES: MaType[] = ["SMA", "EMA", "SMMA", "WMA", "VWMA"];
const SRC_INPUT: InputDef = { key: "source", label: "Source", type: "source", def: "close" };
const BENCH: InputDef = { key: "symbol", label: "Symbol", type: "symbol", def: "NSE:NIFTY50-INDEX" };

/** Source series without importing indicatorCatalog (it imports this file). */
function src(c: OhlcvCandle[], s: InputValue | undefined): number[] {
  switch (str(s, "close")) {
    case "open": return c.map((k) => k.open);
    case "high": return c.map((k) => k.high);
    case "low": return c.map((k) => k.low);
    case "hl2": return c.map((k) => (k.high + k.low) / 2);
    case "hlc3": return c.map((k) => (k.high + k.low + k.close) / 3);
    case "ohlc4": return c.map((k) => (k.open + k.high + k.low + k.close) / 4);
    case "hlcc4": return c.map((k) => (k.high + k.low + 2 * k.close) / 4);
    default: return c.map((k) => k.close);
  }
}

const ok = (v: number | null | undefined): v is number => v != null && Number.isFinite(v);

/** f over each full window of n defined values (else null). */
export function rolling(a: Arr, n: number, f: (w: number[]) => number | null): Arr {
  const out: Arr = new Array(a.length).fill(null);
  if (n < 1) return out;
  let bad = 0;
  for (let i = 0; i < a.length; i++) {
    if (!ok(a[i])) bad++;
    if (i >= n && !ok(a[i - n])) bad--;
    if (i >= n - 1 && bad === 0) {
      const v = f(a.slice(i - n + 1, i + 1) as number[]);
      out[i] = ok(v) ? v : null;
    }
  }
  return out;
}

/** Sliding sum (null while any value in the window is missing). */
export function rsum(a: Arr, n: number): Arr {
  const out: Arr = new Array(a.length).fill(null);
  let s = 0;
  let bad = 0;
  for (let i = 0; i < a.length; i++) {
    const v = a[i];
    if (ok(v)) s += v;
    else bad++;
    if (i >= n) {
      const o = a[i - n];
      if (ok(o)) s -= o;
      else bad--;
    }
    if (i >= n - 1 && bad === 0) out[i] = s;
  }
  return out;
}

const mean = (w: number[]) => w.reduce((x, y) => x + y, 0) / w.length;
/** Population standard deviation (TradingView's ta.stdev default). */
export const stdevOf = (w: number[]) => {
  const m = mean(w);
  return Math.sqrt(w.reduce((s, x) => s + (x - m) ** 2, 0) / w.length);
};
const rstdev = (a: Arr, n: number) => rolling(a, n, stdevOf);
const highest = (a: Arr, n: number) => rolling(a, n, (w) => Math.max(...w));
const lowest = (a: Arr, n: number) => rolling(a, n, (w) => Math.min(...w));
const change = (a: Arr, k = 1): Arr => a.map((v, i) => (i < k || !ok(v) || !ok(a[i - k]) ? null : v - (a[i - k] as number)));
const map2 = (a: Arr, b: Arr, f: (x: number, y: number) => number | null): Arr => a.map((x, i) => (ok(x) && ok(b[i]) ? f(x, b[i] as number) : null));
const ma = (t: MaType | string, a: Arr, n: number, vols?: number[]) => movingAverage((MA_TYPES.includes(t as MaType) ? t : "SMA") as MaType, a, Math.max(1, Math.round(n)), vols);
const emaN = (a: Arr, n: number) => ma("EMA", a, n);
const smaN = (a: Arr, n: number) => ma("SMA", a, n);

/** Least-squares line through w (x = 0 … n−1). */
export function fitLine(w: number[]): { icpt: number; slope: number } {
  const n = w.length;
  let sx = 0, sy = 0, sxy = 0, sxx = 0;
  for (let i = 0; i < n; i++) {
    sx += i;
    sy += w[i];
    sxy += i * w[i];
    sxx += i * i;
  }
  const den = n * sxx - sx * sx;
  const slope = den !== 0 ? (n * sxy - sx * sy) / den : 0;
  return { icpt: (sy - slope * sx) / n, slope };
}

/** ta.linreg: the regression line's value `offset` bars back from the window end. */
export const linreg = (a: Arr, n: number, offset = 0) =>
  rolling(a, n, (w) => {
    const { icpt, slope } = fitLine(w);
    return icpt + slope * (w.length - 1 - offset);
  });

/** Pearson correlation of two aligned series over n bars. */
export function correlation(a: Arr, b: Arr, n: number): Arr {
  const out: Arr = new Array(a.length).fill(null);
  for (let i = n - 1; i < a.length; i++) {
    let sa = 0, sb = 0, saa = 0, sbb = 0, sab = 0, good = true;
    for (let j = i - n + 1; j <= i; j++) {
      const x = a[j];
      const y = b[j];
      if (!ok(x) || !ok(y)) {
        good = false;
        break;
      }
      sa += x;
      sb += y;
      saa += x * x;
      sbb += y * y;
      sab += x * y;
    }
    if (!good) continue;
    const cov = sab / n - (sa / n) * (sb / n);
    const va = saa / n - (sa / n) ** 2;
    const vb = sbb / n - (sb / n) ** 2;
    out[i] = va > 0 && vb > 0 ? Math.max(-1, Math.min(1, cov / Math.sqrt(va * vb))) : null;
  }
  return out;
}

/** ta.percentrank: % of the previous n values ≤ the current one. */
function percentRank(a: Arr, n: number): Arr {
  return a.map((v, i) => {
    if (!ok(v) || i < n) return null;
    let le = 0;
    for (let j = i - n; j < i; j++) {
      const x = a[j];
      if (!ok(x)) return null;
      if (x <= v) le++;
    }
    return (le / n) * 100;
  });
}

/** Symmetric 4-bar weighted MA (1-2-2-1)/6. */
const swma = (a: Arr): Arr =>
  a.map((v, i) => (i < 3 || !ok(v) || !ok(a[i - 1]) || !ok(a[i - 2]) || !ok(a[i - 3]) ? null : ((a[i - 3] as number) + 2 * (a[i - 2] as number) + 2 * (a[i - 1] as number) + v) / 6));

const trueRange = (c: OhlcvCandle[]): number[] =>
  c.map((k, i) => (i === 0 ? k.high - k.low : Math.max(k.high - k.low, Math.abs(k.high - c[i - 1].close), Math.abs(k.low - c[i - 1].close))));

/** ta.tsi in [−1, 1]: double-smoothed momentum over double-smoothed |momentum|. */
function tsiRaw(a: number[], short: number, long: number): Arr {
  const pc = change(a);
  const num_ = emaN(emaN(pc, long), short);
  const den = emaN(emaN(pc.map((v) => (v == null ? null : Math.abs(v))), long), short);
  return map2(num_, den, (x, y) => (y === 0 ? 0 : x / y));
}

/** Bars per year for annualising (intraday: 252 sessions × 375 minutes). */
function perYear(interval: string): number {
  const g = intervalGroup(interval);
  if (g === "days") return 252;
  if (g === "weeks") return 52;
  if (g === "months") return 12;
  return (252 * 375 * 60) / Math.max(1, intervalSeconds(interval));
}

/** Another symbol's closes aligned to these candles (carried forward over gaps). */
function otherCloses(c: OhlcvCandle[], ctx: ComputeCtx, sym: string): Arr {
  const m = ctx.other?.(sym);
  if (!m || m.size === 0) return c.map(() => null);
  let last: number | null = null;
  return c.map((k) => {
    const v = m.get(k.time);
    if (ok(v)) last = v;
    return last;
  });
}

const dayOf = (t: number) => Math.floor(t / 86400);

// ---------------------------------------------------------------------------
// Pivots / zig zag
// ---------------------------------------------------------------------------

/** Indexes of confirmed swing highs / lows: `left` bars before and `right` after are lower / higher. */
export function pivots(c: OhlcvCandle[], left: number, right: number): { highs: number[]; lows: number[] } {
  const highs: number[] = [];
  const lows: number[] = [];
  for (let i = left; i < c.length - right; i++) {
    let hi = true;
    let lo = true;
    for (let j = i - left; j <= i + right && (hi || lo); j++) {
      if (j === i) continue;
      if (c[j].high > c[i].high || (j > i && c[j].high === c[i].high)) hi = false;
      if (c[j].low < c[i].low || (j > i && c[j].low === c[i].low)) lo = false;
    }
    if (hi) highs.push(i);
    if (lo) lows.push(i);
  }
  return { highs, lows };
}

export interface ZigPoint { i: number; price: number; high: boolean; confirmed: boolean }

/** Zig zag: a new swing needs a `devPct` % reversal and at least `depth` bars since the last pivot. */
export function zigzag(c: OhlcvCandle[], devPct: number, depth: number): ZigPoint[] {
  const out: ZigPoint[] = [];
  if (c.length < 2) return out;
  let dir = 0; // 1 = looking for a high, -1 = a low
  let ext = 0; // index of the running extreme
  let lastPivot = 0;
  for (let i = 1; i < c.length; i++) {
    if (dir === 0) {
      if (c[i].high > c[ext].high * (1 + devPct / 100)) {
        out.push({ i: ext, price: c[ext].low, high: false, confirmed: true });
        dir = 1;
        lastPivot = ext;
        ext = i;
      } else if (c[i].low < c[ext].low * (1 - devPct / 100)) {
        out.push({ i: ext, price: c[ext].high, high: true, confirmed: true });
        dir = -1;
        lastPivot = ext;
        ext = i;
      } else if (c[i].high >= c[ext].high && c[i].low <= c[ext].low) ext = i;
      continue;
    }
    if (dir === 1) {
      if (c[i].high >= c[ext].high) ext = i;
      else if (c[i].low <= c[ext].high * (1 - devPct / 100) && ext - lastPivot >= depth) {
        out.push({ i: ext, price: c[ext].high, high: true, confirmed: true });
        lastPivot = ext;
        dir = -1;
        ext = i;
      }
    } else if (c[i].low <= c[ext].low) ext = i;
    else if (c[i].high >= c[ext].low * (1 + devPct / 100) && ext - lastPivot >= depth) {
      out.push({ i: ext, price: c[ext].low, high: false, confirmed: true });
      lastPivot = ext;
      dir = 1;
      ext = i;
    }
  }
  if (dir !== 0 && ext !== out[out.length - 1]?.i) out.push({ i: ext, price: dir === 1 ? c[ext].high : c[ext].low, high: dir === 1, confirmed: false });
  return out;
}

/** A continuous line through the pivots (linear between them). */
function interpolate(n: number, pts: { i: number; price: number }[]): Arr {
  const out: Arr = new Array(n).fill(null);
  for (let k = 0; k + 1 < pts.length; k++) {
    const a = pts[k];
    const b = pts[k + 1];
    const span = Math.max(1, b.i - a.i);
    for (let i = a.i; i <= b.i; i++) out[i] = a.price + ((b.price - a.price) * (i - a.i)) / span;
  }
  return out;
}

// ---------------------------------------------------------------------------
// Builders
// ---------------------------------------------------------------------------

function overlayMa(type: string, name: string, short: string, desc: string, inputs: InputDef[], color: string,
  f: (c: OhlcvCandle[], inp: Record<string, InputValue>) => Arr, extra?: Partial<IndicatorDef>): IndicatorDef {
  return {
    type, name, short, desc, inputs, category: "Moving averages", overlay: true,
    plots: [{ key: "ma", label: short, color }],
    compute: (c, inp) => ({ plots: [f(c, inp)] }),
    ...extra,
  };
}

function pane(type: string, name: string, short: string, category: IndicatorDef["category"], desc: string, inputs: InputDef[], plots: PlotDef[],
  compute: IndicatorDef["compute"], extra?: Partial<IndicatorDef>): IndicatorDef {
  return { type, name, short, category, desc, inputs, plots, compute, overlay: false, ...extra };
}

const len = (key: string, label: string, def: number, max = 500): InputDef => ({ key, label, type: "int", def, min: 1, max });
const flt = (key: string, label: string, def: number, step = 0.1, min = 0, max = 100): InputDef => ({ key, label, type: "float", def, step, min, max });

/** Cross dots of two lines (TradingView's MA-cross style). */
function crossDots(a: Arr, b: Arr): Arr {
  return a.map((v, i) => {
    if (i === 0 || !ok(v) || !ok(b[i]) || !ok(a[i - 1]) || !ok(b[i - 1])) return null;
    const was = (a[i - 1] as number) - (b[i - 1] as number);
    const now = v - (b[i] as number);
    return (was <= 0 && now > 0) || (was >= 0 && now < 0) ? v : null;
  });
}

function crossDef(type: string, name: string, short: string, desc: string, aType: MaType, aLen: number, bType: MaType, bLen: number): IndicatorDef {
  return {
    type, name, short, desc, category: "Moving averages", overlay: true,
    inputs: [len("fast", `${aType} length`, aLen, 2000), len("slow", `${bType} length`, bLen, 2000), SRC_INPUT],
    plots: [
      { key: "fast", label: `${aType} fast`, color: "#43A047" },
      { key: "slow", label: `${bType} slow`, color: "#F44336" },
      { key: "cross", label: "Cross", color: "#2962FF", kind: "circles" },
    ],
    compute: (c, inp) => {
      const s = src(c, inp.source);
      const v = c.map((k) => k.volume);
      const a = ma(aType, s, num(inp.fast, aLen), v);
      const b = ma(bType, s, num(inp.slow, bLen), v);
      return { plots: [a, b, crossDots(a, b)] };
    },
  };
}

/** Moving average of `n` points with the given window weights (oldest first). */
function weighted(a: number[], n: number, w: number[]): Arr {
  const norm = w.reduce((x, y) => x + y, 0) || 1;
  return a.map((_, i) => {
    if (i < n - 1) return null;
    let s = 0;
    for (let j = 0; j < n; j++) s += a[i - n + 1 + j] * w[j];
    return s / norm;
  });
}

function kama(a: number[], n: number, fast: number, slow: number): Arr {
  const fsc = 2 / (fast + 1);
  const ssc = 2 / (slow + 1);
  const out: Arr = new Array(a.length).fill(null);
  let k: number | null = null;
  for (let i = n; i < a.length; i++) {
    const ch = Math.abs(a[i] - a[i - n]);
    let vol = 0;
    for (let j = i - n + 1; j <= i; j++) vol += Math.abs(a[j] - a[j - 1]);
    const er = vol ? ch / vol : 0;
    const sc = (er * (fsc - ssc) + ssc) ** 2;
    k = (k ?? a[i - 1]) + sc * (a[i] - (k ?? a[i - 1]));
    out[i] = k;
  }
  return out;
}

/** Anchored VWAP (+ volume-weighted σ) restarting each anchor period. */
function anchoredVwap(c: OhlcvCandle[], anchor: string, source: InputValue | undefined): { vwap: Arr; sd: Arr } {
  const s = src(c, source ?? "hlc3");
  const period = (t: number): number => {
    switch (anchor) {
      case "Week": return calendarBucket(t, { unit: "W", n: 1 });
      case "Month": return calendarBucket(t, { unit: "M", n: 1 });
      case "Quarter": return calendarBucket(t, { unit: "M", n: 3 });
      case "Year": return calendarBucket(t, { unit: "M", n: 12 });
      default: return dayOf(t);
    }
  };
  const vwap: Arr = [];
  const sd: Arr = [];
  let p = Number.NaN;
  let pv = 0;
  let v = 0;
  let p2v = 0;
  c.forEach((k, i) => {
    const b = period(k.time);
    if (b !== p) {
      p = b;
      pv = 0;
      v = 0;
      p2v = 0;
    }
    const vol = k.volume > 0 ? k.volume : 0;
    pv += s[i] * vol;
    v += vol;
    p2v += s[i] * s[i] * vol;
    if (v <= 0) {
      vwap.push(null);
      sd.push(null);
      return;
    }
    const m = pv / v;
    vwap.push(m);
    sd.push(Math.sqrt(Math.max(0, p2v / v - m * m)));
  });
  return { vwap, sd };
}

/** Previous period's H / L / C for each bar (CPR, pivots). */
function prevPeriod(c: OhlcvCandle[], bucketOf: (t: number) => number): Map<number, { h: number; l: number; c: number }> {
  const periods: [number, { h: number; l: number; c: number }][] = [];
  for (const k of c) {
    const b = bucketOf(k.time);
    const last = periods[periods.length - 1];
    if (!last || last[0] !== b) periods.push([b, { h: k.high, l: k.low, c: k.close }]);
    else {
      last[1].h = Math.max(last[1].h, k.high);
      last[1].l = Math.min(last[1].l, k.low);
      last[1].c = k.close;
    }
  }
  const prev = new Map<number, { h: number; l: number; c: number }>();
  for (let i = 1; i < periods.length; i++) prev.set(periods[i][0], periods[i - 1][1]);
  return prev;
}

function cprDef(type: string, name: string, unit: "W" | "M"): IndicatorDef {
  return {
    type, name, short: unit === "W" ? "CPR W" : "CPR M", category: "Trend", overlay: true, desk: true,
    desc: `Central pivot range (TC / pivot / BC) and R1–R3 / S1–S3 from the previous ${unit === "W" ? "week" : "month"}'s range.`,
    inputs: [],
    plots: [
      { key: "tc", label: "TC", color: "#2962FF", kind: "step" },
      { key: "p", label: "Pivot", color: "#E040FB", kind: "step", width: 2 },
      { key: "bc", label: "BC", color: "#2962FF", kind: "step" },
      { key: "r1", label: "R1", color: "#EF5350", kind: "step", dash: 2 },
      { key: "r2", label: "R2", color: "#EF5350", kind: "step", dash: 2 },
      { key: "r3", label: "R3", color: "#EF5350", kind: "step", dash: 2 },
      { key: "s1", label: "S1", color: "#26A69A", kind: "step", dash: 2 },
      { key: "s2", label: "S2", color: "#26A69A", kind: "step", dash: 2 },
      { key: "s3", label: "S3", color: "#26A69A", kind: "step", dash: 2 },
    ],
    fills: [{ key: "cpr", label: "CPR band", a: 0, b: 2, color: "rgba(41,98,255,0.10)" }],
    compute: (c) => {
      const bucketOf = (t: number) => calendarBucket(t, { unit, n: 1 });
      const prev = prevPeriod(c, bucketOf);
      const out = Array.from({ length: 9 }, () => [] as Arr);
      for (const k of c) {
        const d = prev.get(bucketOf(k.time));
        if (!d) {
          out.forEach((a) => a.push(null));
          continue;
        }
        const p = (d.h + d.l + d.c) / 3;
        const bc = (d.h + d.l) / 2;
        const tc = 2 * p - bc;
        const rng = d.h - d.l;
        [Math.max(tc, bc), p, Math.min(tc, bc), 2 * p - d.l, p + rng, d.h + 2 * (p - d.l), 2 * p - d.h, p - rng, d.l - 2 * (d.h - p)]
          .forEach((v, i) => out[i].push(v));
      }
      return { plots: out };
    },
  };
}

// ---------------------------------------------------------------------------
// Candlestick patterns
// ---------------------------------------------------------------------------

export interface PatternHit { i: number; name: string; code: string; bias: 1 | -1 | 0 }

export function candlePatterns(c: OhlcvCandle[]): PatternHit[] {
  const out: PatternHit[] = [];
  const trend = sma(c.map((k) => k.close), 10);
  const body = (k: OhlcvCandle) => Math.abs(k.close - k.open);
  const rng = (k: OhlcvCandle) => k.high - k.low;
  const upper = (k: OhlcvCandle) => k.high - Math.max(k.open, k.close);
  const lower = (k: OhlcvCandle) => Math.min(k.open, k.close) - k.low;
  const green = (k: OhlcvCandle) => k.close > k.open;
  const red = (k: OhlcvCandle) => k.close < k.open;
  const avgBody = sma(c.map(body), 14);
  for (let i = 1; i < c.length; i++) {
    const k = c[i];
    const p = c[i - 1];
    const r = rng(k);
    if (r <= 0) continue;
    const ab = avgBody[i] ?? body(k);
    const up = trend[i - 1] != null && p.close > (trend[i - 1] as number);
    const down = trend[i - 1] != null && p.close < (trend[i - 1] as number);
    const long = (x: OhlcvCandle) => body(x) > (ab || 0) * 1.1;
    const add = (name: string, code: string, bias: 1 | -1 | 0) => out.push({ i, name, code, bias });
    // three-bar patterns first (they explain the bar best)
    if (i >= 2) {
      const a = c[i - 2];
      const small = body(p) < (ab || body(a)) * 0.5;
      if (red(a) && long(a) && small && green(k) && k.close > (a.open + a.close) / 2 && Math.max(p.open, p.close) < a.close + body(a) * 0.1) {
        add("Morning Star", "MS", 1);
        continue;
      }
      if (green(a) && long(a) && small && red(k) && k.close < (a.open + a.close) / 2 && Math.min(p.open, p.close) > a.close - body(a) * 0.1) {
        add("Evening Star", "ES", -1);
        continue;
      }
      if ([a, p, k].every(green) && [a, p, k].every(long) && p.close > a.close && k.close > p.close && p.open > a.open && p.open < a.close && k.open > p.open && k.open < p.close) {
        add("Three White Soldiers", "3WS", 1);
        continue;
      }
      if ([a, p, k].every(red) && [a, p, k].every(long) && p.close < a.close && k.close < p.close && p.open < a.open && p.open > a.close && k.open < p.open && k.open > p.close) {
        add("Three Black Crows", "3BC", -1);
        continue;
      }
    }
    // two-bar
    if (red(p) && green(k) && k.close >= p.open && k.open <= p.close && body(k) > body(p)) {
      add("Bullish Engulfing", "BE", 1);
      continue;
    }
    if (green(p) && red(k) && k.close <= p.open && k.open >= p.close && body(k) > body(p)) {
      add("Bearish Engulfing", "BE", -1);
      continue;
    }
    if (red(p) && long(p) && green(k) && k.open > p.close && k.close < p.open && body(k) < body(p) * 0.6) {
      add("Bullish Harami", "BH", 1);
      continue;
    }
    if (green(p) && long(p) && red(k) && k.open < p.close && k.close > p.open && body(k) < body(p) * 0.6) {
      add("Bearish Harami", "BH", -1);
      continue;
    }
    if (red(p) && long(p) && green(k) && k.open < p.low && k.close > (p.open + p.close) / 2 && k.close < p.open) {
      add("Piercing Line", "PL", 1);
      continue;
    }
    if (green(p) && long(p) && red(k) && k.open > p.high && k.close < (p.open + p.close) / 2 && k.close > p.open) {
      add("Dark Cloud Cover", "DCC", -1);
      continue;
    }
    // single bar
    const b = body(k);
    if (b >= r * 0.95) {
      add(green(k) ? "Bullish Marubozu" : "Bearish Marubozu", "MB", green(k) ? 1 : -1);
      continue;
    }
    if (b <= r * 0.1) {
      add("Doji", "D", 0);
      continue;
    }
    if (lower(k) >= 2 * b && upper(k) <= Math.max(b * 0.3, r * 0.1)) {
      if (down) add("Hammer", "H", 1);
      else if (up) add("Hanging Man", "HM", -1);
      continue;
    }
    if (upper(k) >= 2 * b && lower(k) <= Math.max(b * 0.3, r * 0.1)) {
      if (down) add("Inverted Hammer", "IH", 1);
      else if (up) add("Shooting Star", "SS", -1);
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// Volume profile (shared by the visible-range indicator and the FRVP tool)
// ---------------------------------------------------------------------------

export interface Profile {
  lo: number;
  step: number;
  up: number[];
  down: number[];
  poc: number;
  vaLo: number;
  vaHi: number;
}

/** Volume by price over the candles: each bar's volume spread evenly across the rows it spans. */
export function volumeProfileRows(c: OhlcvCandle[], rows: number, vaPct: number): Profile | null {
  let lo = Infinity;
  let hi = -Infinity;
  for (const k of c) {
    if (k.low < lo) lo = k.low;
    if (k.high > hi) hi = k.high;
  }
  rows = Math.max(2, Math.min(500, Math.round(rows)));
  if (!(hi > lo)) return null;
  const step = (hi - lo) / rows;
  const up = new Array<number>(rows).fill(0);
  const down = new Array<number>(rows).fill(0);
  for (const k of c) {
    const r0 = Math.max(0, Math.min(rows - 1, Math.floor((k.low - lo) / step)));
    const r1 = Math.max(0, Math.min(rows - 1, Math.floor((k.high - lo) / step)));
    const share = (k.volume || 0) / (r1 - r0 + 1);
    const into = k.close >= k.open ? up : down;
    for (let i = r0; i <= r1; i++) into[i] += share;
  }
  const tot = up.map((u, i) => u + down[i]);
  const total = tot.reduce((x, y) => x + y, 0);
  let poc = 0;
  tot.forEach((v, i) => {
    if (v > tot[poc]) poc = i;
  });
  let acc = tot[poc];
  let vaLo = poc;
  let vaHi = poc;
  const target = total * Math.max(0.05, Math.min(1, vaPct));
  while (acc < target && (vaLo > 0 || vaHi < rows - 1)) {
    const dn = vaLo > 0 ? tot[vaLo - 1] : -1;
    const upv = vaHi < rows - 1 ? tot[vaHi + 1] : -1;
    if (upv >= dn) acc += tot[++vaHi];
    else acc += tot[--vaLo];
  }
  return { lo, step, up, down, poc, vaLo, vaHi };
}

// ---------------------------------------------------------------------------
// The catalog
// ---------------------------------------------------------------------------

const GUPPY_SHORT = [3, 5, 8, 10, 12, 15];
const GUPPY_LONG = [30, 35, 40, 45, 50, 60];
const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
const FIB_COLORS = ["#787B86", "#F44336", "#81C784", "#4CAF50", "#009688", "#64B5F6", "#787B86"];

export const MORE_INDICATORS: IndicatorDef[] = [
  // ---- moving averages ----------------------------------------------------
  overlayMa("alma", "Arnaud Legoux Moving Average", "ALMA", "Gaussian-weighted average centred near the newest bar: smooth with little lag.",
    [len("length", "Window size", 9, 2000), flt("offset", "Offset", 0.85, 0.01, 0, 1), flt("sigma", "Sigma", 6, 0.1, 0.1, 50), SRC_INPUT], "#2962FF",
    (c, inp) => {
      const n = num(inp.length, 9);
      const m = num(inp.offset, 0.85) * (n - 1);
      const s = n / num(inp.sigma, 6);
      const w = Array.from({ length: n }, (_, j) => Math.exp(-((j - m) ** 2) / (2 * s * s)));
      return weighted(src(c, inp.source), n, w);
    }),
  overlayMa("lsma", "Least Squares Moving Average", "LSMA", "End point of the least-squares line through the last N closes.",
    [len("length", "Length", 25, 2000), { key: "offset", label: "Offset", type: "int", def: 0, min: 0, max: 500 }, SRC_INPUT], "#2962FF",
    (c, inp) => linreg(src(c, inp.source), num(inp.length, 25), num(inp.offset, 0))),
  overlayMa("lrc", "Linear Regression Curve", "LRC", "Rolling linear-regression value — the curve the regression end points trace.",
    [len("length", "Length", 9, 2000), SRC_INPUT], "#FF9800", (c, inp) => linreg(src(c, inp.source), num(inp.length, 9))),
  overlayMa("mcginley", "McGinley Dynamic", "McGinley", "A moving average that speeds up in fast markets and slows in slow ones.",
    [len("length", "Length", 14, 2000), SRC_INPUT], "#00BCD4", (c, inp) => {
      const n = num(inp.length, 14);
      const s = src(c, inp.source);
      const e = ema(s, n);
      let mg: number | null = null;
      return s.map((v, i) => {
        if (mg == null) {
          mg = e[i];
          return mg;
        }
        mg = mg + (v - mg) / (n * Math.pow(v / mg, 4));
        return Number.isFinite(mg) ? mg : null;
      });
    }),
  overlayMa("maadaptive", "Moving Average Adaptive", "AMA", "Kaufman-style adaptive average: the efficiency ratio sets the smoothing.",
    [len("length", "Period", 14, 2000), SRC_INPUT], "#AB47BC", (c, inp) => kama(src(c, inp.source), num(inp.length, 14), 2, 30)),
  overlayMa("kama", "Kaufman's Adaptive Moving Average (KAMA)", "KAMA", "Adaptive average: fast when price trends efficiently, flat in chop.",
    [len("length", "Length", 10, 2000), len("fast", "Fast length", 2, 100), len("slow", "Slow length", 30, 500), SRC_INPUT], "#FF6D00",
    (c, inp) => kama(src(c, inp.source), num(inp.length, 10), num(inp.fast, 2), num(inp.slow, 30)), { desk: true, isNew: true }),
  overlayMa("jma", "Jurik Moving Average", "JMA", "Low-lag adaptive average (phase / power tune overshoot and smoothness).",
    [len("length", "Length", 7, 500), { key: "phase", label: "Phase", type: "int", def: 50, min: -100, max: 100 }, len("power", "Power", 2, 10), SRC_INPUT], "#26C6DA",
    (c, inp) => {
      const n = num(inp.length, 7);
      const phase = num(inp.phase, 50);
      const pr = phase < -100 ? 0.5 : phase > 100 ? 2.5 : phase / 100 + 1.5;
      const beta = (0.45 * (n - 1)) / (0.45 * (n - 1) + 2);
      const alpha = Math.pow(beta, num(inp.power, 2));
      let e0 = 0, e1 = 0, e2 = 0, j = 0;
      return src(c, inp.source).map((v, i) => {
        e0 = (1 - alpha) * v + alpha * (i ? e0 : v);
        e1 = (v - e0) * (1 - beta) + beta * e1;
        e2 = (e0 + pr * e1 - (i ? j : v)) * (1 - alpha) ** 2 + alpha ** 2 * e2;
        j = e2 + (i ? j : v);
        return i < n ? null : j;
      });
    }, { desk: true, isNew: true }),
  overlayMa("mahamming", "Moving Average Hamming", "HMA-w", "Average weighted by a Hamming window (bell-shaped weights).",
    [len("length", "Length", 10, 2000), SRC_INPUT], "#7E57C2", (c, inp) => {
      const n = num(inp.length, 10);
      const w = Array.from({ length: n }, (_, j) => (n === 1 ? 1 : 0.54 - 0.46 * Math.cos((2 * Math.PI * j) / (n - 1))));
      return weighted(src(c, inp.source), n, w);
    }),
  {
    type: "matriple", name: "Moving Average Triple", short: "MA×3", category: "Moving averages", overlay: true,
    desc: "Three moving averages of the same type.",
    inputs: [{ key: "maType", label: "MA type", type: "select", def: "SMA", options: MA_TYPES }, SRC_INPUT, len("l1", "Length #1", 20, 2000), len("l2", "Length #2", 50, 2000), len("l3", "Length #3", 100, 2000)],
    plots: [
      { key: "m1", label: "MA #1", color: "#F6C309" },
      { key: "m2", label: "MA #2", color: "#FB9800" },
      { key: "m3", label: "MA #3", color: "#F60C0C" },
    ],
    compute: (c, inp) => {
      const s = src(c, inp.source);
      const v = c.map((k) => k.volume);
      return { plots: [20, 50, 100].map((d, i) => ma(str(inp.maType, "SMA"), s, num(inp[`l${i + 1}`], d), v)) };
    },
  },
  {
    type: "mamulti", name: "Moving Average Multiple", short: "MA×6", category: "Moving averages", overlay: true,
    desc: "Six independently configured moving averages.",
    inputs: [SRC_INPUT, ...[5, 10, 20, 50, 100, 200].flatMap((d, i): InputDef[] => [
      { key: `t${i + 1}`, label: `MA #${i + 1} type`, type: "select", def: i < 3 ? "EMA" : "SMA", options: MA_TYPES },
      len(`l${i + 1}`, `MA #${i + 1} length`, d, 2000),
    ])],
    plots: ["#F6C309", "#FB9800", "#FB6500", "#F60C0C", "#AB47BC", "#2962FF"].map((color, i) => ({ key: `m${i + 1}`, label: `MA #${i + 1}`, color })),
    compute: (c, inp) => {
      const s = src(c, inp.source);
      const v = c.map((k) => k.volume);
      return { plots: [5, 10, 20, 50, 100, 200].map((d, i) => ma(str(inp[`t${i + 1}`], i < 3 ? "EMA" : "SMA"), s, num(inp[`l${i + 1}`], d), v)) };
    },
  },
  {
    type: "guppy", name: "Guppy Multiple Moving Average", short: "GMMA", category: "Moving averages", overlay: true,
    desc: "Six short (traders) and six long (investors) EMAs — compression and spread show trend strength.",
    inputs: [SRC_INPUT],
    plots: [
      ...GUPPY_SHORT.map((n) => ({ key: `s${n}`, label: `EMA ${n}`, color: "#00B8D4" })),
      ...GUPPY_LONG.map((n) => ({ key: `l${n}`, label: `EMA ${n}`, color: "#F44336" })),
    ],
    compute: (c, inp) => {
      const s = src(c, inp.source);
      return { plots: [...GUPPY_SHORT, ...GUPPY_LONG].map((n) => ema(s, n)) };
    },
  },
  crossDef("emacross", "EMA Cross", "EMA Cross", "Fast and slow EMAs; dots mark each crossover.", "EMA", 9, "EMA", 26),
  crossDef("macross", "MA Cross", "MA Cross", "Fast and slow SMAs; dots mark each crossover.", "SMA", 9, "SMA", 21),
  crossDef("maemacross", "MA with EMA Cross", "MA/EMA Cross", "An SMA and an EMA of the same length; dots mark crossovers.", "SMA", 10, "EMA", 10),

  // ---- bands & channels -----------------------------------------------------
  pane("bbpb", "Bollinger Bands %B", "%B", "Bands & channels", "Where price sits inside the Bollinger Bands: 1 = upper band, 0 = lower band.",
    [len("length", "Length", 20, 1000), SRC_INPUT, flt("mult", "StdDev", 2, 0.1, 0.1, 10)],
    [{ key: "pb", label: "%B", color: "#26A69A" }],
    (c, inp) => {
      const s = src(c, inp.source);
      const b = bollinger(s, num(inp.length, 20), num(inp.mult, 2));
      return { plots: [s.map((v, i) => (b.upper[i] == null || b.lower[i] == null || b.upper[i] === b.lower[i] ? null : (v - (b.lower[i] as number)) / ((b.upper[i] as number) - (b.lower[i] as number))))] };
    }, { levels: [1, 0.5, 0], fills: [{ key: "bg", label: "Background", a: { level: 1 }, b: { level: 0 }, color: "rgba(38,166,154,0.08)" }] }),
  pane("bbw", "Bollinger Bands Width", "BBW", "Bands & channels", "Band width relative to the basis — squeezes show as lows.",
    [len("length", "Length", 20, 1000), SRC_INPUT, flt("mult", "StdDev", 2, 0.1, 0.1, 10)],
    [{ key: "w", label: "BBW", color: "#2962FF" }],
    (c, inp) => {
      const b = bollinger(src(c, inp.source), num(inp.length, 20), num(inp.mult, 2));
      return { plots: [b.middle.map((m, i) => (m == null || !m || b.upper[i] == null || b.lower[i] == null ? null : ((b.upper[i] as number) - (b.lower[i] as number)) / m))] };
    }),
  {
    type: "machannel", name: "Moving Average Channel", short: "MAC", category: "Bands & channels", overlay: true,
    desc: "Moving average of the highs above, of the lows below.",
    inputs: [len("upLen", "Upper length", 20, 1000), len("loLen", "Lower length", 20, 1000), { key: "upOff", label: "Upper offset", type: "int", def: 0, min: -500, max: 500 }, { key: "loOff", label: "Lower offset", type: "int", def: 0, min: -500, max: 500 }],
    plots: [
      { key: "u", label: "Upper", color: "#2962FF" },
      { key: "l", label: "Lower", color: "#F44336" },
    ],
    fills: [{ key: "bg", label: "Background", a: 0, b: 1, color: "rgba(41,98,255,0.06)" }],
    compute: (c, inp) => ({
      plots: [sma(c.map((k) => k.high), num(inp.upLen, 20)), sma(c.map((k) => k.low), num(inp.loLen, 20))],
      shifts: [num(inp.upOff, 0), num(inp.loOff, 0)],
    }),
  },
  {
    type: "pricechannel", name: "Price Channel", short: "PC", category: "Bands & channels", overlay: true,
    desc: "Highest high and lowest low of the last N bars with the centre line.",
    inputs: [len("length", "Length", 20, 1000), { key: "offset", label: "Offset", type: "int", def: 0, min: -500, max: 500 }],
    plots: [
      { key: "h", label: "Highprice line", color: "#F50057" },
      { key: "l", label: "Lowprice line", color: "#F50057" },
      { key: "m", label: "Centerline", color: "#2962FF" },
    ],
    compute: (c, inp) => {
      const n = num(inp.length, 20);
      const hh = highest(c.map((k) => k.high), n);
      const ll = lowest(c.map((k) => k.low), n);
      const o = num(inp.offset, 0);
      return { plots: [hh, ll, map2(hh, ll, (h, l) => (h + l) / 2)], shifts: [o, o, o] };
    },
  },
  {
    type: "seb", name: "Standard Error Bands", short: "SEB", category: "Bands & channels", overlay: true,
    desc: "Linear-regression line with bands N standard errors away, smoothed.",
    inputs: [len("length", "Length", 21, 1000), flt("mult", "Standard errors", 2, 0.1, 0.1, 10), len("avg", "Averaging length", 3, 100), SRC_INPUT],
    plots: [
      { key: "u", label: "Upper", color: "#2962FF" },
      { key: "m", label: "Basis", color: "#FF6D00" },
      { key: "l", label: "Lower", color: "#2962FF" },
    ],
    fills: [{ key: "bg", label: "Background", a: 0, b: 2, color: "rgba(41,98,255,0.06)" }],
    compute: (c, inp) => {
      const n = num(inp.length, 21);
      const k = num(inp.mult, 2);
      const avg = num(inp.avg, 3);
      const s = src(c, inp.source);
      const lr: Arr = [];
      const se: Arr = [];
      s.forEach((_, i) => {
        if (i < n - 1) {
          lr.push(null);
          se.push(null);
          return;
        }
        const w = s.slice(i - n + 1, i + 1);
        const { icpt, slope } = fitLine(w);
        let ss = 0;
        w.forEach((v, j) => (ss += (v - (icpt + slope * j)) ** 2));
        lr.push(icpt + slope * (n - 1));
        se.push(Math.sqrt(ss / Math.max(1, n - 2)));
      });
      const up = smaN(map2(lr, se, (a, b) => a + k * b), avg);
      const lo = smaN(map2(lr, se, (a, b) => a - k * b), avg);
      return { plots: [up, smaN(lr, avg), lo] };
    },
  },
  {
    type: "ckstop", name: "Chande Kroll Stop", short: "CK Stop", category: "Bands & channels", overlay: true,
    desc: "ATR stops off the recent extremes: long stop below, short stop above.",
    inputs: [len("p", "ATR length (p)", 10), flt("x", "ATR coefficient (x)", 1, 0.1, 0.1, 10), len("q", "Stop length (q)", 9)],
    plots: [
      { key: "short", label: "Stop short", color: "#EF5350" },
      { key: "long", label: "Stop long", color: "#26A69A" },
    ],
    compute: (c, inp) => {
      const p = num(inp.p, 10);
      const x = num(inp.x, 1);
      const q = num(inp.q, 9);
      const a = atr(c, p);
      const hs = map2(highest(c.map((k) => k.high), p), a, (h, v) => h - x * v);
      const ls = map2(lowest(c.map((k) => k.low), p), a, (l, v) => l + x * v);
      return { plots: [highest(hs, q), lowest(ls, q)] };
    },
  },

  // ---- oscillators ----------------------------------------------------------
  pane("ac", "Accelerator Oscillator", "AC", "Oscillators", "Awesome Oscillator minus its 5-bar SMA — momentum of momentum.", [],
    [{ key: "ac", label: "AC", color: "#26A69A", kind: "hist" }],
    (c, _inp, ctx) => {
      const mid = c.map((k) => (k.high + k.low) / 2);
      const ao = map2(sma(mid, 5), sma(mid, 34), (a, b) => a - b);
      const ac = map2(ao, smaN(ao, 5), (a, b) => a - b);
      return { plots: [ac], colors: [ac.map((v, i) => (v == null ? null : i && ac[i - 1] != null && v < (ac[i - 1] as number) ? ctx.down : ctx.up))] };
    }, { levels: [0] }),
  pane("cmo", "Chande Momentum Oscillator", "CMO", "Oscillators", "Sum of gains minus losses over their total, −100 to 100.",
    [len("length", "Length", 9), SRC_INPUT], [{ key: "cmo", label: "CMO", color: "#2962FF" }],
    (c, inp) => {
      const m = change(src(c, inp.source));
      const n = num(inp.length, 9);
      const up = rsum(m.map((v) => (v == null ? null : Math.max(0, v))), n);
      const dn = rsum(m.map((v) => (v == null ? null : Math.max(0, -v))), n);
      return { plots: [map2(up, dn, (a, b) => (a + b === 0 ? 0 : (100 * (a - b)) / (a + b)))] };
    }, { levels: [50, 0, -50] }),
  pane("crsi", "Connors RSI", "CRSI", "Oscillators", "Average of RSI(3), RSI of the up/down streak and the percent rank of the 1-bar return.",
    [len("rsiLen", "RSI length", 3), len("streakLen", "Up/down length", 2), len("rankLen", "ROC length", 100, 1000)],
    [{ key: "crsi", label: "CRSI", color: "#2962FF" }],
    (c, inp) => {
      const cl = c.map((k) => k.close);
      const r = rsi(cl, num(inp.rsiLen, 3));
      const streak: number[] = [];
      cl.forEach((v, i) => {
        const p = streak[i - 1] ?? 0;
        streak.push(i === 0 ? 0 : v > cl[i - 1] ? (p >= 0 ? p + 1 : 1) : v < cl[i - 1] ? (p <= 0 ? p - 1 : -1) : 0);
      });
      const rs = rsi(streak, num(inp.streakLen, 2));
      const pr = percentRank(cl.map((v, i) => (i === 0 || !cl[i - 1] ? null : ((v - cl[i - 1]) / cl[i - 1]) * 100)), num(inp.rankLen, 100));
      return { plots: [r.map((v, i) => (ok(v) && ok(rs[i]) && ok(pr[i]) ? (v + (rs[i] as number) + (pr[i] as number)) / 3 : null))] };
    }, { levels: [70, 30], fills: [{ key: "bg", label: "Background", a: { level: 70 }, b: { level: 30 }, color: "rgba(126,87,194,0.08)" }] }),
  pane("coppock", "Coppock Curve", "Coppock", "Oscillators", "WMA of the sum of a long and a short rate of change.",
    [len("wma", "WMA length", 10), len("long", "Long RoC length", 14), len("short", "Short RoC length", 11)],
    [{ key: "cc", label: "Coppock", color: "#2962FF" }],
    (c, inp) => {
      const cl = c.map((k) => k.close);
      const roc = (n: number): Arr => cl.map((v, i) => (i < n || !cl[i - n] ? null : ((v - cl[i - n]) / cl[i - n]) * 100));
      return { plots: [ma("WMA", map2(roc(num(inp.long, 14)), roc(num(inp.short, 11)), (a, b) => a + b), num(inp.wma, 10))] };
    }, { levels: [0] }),
  pane("dpo", "Detrended Price Oscillator", "DPO", "Oscillators", "Close minus a displaced moving average — isolates cycles.",
    [len("length", "Period", 21)], [{ key: "dpo", label: "DPO", color: "#43A047" }],
    (c, inp) => {
      const n = num(inp.length, 21);
      const back = Math.floor(n / 2) + 1;
      const m = sma(c.map((k) => k.close), n);
      return { plots: [c.map((k, i) => (i < back || m[i - back] == null ? null : k.close - (m[i - back] as number)))] };
    }, { levels: [0] }),
  pane("fisher", "Fisher Transform", "Fisher", "Oscillators", "Gaussian-normalised price: sharp turning points.",
    [len("length", "Length", 9)],
    [
      { key: "f", label: "Fisher", color: "#2962FF" },
      { key: "t", label: "Trigger", color: "#FF6D00" },
    ],
    (c, inp) => {
      const n = num(inp.length, 9);
      const hl2 = c.map((k) => (k.high + k.low) / 2);
      const hh = highest(hl2, n);
      const ll = lowest(hl2, n);
      let value = 0;
      let fish = 0;
      const f: Arr = [];
      const t: Arr = [];
      hl2.forEach((v, i) => {
        if (hh[i] == null || ll[i] == null) {
          f.push(null);
          t.push(null);
          return;
        }
        const r = Math.max((hh[i] as number) - (ll[i] as number), 0.001);
        value = Math.max(-0.999, Math.min(0.999, 0.66 * ((v - (ll[i] as number)) / r - 0.5) + 0.67 * value));
        const prev = fish;
        fish = 0.5 * Math.log((1 + value) / (1 - value)) + 0.5 * fish;
        f.push(fish);
        t.push(f.length > 1 && f[f.length - 2] != null ? prev : null);
      });
      return { plots: [f, t] };
    }, { levels: [1.5, 0.75, 0, -0.75, -1.5] }),
  pane("kst", "Know Sure Thing", "KST", "Oscillators", "Weighted sum of four smoothed rates of change, with a signal line.",
    [len("r1", "RoC length #1", 10), len("r2", "RoC length #2", 15), len("r3", "RoC length #3", 20), len("r4", "RoC length #4", 30), len("s1", "SMA length #1", 10), len("s2", "SMA length #2", 10), len("s3", "SMA length #3", 10), len("s4", "SMA length #4", 15), len("sig", "Signal length", 9)],
    [
      { key: "kst", label: "KST", color: "#089981" },
      { key: "sig", label: "Signal", color: "#F23645" },
    ],
    (c, inp) => {
      const cl = c.map((k) => k.close);
      const roc = (n: number): Arr => cl.map((v, i) => (i < n || !cl[i - n] ? null : ((v - cl[i - n]) / cl[i - n]) * 100));
      const parts = [1, 2, 3, 4].map((k) => smaN(roc(num(inp[`r${k}`], [10, 15, 20, 30][k - 1])), num(inp[`s${k}`], [10, 10, 10, 15][k - 1])));
      const kst = parts[0].map((_, i) => (parts.every((p) => ok(p[i])) ? parts.reduce((s, p, k) => s + (k + 1) * (p[i] as number), 0) : null));
      return { plots: [kst, smaN(kst, num(inp.sig, 9))] };
    }, { levels: [0] }),
  pane("priceosc", "Price Oscillator", "PPO", "Oscillators", "Gap between a short and a long moving average, as % of the long.",
    [len("short", "Short length", 10), len("long", "Long length", 21), { key: "exp", label: "Exponential", type: "bool", def: false }, SRC_INPUT],
    [{ key: "po", label: "Osc", color: "#089981" }],
    (c, inp) => {
      const s = src(c, inp.source);
      const t = bool(inp.exp, false) ? "EMA" : "SMA";
      const a = ma(t, s, num(inp.short, 10));
      const b = ma(t, s, num(inp.long, 21));
      return { plots: [map2(a, b, (x, y) => (y === 0 ? null : ((x - y) / y) * 100))] };
    }, { levels: [0] }),
  pane("rci", "Rank Correlation Index", "RCI", "Oscillators", "Spearman correlation between price ranks and time, −100 to 100.",
    [len("length", "Length", 10), SRC_INPUT], [{ key: "rci", label: "RCI", color: "#2962FF" }],
    (c, inp) => {
      const n = num(inp.length, 10);
      return {
        plots: [rolling(src(c, inp.source), n, (w) => {
          const order = w.map((v, i) => ({ v, i })).sort((a, b) => a.v - b.v);
          const rank = new Array<number>(n);
          for (let k = 0; k < n;) {
            let j = k;
            while (j + 1 < n && order[j + 1].v === order[k].v) j++;
            const r = (k + j) / 2 + 1;
            for (let m = k; m <= j; m++) rank[order[m].i] = r;
            k = j + 1;
          }
          let d = 0;
          for (let i = 0; i < n; i++) d += (i + 1 - rank[i]) ** 2;
          return n > 1 ? (1 - (6 * d) / (n * (n * n - 1))) * 100 : null;
        })],
      };
    }, { levels: [80, 0, -80] }),
  pane("rvgi", "Relative Vigor Index", "RVGI", "Oscillators", "Close − open relative to the range, smoothed — conviction behind moves.",
    [len("length", "Length", 10)],
    [
      { key: "rvi", label: "RVGI", color: "#089981" },
      { key: "sig", label: "Signal", color: "#F23645" },
    ],
    (c, inp) => {
      const n = num(inp.length, 10);
      const nu = rsum(swma(c.map((k) => k.close - k.open)), n);
      const de = rsum(swma(c.map((k) => k.high - k.low)), n);
      const r = map2(nu, de, (a, b) => (b === 0 ? 0 : a / b));
      return { plots: [r, swma(r)] };
    }, { levels: [0] }),
  pane("smii", "SMI Ergodic Indicator", "SMII", "Oscillators", "True strength index (−1 to 1) with its EMA signal.",
    [len("short", "Short length", 5), len("long", "Long length", 20), len("sig", "Signal length", 5)],
    [
      { key: "erg", label: "SMI", color: "#2962FF" },
      { key: "sig", label: "Signal", color: "#FF6D00" },
    ],
    (c, inp) => {
      const erg = tsiRaw(c.map((k) => k.close), num(inp.short, 5), num(inp.long, 20));
      return { plots: [erg, emaN(erg, num(inp.sig, 5))] };
    }, { levels: [0] }),
  pane("smio", "SMI Ergodic Oscillator", "SMIO", "Oscillators", "SMI Ergodic minus its signal, as a histogram.",
    [len("short", "Short length", 5), len("long", "Long length", 20), len("sig", "Signal length", 5)],
    [{ key: "osc", label: "SMIO", color: "#FF5252", kind: "hist" }],
    (c, inp, ctx) => {
      const erg = tsiRaw(c.map((k) => k.close), num(inp.short, 5), num(inp.long, 20));
      const osc = map2(erg, emaN(erg, num(inp.sig, 5)), (a, b) => a - b);
      return { plots: [osc], colors: [osc.map((v) => (v == null ? null : v >= 0 ? ctx.up : ctx.down))] };
    }, { levels: [0] }),
  pane("tsi", "True Strength Index", "TSI", "Oscillators", "Double-smoothed momentum ×100 with a signal line.",
    [len("long", "Long length", 25), len("short", "Short length", 13), len("sig", "Signal length", 13)],
    [
      { key: "tsi", label: "TSI", color: "#2962FF" },
      { key: "sig", label: "Signal", color: "#E91E63" },
    ],
    (c, inp) => {
      const t = tsiRaw(c.map((k) => k.close), num(inp.short, 13), num(inp.long, 25)).map((v) => (v == null ? null : v * 100));
      return { plots: [t, emaN(t, num(inp.sig, 13))] };
    }, { levels: [0] }),
  pane("uo", "Ultimate Oscillator", "UO", "Oscillators", "Buying pressure over three windows (7 / 14 / 28), weighted 4:2:1.",
    [len("f", "Fast length", 7), len("m", "Middle length", 14), len("s", "Slow length", 28)],
    [{ key: "uo", label: "UO", color: "#F44336" }],
    (c, inp) => {
      const bp = c.map((k, i) => k.close - Math.min(k.low, i ? c[i - 1].close : k.low));
      const tr = c.map((k, i) => Math.max(k.high, i ? c[i - 1].close : k.high) - Math.min(k.low, i ? c[i - 1].close : k.low));
      const avg = (n: number) => map2(rsum(bp, n), rsum(tr, n), (a, b) => (b === 0 ? 0 : a / b));
      const a = avg(num(inp.f, 7));
      const b = avg(num(inp.m, 14));
      const d = avg(num(inp.s, 28));
      return { plots: [a.map((v, i) => (ok(v) && ok(b[i]) && ok(d[i]) ? (100 * (4 * v + 2 * (b[i] as number) + (d[i] as number))) / 7 : null))] };
    }, { levels: [70, 50, 30] }),
  pane("lrslope", "Linear Regression Slope", "LR Slope", "Oscillators", "Slope (price per bar) of the least-squares line over N bars.",
    [len("length", "Length", 14), SRC_INPUT], [{ key: "s", label: "Slope", color: "#FF5252" }],
    (c, inp) => ({ plots: [rolling(src(c, inp.source), num(inp.length, 14), (w) => fitLine(w).slope)] }), { levels: [0] }),
  pane("trendstrength", "Trend Strength Index", "TSI-T", "Oscillators", "Correlation of price with time over N bars: +1 steady uptrend, −1 steady downtrend.",
    [len("length", "Length", 14), SRC_INPUT], [{ key: "t", label: "Trend strength", color: "#2962FF" }],
    (c, inp) => ({ plots: [correlation(src(c, inp.source), c.map((_, i) => i), num(inp.length, 14))] }), { levels: [0.5, 0, -0.5] }),
  pane("chopzone", "Chop Zone", "Chop Zone", "Oscillators", "Colour-coded angle of the 34 EMA: blues/greens trending up, reds/oranges trending down, yellow chop.",
    [len("periods", "Periods", 30)], [{ key: "cz", label: "Chop zone", color: "#FFEB3B", kind: "columns" }],
    (c, inp) => {
      const n = num(inp.periods, 30);
      const e = ema(c.map((k) => k.close), 34);
      const hh = highest(c.map((k) => k.high), n);
      const ll = lowest(c.map((k) => k.low), n);
      const vals: Arr = [];
      const colors: (string | null)[] = [];
      c.forEach((k, i) => {
        if (i === 0 || e[i] == null || e[i - 1] == null || hh[i] == null || ll[i] == null || hh[i] === ll[i]) {
          vals.push(null);
          colors.push(null);
          return;
        }
        const span = (25 / ((hh[i] as number) - (ll[i] as number))) * (ll[i] as number);
        const y2 = (((e[i - 1] as number) - (e[i] as number)) / ((k.high + k.low + k.close) / 3)) * span;
        const cc = Math.sqrt(1 + y2 * y2);
        const ang0 = Math.round((180 * Math.acos(1 / cc)) / Math.PI);
        const ang = y2 > 0 ? -ang0 : ang0;
        const col = ang >= 5 ? "#26C6DA" : ang >= 3.57 ? "#43A047" : ang >= 2.14 ? "#A5D6A7" : ang >= 0.71 ? "#00E676"
          : ang <= -5 ? "#D50000" : ang <= -3.57 ? "#E91E63" : ang <= -2.14 ? "#FF6D00" : ang <= -0.71 ? "#FFB74D" : "#FFEB3B";
        vals.push(1);
        colors.push(col);
      });
      return { plots: [vals], colors: [colors] };
    }),
  pane("truerange", "True Range", "TR", "Volatility", "Each bar's true range: max(high, prev close) − min(low, prev close).", [],
    [{ key: "tr", label: "TR", color: "#FF7043" }],
    (c) => ({ plots: [trueRange(c)] }), { desk: true }),

  // ---- trend ----------------------------------------------------------------
  pane("vortex", "Vortex Indicator", "VI", "Trend", "+VI / −VI: upward vs downward trend movement over N bars.",
    [len("length", "Period", 14)],
    [
      { key: "p", label: "VI +", color: "#2962FF" },
      { key: "m", label: "VI −", color: "#E91E63" },
    ],
    (c, inp) => {
      const n = num(inp.length, 14);
      const vmp = rsum(c.map((k, i) => (i ? Math.abs(k.high - c[i - 1].low) : null)), n);
      const vmm = rsum(c.map((k, i) => (i ? Math.abs(k.low - c[i - 1].high) : null)), n);
      const str_ = rsum(trueRange(c).map((v, i) => (i ? v : null)), n);
      return { plots: [map2(vmp, str_, (a, b) => (b ? a / b : null)), map2(vmm, str_, (a, b) => (b ? a / b : null))] };
    }, { levels: [1] }),
  pane("massindex", "Mass Index", "Mass", "Trend", "Sum of the ratio of single to double EMA of the range — reversal bulges.",
    [len("length", "Length", 10)], [{ key: "mi", label: "Mass index", color: "#2962FF" }],
    (c, inp) => {
      const span = c.map((k) => k.high - k.low);
      const e1 = ema(span, 9);
      const e2 = emaN(e1, 9);
      return { plots: [rsum(map2(e1, e2, (a, b) => (b ? a / b : null)), num(inp.length, 10))] };
    }, { levels: [27, 26.5] }),
  pane("majority", "Majority Rule", "MR", "Trend", "Share of the last N bars that closed up, in %.",
    [len("length", "Length", 14)], [{ key: "mr", label: "Majority rule", color: "#FF6D00" }],
    (c, inp) => ({ plots: [rsum(c.map((k, i) => (i ? (k.close > c[i - 1].close ? 1 : 0) : null)), num(inp.length, 14)).map((v) => (v == null ? null : (v / num(inp.length, 14)) * 100))] }),
    { levels: [50] }),
  {
    type: "alligator", name: "Williams Alligator", short: "Alligator", category: "Trend", overlay: true,
    desc: "Jaw, teeth and lips: smoothed averages of the median price shifted forward.",
    inputs: [len("jaw", "Jaw length", 13), len("teeth", "Teeth length", 8), len("lips", "Lips length", 5), len("jawOff", "Jaw offset", 8), len("teethOff", "Teeth offset", 5), len("lipsOff", "Lips offset", 3)],
    plots: [
      { key: "jaw", label: "Jaw", color: "#2962FF" },
      { key: "teeth", label: "Teeth", color: "#E91E63" },
      { key: "lips", label: "Lips", color: "#66BB6A" },
    ],
    compute: (c, inp) => {
      const hl2 = c.map((k) => (k.high + k.low) / 2);
      return {
        plots: [smma(hl2, num(inp.jaw, 13)), smma(hl2, num(inp.teeth, 8)), smma(hl2, num(inp.lips, 5))],
        shifts: [num(inp.jawOff, 8), num(inp.teethOff, 5), num(inp.lipsOff, 3)],
      };
    },
  },
  {
    type: "fractals", name: "Williams Fractal", short: "Fractals", category: "Trend", overlay: true,
    desc: "Swing highs / lows: a bar higher (lower) than N bars on each side.",
    inputs: [len("periods", "Periods", 2, 50)],
    plots: [{ key: "anchor", label: "Fractals", color: "#26A69A", kind: "marks" }],
    compute: (c, inp) => {
      const n = num(inp.periods, 2);
      const { highs, lows } = pivots(c, n, n);
      const anchor: Arr = c.map(() => null);
      const marks: Mark[] = [];
      for (const i of highs) {
        anchor[i] = c[i].high;
        marks.push({ plot: 0, i, pos: "above", shape: "arrowUp", color: "#26A69A" });
      }
      for (const i of lows) {
        if (anchor[i] != null) continue;
        anchor[i] = c[i].low;
        marks.push({ plot: 0, i, pos: "below", shape: "arrowDown", color: "#EF5350" });
      }
      return { plots: [anchor], marks };
    },
  },
  {
    type: "zigzag", name: "Zig Zag", short: "ZigZag", category: "Trend", overlay: true,
    desc: "Connects swings that reversed by at least the deviation %, ignoring smaller noise.",
    inputs: [flt("dev", "Price deviation %", 5, 0.1, 0.1, 100), len("depth", "Pivot legs (depth)", 10, 200)],
    plots: [{ key: "zz", label: "Zig Zag", color: "#2962FF", width: 2 }],
    compute: (c, inp) => ({ plots: [interpolate(c.length, zigzag(c, num(inp.dev, 5), num(inp.depth, 10)))] }),
  },
  pane("asi", "Accumulative Swing Index", "ASI", "Trend", "Running total of Wilder's swing index — trend confirmation and breakouts.",
    [flt("limit", "Limit move value", 10, 1, 0.01, 1e6)], [{ key: "asi", label: "ASI", color: "#2962FF" }],
    (c, inp) => {
      const t = num(inp.limit, 10);
      let acc = 0;
      return {
        plots: [c.map((k, i) => {
          if (i === 0) return null;
          const p = c[i - 1];
          const hc = Math.abs(k.high - p.close);
          const lc = Math.abs(k.low - p.close);
          const hl = k.high - k.low;
          const cp = Math.abs(p.close - p.open);
          const r = hc >= lc && hc >= hl ? hc - 0.5 * lc + 0.25 * cp : lc >= hc && lc >= hl ? lc - 0.5 * hc + 0.25 * cp : hl + 0.25 * cp;
          const kk = Math.max(hc, lc);
          if (r !== 0 && t !== 0) acc += ((50 * (k.close - p.close + 0.5 * (k.close - k.open) + 0.25 * (p.close - p.open))) / r) * (kk / t);
          return acc;
        })],
      };
    }),
  {
    type: "autofib", name: "Auto Fib Retracement", short: "Auto Fib", category: "Trend", overlay: true, desk: true,
    desc: "Fibonacci retracement drawn automatically over the latest zig zag swing (a swing must reverse by the deviation × ATR(10) %).",
    inputs: [flt("dev", "Deviation (× ATR %)", 3, 0.1, 0.1, 100), len("depth", "Depth", 10, 200), { key: "reverse", label: "Reverse", type: "bool", def: false }],
    plots: FIB_LEVELS.map((r, i) => ({ key: `f${i}`, label: String(r), color: FIB_COLORS[i], kind: "step" as const })),
    compute: (c, inp) => {
      // TradingView's auto fib sizes the reversal from volatility: dev × ATR(10) as % of price
      const at = atr(c, 10);
      const pcts = at.map((v, i) => (v == null || !c[i].close ? null : (v / c[i].close) * 100)).filter(ok);
      const atrPct = pcts.length ? mean(pcts.slice(-200)) : 1;
      const zz = zigzag(c, Math.max(0.05, num(inp.dev, 3) * atrPct), num(inp.depth, 10));
      const out = FIB_LEVELS.map(() => c.map((): number | null => null));
      if (zz.length < 2) return { plots: out };
      const a = zz[zz.length - 2];
      const b = zz[zz.length - 1];
      const [p0, p1] = bool(inp.reverse, false) ? [a.price, b.price] : [b.price, a.price];
      FIB_LEVELS.forEach((r, k) => {
        for (let i = a.i; i < c.length; i++) out[k][i] = p0 + (p1 - p0) * r;
      });
      return { plots: out };
    },
  },
  {
    type: "adr", name: "Average Day Range", short: "ADR", category: "Volatility", overlay: true, desk: true,
    desc: "Average high − low of the last N sessions, projected from today's low (target up) and high (target down).",
    inputs: [len("length", "Days", 14, 200)],
    plots: [
      { key: "up", label: "Low + ADR", color: "#26A69A", kind: "step", dash: 2 },
      { key: "dn", label: "High − ADR", color: "#EF5350", kind: "step", dash: 2 },
    ],
    compute: (c, inp, ctx) => {
      const n = num(inp.length, 14);
      const daily = ["days", "weeks", "months"].includes(intervalGroup(ctx.interval));
      const ranges: number[] = [];
      const up: Arr = [];
      const dn: Arr = [];
      let day = -1;
      let h = -Infinity;
      let l = Infinity;
      c.forEach((k) => {
        const d = daily ? k.time : dayOf(k.time);
        if (d !== day) {
          if (day !== -1) ranges.push(h - l);
          day = d;
          h = -Infinity;
          l = Infinity;
        }
        h = Math.max(h, k.high);
        l = Math.min(l, k.low);
        if (ranges.length < n) {
          up.push(null);
          dn.push(null);
          return;
        }
        const adr = mean(ranges.slice(-n));
        up.push(l + adr);
        dn.push(h - adr);
      });
      return { plots: [up, dn] };
    },
  },
  cprDef("cprw", "CPR with Pivot levels Weekly", "W"),
  cprDef("cprm", "CPR with Pivot levels Monthly", "M"),
  {
    type: "boring", name: "Boring Candle", short: "Boring", category: "Trend", overlay: true, desk: true,
    desc: "Marks small-body (\"boring\") candles — bodies at most the given % of the range — that often form supply / demand bases.",
    inputs: [flt("pct", "Max body % of range", 50, 1, 1, 100)],
    plots: [{ key: "anchor", label: "Boring", color: "#FFB300", kind: "marks" }],
    compute: (c, inp) => {
      const pct = num(inp.pct, 50) / 100;
      const anchor: Arr = c.map(() => null);
      const marks: Mark[] = [];
      c.forEach((k, i) => {
        const r = k.high - k.low;
        if (r > 0 && Math.abs(k.close - k.open) <= r * pct) {
          anchor[i] = k.high;
          marks.push({ plot: 0, i, pos: "above", shape: "circle", color: "#FFB300" });
        }
      });
      return { plots: [anchor], marks };
    },
  },
  {
    type: "cdlpatterns", name: "Candlestick Patterns", short: "Patterns", category: "Trend", overlay: true, desk: true,
    desc: "Detects engulfing, harami, stars, hammers, dojis, marubozu, piercing / dark cloud and three soldiers / crows.",
    inputs: [
      { key: "bull", label: "Bullish patterns", type: "bool", def: true },
      { key: "bear", label: "Bearish patterns", type: "bool", def: true },
      { key: "neutral", label: "Doji", type: "bool", def: true },
      { key: "labels", label: "Show labels", type: "bool", def: true },
    ],
    plots: [{ key: "anchor", label: "Pattern", color: "#2962FF", kind: "marks" }],
    compute: (c, inp) => {
      const anchor: Arr = c.map(() => null);
      const marks: Mark[] = [];
      for (const h of candlePatterns(c)) {
        if ((h.bias === 1 && !bool(inp.bull, true)) || (h.bias === -1 && !bool(inp.bear, true)) || (h.bias === 0 && !bool(inp.neutral, true))) continue;
        const above = h.bias <= 0;
        anchor[h.i] = above ? c[h.i].high : c[h.i].low;
        marks.push({
          plot: 0, i: h.i, pos: above ? "above" : "below",
          shape: h.bias === 1 ? "arrowUp" : h.bias === -1 ? "arrowDown" : "circle",
          color: h.bias === 1 ? "#26A69A" : h.bias === -1 ? "#EF5350" : "#787B86",
          text: bool(inp.labels, true) ? h.code : undefined, title: h.name,
        });
      }
      return { plots: [anchor], marks };
    },
  },
  pane("rsidiv", "RSI Momentum Divergence", "RSI Div", "Oscillators", "RSI with regular bullish / bearish divergences between price and RSI swings marked.",
    [len("length", "RSI length", 14), len("left", "Pivot lookback left", 5, 50), len("right", "Pivot lookback right", 5, 50), len("min", "Min bars between", 5, 200), len("max", "Max bars between", 60, 500)],
    [{ key: "rsi", label: "RSI", color: "#7E57C2" }],
    (c, inp) => {
      const r = rsi(c.map((k) => k.close), num(inp.length, 14));
      const left = num(inp.left, 5);
      const right = num(inp.right, 5);
      const lo = num(inp.min, 5);
      const hi = num(inp.max, 60);
      // pivots of the RSI itself (TradingView's divergence script pivots the oscillator)
      const piv = (want: "hi" | "lo") => {
        const out: number[] = [];
        for (let i = left; i < r.length - right; i++) {
          const v = r[i];
          if (!ok(v)) continue;
          let good = true;
          for (let j = i - left; j <= i + right && good; j++) {
            if (j === i || !ok(r[j])) continue;
            if (want === "hi" ? (r[j] as number) > v || (j > i && r[j] === v) : (r[j] as number) < v || (j > i && r[j] === v)) good = false;
          }
          if (good) out.push(i);
        }
        return out;
      };
      const marks: Mark[] = [];
      const lows = piv("lo");
      for (let k = 1; k < lows.length; k++) {
        const a = lows[k - 1];
        const b = lows[k];
        if (b - a < lo || b - a > hi) continue;
        if (c[b].low < c[a].low && (r[b] as number) > (r[a] as number)) marks.push({ plot: 0, i: b, pos: "below", shape: "arrowUp", color: "#26A69A", text: "Bull" });
      }
      const highs = piv("hi");
      for (let k = 1; k < highs.length; k++) {
        const a = highs[k - 1];
        const b = highs[k];
        if (b - a < lo || b - a > hi) continue;
        if (c[b].high > c[a].high && (r[b] as number) < (r[a] as number)) marks.push({ plot: 0, i: b, pos: "above", shape: "arrowDown", color: "#EF5350", text: "Bear" });
      }
      return { plots: [r], marks };
    }, { levels: [70, 50, 30], desk: true, fills: [{ key: "bg", label: "Background", a: { level: 70 }, b: { level: 30 }, color: "rgba(126,87,194,0.08)" }] }),
  {
    type: "avwap", name: "Anchored VWAP", short: "AVWAP", category: "Volume", overlay: true, desk: true,
    desc: "VWAP that restarts at each anchor period (session, week, month, quarter or year).",
    inputs: [{ key: "anchor", label: "Anchor period", type: "select", def: "Session", options: ["Session", "Week", "Month", "Quarter", "Year"] }, { ...SRC_INPUT, def: "hlc3" }],
    plots: [{ key: "vwap", label: "VWAP", color: "#2962FF", width: 2 }],
    compute: (c, inp) => ({ plots: [anchoredVwap(c, str(inp.anchor, "Session"), inp.source).vwap] }),
  },
  {
    type: "avwapbands", name: "Anchored VWAP With Bands", short: "AVWAP±σ", category: "Volume", overlay: true, desk: true,
    desc: "Anchored VWAP with ±1 / ±2 / ±3 volume-weighted standard-deviation bands.",
    inputs: [
      { key: "anchor", label: "Anchor period", type: "select", def: "Session", options: ["Session", "Week", "Month", "Quarter", "Year"] },
      { ...SRC_INPUT, def: "hlc3" },
      flt("m1", "Bands multiplier #1", 1, 0.1, 0.1, 10), flt("m2", "Bands multiplier #2", 2, 0.1, 0.1, 10), flt("m3", "Bands multiplier #3", 3, 0.1, 0.1, 10),
    ],
    plots: [
      { key: "vwap", label: "VWAP", color: "#2962FF", width: 2 },
      { key: "u1", label: "Upper #1", color: "#4CAF50" },
      { key: "l1", label: "Lower #1", color: "#4CAF50" },
      { key: "u2", label: "Upper #2", color: "#808000" },
      { key: "l2", label: "Lower #2", color: "#808000" },
      { key: "u3", label: "Upper #3", color: "#00897B" },
      { key: "l3", label: "Lower #3", color: "#00897B" },
    ],
    fills: [
      { key: "b1", label: "Bands fill #1", a: 1, b: 2, color: "rgba(76,175,80,0.08)" },
      { key: "b2", label: "Bands fill #2", a: 3, b: 1, color: "rgba(128,128,0,0.06)" },
      { key: "b2l", label: "Bands fill #2 (lower)", a: 2, b: 4, color: "rgba(128,128,0,0.06)" },
    ],
    compute: (c, inp) => {
      const { vwap, sd } = anchoredVwap(c, str(inp.anchor, "Session"), inp.source);
      const band = (m: number, sign: 1 | -1) => map2(vwap, sd, (v, s) => v + sign * m * s);
      const m = [num(inp.m1, 1), num(inp.m2, 2), num(inp.m3, 3)];
      return { plots: [vwap, band(m[0], 1), band(m[0], -1), band(m[1], 1), band(m[1], -1), band(m[2], 1), band(m[2], -1)] };
    },
  },
  pane("crs", "Comparative Relative Strength", "CRS", "Trend", "Performance against a benchmark over N bars: (close / close[N]) ÷ (bench / bench[N]) − 1. Above 0 = outperforming.",
    [BENCH, len("length", "Period", 50, 1000), len("ma", "MA length", 20, 500)],
    [
      { key: "rs", label: "RS", color: "#2962FF" },
      { key: "ma", label: "MA", color: "#FF6D00" },
    ],
    (c, inp, ctx) => {
      const other = otherCloses(c, ctx, str(inp.symbol, "NSE:NIFTY50-INDEX"));
      const n = num(inp.length, 50);
      const rs: Arr = c.map((k, i) => (i < n || !ok(other[i]) || !ok(other[i - n]) || !c[i - n].close || !other[i - n] ? null : k.close / c[i - n].close / ((other[i] as number) / (other[i - n] as number)) - 1));
      return { plots: [rs, smaN(rs, num(inp.ma, 20))] };
    }, { desk: true, levels: [0] }),
  pane("oi", "Open Interest", "OI", "Volume", "Open interest of the future / option per bar (derivatives only).", [],
    [{ key: "oi", label: "OI", color: "#2962FF", kind: "columns" }],
    (c, _inp, ctx) => {
      const v = c.map((k) => (ok(k.oi) ? (k.oi as number) : null));
      return { plots: [v], colors: [v.map((x, i) => (x == null || i === 0 || v[i - 1] == null ? null : x >= (v[i - 1] as number) ? ctx.up : ctx.down))] };
    }, { desk: true }),
  pane("oichg", "Open Interest Change", "OI Chg", "Volume", "Bar-to-bar change in open interest: green = positions added, red = unwound.", [],
    [{ key: "d", label: "OI change", color: "#26A69A", kind: "hist" }],
    (c, _inp, ctx) => {
      const v = c.map((k, i) => (i && ok(k.oi) && ok(c[i - 1].oi) ? (k.oi as number) - (c[i - 1].oi as number) : null));
      return { plots: [v], colors: [v.map((x) => (x == null ? null : x >= 0 ? ctx.up : ctx.down))] };
    }, { desk: true, levels: [0] }),
  {
    type: "oiprofile", name: "OI Profile", short: "OI Profile", category: "Volume", overlay: true, desk: true, special: "oiprofile",
    desc: "Call and put open interest of the option chain as horizontal bars at each strike (index / F&O underlyings).",
    inputs: [len("strikes", "Strikes each side", 10, 40), flt("width", "Max width % of pane", 25, 1, 5, 80)],
    plots: [],
    compute: () => ({ plots: [] }),
  },

  // ---- volume -----------------------------------------------------------------
  pane("ad", "Accumulation/Distribution", "A/D", "Volume", "Running total of volume weighted by where the close sits in the bar's range.", [],
    [{ key: "ad", label: "A/D", color: "#999915" }],
    (c) => {
      let acc = 0;
      return { plots: [c.map((k) => (acc += k.high === k.low ? 0 : (((k.close - k.low) - (k.high - k.close)) / (k.high - k.low)) * k.volume))] };
    }),
  pane("chaikinosc", "Chaikin Oscillator", "Chaikin Osc", "Volume", "Fast minus slow EMA of the accumulation / distribution line.",
    [len("fast", "Fast length", 3), len("slow", "Slow length", 10)], [{ key: "co", label: "Chaikin Osc", color: "#EC407A" }],
    (c, inp) => {
      let acc = 0;
      const ad = c.map((k) => (acc += k.high === k.low ? 0 : (((k.close - k.low) - (k.high - k.close)) / (k.high - k.low)) * k.volume));
      return { plots: [map2(ema(ad, num(inp.fast, 3)), ema(ad, num(inp.slow, 10)), (a, b) => a - b)] };
    }, { levels: [0] }),
  pane("eom", "Ease Of Movement", "EOM", "Volume", "Price change per unit of volume, smoothed — how easily price moves.",
    [len("length", "Length", 14), flt("div", "Divisor", 10000, 1, 1, 1e12)], [{ key: "eom", label: "EOM", color: "#43A047" }],
    (c, inp) => {
      const hl2 = c.map((k) => (k.high + k.low) / 2);
      const raw = c.map((k, i) => (i === 0 || !k.volume ? null : (num(inp.div, 10000) * (hl2[i] - hl2[i - 1]) * (k.high - k.low)) / k.volume));
      return { plots: [smaN(raw, num(inp.length, 14))] };
    }, { levels: [0] }),
  pane("efi", "Elder's Force Index", "EFI", "Volume", "Price change × volume, EMA-smoothed: the force behind a move.",
    [len("length", "Length", 13)], [{ key: "efi", label: "EFI", color: "#F44336" }],
    (c, inp) => ({ plots: [emaN(c.map((k, i) => (i ? (k.close - c[i - 1].close) * k.volume : null)), num(inp.length, 13))] }), { levels: [0] }),
  pane("klinger", "Klinger Oscillator", "KVO", "Volume", "Volume force: fast minus slow EMA of signed volume, with a signal line.",
    [len("fast", "Fast length", 34), len("slow", "Slow length", 55), len("sig", "Signal length", 13)],
    [
      { key: "kvo", label: "Klinger", color: "#2962FF" },
      { key: "sig", label: "Signal", color: "#43A047" },
    ],
    (c, inp) => {
      const hlc3 = c.map((k) => (k.high + k.low + k.close) / 3);
      const sv = c.map((k, i) => (i === 0 ? 0 : hlc3[i] - hlc3[i - 1] >= 0 ? k.volume : -k.volume));
      const kvo = map2(ema(sv, num(inp.fast, 34)), ema(sv, num(inp.slow, 55)), (a, b) => a - b);
      return { plots: [kvo, emaN(kvo, num(inp.sig, 13))] };
    }, { levels: [0] }),
  pane("netvol", "Net Volume", "Net Vol", "Volume", "Volume signed by the bar's close-to-close direction.", [],
    [{ key: "nv", label: "Net volume", color: "#2962FF" }],
    (c) => ({ plots: [c.map((k, i) => (i === 0 ? null : k.close > c[i - 1].close ? k.volume : k.close < c[i - 1].close ? -k.volume : 0))] }), { levels: [0] }),
  pane("pvt", "Price Volume Trend", "PVT", "Volume", "Running total of volume × percentage price change.", [SRC_INPUT],
    [{ key: "pvt", label: "PVT", color: "#2962FF" }],
    (c, inp) => {
      const s = src(c, inp.source);
      let acc = 0;
      return { plots: [s.map((v, i) => (i === 0 || !s[i - 1] ? (i === 0 ? null : acc) : (acc += ((v - s[i - 1]) / s[i - 1]) * c[i].volume)))] };
    }),
  pane("volosc", "Volume Oscillator", "Vol Osc", "Volume", "Short minus long EMA of volume, as a % of the long.",
    [len("short", "Short length", 5), len("long", "Long length", 10)], [{ key: "vo", label: "Volume osc", color: "#2962FF" }],
    (c, inp) => {
      const v = c.map((k) => k.volume);
      return { plots: [map2(ema(v, num(inp.short, 5)), ema(v, num(inp.long, 10)), (a, b) => (b === 0 ? null : ((a - b) / b) * 100))] };
    }, { levels: [0] }),
  {
    type: "vpvr", name: "Volume Profile Visible Range", short: "VPVR", category: "Volume", overlay: true, special: "vpvr",
    desc: "Volume traded at each price over the bars on screen, with point of control and value area.",
    inputs: [len("rows", "Row size (rows)", 24, 500), len("va", "Value area volume %", 70, 100), flt("width", "Width % of pane", 30, 1, 5, 90), { key: "side", label: "Placement", type: "select", def: "Right", options: ["Right", "Left"] }],
    plots: [],
    compute: () => ({ plots: [] }),
  },
  {
    type: "vpfr", name: "Volume Profile Fixed Range", short: "VPFR", category: "Volume", overlay: true, tool: "frvp",
    desc: "Pick a start and an end bar on the chart; the profile covers exactly that range (a drawing you can move and restyle).",
    inputs: [],
    plots: [],
    compute: () => ({ plots: [] }),
  },

  // ---- volatility ---------------------------------------------------------------
  pane("chaikinvol", "Chaikin Volatility", "Chaikin Vol", "Volatility", "Rate of change of the EMA of the high − low range.",
    [len("length", "Length", 10), len("roc", "RoC length", 10)], [{ key: "cv", label: "Chaikin volatility", color: "#AB47BC" }],
    (c, inp) => {
      const e = ema(c.map((k) => k.high - k.low), num(inp.length, 10));
      const r = num(inp.roc, 10);
      return { plots: [e.map((v, i) => (!ok(v) || i < r || !ok(e[i - r]) || !e[i - r] ? null : ((v - (e[i - r] as number)) / (e[i - r] as number)) * 100))] };
    }, { levels: [0] }),
  pane("rvix", "Relative Volatility Index", "RVI", "Volatility", "RSI-style split of standard deviation into up and down moves, 0–100.",
    [len("length", "StdDev length", 10), len("smooth", "Smoothing length", 14), SRC_INPUT], [{ key: "rvi", label: "RVI", color: "#7E57C2" }],
    (c, inp) => {
      const s = src(c, inp.source);
      const sd = rstdev(s, num(inp.length, 10));
      const ch = change(s);
      const up = emaN(sd.map((v, i) => (v == null || ch[i] == null ? null : (ch[i] as number) <= 0 ? 0 : v)), num(inp.smooth, 14));
      const dn = emaN(sd.map((v, i) => (v == null || ch[i] == null ? null : (ch[i] as number) > 0 ? 0 : v)), num(inp.smooth, 14));
      return { plots: [map2(up, dn, (a, b) => (a + b === 0 ? 50 : (100 * a) / (a + b)))] };
    }, { levels: [80, 50, 20], fills: [{ key: "bg", label: "Background", a: { level: 80 }, b: { level: 20 }, color: "rgba(126,87,194,0.08)" }] }),
  pane("stdev", "Standard Deviation", "StdDev", "Volatility", "Standard deviation of the source over N bars.",
    [len("length", "Length", 20, 1000), SRC_INPUT], [{ key: "sd", label: "StdDev", color: "#2962FF" }],
    (c, inp) => ({ plots: [rstdev(src(c, inp.source), num(inp.length, 20))] })),
  pane("stderr", "Standard Error", "StdErr", "Volatility", "Standard error of the linear-regression fit over N bars.",
    [len("length", "Length", 14, 1000), SRC_INPUT], [{ key: "se", label: "Standard error", color: "#FF6D00" }],
    (c, inp) => ({
      plots: [rolling(src(c, inp.source), num(inp.length, 14), (w) => {
        const { icpt, slope } = fitLine(w);
        const ss = w.reduce((s, v, j) => s + (v - (icpt + slope * j)) ** 2, 0);
        return w.length > 2 ? Math.sqrt(ss / (w.length - 2)) : null;
      })],
    })),
  pane("volcc", "Volatility Close-to-Close", "Vol C-C", "Volatility", "Annualised standard deviation of log close-to-close returns (%).",
    [len("length", "Length", 10, 1000)], [{ key: "v", label: "Volatility", color: "#FF7043" }],
    (c, inp, ctx) => {
      const lr = c.map((k, i) => (i === 0 || !c[i - 1].close ? null : Math.log(k.close / c[i - 1].close)));
      const py = perYear(ctx.interval);
      return { plots: [rolling(lr, num(inp.length, 10), (w) => {
        const m = mean(w);
        return w.length > 1 ? Math.sqrt(w.reduce((s, x) => s + (x - m) ** 2, 0) / (w.length - 1)) * Math.sqrt(py) * 100 : null;
      })] };
    }),
  pane("volzt", "Volatility Zero Trend Close-to-Close", "Vol ZT", "Volatility", "Annualised volatility of log returns assuming zero drift (root mean square, %).",
    [len("length", "Length", 10, 1000)], [{ key: "v", label: "Volatility", color: "#26A69A" }],
    (c, inp, ctx) => {
      const lr = c.map((k, i) => (i === 0 || !c[i - 1].close ? null : Math.log(k.close / c[i - 1].close)));
      const py = perYear(ctx.interval);
      return { plots: [rolling(lr, num(inp.length, 10), (w) => Math.sqrt(w.reduce((s, x) => s + x * x, 0) / w.length) * Math.sqrt(py) * 100)] };
    }),
  pane("volohlc", "Volatility O-H-L-C", "Vol OHLC", "Volatility", "Garman–Klass volatility from open, high, low and close, annualised (%).",
    [len("length", "Length", 10, 1000)], [{ key: "v", label: "Volatility", color: "#AB47BC" }],
    (c, inp, ctx) => {
      const k2 = 2 * Math.log(2) - 1;
      const gk = c.map((k) => (k.low > 0 && k.open > 0 ? 0.5 * Math.log(k.high / k.low) ** 2 - k2 * Math.log(k.close / k.open) ** 2 : null));
      const py = perYear(ctx.interval);
      return { plots: [rolling(gk, num(inp.length, 10), (w) => Math.sqrt(Math.max(0, mean(w)) * py) * 100)] };
    }),
  {
    type: "volindex", name: "Volatility Index", short: "VI", category: "Volatility", overlay: true,
    desc: "Wilder's volatility system: a stop-and-reverse line an ATR multiple from the significant close.",
    inputs: [len("length", "ATR length", 7), flt("mult", "ATR multiplier", 3, 0.1, 0.1, 20)],
    plots: [{ key: "sar", label: "SAR", color: "#FF9800", kind: "points" }],
    compute: (c, inp, ctx) => {
      const a = atr(c, num(inp.length, 7));
      const m = num(inp.mult, 3);
      const out: Arr = [];
      const colors: (string | null)[] = [];
      let long = true;
      let sic: number | null = null;
      c.forEach((k, i) => {
        const v = a[i];
        if (v == null) {
          out.push(null);
          colors.push(null);
          return;
        }
        if (sic == null) sic = k.close;
        sic = long ? Math.max(sic, k.close) : Math.min(sic, k.close);
        let sar = long ? sic - m * v : sic + m * v;
        if (long && k.close < sar) {
          long = false;
          sic = k.close;
          sar = sic + m * v;
        } else if (!long && k.close > sar) {
          long = true;
          sic = k.close;
          sar = sic - m * v;
        }
        out.push(sar);
        colors.push(long ? ctx.up : ctx.down);
      });
      return { plots: [out], colors: [colors] };
    },
  },

  // ---- price statistics -------------------------------------------------------------
  {
    type: "avgprice", name: "Average Price", short: "OHLC4", category: "Statistics", overlay: true,
    desc: "(Open + high + low + close) / 4 of each bar.", inputs: [],
    plots: [{ key: "p", label: "Average price", color: "#2962FF" }],
    compute: (c) => ({ plots: [src(c, "ohlc4")] }),
  },
  {
    type: "medprice", name: "Median Price", short: "HL2", category: "Statistics", overlay: true,
    desc: "(High + low) / 2 of each bar.", inputs: [],
    plots: [{ key: "p", label: "Median price", color: "#FF6D00" }],
    compute: (c) => ({ plots: [src(c, "hl2")] }),
  },
  {
    type: "typprice", name: "Typical Price", short: "HLC3", category: "Statistics", overlay: true,
    desc: "(High + low + close) / 3 of each bar.", inputs: [],
    plots: [{ key: "p", label: "Typical price", color: "#AB47BC" }],
    compute: (c) => ({ plots: [src(c, "hlc3")] }),
  },
  pane("corr", "Correlation Coefficient", "Corr", "Statistics", "Pearson correlation of this symbol's closes with another symbol's, −1 to 1.",
    [BENCH, len("length", "Length", 20, 1000)], [{ key: "r", label: "Correlation", color: "#2962FF" }],
    (c, inp, ctx) => ({ plots: [correlation(c.map((k) => k.close), otherCloses(c, ctx, str(inp.symbol, "NSE:NIFTY50-INDEX")), num(inp.length, 20))] }),
    { levels: [1, 0, -1] }),
  pane("corrlog", "Correlation - Log", "Corr log", "Statistics", "Correlation of the two symbols' log returns — co-movement without the trend.",
    [BENCH, len("length", "Length", 20, 1000)], [{ key: "r", label: "Correlation", color: "#AB47BC" }],
    (c, inp, ctx) => {
      const o = otherCloses(c, ctx, str(inp.symbol, "NSE:NIFTY50-INDEX"));
      const lr = (a: Arr): Arr => a.map((v, i) => (i === 0 || !ok(v) || !ok(a[i - 1]) || !a[i - 1] ? null : Math.log(v / (a[i - 1] as number))));
      return { plots: [correlation(lr(c.map((k) => k.close)), lr(o), num(inp.length, 20))] };
    }, { levels: [1, 0, -1] }),
  pane("ratio", "Ratio", "Ratio", "Statistics", "This symbol's close divided by another symbol's close.",
    [BENCH], [{ key: "r", label: "Ratio", color: "#2962FF" }],
    (c, inp, ctx) => {
      const o = otherCloses(c, ctx, str(inp.symbol, "NSE:NIFTY50-INDEX"));
      return { plots: [c.map((k, i) => (ok(o[i]) && o[i] ? k.close / (o[i] as number) : null))] };
    }),
  pane("spread", "Spread", "Spread", "Statistics", "This symbol's close minus another symbol's close (each × its multiplier).",
    [BENCH, flt("m1", "This symbol ×", 1, 0.1, -1e6, 1e6), flt("m2", "Other symbol ×", 1, 0.1, -1e6, 1e6)], [{ key: "s", label: "Spread", color: "#FF6D00" }],
    (c, inp, ctx) => {
      const o = otherCloses(c, ctx, str(inp.symbol, "NSE:NIFTY50-INDEX"));
      return { plots: [c.map((k, i) => (ok(o[i]) ? k.close * num(inp.m1, 1) - (o[i] as number) * num(inp.m2, 1) : null))] };
    }, { levels: [0] }),
];

/** Symbols an instance's inputs reference (two-symbol indicators). */
export function symbolInputs(def: IndicatorDef, inputs: Record<string, InputValue>): string[] {
  return def.inputs.filter((i) => i.type === "symbol").map((i) => str(inputs[i.key], String(i.def)).trim().toUpperCase()).filter(Boolean);
}

