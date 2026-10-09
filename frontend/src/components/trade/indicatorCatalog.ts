// indicatorCatalog — the chart's built-in indicators as data.
//
// An IndicatorDef says what an indicator is (inputs, plots, whether it
// overlays the price pane, guide levels) and how to compute it from the
// candles. An IndicatorInstance is one copy of it on a chart — its own
// inputs, plot styles, precision and per-timeframe visibility — so a chart
// can carry EMA 9 and EMA 21 at once, TradingView-style. ChartPanel turns
// instances into series; the settings dialog edits them.

import {
  adx,
  atr,
  bollinger,
  cci,
  donchian,
  keltner,
  macd,
  mfi,
  movingAverage,
  obv,
  pivotPoints,
  psar,
  rsi,
  stochastic,
  supertrend,
  volumeProfile,
  vwap,
  williamsR,
  type MaType,
  type OhlcvCandle,
} from "../../lib/indicators";
import { EXTRA_INDICATORS } from "./indicatorExtras";
import { MORE_INDICATORS } from "./indicatorMore";
import { calendarBucket, intervalCount, intervalGroup, parseInterval, type IntervalGroup } from "./chartData";

export type Source = "close" | "open" | "high" | "low" | "hl2" | "hlc3" | "ohlc4" | "hlcc4";
export const SOURCES: Source[] = ["close", "open", "high", "low", "hl2", "hlc3", "ohlc4", "hlcc4"];

