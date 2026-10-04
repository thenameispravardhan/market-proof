// chartData — pure candle helpers for ChartPanel.
//
//   - interval keys ("5S", "1", "75", "D", "2D", "W", "3M" …): parsing,
//     labels, and which Fyers resolution to fetch for each one
//   - client-side aggregation for intervals Fyers doesn't serve natively
//     (75m, 2D, weekly, monthly, quarterly, yearly …)
//   - the non-time chart types — Renko, Line break, Kagi, Point & figure
//     and Range bars — built from the loaded candles
//
// Times are chart times (IST-shifted epoch seconds), as everywhere in the
// chart: a day boundary is a multiple of 86400.

export interface Bar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  /** Open interest at the bar's close (derivatives, when Fyers sends it). */
  oi?: number;
  /** Chart-type extras: Kagi thickness (1 = yang), P&F column (1 = X). */
  flag?: number;
}

// ---------------------------------------------------------------------------
// Intervals
// ---------------------------------------------------------------------------

export type IntervalUnit = "S" | "m" | "D" | "W" | "M";
export type IntervalGroup = "seconds" | "minutes" | "hours" | "days" | "weeks" | "months";

export interface Interval {
  unit: IntervalUnit;
  n: number;
}

export const NATIVE_SECONDS = [5, 10, 15, 30, 45];
export const NATIVE_MINUTES = [1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 120, 180, 240];

export function parseInterval(key: string): Interval | null {
  const raw = (key || "").trim();
  // Lower-case "m" is minutes (the toolbar's "15m"); upper-case "M" months.
  const mins = /^(\d+)m$/.exec(raw);
  if (mins) return { unit: "m", n: Number(mins[1]) };
  const k = raw.toUpperCase();
  let m: RegExpExecArray | null;
  if (k === "D") return { unit: "D", n: 1 };
  if ((m = /^(\d+)S$/.exec(k))) return { unit: "S", n: Number(m[1]) };
  if ((m = /^(\d+)$/.exec(k))) return { unit: "m", n: Number(m[1]) };
  if ((m = /^(\d+)H$/.exec(k))) return { unit: "m", n: Number(m[1]) * 60 };
  if ((m = /^(\d+)D$/.exec(k))) return { unit: "D", n: Number(m[1]) };
  if ((m = /^(\d*)W$/.exec(k))) return { unit: "W", n: Number(m[1] || 1) };
  if ((m = /^(\d*)M$/.exec(k))) return { unit: "M", n: Number(m[1] || 1) };
  return null;
}

export function intervalKey(iv: Interval): string {
  switch (iv.unit) {
    case "S": return `${iv.n}S`;
    case "m": return String(iv.n);
    case "D": return iv.n === 1 ? "D" : `${iv.n}D`;
    case "W": return `${iv.n}W`;
    case "M": return `${iv.n}M`;
  }
}

/** Normalise a user-typed key ("1h" → "60", "1d" → "D", "w" → "1W"). */
export function normalizeInterval(key: string): string | null {
  const iv = parseInterval(key);
  if (!iv || !(iv.n > 0) || iv.n > 100000) return null;
  return intervalKey(iv);
}

/** Toolbar label: 5s, 1m, 75m, 1h, 4h, D, 2D, W, M, 3M. */
export function intervalLabel(key: string): string {
  const iv = parseInterval(key);
  if (!iv) return key;
  switch (iv.unit) {
    case "S": return `${iv.n}s`;
    case "m": return iv.n % 60 === 0 ? `${iv.n / 60}h` : `${iv.n}m`;
    case "D": return iv.n === 1 ? "D" : `${iv.n}D`;
    case "W": return iv.n === 1 ? "W" : `${iv.n}W`;
    case "M": return iv.n === 1 ? "M" : `${iv.n}M`;
  }
}

