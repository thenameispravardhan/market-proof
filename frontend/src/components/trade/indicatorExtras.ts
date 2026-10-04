// indicatorExtras — more built-ins for the indicator catalog: the Indian
// intraday desk set (CPR, previous-day OHLC, ORB, VWAP bands, ATR stops,
// relative volume, squeeze) and the common TradingView oscillators.
// Same IndicatorDef shape as indicatorCatalog; it appends these.

import { atr, bollinger, keltner, linearRegression, movingAverage, rsi, sma, type MaType, type OhlcvCandle } from "../../lib/indicators";
import { intervalGroup, intervalSeconds } from "./chartData";
import type { ComputeCtx, IndicatorDef, InputValue, PlotKind } from "./indicatorCatalog";

const num = (v: InputValue | undefined, d: number) => (typeof v === "number" && Number.isFinite(v) ? v : d);
const str = (v: InputValue | undefined, d: string) => (typeof v === "string" ? v : d);
const MA_TYPES: MaType[] = ["SMA", "EMA", "SMMA", "WMA", "VWMA"];

/** Chart time is already TZ-shifted, so the session day is a plain division. */
const dayOf = (t: number) => Math.floor(t / 86400);

type Ohlc = { o: number; h: number; l: number; c: number };

/** Each session's OHLC, and for every day the previous session's. */
function dailyOhlc(c: OhlcvCandle[]): { prev: Map<number, Ohlc> } {
  const days: [number, Ohlc][] = [];
  for (const k of c) {
    const d = dayOf(k.time);
    const last = days[days.length - 1];
    if (!last || last[0] !== d) days.push([d, { o: k.open, h: k.high, l: k.low, c: k.close }]);
    else {
      last[1].h = Math.max(last[1].h, k.high);
      last[1].l = Math.min(last[1].l, k.low);
      last[1].c = k.close;
    }
  }
  const prev = new Map<number, Ohlc>();
  for (let i = 1; i < days.length; i++) prev.set(days[i][0], days[i - 1][1]);
  return { prev };
}

function rollExt(a: number[], n: number, f: (...x: number[]) => number): (number | null)[] {
  return a.map((_, i) => (i < n - 1 ? null : f(...a.slice(i - n + 1, i + 1))));
}

function midRange(c: OhlcvCandle[], n: number): (number | null)[] {
  const hh = rollExt(c.map((k) => k.high), n, Math.max);
  const ll = rollExt(c.map((k) => k.low), n, Math.min);
  return hh.map((h, i) => (h == null || ll[i] == null ? null : (h + (ll[i] as number)) / 2));
}

function maLike(type: string, name: string, short: string, len: number, color: string, desc: string,
  f: (src: number[], n: number) => (number | null)[]): IndicatorDef {
  return {
    type, name, short, category: "Moving averages", overlay: true, desc,
    inputs: [{ key: "length", label: "Length", type: "int", def: len, min: 1, max: 2000 }],
    plots: [{ key: "ma", label: short, color }],
    compute: (c, inp) => ({ plots: [f(c.map((k) => k.close), num(inp.length, len))] }),
  };
}

function oscLike(type: string, name: string, short: string, desc: string, len: number, color: string,
  f: (c: OhlcvCandle[], n: number, ctx: ComputeCtx) => (number | null)[], levels?: number[], kind?: PlotKind): IndicatorDef {
  return {
    type, name, short, category: "Oscillators", overlay: false, desc, levels,
    inputs: [{ key: "length", label: "Length", type: "int", def: len, min: 1, max: 500 }],
    plots: [{ key: "v", label: short, color, kind }],
    compute: (c, inp, ctx) => {
      const v = f(c, num(inp.length, len), ctx);
      return kind === "hist" ? { plots: [v], colors: [v.map((x) => (x == null ? null : x >= 0 ? ctx.up : ctx.down))] } : { plots: [v] };
    },
  };
}