export function sourceOf(c: OhlcvCandle[], s: Source | string): number[] {
  switch (s) {
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

export type InputValue = number | string | boolean;

export interface InputDef {
  key: string;
  label: string;
  /** "symbol": another instrument (two-symbol indicators fetch its candles). */
  type: "int" | "float" | "source" | "select" | "bool" | "symbol";
  def: InputValue;
  min?: number;
  max?: number;
  step?: number;
  options?: string[];
}

/** How a plot is drawn. "marks" is an invisible anchor that carries the
 *  indicator's markers (patterns, fractals, divergences). */
export type PlotKind = "line" | "hist" | "points" | "step" | "area" | "columns" | "circles" | "marks";
export const PLOT_KINDS: { v: PlotKind; l: string }[] = [
  { v: "line", l: "Line" },
  { v: "step", l: "Step line" },
  { v: "hist", l: "Histogram" },
  { v: "columns", l: "Columns" },
  { v: "area", l: "Area" },
  { v: "circles", l: "Circles" },
  { v: "points", l: "Dots" },
];
export interface PlotDef {
  key: string;
  label: string;
  color: string;
  kind?: PlotKind;
  width?: number;
  dash?: 0 | 1 | 2; // solid, dotted, dashed
  /** Separate price scale inside the pane (e.g. CVD next to the delta). */
  scale?: string;
}

export type Category = "Moving averages" | "Bands & channels" | "Trend" | "Oscillators" | "Volume" | "Volatility" | "Statistics";
export const CATEGORIES: Category[] = ["Moving averages", "Bands & channels", "Trend", "Oscillators", "Volume", "Volatility", "Statistics"];

export interface ComputeCtx {
  interval: string;
  up: string; // theme up / down colors for histogram bars
  down: string;
  /** Real order flow per chart time: [buy, sell, delta]. */
  flow?: Map<number, [number, number, number]>;
  /** Another symbol's closes by chart time (two-symbol indicators). */
  other?: (symbol: string) => Map<number, number> | undefined;
}

/** A marker on one bar of a plot (pattern, fractal, divergence …). */
export interface Mark {
  plot: number;
  i: number;
  pos: "above" | "below" | "in";
  shape: "arrowUp" | "arrowDown" | "circle" | "square";
  color?: string;
  text?: string;
  /** Long name (data window / tooltips). */
  title?: string;
}

export interface ComputeResult {
  plots: (number | null)[][];
  /** Per-point colors (histograms); null = the plot's own color. */
  colors?: ((string | null)[] | null)[];
  /** Bars to shift each plot by (+ = into the future). */
  shifts?: number[];
  marks?: Mark[];
}

/** Shaded area between two plots (or a plot and a fixed level). With
 *  `colorDown`, the fill switches colour where `a` drops below `b`. */
export interface FillDef {
  key: string;
  label: string;
  a: number | { level: number };
  b: number | { level: number };
  color: string;
  colorDown?: string;
}
export interface IndicatorDef {
  type: string;
  name: string;
  short: string;
  category: Category;
  overlay: boolean;
  /** Overlay drawn on its own bottom scale (Volume). */
  ownScale?: boolean;
  intradayOnly?: boolean;
  desc: string;
  inputs: InputDef[];
  plots: PlotDef[];
  levels?: number[];
  fills?: FillDef[];
  /** Fyers desk set (listed under "Fyers indicators" in the picker). */
  desk?: boolean;
  isNew?: boolean;
  /** Not a series: adding it arms this drawing tool (e.g. fixed-range profile). */
  tool?: string;
  /** Rendered by the chart itself rather than as series. */
  special?: "vpvr" | "oiprofile";
  compute: (c: OhlcvCandle[], inp: Record<string, InputValue>, ctx: ComputeCtx) => ComputeResult;
}

const num = (v: InputValue | undefined, d: number) => (typeof v === "number" && Number.isFinite(v) ? v : d);
const str = (v: InputValue | undefined, d: string) => (typeof v === "string" ? v : d);

function shiftArr(a: (number | null)[], by: number): (number | null)[] {
  if (!by) return a;
  const out = new Array<number | null>(a.length).fill(null);
  for (let i = 0; i < a.length; i++) {
    const j = i + by;
    if (j >= 0 && j < a.length) out[j] = a[i];
  }
  return out;
}

const MA_TYPES: MaType[] = ["SMA", "EMA", "SMMA", "WMA", "VWMA"];
const SMOOTHING = ["None", ...MA_TYPES];

function maDef(type: MaType, name: string, len: number, color: string, desc: string): IndicatorDef {
  return {
    type: type.toLowerCase(),
    name,
    short: type === "SMMA" ? "SMMA" : type,
    category: "Moving averages",
    overlay: true,
    desc,
    inputs: [
      { key: "length", label: "Length", type: "int", def: len, min: 1, max: 2000 },
      { key: "source", label: "Source", type: "source", def: "close" },
      { key: "offset", label: "Offset", type: "int", def: 0, min: -500, max: 500 },
      { key: "smoothType", label: "Smoothing line", type: "select", def: "None", options: SMOOTHING },
      { key: "smoothLen", label: "Smoothing length", type: "int", def: 9, min: 1, max: 500 },
    ],
    plots: [
      { key: "ma", label: "Plot", color },
      { key: "smooth", label: "Smoothed MA", color: "#F7525F" },
    ],
    compute: (c, inp) => {
      const vols = c.map((k) => k.volume);
      const ma = shiftArr(movingAverage(type, sourceOf(c, str(inp.source, "close")), num(inp.length, len), vols), num(inp.offset, 0));
      const st = str(inp.smoothType, "None");
      const smooth = st === "None" ? ma.map(() => null) : movingAverage(st as MaType, ma, num(inp.smoothLen, 9), vols);
      return { plots: [ma, smooth] };
    },
  };
}

function midRange(c: OhlcvCandle[], len: number): (number | null)[] {
  const out = new Array<number | null>(c.length).fill(null);
  for (let i = len - 1; i < c.length; i++) {
    let hh = -Infinity;
    let ll = Infinity;
    for (let j = i - len + 1; j <= i; j++) {
      if (c[j].high > hh) hh = c[j].high;
      if (c[j].low < ll) ll = c[j].low;
    }
    out[i] = (hh + ll) / 2;
  }
  return out;
}

/** Pivot period for the chart interval: daily pivots on intraday, monthly
 *  on daily, yearly on weekly and above. */
function pivotPeriod(interval: string): (t: number) => number {
  const g = intervalGroup(interval);
  if (g === "seconds" || g === "minutes" || g === "hours") return (t) => Math.floor(t / 86400);
  if (g === "days") return (t) => calendarBucket(t, { unit: "M", n: 1 });
  return (t) => calendarBucket(t, { unit: "M", n: 12 });
}

export const INDICATORS: IndicatorDef[] = [
  {
    type: "volume",
    name: "Volume",
    short: "Volume",
    category: "Volume",
    overlay: true,
    ownScale: true,
    desc: "Traded volume per bar, colored by direction, with a moving average.",
    inputs: [
      { key: "maLength", label: "MA length", type: "int", def: 20, min: 1, max: 500 },
      { key: "prevClose", label: "Color based on previous close", type: "bool", def: false },
    ],
    plots: [
      { key: "vol", label: "Volume", color: "#26A69A", kind: "hist" },
      { key: "ma", label: "Volume MA", color: "#2962FF" },
    ],
    compute: (c, inp, ctx) => {
      const vols = c.map((k) => k.volume);
      const prev = inp.prevClose === true;
      const colors = c.map((k, i) => {
        const up = prev ? i === 0 || k.close >= c[i - 1].close : k.close >= k.open;
        return up ? ctx.up : ctx.down;
      });
      return { plots: [vols, movingAverage("SMA", vols, num(inp.maLength, 20))], colors: [colors, null] };
    },
  },
  maDef("SMA", "Moving Average Simple", 9, "#2962FF", "Arithmetic mean of the last N values."),
  maDef("EMA", "Moving Average Exponential", 9, "#2962FF", "Exponentially weighted average — reacts faster than the SMA."),
  maDef("WMA", "Moving Average Weighted", 9, "#7E57C2", "Linearly weighted average, newest bar weighted most."),
  maDef("VWMA", "Volume Weighted Moving Average", 20, "#26C6DA", "Average weighted by each bar's volume."),
  maDef("SMMA", "Smoothed Moving Average", 7, "#FF9800", "Wilder's running (RMA) average."),
  {
    type: "ribbon",
    name: "Moving Average Ribbon",
    short: "MA Ribbon",
    category: "Moving averages",
    overlay: true,
    desc: "Four moving averages of rising length — trend strength at a glance.",
    inputs: [
      { key: "maType", label: "MA type", type: "select", def: "EMA", options: MA_TYPES },
      { key: "source", label: "Source", type: "source", def: "close" },
      { key: "l1", label: "MA #1 length", type: "int", def: 20, min: 1, max: 2000 },
      { key: "l2", label: "MA #2 length", type: "int", def: 50, min: 1, max: 2000 },
      { key: "l3", label: "MA #3 length", type: "int", def: 100, min: 1, max: 2000 },
      { key: "l4", label: "MA #4 length", type: "int", def: 200, min: 1, max: 2000 },
    ],
    plots: [
      { key: "m1", label: "MA #1", color: "#F6C309" },
      { key: "m2", label: "MA #2", color: "#FB9800" },
      { key: "m3", label: "MA #3", color: "#FB6500" },
      { key: "m4", label: "MA #4", color: "#F60C0C" },
    ],
    compute: (c, inp) => {
      const src = sourceOf(c, str(inp.source, "close"));
      const t = str(inp.maType, "EMA") as MaType;
      const vols = c.map((k) => k.volume);
      return { plots: (["l1", "l2", "l3", "l4"] as const).map((k, i) => movingAverage(t, src, num(inp[k], [20, 50, 100, 200][i]), vols)) };
    },
  },
  {
    type: "vwap",
    name: "VWAP",
    short: "VWAP",
    category: "Volume",
    overlay: true,
    intradayOnly: true,
    desc: "Session volume-weighted average price (resets each day).",
    inputs: [],
    plots: [{ key: "vwap", label: "VWAP", color: "#CE93D8", width: 2 }],
    compute: (c) => ({ plots: [vwap(c)] }),
  },
  {
    type: "bb",
    name: "Bollinger Bands",
    short: "BB",
    category: "Bands & channels",
    overlay: true,
    desc: "SMA basis with bands N standard deviations away.",
    inputs: [
      { key: "length", label: "Length", type: "int", def: 20, min: 1, max: 1000 },
      { key: "source", label: "Source", type: "source", def: "close" },
      { key: "mult", label: "StdDev", type: "float", def: 2, min: 0.1, max: 10, step: 0.1 },
      { key: "offset", label: "Offset", type: "int", def: 0, min: -500, max: 500 },
    ],
    plots: [
      { key: "basis", label: "Basis", color: "#FF6D00" },
      { key: "upper", label: "Upper", color: "#2962FF" },
      { key: "lower", label: "Lower", color: "#2962FF" },
    ],
    compute: (c, inp) => {
      const b = bollinger(sourceOf(c, str(inp.source, "close")), num(inp.length, 20), num(inp.mult, 2));
      const o = num(inp.offset, 0);
      return { plots: [b.middle, b.upper, b.lower].map((a) => shiftArr(a, o)) };
    },
  },
  {
    type: "keltner",
    name: "Keltner Channels",
    short: "KC",
    category: "Bands & channels",
    overlay: true,
    desc: "EMA basis with bands a multiple of ATR away.",
    inputs: [
      { key: "length", label: "Length", type: "int", def: 20, min: 1, max: 1000 },
      { key: "mult", label: "Multiplier", type: "float", def: 2, min: 0.1, max: 10, step: 0.1 },
      { key: "atrLength", label: "ATR length", type: "int", def: 10, min: 1, max: 500 },
    ],
    plots: [
      { key: "upper", label: "Upper", color: "#2962FF" },
      { key: "basis", label: "Basis", color: "#2962FF", dash: 2 },
      { key: "lower", label: "Lower", color: "#2962FF" },
    ],
    compute: (c, inp) => {
      const k = keltner(c, num(inp.length, 20), num(inp.mult, 2), num(inp.atrLength, 10));
      return { plots: [k.upper, k.middle, k.lower] };
    },
  },
  {
    type: "donchian",
    name: "Donchian Channels",
    short: "DC",
    category: "Bands & channels",
    overlay: true,
    desc: "Highest high / lowest low of the last N bars.",
    inputs: [{ key: "length", label: "Length", type: "int", def: 20, min: 1, max: 1000 }],
    plots: [
      { key: "upper", label: "Upper", color: "#26C6DA" },
      { key: "basis", label: "Basis", color: "#FF6D00", dash: 2 },
      { key: "lower", label: "Lower", color: "#26C6DA" },
    ],
    compute: (c, inp) => {
      const d = donchian(c, num(inp.length, 20));
      return { plots: [d.upper, d.middle, d.lower] };
    },
  },
  {
    type: "ichimoku",
    name: "Ichimoku Cloud",
    short: "Ichimoku",
    category: "Trend",
    overlay: true,
    desc: "Conversion, base, leading spans (cloud) and lagging span.",
    inputs: [
      { key: "conv", label: "Conversion line length", type: "int", def: 9, min: 1, max: 500 },
      { key: "base", label: "Base line length", type: "int", def: 26, min: 1, max: 500 },
      { key: "spanB", label: "Leading span B length", type: "int", def: 52, min: 1, max: 500 },
      { key: "disp", label: "Lagging span", type: "int", def: 26, min: 1, max: 500 },
    ],
    plots: [
      { key: "tenkan", label: "Conversion line", color: "#2962FF" },
      { key: "kijun", label: "Base line", color: "#B71C1C" },
      { key: "lag", label: "Lagging span", color: "#43A047" },
      { key: "spanA", label: "Leading span A", color: "#A5D6A7" },
      { key: "spanB", label: "Leading span B", color: "#EF9A9A" },
    ],
    compute: (c, inp) => {
      const tenkan = midRange(c, num(inp.conv, 9));
      const kijun = midRange(c, num(inp.base, 26));
      const spanA = tenkan.map((t, i) => (t === null || kijun[i] === null ? null : (t + (kijun[i] as number)) / 2));
      const spanB = midRange(c, num(inp.spanB, 52));
      // TradingView plots the cloud displacement − 1 bars ahead and the
      // lagging span displacement − 1 bars back (26 → 25), as the backtester does
      const disp = Math.max(0, num(inp.disp, 26) - 1);
      return { plots: [tenkan, kijun, c.map((k) => k.close), spanA, spanB], shifts: [0, 0, -disp, disp, disp] };
    },
  },
  {
    type: "supertrend",
    name: "Supertrend",
    short: "Supertrend",
    category: "Trend",
    overlay: true,
    desc: "ATR trailing stop that flips with the trend.",
    inputs: [
      { key: "atrLength", label: "ATR length", type: "int", def: 10, min: 1, max: 500 },
      { key: "factor", label: "Factor", type: "float", def: 3, min: 0.1, max: 20, step: 0.1 },
    ],
    plots: [
      { key: "up", label: "Up trend", color: "#26A69A", width: 2 },
      { key: "down", label: "Down trend", color: "#EF5350", width: 2 },
    ],
    compute: (c, inp) => {
      const st = supertrend(c, num(inp.atrLength, 10), num(inp.factor, 3));
      return {
        plots: [
          st.value.map((v, i) => (st.up[i] === true ? v : null)),
          st.value.map((v, i) => (st.up[i] === false ? v : null)),
        ],
      };
    },
  },
  {
    type: "psar",
    name: "Parabolic SAR",
    short: "SAR",
    category: "Trend",
    overlay: true,
    desc: "Wilder's stop-and-reverse dots.",
    inputs: [
      { key: "start", label: "Start", type: "float", def: 0.02, min: 0.001, max: 1, step: 0.01 },
      { key: "max", label: "Max value", type: "float", def: 0.2, min: 0.01, max: 2, step: 0.01 },
    ],
    plots: [{ key: "sar", label: "ParabolicSAR", color: "#FFD54F", kind: "points" }],
    compute: (c, inp) => ({ plots: [psar(c, num(inp.start, 0.02), num(inp.max, 0.2))] }),
  },
  {
    type: "pivots",
    name: "Pivot Points Standard",
    short: "Pivots",
    category: "Trend",
    overlay: true,
    desc: "Floor pivots from the previous day (intraday), month (daily) or year.",
    inputs: [],
    plots: [
      { key: "p", label: "P", color: "#FF9800", kind: "step" },
      { key: "r1", label: "R1", color: "#EF5350", kind: "step" },
      { key: "s1", label: "S1", color: "#26A69A", kind: "step" },
      { key: "r2", label: "R2", color: "#EF5350", kind: "step", dash: 2 },
      { key: "s2", label: "S2", color: "#26A69A", kind: "step", dash: 2 },
      { key: "r3", label: "R3", color: "#EF5350", kind: "step", dash: 1 },
      { key: "s3", label: "S3", color: "#26A69A", kind: "step", dash: 1 },
    ],
    compute: (c, _inp, ctx) => {
      const p = pivotPoints(c, pivotPeriod(ctx.interval));
      return { plots: [p.p, p.r1, p.s1, p.r2, p.s2, p.r3, p.s3] };
    },
  },
  {
    type: "vprofile",
    name: "Session Volume Profile",
    short: "SVP",
    category: "Volume",
    overlay: true,
    intradayOnly: true,
    desc: "Developing point of control and value area for each session.",
    inputs: [{ key: "va", label: "Value area volume %", type: "int", def: 70, min: 10, max: 99 }],
    plots: [
      { key: "poc", label: "POC", color: "#FF8C00", width: 2 },
      { key: "vah", label: "VAH", color: "#999999", dash: 2 },
      { key: "val", label: "VAL", color: "#999999", dash: 2 },
    ],
    compute: (c, inp) => {
      const vp = volumeProfile(c, num(inp.va, 70) / 100);
      const gap = (a: (number | null)[]) =>
        a.map((v, i) => (i && Math.floor(c[i].time / 86400) !== Math.floor(c[i - 1].time / 86400) ? null : v));
      return { plots: [gap(vp.poc), gap(vp.vah), gap(vp.val)] };
    },
  },
  {
    type: "rsi",
    name: "Relative Strength Index",
    short: "RSI",
    category: "Oscillators",
    overlay: false,
    desc: "Momentum oscillator, 0–100; 70 / 30 overbought / oversold.",
    inputs: [
      { key: "length", label: "RSI length", type: "int", def: 14, min: 1, max: 500 },
      { key: "source", label: "Source", type: "source", def: "close" },
      { key: "smoothType", label: "MA type", type: "select", def: "SMA", options: SMOOTHING },
      { key: "smoothLen", label: "MA length", type: "int", def: 14, min: 1, max: 500 },
    ],
    plots: [
      { key: "rsi", label: "RSI", color: "#7E57C2" },
      { key: "ma", label: "RSI-based MA", color: "#F7B500" },
    ],
    levels: [70, 50, 30],
    compute: (c, inp) => {
      const r = rsi(sourceOf(c, str(inp.source, "close")), num(inp.length, 14));
      const st = str(inp.smoothType, "SMA");
      return { plots: [r, st === "None" ? r.map(() => null) : movingAverage(st as MaType, r, num(inp.smoothLen, 14))] };
    },
  },
  {
    type: "macd",
    name: "MACD",
    short: "MACD",
    category: "Oscillators",
    overlay: false,
    desc: "Fast − slow EMA, its signal line and histogram.",
    inputs: [
      { key: "fast", label: "Fast length", type: "int", def: 12, min: 1, max: 500 },
      { key: "slow", label: "Slow length", type: "int", def: 26, min: 1, max: 500 },
      { key: "signal", label: "Signal smoothing", type: "int", def: 9, min: 1, max: 500 },
      { key: "source", label: "Source", type: "source", def: "close" },
    ],
    plots: [
      { key: "hist", label: "Histogram", color: "#26A69A", kind: "hist" },
      { key: "macd", label: "MACD", color: "#2962FF" },
      { key: "signal", label: "Signal", color: "#FF6D00" },
    ],
    compute: (c, inp, ctx) => {
      const m = macd(sourceOf(c, str(inp.source, "close")), num(inp.fast, 12), num(inp.slow, 26), num(inp.signal, 9));
      const colors = m.histogram.map((v, i) => {
        if (v === null) return null;
        const p = m.histogram[i - 1];
        const rising = p == null || v >= p;
        return v >= 0 ? (rising ? ctx.up : withA(ctx.up, 0.5)) : rising ? withA(ctx.down, 0.5) : ctx.down;
      });
      return { plots: [m.histogram, m.macd, m.signal], colors: [colors, null, null] };
    },
  },
  {
    type: "stoch",
    name: "Stochastic",
    short: "Stoch",
    category: "Oscillators",
    overlay: false,
    desc: "%K / %D — close relative to the recent high-low range.",
    inputs: [
      { key: "k", label: "%K length", type: "int", def: 14, min: 1, max: 500 },
      { key: "kSmooth", label: "%K smoothing", type: "int", def: 3, min: 1, max: 100 },
      { key: "d", label: "%D smoothing", type: "int", def: 3, min: 1, max: 100 },
    ],
    plots: [
      { key: "k", label: "%K", color: "#2962FF" },
      { key: "d", label: "%D", color: "#FF6D00" },
    ],
    levels: [80, 50, 20],
    compute: (c, inp) => {
      const s = stochastic(c, num(inp.k, 14), num(inp.kSmooth, 3), num(inp.d, 3));
      return { plots: [s.k, s.d] };
    },
  },
  {
    type: "adx",
    name: "Directional Movement Index",
    short: "DMI",
    category: "Trend",
    overlay: false,
    desc: "ADX trend strength with +DI / −DI.",
    inputs: [{ key: "length", label: "DI length", type: "int", def: 14, min: 1, max: 500 }],
    plots: [
      { key: "adx", label: "ADX", color: "#F23645", width: 2 },
      { key: "plus", label: "+DI", color: "#26A69A" },
      { key: "minus", label: "−DI", color: "#EF5350" },
    ],
    levels: [25],
    compute: (c, inp) => {
      const a = adx(c, num(inp.length, 14));
      return { plots: [a.adx, a.plusDi, a.minusDi] };
    },
  },
  {
    type: "atr",
    name: "Average True Range",
    short: "ATR",
    category: "Volatility",
    overlay: false,
    desc: "Average of the true range — volatility in price units.",
    inputs: [{ key: "length", label: "Length", type: "int", def: 14, min: 1, max: 500 }],
    plots: [{ key: "atr", label: "ATR", color: "#FF8C00" }],
    compute: (c, inp) => ({ plots: [atr(c, num(inp.length, 14))] }),
  },
  {
    type: "obv",
    name: "On Balance Volume",
    short: "OBV",
    category: "Volume",
    overlay: false,
    desc: "Running total of volume signed by the bar's direction.",
    inputs: [],
    plots: [{ key: "obv", label: "OBV", color: "#26C6DA" }],
    compute: (c) => ({ plots: [obv(c)] }),
  },
  {
    type: "cci",
    name: "Commodity Channel Index",
    short: "CCI",
    category: "Oscillators",
    overlay: false,
    desc: "Deviation of the typical price from its average.",
    inputs: [{ key: "length", label: "Length", type: "int", def: 20, min: 1, max: 500 }],
    plots: [{ key: "cci", label: "CCI", color: "#AB47BC" }],
    levels: [100, 0, -100],
    compute: (c, inp) => ({ plots: [cci(c, num(inp.length, 20))] }),
  },
  {
    type: "mfi",
    name: "Money Flow Index",
    short: "MFI",
    category: "Volume",
    overlay: false,
    desc: "Volume-weighted RSI.",
    inputs: [{ key: "length", label: "Length", type: "int", def: 14, min: 1, max: 500 }],
    plots: [{ key: "mfi", label: "MF", color: "#FFCA28" }],
    levels: [80, 20],
    compute: (c, inp) => ({ plots: [mfi(c, num(inp.length, 14))] }),
  },
  {
    type: "wpr",
    name: "Williams %R",
    short: "%R",
    category: "Oscillators",
    overlay: false,
    desc: "Close relative to the N-bar high, 0 to −100.",
    inputs: [{ key: "length", label: "Length", type: "int", def: 14, min: 1, max: 500 }],
    plots: [{ key: "wpr", label: "%R", color: "#EC407A" }],
    levels: [-20, -50, -80],
    compute: (c, inp) => ({ plots: [williamsR(c, num(inp.length, 14))] }),
  },
  {
    type: "flow",
    name: "Real Order Flow (recorded ticks)",
    short: "Order flow",
    category: "Volume",
    overlay: false,
    desc: "Buy − sell volume from the tick recorder (Lee-Ready) with cumulative delta.",
    inputs: [],
    plots: [
      { key: "delta", label: "Δ ticks", color: "#26A69A", kind: "hist" },
      { key: "cvd", label: "CVD ticks", color: "#26C6DA", width: 2, scale: "cvd" },
    ],
    compute: (c, _inp, ctx) => {
      const delta: (number | null)[] = [];
      const cvd: (number | null)[] = [];
      const colors: (string | null)[] = [];
      let run = 0;
      let day = -1;
      for (const k of c) {
        const f = ctx.flow?.get(k.time);
        if (!f) {
          delta.push(null);
          cvd.push(null);
          colors.push(null);
          continue;
        }
        const d = Math.floor(k.time / 86400);
        if (d !== day) {
          run = 0;
          day = d;
        }
        run += f[2];
        delta.push(f[2]);
        cvd.push(run);
        colors.push(f[2] >= 0 ? ctx.up : ctx.down);
      }
      return { plots: [delta, cvd], colors: [colors, null] };
    },
  },
  ...EXTRA_INDICATORS,
  ...MORE_INDICATORS,
];

// Shaded areas for the classic bands / oscillator zones, the Fyers desk set
// and the "new" badges — kept as data next to the defs they decorate.
const BAND_FILLS: Record<string, FillDef[]> = {
  ichimoku: [{ key: "cloud", label: "Cloud", a: 3, b: 4, color: "rgba(67,160,71,0.18)", colorDown: "rgba(244,67,54,0.18)" }],
  bb: [{ key: "bg", label: "Background", a: 1, b: 2, color: "rgba(33,150,243,0.08)" }],
  keltner: [{ key: "bg", label: "Background", a: 0, b: 2, color: "rgba(33,150,243,0.06)" }],
  donchian: [{ key: "bg", label: "Background", a: 0, b: 2, color: "rgba(38,198,218,0.06)" }],
  envelopes: [{ key: "bg", label: "Background", a: 0, b: 2, color: "rgba(255,152,0,0.06)" }],
  vwapbands: [{ key: "b1", label: "Band #1 fill", a: 1, b: 2, color: "rgba(38,166,154,0.08)" }],
  rsi: [{ key: "bg", label: "Background", a: { level: 70 }, b: { level: 30 }, color: "rgba(126,87,194,0.08)" }],
  stoch: [{ key: "bg", label: "Background", a: { level: 80 }, b: { level: 20 }, color: "rgba(33,150,243,0.08)" }],
  stochrsi: [{ key: "bg", label: "Background", a: { level: 80 }, b: { level: 20 }, color: "rgba(33,150,243,0.08)" }],
  mfi: [{ key: "bg", label: "Background", a: { level: 80 }, b: { level: 20 }, color: "rgba(255,202,40,0.07)" }],
  cci: [{ key: "bg", label: "Background", a: { level: 100 }, b: { level: -100 }, color: "rgba(171,71,188,0.07)" }],
  wpr: [{ key: "bg", label: "Background", a: { level: -20 }, b: { level: -80 }, color: "rgba(236,64,122,0.07)" }],
};
const DESK_TYPES = new Set(["atrstop", "chandelier", "orb", "squeeze", "cpr", "pdhl", "rvol"]);
const NEW_TYPES = new Set(["atrstop", "chandelier", "orb", "squeeze", "jma", "kama"]);
for (const d of INDICATORS) {
  if (!d.fills && BAND_FILLS[d.type]) d.fills = BAND_FILLS[d.type];
  if (DESK_TYPES.has(d.type)) d.desk = true;
  if (NEW_TYPES.has(d.type)) d.isNew = true;
}

function withA(hex: string, a: number): string {
  const m = /^#([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return hex;
  const h = m[1];
  return `rgba(${parseInt(h.slice(0, 2), 16)},${parseInt(h.slice(2, 4), 16)},${parseInt(h.slice(4, 6), 16)},${a})`;
}

export const INDICATOR_BY_TYPE = new Map(INDICATORS.map((d) => [d.type, d]));

// ---------------------------------------------------------------------------
// Instances
// ---------------------------------------------------------------------------

export interface PlotStyle {
  color: string;
  width: number;
  dash: 0 | 1 | 2;
  visible: boolean;
  /** Plot type override (Style tab); unset = the definition's. */
  kind?: PlotKind;
}

export interface FillStyle {
  color: string;
  visible: boolean;
}

export interface VisRange {
  on: boolean;
  min: number;
  max: number;
}

export const VIS_GROUPS: { id: IntervalGroup; label: string; max: number }[] = [
  { id: "seconds", label: "Seconds", max: 59 },
  { id: "minutes", label: "Minutes", max: 59 },
  { id: "hours", label: "Hours", max: 24 },
  { id: "days", label: "Days", max: 366 },
  { id: "weeks", label: "Weeks", max: 52 },
  { id: "months", label: "Months", max: 12 },
];

export interface IndicatorInstance {
  uid: string;
  type: string;
  inputs: Record<string, InputValue>;
  plots: PlotStyle[];
  visible: boolean;
  /** Decimals; null = the symbol's default. */
  precision: number | null;
  labelsOnScale: boolean;
  valuesInStatus: boolean;
  vis?: Partial<Record<IntervalGroup, VisRange>>;
  fills?: FillStyle[];
  /** Pane placement from the legend's More menu: its own pane, the price
   *  pane, or another indicator's pane (that indicator's uid). */
  pane?: "own" | "main" | string;
  scale?: "left" | "right" | "new";
}

let uidSeq = 0;
export function newUid(): string {
  uidSeq += 1;
  return `i${Date.now().toString(36)}${uidSeq}`;
}

export function defaultInputs(def: IndicatorDef): Record<string, InputValue> {
  return Object.fromEntries(def.inputs.map((i) => [i.key, i.def]));
}

export function defaultPlots(def: IndicatorDef): PlotStyle[] {
  return def.plots.map((p) => ({ color: p.color, width: p.width ?? 1, dash: p.dash ?? 0, visible: true }));
}

export function defaultFills(def: IndicatorDef): FillStyle[] {
  return (def.fills ?? []).map((f) => ({ color: f.color, visible: true }));
}

/** One input value made valid for its definition, or the default when it
 *  can't be: numbers clamped to min / max (ints rounded), numeric strings
 *  from older saves parsed, selects / sources limited to their options. */
export function sanitizeInput(i: InputDef, v: unknown): InputValue {
  switch (i.type) {
    case "int":
    case "float": {
      const n = typeof v === "number" ? v : typeof v === "string" && v.trim() !== "" ? Number(v) : Number.NaN;
      if (!Number.isFinite(n)) return i.def;
      const r = i.type === "int" ? Math.round(n) : n;
      return Math.min(i.max ?? Infinity, Math.max(i.min ?? -Infinity, r));
    }
    case "bool":
      return typeof v === "boolean" ? v : v === "true" ? true : v === "false" ? false : i.def;
    case "source":
      return typeof v === "string" && (SOURCES as string[]).includes(v) ? v : i.def;
    case "select":
      return typeof v === "string" && (i.options ?? []).includes(v) ? v : i.def;
    case "symbol":
      return typeof v === "string" ? v.trim().toUpperCase() : i.def;
    default:
      return i.def;
  }
}

/** Every input of `def`, valid (unknown keys dropped, missing ones defaulted). */
export function sanitizeInputs(def: IndicatorDef, raw: unknown): Record<string, InputValue> {
  const r = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  return Object.fromEntries(def.inputs.map((i) => [i.key, i.key in r ? sanitizeInput(i, r[i.key]) : i.def]));
}

const PLOT_KIND_SET = new Set<string>(PLOT_KINDS.map((k) => k.v));

function sanitizePlot(base: PlotStyle, raw: unknown): PlotStyle {
  if (!raw || typeof raw !== "object") return base;
  const r = raw as Partial<PlotStyle>;
  const out: PlotStyle = {
    color: typeof r.color === "string" && r.color.trim() ? r.color : base.color,
    width: typeof r.width === "number" && Number.isFinite(r.width) ? Math.min(4, Math.max(1, Math.round(r.width))) : base.width,
    dash: r.dash === 0 || r.dash === 1 || r.dash === 2 ? r.dash : base.dash,
    visible: typeof r.visible === "boolean" ? r.visible : base.visible,
  };
  if (typeof r.kind === "string" && PLOT_KIND_SET.has(r.kind)) out.kind = r.kind;
  return out;
}

function sanitizeVis(raw: unknown): IndicatorInstance["vis"] {
  if (!raw || typeof raw !== "object") return undefined;
  const r = raw as Record<string, Partial<VisRange> | undefined>;
  const out: Partial<Record<IntervalGroup, VisRange>> = {};
  for (const g of VIS_GROUPS) {
    const v = r[g.id];
    if (!v || typeof v !== "object") continue;
    const clamp = (x: unknown, d: number) => (typeof x === "number" && Number.isFinite(x) ? Math.min(g.max, Math.max(1, Math.round(x))) : d);
    const a = clamp(v.min, 1);
    const b = clamp(v.max, g.max);
    out[g.id] = { on: v.on !== false, min: Math.min(a, b), max: Math.max(a, b) };
  }
  return Object.keys(out).length ? out : undefined;
}

export function newInstance(type: string, inputs?: Record<string, InputValue>): IndicatorInstance | null {
  const def = INDICATOR_BY_TYPE.get(type);
  if (!def) return null;
  return {
    uid: newUid(),
    type,
    inputs: sanitizeInputs(def, inputs ?? {}),
    plots: defaultPlots(def),
    fills: defaultFills(def),
    visible: true,
    precision: null,
    labelsOnScale: true,
    valuesInStatus: true,
  };
}

/** Repair an instance loaded from storage against the current catalog. */
export function sanitizeInstance(raw: unknown): IndicatorInstance | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Partial<IndicatorInstance>;
  const def = r.type ? INDICATOR_BY_TYPE.get(r.type) : undefined;
  if (!def) return null;
  const plots = defaultPlots(def).map((p, i) => sanitizePlot(p, Array.isArray(r.plots) ? r.plots[i] : undefined));
  const fills = defaultFills(def).map((f, i) => {
    const x = Array.isArray(r.fills) ? (r.fills[i] as Partial<FillStyle> | null | undefined) : undefined;
    return {
      color: x && typeof x.color === "string" && x.color.trim() ? x.color : f.color,
      visible: x && typeof x.visible === "boolean" ? x.visible : f.visible,
    };
  });
  const vis = sanitizeVis(r.vis);
  return {
    uid: typeof r.uid === "string" && r.uid ? r.uid : newUid(),
    type: def.type,
    inputs: sanitizeInputs(def, r.inputs),
    plots,
    fills,
    visible: r.visible !== false,
    precision: typeof r.precision === "number" && Number.isInteger(r.precision) && r.precision >= 0 && r.precision <= 8 ? r.precision : null,
    labelsOnScale: r.labelsOnScale !== false,
    valuesInStatus: r.valuesInStatus !== false,
    ...(vis ? { vis } : {}),
    ...(typeof r.pane === "string" && r.pane ? { pane: r.pane } : {}),
    ...(r.scale === "left" || r.scale === "right" || r.scale === "new" ? { scale: r.scale } : {}),
  };
}

/** Copies of `items` with fresh uids, keeping indicators that were merged
 *  into another one's pane attached to that indicator's new uid (a template
 *  or a layout applied twice must not lose its pane grouping). */
export function cloneInstances(items: unknown[]): IndicatorInstance[] {
  const fresh = items.map((raw) => sanitizeInstance(raw)).filter((x): x is IndicatorInstance => x !== null);
  const ids = new Map(fresh.map((x) => [x.uid, newUid()]));
  return fresh.map((x) => {
    const pane = x.pane && x.pane !== "own" && x.pane !== "main" ? ids.get(x.pane) : x.pane;
    const { pane: _p, ...rest } = x;
    return { ...rest, uid: ids.get(x.uid) as string, ...(pane ? { pane } : {}) };
  });
}

/** Everything "Save as default" keeps: no identity, no placement (pane,
 *  pinned scale) and not the hidden flag, so a default saved from a hidden
 *  copy still adds visible copies. */
export function instanceDefaults(i: IndicatorInstance): Omit<IndicatorInstance, "uid" | "type" | "pane" | "scale" | "visible"> {
  const { uid: _u, type: _t, pane: _p, scale: _s, visible: _v, ...rest } = i;
  return JSON.parse(JSON.stringify(rest));
}

/** Legend arguments, e.g. "9 close 0". */
export function argsLabel(inst: IndicatorInstance): string {
  const def = INDICATOR_BY_TYPE.get(inst.type);
  if (!def) return "";
  return def.inputs
    .filter((i) => i.type !== "bool" && i.key !== "smoothType" && i.key !== "smoothLen")
    .map((i) => String(inst.inputs[i.key] ?? i.def))
    .join(" ");
}

export function instanceTitle(inst: IndicatorInstance): string {
  return INDICATOR_BY_TYPE.get(inst.type)?.short ?? inst.type;
}

/** Is the instance shown on this interval (Visibility tab)? */
export function visibleOnInterval(inst: IndicatorInstance, interval: string): boolean {
  const g = intervalGroup(interval);
  const v = inst.vis?.[g];
  if (!v) return true;
  if (!v.on) return false;
  const n = intervalCount(interval);
  const g0 = VIS_GROUPS.find((x) => x.id === g);
  // a range typed backwards (from 30 to 5) still means 5–30; one that runs to
  // the group's end is open-ended (a 90-minute chart is in "Minutes 1 to 59")
  const lo = Math.min(v.min, v.max);
  const hi = Math.max(v.min, v.max);
  return n >= lo && (n <= hi || (g0 !== undefined && hi >= g0.max));
}

/** Old charts stored booleans per built-in; turn them into instances
 *  with the inputs those built-ins used. */
export function migrateActive(active: Record<string, boolean> | undefined, volumeOn: boolean | undefined): IndicatorInstance[] {
  const out: IndicatorInstance[] = [];
  const add = (type: string, inputs?: Record<string, InputValue>) => {
    const i = newInstance(type, inputs);
    if (i) out.push(i);
  };
  if (volumeOn !== false) add("volume");
  if (!active) return out;
  const legacy: Record<string, [string, Record<string, InputValue>?]> = {
    sma: ["sma", { length: 20 }],
    ema: ["ema", { length: 50 }],
    wma: ["wma", { length: 20 }],
    vwap: ["vwap"],
    bb: ["bb"],
    supertrend: ["supertrend"],
    psar: ["psar"],
    ichimoku: ["ichimoku"],
    donchian: ["donchian"],
    vprofile: ["vprofile"],
    rsi: ["rsi", { smoothType: "None" }],
    macd: ["macd"],
    stoch: ["stoch"],
    adx: ["adx"],
    atr: ["atr"],
    obv: ["obv"],
    cci: ["cci"],
    mfi: ["mfi"],
    wpr: ["wpr"],
    flow: ["flow"],
  };
  for (const [k, on] of Object.entries(active)) {
    if (on && legacy[k]) add(legacy[k][0], legacy[k][1]);
  }
  return out;
}

export function isIntradayKey(interval: string): boolean {
  const iv = parseInterval(interval);
  return iv != null && (iv.unit === "S" || iv.unit === "m");
}