export function intervalLongLabel(key: string): string {
  const iv = parseInterval(key);
  if (!iv) return key;
  const pl = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;
  switch (iv.unit) {
    case "S": return pl(iv.n, "second");
    case "m": return iv.n % 60 === 0 ? pl(iv.n / 60, "hour") : pl(iv.n, "minute");
    case "D": return pl(iv.n, "day");
    case "W": return pl(iv.n, "week");
    case "M": return pl(iv.n, "month");
  }
}

export function intervalGroup(key: string): IntervalGroup {
  const iv = parseInterval(key);
  if (!iv) return "minutes";
  if (iv.unit === "S") return "seconds";
  if (iv.unit === "m") return iv.n % 60 === 0 ? "hours" : "minutes";
  if (iv.unit === "D") return "days";
  if (iv.unit === "W") return "weeks";
  return "months";
}

/** The interval's length in the group's own unit (for visibility ranges). */
export function intervalCount(key: string): number {
  const iv = parseInterval(key);
  if (!iv) return 1;
  return iv.unit === "m" && iv.n % 60 === 0 ? iv.n / 60 : iv.n;
}

/** Nominal bar length in seconds (weeks / months approximate). */
export function intervalSeconds(key: string): number {
  const iv = parseInterval(key);
  if (!iv) return 300;
  switch (iv.unit) {
    case "S": return iv.n;
    case "m": return iv.n * 60;
    case "D": return iv.n * 86400;
    case "W": return iv.n * 7 * 86400;
    case "M": return iv.n * 30 * 86400;
  }
}

export function isIntraday(key: string): boolean {
  const iv = parseInterval(key);
  return iv != null && (iv.unit === "S" || iv.unit === "m");
}

export interface FetchPlan {
  /** Fyers resolution code to request. */
  res: string;
  /** True when the chart aggregates `res` candles into the interval. */
  aggregate: boolean;
  /** Calendar days fetched up-front / per scroll-back page. */
  initialDays: number;
  chunkDays: number;
}

// Calendar days per request for each native minute resolution (Fyers caps
// intraday history at 100 days per request, daily at 366).
const MINUTE_DAYS: Record<number, number> = {
  1: 7, 2: 10, 3: 15, 5: 20, 10: 40, 15: 60, 20: 80, 30: 90, 45: 100, 60: 100, 120: 100, 180: 100, 240: 100,
};

function largestDivisor(n: number, of: number[]): number | null {
  let best: number | null = null;
  for (const d of of) if (n % d === 0) best = d;
  return best;
}

export function fetchPlan(key: string): FetchPlan {
  const iv = parseInterval(key) ?? { unit: "m", n: 5 };
  if (iv.unit === "S") {
    const base = NATIVE_SECONDS.includes(iv.n) ? iv.n : largestDivisor(iv.n, NATIVE_SECONDS) ?? 5;
    return { res: `${base}S`, aggregate: base !== iv.n, initialDays: 2, chunkDays: 2 };
  }
  if (iv.unit === "m") {
    const base = NATIVE_MINUTES.includes(iv.n) ? iv.n : largestDivisor(iv.n, NATIVE_MINUTES) ?? 1;
    const days = Math.min(100, Math.ceil((MINUTE_DAYS[base] ?? 20) * Math.max(1, iv.n / base) ** 0.5));
    return { res: String(base), aggregate: base !== iv.n, initialDays: days, chunkDays: days };
  }
  return { res: "D", aggregate: !(iv.unit === "D" && iv.n === 1), initialDays: 365, chunkDays: 365 };
}

// ---------------------------------------------------------------------------
// Aggregation
// ---------------------------------------------------------------------------

const DAY = 86400;

/** Calendar bucket id for day-or-longer intervals (chart time in). */
export function calendarBucket(t: number, iv: Interval): number {
  const day = Math.floor(t / DAY);
  if (iv.unit === "D") return Math.floor(day / iv.n);
  if (iv.unit === "W") {
    // 1970-01-01 was a Thursday → +3 makes Monday the first day of a week.
    const week = Math.floor((day + 3) / 7);
    return Math.floor(week / iv.n);
  }
  const d = new Date(day * DAY * 1000);
  const month = d.getUTCFullYear() * 12 + d.getUTCMonth();
  return Math.floor(month / iv.n);
}