export const EXTRA_INDICATORS: IndicatorDef[] = [
  maLike("hma", "Hull Moving Average", "HMA", 9, "#FFB300", "WMA of 2·WMA(n/2) − WMA(n) over √n — fast and smooth.", (src, n) => {
    const half = movingAverage("WMA", src, Math.max(1, Math.round(n / 2)));
    const full = movingAverage("WMA", src, n);
    const diff = half.map((h, i) => (h == null || full[i] == null ? null : 2 * h - (full[i] as number)));
    return movingAverage("WMA", diff, Math.max(1, Math.round(Math.sqrt(n))));
  }),
  maLike("dema", "Double EMA", "DEMA", 9, "#43A047", "2·EMA − EMA(EMA): less lag than a plain EMA.", (src, n) => {
    const e1 = movingAverage("EMA", src, n);
    const e2 = movingAverage("EMA", e1, n);
    return e1.map((v, i) => (v == null || e2[i] == null ? null : 2 * v - (e2[i] as number)));
  }),
  maLike("tema", "Triple EMA", "TEMA", 9, "#00897B", "3·EMA − 3·EMA² + EMA³.", (src, n) => {
    const e1 = movingAverage("EMA", src, n);
    const e2 = movingAverage("EMA", e1, n);
    const e3 = movingAverage("EMA", e2, n);
    return e1.map((v, i) => (v == null || e2[i] == null || e3[i] == null ? null : 3 * v - 3 * (e2[i] as number) + (e3[i] as number)));
  }),
  {
    type: "pdhl",
    name: "Previous Day OHLC",
    short: "PD OHLC",
    category: "Trend",
    overlay: true,
    intradayOnly: true,
    desc: "Yesterday's open, high, low and close drawn across today.",
    inputs: [],
    plots: [
      { key: "pdh", label: "PDH", color: "#26A69A", kind: "step", dash: 2 },
      { key: "pdl", label: "PDL", color: "#EF5350", kind: "step", dash: 2 },
      { key: "pdc", label: "PDC", color: "#B0BEC5", kind: "step", dash: 1 },
      { key: "pdo", label: "PDO", color: "#78909C", kind: "step", dash: 1 },
    ],
    compute: (c) => {
      const { prev } = dailyOhlc(c);
      const out = [[], [], [], []] as (number | null)[][];
      for (const k of c) {
        const p = prev.get(dayOf(k.time));
        [p?.h, p?.l, p?.c, p?.o].forEach((v, i) => out[i].push(v ?? null));
      }
      return { plots: out };
    },
  },
  {
    type: "cpr",
    name: "CPR with Pivot levels (Daily)",
    short: "CPR",
    category: "Trend",
    overlay: true,
    intradayOnly: true,
    desc: "Central pivot range (TC / pivot / BC) plus R1–R3 / S1–S3 from yesterday's range. A narrow CPR often precedes a trending day.",
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
    compute: (c) => {
      const { prev } = dailyOhlc(c);
      const out = Array.from({ length: 9 }, () => [] as (number | null)[]);
      for (const k of c) {
        const d = prev.get(dayOf(k.time));
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
  },
  {
    type: "orb",
    name: "Opening Range Breakout (ORB)",
    short: "ORB",
    category: "Trend",
    overlay: true,
    intradayOnly: true,
    desc: "High / low of the first N minutes of each session, extended through the day.",
    inputs: [{ key: "minutes", label: "Range minutes", type: "int", def: 15, min: 1, max: 375 }],
    plots: [
      { key: "hi", label: "OR high", color: "#26A69A", kind: "step", width: 2 },
      { key: "lo", label: "OR low", color: "#EF5350", kind: "step", width: 2 },
    ],
    compute: (c, inp) => {
      const span = num(inp.minutes, 15) * 60;
      const hi: (number | null)[] = [];
      const lo: (number | null)[] = [];
      let day = -1;
      let start = 0;
      let h = -Infinity;
      let l = Infinity;
      for (const k of c) {
        const d = dayOf(k.time);
        if (d !== day) {
          day = d;
          start = k.time;
          h = -Infinity;
          l = Infinity;
        }
        if (k.time < start + span) {
          h = Math.max(h, k.high);
          l = Math.min(l, k.low);
        }
        const done = k.time >= start + span;
        hi.push(done ? h : null);
        lo.push(done ? l : null);
      }
      return { plots: [hi, lo] };
    },
  },
  {
    type: "vwapbands",
    name: "VWAP With Bands",
    short: "VWAP±σ",
    category: "Volume",
    overlay: true,
    intradayOnly: true,
    desc: "Session VWAP with ±1σ / ±2σ volume-weighted deviation bands — the reference institutions execute against.",
    inputs: [
      { key: "m1", label: "Band #1 mult", type: "float", def: 1, min: 0.1, max: 10, step: 0.1 },
      { key: "m2", label: "Band #2 mult", type: "float", def: 2, min: 0.1, max: 10, step: 0.1 },
    ],
    plots: [
      { key: "vwap", label: "VWAP", color: "#CE93D8", width: 2 },
      { key: "u1", label: "Upper #1", color: "#26A69A" },
      { key: "l1", label: "Lower #1", color: "#26A69A" },
      { key: "u2", label: "Upper #2", color: "#EF5350", dash: 2 },
      { key: "l2", label: "Lower #2", color: "#EF5350", dash: 2 },
    ],
    compute: (c, inp) => {
      const m1 = num(inp.m1, 1);
      const m2 = num(inp.m2, 2);
      const out = Array.from({ length: 5 }, () => [] as (number | null)[]);
      let day = -1;
      let pv = 0;
      let v = 0;
      let p2v = 0;
      for (const k of c) {
        const d = dayOf(k.time);
        if (d !== day) {
          day = d;
          pv = 0;
          v = 0;
          p2v = 0;
        }
        const tp = (k.high + k.low + k.close) / 3;
        pv += tp * k.volume;
        v += k.volume;
        p2v += tp * tp * k.volume;
        if (v <= 0) {
          out.forEach((a) => a.push(null));
          continue;
        }
        const mean = pv / v;
        const sd = Math.sqrt(Math.max(0, p2v / v - mean * mean));
        [mean, mean + m1 * sd, mean - m1 * sd, mean + m2 * sd, mean - m2 * sd].forEach((x, i) => out[i].push(x));
      }
      return { plots: out };
    },
  },
  {
    type: "chandelier",
    name: "Chandelier Exit",
    short: "Chandelier",
    category: "Trend",
    overlay: true,
    desc: "Trailing stops N×ATR from the highest high (longs) / lowest low (shorts).",
    inputs: [
      { key: "length", label: "ATR period", type: "int", def: 22, min: 1, max: 500 },
      { key: "mult", label: "ATR multiplier", type: "float", def: 3, min: 0.1, max: 20, step: 0.1 },
    ],
    plots: [
      { key: "long", label: "Long stop", color: "#26A69A", kind: "step" },
      { key: "short", label: "Short stop", color: "#EF5350", kind: "step" },
    ],
    compute: (c, inp) => {
      const n = num(inp.length, 22);
      const m = num(inp.mult, 3);
      const a = atr(c, n);
      const hh = rollExt(c.map((k) => k.high), n, Math.max);
      const ll = rollExt(c.map((k) => k.low), n, Math.min);
      return {
        plots: [
          a.map((x, i) => (x == null || hh[i] == null ? null : (hh[i] as number) - m * x)),
          a.map((x, i) => (x == null || ll[i] == null ? null : (ll[i] as number) + m * x)),
        ],
      };
    },
  },
  {
    type: "atrstop",
    name: "ATR Trailing Stop Loss",
    short: "ATR TSL",
    category: "Trend",
    overlay: true,
    desc: "Ratcheting stop N×ATR behind price; flips side when price closes through it.",
    inputs: [
      { key: "length", label: "ATR period", type: "int", def: 14, min: 1, max: 500 },
      { key: "mult", label: "ATR multiplier", type: "float", def: 2, min: 0.1, max: 20, step: 0.1 },
    ],
    plots: [{ key: "stop", label: "Stop", color: "#FFB300", kind: "step", width: 2 }],
    compute: (c, inp, ctx) => {
      const a = atr(c, num(inp.length, 14));
      const m = num(inp.mult, 2);
      const stop: (number | null)[] = [];
      const colors: (string | null)[] = [];
      let s: number | null = null;
      let long = true;
      for (let i = 0; i < c.length; i++) {
        const x = a[i];
        if (x == null) {
          stop.push(null);
          colors.push(null);
          continue;
        }
        const cl = c[i].close;
        if (s == null) s = cl - m * x;
        else if (long) {
          if (cl < s) {
            long = false;
            s = cl + m * x;
          } else s = Math.max(s, cl - m * x);
        } else if (cl > s) {
          long = true;
          s = cl - m * x;
        } else s = Math.min(s, cl + m * x);
        stop.push(s);
        colors.push(long ? ctx.up : ctx.down);
      }
      return { plots: [stop], colors: [colors] };
    },
  },
  {
    type: "hl52",
    name: "52 Week High/Low",
    short: "52W H/L",
    category: "Trend",
    overlay: true,
    desc: "Highest high and lowest low of the trailing 52 weeks (within the loaded history).",
    inputs: [],
    plots: [
      { key: "h", label: "52W high", color: "#26A69A", kind: "step", dash: 2 },
      { key: "l", label: "52W low", color: "#EF5350", kind: "step", dash: 2 },
    ],
    compute: (c) => {
      const yr = 365 * 86400;
      const h: (number | null)[] = [];
      const l: (number | null)[] = [];
      let j = 0;
      // ponytail: O(n·window) scan; fine for chart-sized series, monotonic deque if it ever shows in a profile
      for (let i = 0; i < c.length; i++) {
        while (c[j].time < c[i].time - yr) j++;
        let hh = -Infinity;
        let ll = Infinity;
        for (let k = j; k <= i; k++) {
          if (c[k].high > hh) hh = c[k].high;
          if (c[k].low < ll) ll = c[k].low;
        }
        h.push(hh);
        l.push(ll);
      }
      return { plots: [h, l] };
    },
  },
  {
    type: "envelopes",
    name: "Envelopes",
    short: "Env",
    category: "Bands & channels",
    overlay: true,
    desc: "Moving average with bands a fixed percentage above and below.",
    inputs: [
      { key: "length", label: "Length", type: "int", def: 20, min: 1, max: 1000 },
      { key: "pct", label: "Percent", type: "float", def: 2, min: 0.1, max: 50, step: 0.1 },
      { key: "maType", label: "MA type", type: "select", def: "SMA", options: MA_TYPES },
    ],
    plots: [
      { key: "u", label: "Upper", color: "#EF5350" },
      { key: "b", label: "Basis", color: "#FF9800" },
      { key: "l", label: "Lower", color: "#26A69A" },
    ],
    compute: (c, inp) => {
      const b = movingAverage(str(inp.maType, "SMA") as MaType, c.map((k) => k.close), num(inp.length, 20), c.map((k) => k.volume));
      const p = num(inp.pct, 2) / 100;
      return { plots: [b.map((x) => (x == null ? null : x * (1 + p))), b, b.map((x) => (x == null ? null : x * (1 - p)))] };
    },
  },
  {
    type: "stochrsi",
    name: "Stochastic RSI",
    short: "Stoch RSI",
    category: "Oscillators",
    overlay: false,
    desc: "Stochastic of the RSI, smoothed into %K and %D.",
    inputs: [
      { key: "rsiLen", label: "RSI length", type: "int", def: 14, min: 1, max: 500 },
      { key: "stochLen", label: "Stochastic length", type: "int", def: 14, min: 1, max: 500 },
      { key: "k", label: "K", type: "int", def: 3, min: 1, max: 100 },
      { key: "d", label: "D", type: "int", def: 3, min: 1, max: 100 },
    ],
    plots: [
      { key: "k", label: "K", color: "#2962FF" },
      { key: "d", label: "D", color: "#FF6D00" },
    ],
    levels: [80, 20],
    compute: (c, inp) => {
      const r = rsi(c.map((k) => k.close), num(inp.rsiLen, 14));
      const n = num(inp.stochLen, 14);
      const raw = r.map((v, i) => {
        if (v == null || i < n - 1) return null;
        const win = r.slice(i - n + 1, i + 1);
        if (win.some((x) => x == null)) return null;
        const hi = Math.max(...(win as number[]));
        const lo = Math.min(...(win as number[]));
        return hi === lo ? 0 : ((v - lo) / (hi - lo)) * 100;
      });
      const k = movingAverage("SMA", raw, num(inp.k, 3));
      return { plots: [k, movingAverage("SMA", k, num(inp.d, 3))] };
    },
  },
  oscLike("roc", "Rate Of Change", "ROC", "% change of the close over N bars.", 9, "#2962FF", (c, n) =>
    c.map((k, i) => (i < n || !c[i - n].close ? null : ((k.close - c[i - n].close) / c[i - n].close) * 100)), [0]),
  oscLike("mom", "Momentum", "Mom", "Close minus the close N bars ago.", 10, "#2962FF", (c, n) =>
    c.map((k, i) => (i < n ? null : k.close - c[i - n].close)), [0]),
  oscLike("cmf", "Chaikin Money Flow", "CMF", "Volume-weighted accumulation / distribution over N bars.", 20, "#43A047", (c, n) => {
    const mfv = c.map((k) => (k.high === k.low ? 0 : ((k.close - k.low - (k.high - k.close)) / (k.high - k.low)) * k.volume));
    const a = sma(mfv, n);
    const v = sma(c.map((k) => k.volume), n);
    return a.map((x, i) => (x == null || !v[i] ? null : x / (v[i] as number)));
  }, [0]),
  oscLike("chop", "Choppiness Index", "CHOP", "Trending (low) vs ranging (high) market, 0–100.", 14, "#26C6DA", (c, n) => {
    const tr = c.map((k, i) => (i === 0 ? k.high - k.low : Math.max(k.high - k.low, Math.abs(k.high - c[i - 1].close), Math.abs(k.low - c[i - 1].close))));
    const hh = rollExt(c.map((k) => k.high), n, Math.max);
    const ll = rollExt(c.map((k) => k.low), n, Math.min);
    return c.map((_, i) => {
      if (i < n || hh[i] == null || ll[i] == null) return null;
      let s = 0;
      for (let j = i - n + 1; j <= i; j++) s += tr[j];
      const rng = (hh[i] as number) - (ll[i] as number);
      return rng > 0 && n > 1 ? (100 * Math.log10(s / rng)) / Math.log10(n) : null;
    });
  }, [61.8, 38.2]),
  oscLike("hv", "Historical Volatility", "HV", "Annualised stdev of log returns (%).", 10, "#FF7043", (c, n, ctx) => {
    const lr = c.map((k, i) => (i === 0 || !c[i - 1].close ? 0 : Math.log(k.close / c[i - 1].close)));
    const g = intervalGroup(ctx.interval);
    const perYear = g === "days" ? 252 : g === "weeks" ? 52 : g === "months" ? 12 : (252 * 375 * 60) / Math.max(1, intervalSeconds(ctx.interval));
    return lr.map((_, i) => {
      if (i < n || n < 2) return null;
      const w = lr.slice(i - n + 1, i + 1);
      const m = w.reduce((a, b) => a + b, 0) / n;
      return Math.sqrt(w.reduce((a, b) => a + (b - m) ** 2, 0) / (n - 1)) * Math.sqrt(perYear) * 100;
    });
  }),
  oscLike("ao", "Awesome Oscillator", "AO", "SMA(5) − SMA(34) of the bar midpoint.", 34, "#26A69A", (c, n) => {
    const mid = c.map((k) => (k.high + k.low) / 2);
    const f = sma(mid, 5);
    const s = sma(mid, n);
    return f.map((x, i) => (x == null || s[i] == null ? null : x - (s[i] as number)));
  }, [0], "hist"),
  oscLike("bop", "Balance of Power", "BOP", "(close − open) / (high − low), smoothed.", 14, "#AB47BC", (c, n) =>
    movingAverage("SMA", c.map((k) => (k.high === k.low ? 0 : (k.close - k.open) / (k.high - k.low))), n), [0]),
  oscLike("trix", "TRIX", "TRIX", "1-bar change of a triple-smoothed EMA (×10⁴).", 18, "#EF5350", (c, n) => {
    const e3 = movingAverage("EMA", movingAverage("EMA", movingAverage("EMA", c.map((k) => Math.log(k.close)), n), n), n);
    return e3.map((x, i) => (x == null || i === 0 || e3[i - 1] == null ? null : (x - (e3[i - 1] as number)) * 10000));
  }, [0]),
  {
    type: "aroon",
    name: "Aroon",
    short: "Aroon",
    category: "Trend",
    overlay: false,
    desc: "How recently the N-bar high / low happened, as 0–100.",
    inputs: [{ key: "length", label: "Length", type: "int", def: 14, min: 1, max: 500 }],
    plots: [
      { key: "up", label: "Aroon up", color: "#FB8C00" },
      { key: "dn", label: "Aroon down", color: "#2962FF" },
    ],
    compute: (c, inp) => {
      const n = num(inp.length, 14);
      const up: (number | null)[] = [];
      const dn: (number | null)[] = [];
      for (let i = 0; i < c.length; i++) {
        if (i < n) {
          up.push(null);
          dn.push(null);
          continue;
        }
        let hi = i;
        let lo = i;
        for (let j = i - n; j <= i; j++) {
          if (c[j].high >= c[hi].high) hi = j;
          if (c[j].low <= c[lo].low) lo = j;
        }
        up.push((100 * (n - (i - hi))) / n);
        dn.push((100 * (n - (i - lo))) / n);
      }
      return { plots: [up, dn] };
    },
  },
  {
    type: "rvol",
    name: "Relative Volume",
    short: "RVOL",
    category: "Volume",
    overlay: false,
    desc: "Bar volume ÷ the average volume at the same time of day over the last N sessions (daily: ÷ N-bar average). ≥2 = unusual activity.",
    inputs: [{ key: "length", label: "Sessions", type: "int", def: 10, min: 1, max: 100 }],
    plots: [{ key: "rvol", label: "RVOL", color: "#607D8B", kind: "hist" }],
    levels: [1, 2],
    compute: (c, inp, ctx) => {
      const n = num(inp.length, 10);
      const intra = ["seconds", "minutes", "hours"].includes(intervalGroup(ctx.interval));
      const hist = new Map<number, number[]>();
      const avgN = sma(c.map((k) => k.volume), n);
      const out = c.map((k, i) => {
        if (!intra) return i > 0 && avgN[i - 1] ? k.volume / (avgN[i - 1] as number) : null;
        const slot = k.time % 86400;
        const h = hist.get(slot) ?? [];
        const r = h.length ? k.volume / Math.max(1, h.reduce((a, b) => a + b, 0) / h.length) : null;
        h.push(k.volume);
        if (h.length > n) h.shift();
        hist.set(slot, h);
        return r;
      });
      return { plots: [out], colors: [out.map((r) => (r != null && r >= 2 ? ctx.up : null))] };
    },
  },
  {
    type: "squeeze",
    name: "Squeeze Momentum",
    short: "Squeeze",
    category: "Volatility",
    overlay: false,
    desc: "Bollinger Bands inside Keltner = squeeze (volatility coiled, dots); histogram = linear-regression momentum.",
    inputs: [
      { key: "length", label: "Length", type: "int", def: 20, min: 2, max: 500 },
      { key: "bbMult", label: "BB mult", type: "float", def: 2, min: 0.1, max: 10, step: 0.1 },
      { key: "kcMult", label: "KC mult", type: "float", def: 1.5, min: 0.1, max: 10, step: 0.1 },
    ],
    plots: [
      { key: "mom", label: "Momentum", color: "#26A69A", kind: "hist" },
      { key: "sq", label: "Squeeze on", color: "#FFB300", kind: "points" },
    ],
    compute: (c, inp, ctx) => {
      const n = num(inp.length, 20);
      const bb = bollinger(c.map((k) => k.close), n, num(inp.bbMult, 2));
      const kc = keltner(c, n, num(inp.kcMult, 1.5), n);
      const mid = midRange(c, n);
      const basis = sma(c.map((k) => k.close), n);
      const dev = c.map((k, i) => (mid[i] == null || basis[i] == null ? null : k.close - ((mid[i] as number) + (basis[i] as number)) / 2));
      const mom = dev.map((_, i) => {
        if (i < n - 1) return null;
        const w = dev.slice(i - n + 1, i + 1);
        if (w.some((x) => x == null)) return null;
        return linearRegression(w as number[])?.end ?? null;
      });
      const sq = c.map((_, i) => {
        const [bu, bl, ku, kl] = [bb.upper[i], bb.lower[i], kc.upper[i], kc.lower[i]];
        return bu != null && bl != null && ku != null && kl != null && bu < ku && bl > kl ? 0 : null;
      });
      return { plots: [mom, sq], colors: [mom.map((m) => (m == null ? null : m >= 0 ? ctx.up : ctx.down)), null] };
    },
  },
];