function combine(into: Bar, b: Bar): void {
  if (b.high > into.high) into.high = b.high;
  if (b.low < into.low) into.low = b.low;
  into.close = b.close;
  into.volume += b.volume;
  if (b.oi !== undefined) into.oi = b.oi; // a level, not a flow: the bucket's last
}

/**
 * Aggregate base candles into `key`. Intraday buckets anchor to each
 * day's first bar (the session open), day-and-longer ones to the
 * calendar. Each output bar takes the time of its first input bar.
 */
export function aggregate(bars: Bar[], key: string): Bar[] {
  const iv = parseInterval(key);
  if (!iv || bars.length === 0) return bars.slice();
  const out: Bar[] = [];
  if (iv.unit === "S" || iv.unit === "m") {
    const len = iv.unit === "S" ? iv.n : iv.n * 60;
    let day = -1;
    let anchor = 0;
    let bucket = -1;
    for (const b of bars) {
      const d = Math.floor(b.time / DAY);
      if (d !== day) {
        day = d;
        anchor = b.time;
        bucket = -1;
      }
      const k = Math.floor((b.time - anchor) / len);
      if (k !== bucket || out.length === 0) {
        bucket = k;
        out.push({ time: anchor + k * len, open: b.open, high: b.high, low: b.low, close: b.close, volume: b.volume, ...(b.oi !== undefined ? { oi: b.oi } : {}) });
      } else {
        combine(out[out.length - 1], b);
      }
    }
    return out;
  }
  let bucket = Number.NaN;
  for (const b of bars) {
    const k = calendarBucket(b.time, iv);
    if (k !== bucket || out.length === 0) {
      bucket = k;
      out.push({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close, volume: b.volume, ...(b.oi !== undefined ? { oi: b.oi } : {}) });
    } else {
      combine(out[out.length - 1], b);
    }
  }
  return out;
}

/** Derivative symbols (futures / options) — the ones Fyers has open interest for. */
export function isDerivative(symbol: string): boolean {
  return /(FUT|CE|PE)$/i.test(symbol.trim());
}

/**
 * Prepend an older aggregated page to the current bars, merging the one
 * bucket a page boundary can split (only calendar intervals can — the
 * chart pages intraday history on day boundaries).
 */
export function mergeOlder(older: Bar[], current: Bar[], key: string): Bar[] {
  if (older.length === 0) return current.slice();
  if (current.length === 0) return older.slice();
  const iv = parseInterval(key);
  const a = older[older.length - 1];
  const b = current[0];
  if (iv && (iv.unit === "D" || iv.unit === "W" || iv.unit === "M") && !(iv.unit === "D" && iv.n === 1)) {
    if (calendarBucket(a.time, iv) === calendarBucket(b.time, iv)) {
      const merged: Bar = { ...a };
      combine(merged, b);
      return [...older.slice(0, -1), merged, ...current.slice(1)];
    }
  }
  return [...older, ...current];
}

/** Where a live tick at chart time `t` belongs relative to the last bar. */
export function liveBucket(
  last: Bar,
  t: number,
  key: string,
): { kind: "same" } | { kind: "new"; time: number } | { kind: "stale" } {
  if (t < last.time) return { kind: "stale" };
  const iv = parseInterval(key);
  if (!iv) return { kind: "same" };
  if (iv.unit === "S" || iv.unit === "m") {
    const len = iv.unit === "S" ? iv.n : iv.n * 60;
    if (Math.floor(t / DAY) !== Math.floor(last.time / DAY)) {
      // A new session: anchor to the NSE open (09:15) when past it.
      const day0 = Math.floor(t / DAY) * DAY;
      const open = day0 + 555 * 60;
      const anchor = t >= open ? open : day0;
      return { kind: "new", time: anchor + Math.floor((t - anchor) / len) * len };
    }
    const n = Math.floor((t - last.time) / len);
    return n === 0 ? { kind: "same" } : { kind: "new", time: last.time + n * len };
  }
  if (iv.unit === "D" && iv.n === 1) {
    const n = Math.floor((t - last.time) / DAY);
    return n === 0 ? { kind: "same" } : { kind: "new", time: last.time + n * DAY };
  }
  return calendarBucket(t, iv) === calendarBucket(last.time, iv)
    ? { kind: "same" }
    : { kind: "new", time: Math.floor(t / DAY) * DAY };
}

// ---------------------------------------------------------------------------
// Non-time-based chart types
// ---------------------------------------------------------------------------

/** Average true range of the whole set (box size for "auto" bricks). */
export function autoBox(bars: Bar[], period = 14): number {
  if (bars.length < 2) return bars[0] ? Math.max(bars[0].close * 0.005, 0.05) : 1;
  const start = Math.max(1, bars.length - period * 10);
  let sum = 0;
  let n = 0;
  for (let i = start; i < bars.length; i++) {
    const b = bars[i];
    const pc = bars[i - 1].close;
    sum += Math.max(b.high - b.low, Math.abs(b.high - pc), Math.abs(b.low - pc));
    n++;
  }
  const atr = n ? sum / n : 1;
  // Round to a "nice" tick so brick edges land on readable prices.
  const mag = 10 ** Math.floor(Math.log10(Math.max(atr, 1e-6)));
  const nice = [1, 2, 2.5, 5, 10].map((m) => m * mag).reduce((a, b) => (Math.abs(b - atr) < Math.abs(a - atr) ? b : a));
  return Math.max(nice, 0.01);
}

/** Bricks formed inside the same source bar need strictly rising times. */
function stamp(out: Bar[], t: number): number {
  const prev = out.length ? out[out.length - 1].time : -Infinity;
  return t > prev ? t : prev + 1;
}

/** Traditional Renko on closes: one box to continue, two to reverse. */
export function renko(bars: Bar[], box: number): Bar[] {
  const out: Bar[] = [];
  if (bars.length === 0 || !(box > 0)) return out;
  let hi = Math.round(bars[0].close / box) * box;
  let lo = hi;
  let vol = 0;
  for (const b of bars) {
    vol += b.volume;
    const c = b.close;
    while (c >= hi + box) {
      const o = hi;
      hi += box;
      lo = hi - box;
      out.push({ time: stamp(out, b.time), open: o, high: hi, low: o, close: hi, volume: vol });
      vol = 0;
    }
    while (c <= lo - box) {
      const o = lo;
      lo -= box;
      hi = lo + box;
      out.push({ time: stamp(out, b.time), open: o, high: o, low: lo, close: lo, volume: vol });
      vol = 0;
    }
  }
  return out;
}

/** Three-line break (n configurable). */
export function lineBreak(bars: Bar[], lines = 3): Bar[] {
  const out: Bar[] = [];
  if (bars.length === 0) return out;
  let vol = 0;
  let i = 0;
  // seed: the first close that differs from the first open
  for (; i < bars.length; i++) {
    vol += bars[i].volume;
    if (bars[i].close !== bars[0].open) break;
  }
  if (i >= bars.length) return out;
  const o0 = bars[0].open;
  const c0 = bars[i].close;
  out.push({ time: bars[i].time, open: o0, close: c0, high: Math.max(o0, c0), low: Math.min(o0, c0), volume: vol });
  vol = 0;
  for (i = i + 1; i < bars.length; i++) {
    const b = bars[i];
    vol += b.volume;
    const last = out[out.length - 1];
    const up = last.close > last.open;
    const recent = out.slice(-lines);
    const hi = Math.max(...recent.map((x) => x.high));
    const lo = Math.min(...recent.map((x) => x.low));
    let o: number | null = null;
    if (up) {
      if (b.close > last.close) o = last.close;
      else if (b.close < lo) o = last.open;
    } else {
      if (b.close < last.close) o = last.close;
      else if (b.close > hi) o = last.open;
    }
    if (o !== null) {
      out.push({ time: stamp(out, b.time), open: o, close: b.close, high: Math.max(o, b.close), low: Math.min(o, b.close), volume: vol });
      vol = 0;
    }
  }
  return out;
}

/** Kagi on closes. Each segment is a bar: open = start, close = end;
 *  flag 1 = yang (thick, above the last shoulder), 0 = yin. */
export function kagi(bars: Bar[], reversal: number): Bar[] {
  const out: Bar[] = [];
  if (bars.length < 2 || !(reversal > 0)) return out;
  let start = bars[0].close;
  let cur = start;
  let dir = 0;
  let vol = 0;
  let thick = 1;
  let lastShoulder = -Infinity;
  let lastWaist = Infinity;
  let t = bars[0].time;
  const push = (time: number) => {
    const up = cur > start;
    if (up && cur > lastShoulder) thick = 1;
    if (!up && cur < lastWaist) thick = 0;
    if (up) lastShoulder = cur;
    else lastWaist = cur;
    out.push({ time: stamp(out, time), open: start, close: cur, high: Math.max(start, cur), low: Math.min(start, cur), volume: vol, flag: thick });
    vol = 0;
  };
  for (let i = 1; i < bars.length; i++) {
    const c = bars[i].close;
    vol += bars[i].volume;
    if (dir === 0) {
      if (Math.abs(c - start) >= reversal) {
        dir = c > start ? 1 : -1;
        cur = c;
        t = bars[i].time;
      }
      continue;
    }
    if (dir > 0) {
      if (c > cur) { cur = c; t = bars[i].time; }
      else if (cur - c >= reversal) { push(t); start = cur; cur = c; dir = -1; t = bars[i].time; }
    } else {
      if (c < cur) { cur = c; t = bars[i].time; }
      else if (c - cur >= reversal) { push(t); start = cur; cur = c; dir = 1; t = bars[i].time; }
    }
  }
  if (dir !== 0) push(t);
  return out;
}

/** Point & figure on closes. A column is a bar from its bottom box to its
 *  top box; flag 1 = X (rising) column, 0 = O. */
export function pointFigure(bars: Bar[], box: number, reversal = 3): Bar[] {
  const out: Bar[] = [];
  if (bars.length === 0 || !(box > 0)) return out;
  const fl = (p: number) => Math.floor(p / box + 1e-9) * box;
  const cl = (p: number) => Math.ceil(p / box - 1e-9) * box;
  const base = fl(bars[0].close);
  let col: Bar | null = null;
  let vol = 0;
  for (const b of bars) {
    vol += b.volume;
    const c = b.close;
    if (!col) {
      if (c >= base + box) col = { time: b.time, open: base, high: fl(c), low: base, close: fl(c), volume: vol, flag: 1 };
      else if (c <= base - box) col = { time: b.time, open: base, high: base, low: cl(c), close: cl(c), volume: vol, flag: 0 };
      if (col) { out.push(col); vol = 0; }
      continue;
    }
    if (col.flag === 1) {
      if (c >= col.high + box) { col.high = fl(c); col.close = col.high; col.volume += vol; vol = 0; }
      else if (c <= col.high - reversal * box) {
        const top = col.high - box;
        col = { time: stamp(out, b.time), open: top, high: top, low: cl(c), close: cl(c), volume: vol, flag: 0 };
        out.push(col);
        vol = 0;
      }
    } else {
      if (c <= col.low - box) { col.low = cl(c); col.close = col.low; col.volume += vol; vol = 0; }
      else if (c >= col.low + reversal * box) {
        const bottom = col.low + box;
        col = { time: stamp(out, b.time), open: bottom, high: fl(c), low: bottom, close: fl(c), volume: vol, flag: 1 };
        out.push(col);
        vol = 0;
      }
    }
  }
  return out;
}

/** Range bars: every bar spans exactly `range` from low to high. The
 *  intrabar path is approximated as open → nearer extreme → other → close. */
export function rangeBars(bars: Bar[], range: number): Bar[] {
  const out: Bar[] = [];
  if (bars.length === 0 || !(range > 0)) return out;
  let cur: Bar | null = null;
  let vol = 0;
  const feed = (p: number, t: number) => {
    if (!cur) {
      cur = { time: stamp(out, t), open: p, high: p, low: p, close: p, volume: 0 };
      return;
    }
    for (let guard = 0; guard < 10000; guard++) {
      if (p > cur.low + range) {
        cur.high = cur.low + range;
        cur.close = cur.high;
        cur.volume = vol;
        vol = 0;
        out.push(cur);
        const o: number = cur.close;
        cur = { time: stamp(out, t), open: o, high: o, low: o, close: o, volume: 0 };
      } else if (p < cur.high - range) {
        cur.low = cur.high - range;
        cur.close = cur.low;
        cur.volume = vol;
        vol = 0;
        out.push(cur);
        const o: number = cur.close;
        cur = { time: stamp(out, t), open: o, high: o, low: o, close: o, volume: 0 };
      } else break;
    }
    if (p > cur.high) cur.high = p;
    if (p < cur.low) cur.low = p;
    cur.close = p;
  };
  for (const b of bars) {
    vol += b.volume;
    const upFirst = Math.abs(b.high - b.open) < Math.abs(b.open - b.low);
    for (const p of upFirst ? [b.open, b.high, b.low, b.close] : [b.open, b.low, b.high, b.close]) feed(p, b.time);
  }
  if (cur) {
    const last = cur as Bar;
    last.volume = vol;
    out.push(last);
  }
  return out;
}

// ---------------------------------------------------------------------------
// Time zones / formatting
// ---------------------------------------------------------------------------

export const IST_OFFSET = 19800;

export const TIMEZONES: { id: string; label: string }[] = [
  { id: "exchange", label: "Exchange (UTC+5:30 Kolkata)" },
  { id: "local", label: "Browser local time" },
  { id: "UTC", label: "UTC" },
  { id: "Asia/Kolkata", label: "(UTC+5:30) Kolkata" },
  { id: "Asia/Dubai", label: "(UTC+4) Dubai" },
  { id: "Asia/Singapore", label: "(UTC+8) Singapore" },
  { id: "Asia/Hong_Kong", label: "(UTC+8) Hong Kong" },
  { id: "Asia/Tokyo", label: "(UTC+9) Tokyo" },
  { id: "Australia/Sydney", label: "Sydney" },
  { id: "Europe/London", label: "London" },
  { id: "Europe/Berlin", label: "Berlin" },
  { id: "Europe/Moscow", label: "(UTC+3) Moscow" },
  { id: "America/New_York", label: "New York" },
  { id: "America/Chicago", label: "Chicago" },
  { id: "America/Los_Angeles", label: "Los Angeles" },
  { id: "America/Sao_Paulo", label: "São Paulo" },
];

export const DATE_FORMATS = [
  "dd MMM 'yy",
  "MMM dd 'yy",
  "dd-MM-yyyy",
  "yyyy-MM-dd",
  "MM/dd/yy",
  "dd/MM/yy",
  "dd.MM.yyyy",
] as const;
export type DateFormat = (typeof DATE_FORMATS)[number];

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export interface WallClock {
  y: number;
  mo: number; // 0-11
  d: number;
  h: number;
  mi: number;
  s: number;
  dow: number;
}

const dtfCache = new Map<string, Intl.DateTimeFormat>();

/** Wall-clock parts of a chart time in the given zone. */
export function wallClock(chartTime: number, tz: string): WallClock {
  if (tz === "exchange" || tz === "Asia/Kolkata") {
    const d = new Date(chartTime * 1000);
    return { y: d.getUTCFullYear(), mo: d.getUTCMonth(), d: d.getUTCDate(), h: d.getUTCHours(), mi: d.getUTCMinutes(), s: d.getUTCSeconds(), dow: d.getUTCDay() };
  }
  const epoch = (chartTime - IST_OFFSET) * 1000;
  if (tz === "local") {
    const d = new Date(epoch);
    return { y: d.getFullYear(), mo: d.getMonth(), d: d.getDate(), h: d.getHours(), mi: d.getMinutes(), s: d.getSeconds(), dow: d.getDay() };
  }
  try {
    let f = dtfCache.get(tz);
    if (!f) {
      f = new Intl.DateTimeFormat("en-US", {
        timeZone: tz, hourCycle: "h23", year: "numeric", month: "numeric", day: "numeric",
        hour: "numeric", minute: "numeric", second: "numeric", weekday: "short",
      });
      dtfCache.set(tz, f);
    }
    const p: Record<string, string> = {};
    for (const part of f.formatToParts(new Date(epoch))) p[part.type] = part.value;
    const dows = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
    return { y: Number(p.year), mo: Number(p.month) - 1, d: Number(p.day), h: Number(p.hour) % 24, mi: Number(p.minute), s: Number(p.second), dow: Math.max(0, dows.indexOf(p.weekday)) };
  } catch {
    return wallClock(chartTime, "exchange");
  }
}

const p2 = (n: number) => String(n).padStart(2, "0");

export function formatDate(w: WallClock, fmt: DateFormat): string {
  const yy = String(w.y).slice(2);
  switch (fmt) {
    case "dd MMM 'yy": return `${p2(w.d)} ${MONTHS[w.mo]} '${yy}`;
    case "MMM dd 'yy": return `${MONTHS[w.mo]} ${p2(w.d)} '${yy}`;
    case "dd-MM-yyyy": return `${p2(w.d)}-${p2(w.mo + 1)}-${w.y}`;
    case "yyyy-MM-dd": return `${w.y}-${p2(w.mo + 1)}-${p2(w.d)}`;
    case "MM/dd/yy": return `${p2(w.mo + 1)}/${p2(w.d)}/${yy}`;
    case "dd/MM/yy": return `${p2(w.d)}/${p2(w.mo + 1)}/${yy}`;
    case "dd.MM.yyyy": return `${p2(w.d)}.${p2(w.mo + 1)}.${w.y}`;
  }
}

export function formatClock(w: WallClock, hour12: boolean, seconds = false): string {
  const s = seconds ? `:${p2(w.s)}` : "";
  if (!hour12) return `${p2(w.h)}:${p2(w.mi)}${s}`;
  const h = w.h % 12 === 0 ? 12 : w.h % 12;
  return `${h}:${p2(w.mi)}${s} ${w.h < 12 ? "AM" : "PM"}`;
}

export function monthName(mo: number): string {
  return MONTHS[mo] ?? "";
}

/** "+5:30"-style offset of a zone right now, for the clock. */
export function zoneOffsetLabel(tz: string): string {
  const now = Math.floor(Date.now() / 1000) + IST_OFFSET;
  const w = wallClock(now, tz);
  const asUtc = Date.UTC(w.y, w.mo, w.d, w.h, w.mi) / 1000;
  const off = Math.round((asUtc - (now - IST_OFFSET)) / 60);
  const sign = off >= 0 ? "+" : "-";
  const a = Math.abs(off);
  return `UTC${sign}${Math.floor(a / 60)}${a % 60 ? `:${p2(a % 60)}` : ""}`;
}

/** Human time span: "3d 4h", "45m", "12s". */
export function formatSpan(seconds: number): string {
  const s = Math.abs(Math.round(seconds));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return h ? `${d}d ${h}h` : `${d}d`;
  if (h > 0) return m ? `${h}h ${m}m` : `${h}h`;
  if (m > 0) return `${m}m`;
  return `${s % 60}s`;
}
