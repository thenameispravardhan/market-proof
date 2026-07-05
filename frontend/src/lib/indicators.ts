// indicators — pure indicator math for the Trade-page chart.
//
// Every function takes the candle series (oldest → newest) and returns
// arrays aligned 1:1 with the input; warm-up slots are `null` so the
// chart layer can skip them when building line data. No chart imports
// here — this file is plain math so it stays unit-testable.

export interface OhlcvCandle {
  time: number; // chart time (epoch seconds, already TZ-shifted)
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

/** Simple moving average of `values` over `period`. */
export function sma(values: number[], period: number): (number | null)[] {
  const out: (number | null)[] = new Array(values.length).fill(null);
  if (period <= 0) return out;
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    sum += values[i];
    if (i >= period) sum -= values[i - period];
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}

/** Exponential moving average (seeded with the SMA of the first period). */
export function ema(values: number[], period: number): (number | null)[] {
  const out: (number | null)[] = new Array(values.length).fill(null);
  if (period <= 0 || values.length < period) return out;
  const k = 2 / (period + 1);
  let seed = 0;
  for (let i = 0; i < period; i++) seed += values[i];
  let prev = seed / period;
  out[period - 1] = prev;
  for (let i = period; i < values.length; i++) {
    prev = values[i] * k + prev * (1 - k);
    out[i] = prev;
  }
  return out;
}

/** Session VWAP — cumulative Σ(typical·vol)/Σ(vol), reset each day.
 *  Day boundaries use the candle's (TZ-shifted) chart time, so a shift
 *  to IST makes the reset land on the Indian session boundary. Bars
 *  with zero volume carry the previous VWAP forward. */
export function vwap(candles: OhlcvCandle[]): (number | null)[] {
  const out: (number | null)[] = new Array(candles.length).fill(null);
  let day = -1;
  let cumPV = 0;
  let cumV = 0;
  for (let i = 0; i < candles.length; i++) {
    const c = candles[i];
    const d = Math.floor(c.time / 86400);
    if (d !== day) {
      day = d;
      cumPV = 0;
      cumV = 0;
    }
    const typical = (c.high + c.low + c.close) / 3;
    cumPV += typical * c.volume;
    cumV += c.volume;
    out[i] = cumV > 0 ? cumPV / cumV : out[i - 1] ?? null;
  }
  return out;
}

export interface BollingerBands {
  upper: (number | null)[];
  middle: (number | null)[];
  lower: (number | null)[];
}

/** Bollinger bands: SMA(period) ± mult·stddev(period). */
export function bollinger(
  values: number[],
  period = 20,
  mult = 2,
): BollingerBands {
  const middle = sma(values, period);
  const upper: (number | null)[] = new Array(values.length).fill(null);
  const lower: (number | null)[] = new Array(values.length).fill(null);
  for (let i = period - 1; i < values.length; i++) {
    const mean = middle[i];
    if (mean === null) continue;
    let variance = 0;
    for (let j = i - period + 1; j <= i; j++) {
      const d = values[j] - mean;
      variance += d * d;
    }
    const sd = Math.sqrt(variance / period);
    upper[i] = mean + mult * sd;
    lower[i] = mean - mult * sd;
  }
  return { upper, middle, lower };
}

/** RSI with Wilder's smoothing. */
export function rsi(values: number[], period = 14): (number | null)[] {
  const out: (number | null)[] = new Array(values.length).fill(null);
  if (values.length <= period) return out;
  let gain = 0;
  let loss = 0;
  for (let i = 1; i <= period; i++) {
    const d = values[i] - values[i - 1];
    if (d >= 0) gain += d;
    else loss -= d;
  }
  let avgGain = gain / period;
  let avgLoss = loss / period;
  const toRsi = () =>
    avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
  out[period] = toRsi();
  for (let i = period + 1; i < values.length; i++) {
    const d = values[i] - values[i - 1];
    avgGain = (avgGain * (period - 1) + Math.max(d, 0)) / period;
    avgLoss = (avgLoss * (period - 1) + Math.max(-d, 0)) / period;
    out[i] = toRsi();
  }
  return out;
}

/** Heikin-Ashi transform. Returns a new candle array (same times and
 *  volumes); the classic smoothing:
 *    haClose = (o + h + l + c) / 4
 *    haOpen  = (prevHaOpen + prevHaClose) / 2   (seed: (o + c) / 2)
 *    haHigh  = max(h, haOpen, haClose), haLow = min(l, haOpen, haClose) */
export function heikinAshi(candles: OhlcvCandle[]): OhlcvCandle[] {
  const out: OhlcvCandle[] = [];
  let prevOpen = 0;
  let prevClose = 0;
  for (let i = 0; i < candles.length; i++) {
    const c = candles[i];
    const haClose = (c.open + c.high + c.low + c.close) / 4;
    const haOpen = i === 0 ? (c.open + c.close) / 2 : (prevOpen + prevClose) / 2;
    out.push({
      time: c.time,
      open: haOpen,
      high: Math.max(c.high, haOpen, haClose),
      low: Math.min(c.low, haOpen, haClose),
      close: haClose,
      volume: c.volume,
    });
    prevOpen = haOpen;
    prevClose = haClose;
  }
  return out;
}

/** One Heikin-Ashi bar from a raw bar + the previous HA bar's open/close.
 *  Used by the chart's live-tick path so the forming bar updates without
 *  recomputing the whole series. */
export function heikinAshiBar(
  raw: OhlcvCandle,
  prevHa: { open: number; close: number } | null,
): OhlcvCandle {
  const haClose = (raw.open + raw.high + raw.low + raw.close) / 4;
  const haOpen = prevHa ? (prevHa.open + prevHa.close) / 2 : (raw.open + raw.close) / 2;
  return {
    time: raw.time,
    open: haOpen,
    high: Math.max(raw.high, haOpen, haClose),
    low: Math.min(raw.low, haOpen, haClose),
    close: haClose,
    volume: raw.volume,
  };
}

/** Weighted moving average — the most recent value carries weight `period`. */
export function wma(values: number[], period: number): (number | null)[] {
  const out: (number | null)[] = new Array(values.length).fill(null);
  if (period <= 0 || values.length < period) return out;
  const denom = (period * (period + 1)) / 2;
  for (let i = period - 1; i < values.length; i++) {
    let acc = 0;
    for (let j = 0; j < period; j++) acc += values[i - j] * (period - j);
    out[i] = acc / denom;
  }
  return out;
}

/** True range per bar (uses the previous close from the second bar on). */
function trueRange(candles: OhlcvCandle[]): number[] {
  const out: number[] = new Array(candles.length).fill(0);
  for (let i = 0; i < candles.length; i++) {
    const c = candles[i];
    if (i === 0) {
      out[i] = c.high - c.low;
    } else {
      const pc = candles[i - 1].close;
      out[i] = Math.max(c.high - c.low, Math.abs(c.high - pc), Math.abs(c.low - pc));
    }
  }
  return out;
}

/** Average true range with Wilder's smoothing. */
export function atr(candles: OhlcvCandle[], period = 14): (number | null)[] {
  const out: (number | null)[] = new Array(candles.length).fill(null);
  if (candles.length <= period) return out;
  const tr = trueRange(candles);
  let acc = 0;
  for (let i = 1; i <= period; i++) acc += tr[i];
  let prev = acc / period;
  out[period] = prev;
  for (let i = period + 1; i < candles.length; i++) {
    prev = (prev * (period - 1) + tr[i]) / period;
    out[i] = prev;
  }
  return out;
}

export interface SupertrendResult {
  /** The supertrend level for each bar (null during warm-up). */
  value: (number | null)[];
  /** true = uptrend (line below price), false = downtrend. */
  up: (boolean | null)[];
}

/** Supertrend(period, mult) — ATR band trailing stop that flips with trend. */
export function supertrend(
  candles: OhlcvCandle[],
  period = 10,
  mult = 3,
): SupertrendResult {
  const n = candles.length;
  const value: (number | null)[] = new Array(n).fill(null);
  const up: (boolean | null)[] = new Array(n).fill(null);
  const a = atr(candles, period);
  let fUpper = 0;
  let fLower = 0;
  let trendUp = true;
  let started = false;
  for (let i = 0; i < n; i++) {
    const av = a[i];
    if (av === null) continue;
    const mid = (candles[i].high + candles[i].low) / 2;
    const bUpper = mid + mult * av;
    const bLower = mid - mult * av;
    if (!started) {
      fUpper = bUpper;
      fLower = bLower;
      trendUp = candles[i].close >= mid;
      started = true;
    } else {
      const prevClose = candles[i - 1].close;
      fUpper = bUpper < fUpper || prevClose > fUpper ? bUpper : fUpper;
      fLower = bLower > fLower || prevClose < fLower ? bLower : fLower;
      if (candles[i].close > fUpper) trendUp = true;
      else if (candles[i].close < fLower) trendUp = false;
    }
    up[i] = trendUp;
    value[i] = trendUp ? fLower : fUpper;
  }
  return { value, up };
}

/** Parabolic SAR (step 0.02, max 0.2) — classic Wilder acceleration. */
export function psar(
  candles: OhlcvCandle[],
  step = 0.02,
  maxStep = 0.2,
): (number | null)[] {
  const n = candles.length;
  const out: (number | null)[] = new Array(n).fill(null);
  if (n < 2) return out;
  let rising = candles[1].close >= candles[0].close;
  let sar = rising ? candles[0].low : candles[0].high;
  let ep = rising ? candles[0].high : candles[0].low;
  let af = step;
  for (let i = 1; i < n; i++) {
    sar = sar + af * (ep - sar);
    const c = candles[i];
    if (rising) {
      // SAR may not sit inside the prior two bars' range
      sar = Math.min(sar, candles[i - 1].low, i >= 2 ? candles[i - 2].low : candles[i - 1].low);
      if (c.low < sar) {
        rising = false;
        sar = ep;
        ep = c.low;
        af = step;
      } else if (c.high > ep) {
        ep = c.high;
        af = Math.min(af + step, maxStep);
      }
    } else {
      sar = Math.max(sar, candles[i - 1].high, i >= 2 ? candles[i - 2].high : candles[i - 1].high);
      if (c.high > sar) {
        rising = true;
        sar = ep;
        ep = c.high;
        af = step;
      } else if (c.low < ep) {
        ep = c.low;
        af = Math.min(af + step, maxStep);
      }
    }
    out[i] = sar;
  }
  return out;
}

/** Highest high / lowest low over the trailing `period` bars (inclusive). */
function rollingExtremes(
  candles: OhlcvCandle[],
  period: number,
): { hh: (number | null)[]; ll: (number | null)[] } {
  const n = candles.length;
  const hh: (number | null)[] = new Array(n).fill(null);
  const ll: (number | null)[] = new Array(n).fill(null);
  for (let i = period - 1; i < n; i++) {
    let h = -Infinity;
    let l = Infinity;
    for (let j = i - period + 1; j <= i; j++) {
      if (candles[j].high > h) h = candles[j].high;
      if (candles[j].low < l) l = candles[j].low;
    }
    hh[i] = h;
    ll[i] = l;
  }
  return { hh, ll };
}

export interface IchimokuResult {
  tenkan: (number | null)[]; // conversion (9)
  kijun: (number | null)[]; // base (26)
  spanA: (number | null)[]; // leading span A — plot shifted +26
  spanB: (number | null)[]; // leading span B — plot shifted +26
  chikou: (number | null)[]; // lagging close — plot shifted -26
}

/** Ichimoku (9, 26, 52). Arrays are aligned to the input; the chart
 *  layer applies the ±26-bar plot shifts. */
export function ichimoku(candles: OhlcvCandle[]): IchimokuResult {
  const n = candles.length;
  const mid = (ex: { hh: (number | null)[]; ll: (number | null)[] }, i: number) =>
    ex.hh[i] !== null && ex.ll[i] !== null
      ? ((ex.hh[i] as number) + (ex.ll[i] as number)) / 2
      : null;
  const e9 = rollingExtremes(candles, 9);
  const e26 = rollingExtremes(candles, 26);
  const e52 = rollingExtremes(candles, 52);
  const tenkan: (number | null)[] = new Array(n).fill(null);
  const kijun: (number | null)[] = new Array(n).fill(null);
  const spanA: (number | null)[] = new Array(n).fill(null);
  const spanB: (number | null)[] = new Array(n).fill(null);
  const chikou: (number | null)[] = new Array(n).fill(null);
  for (let i = 0; i < n; i++) {
    tenkan[i] = mid(e9, i);
    kijun[i] = mid(e26, i);
    if (tenkan[i] !== null && kijun[i] !== null) {
      spanA[i] = ((tenkan[i] as number) + (kijun[i] as number)) / 2;
    }
    spanB[i] = mid(e52, i);
    chikou[i] = candles[i].close;
  }
  return { tenkan, kijun, spanA, spanB, chikou };
}

export interface DonchianResult {
  upper: (number | null)[];
  middle: (number | null)[];
  lower: (number | null)[];
}

/** Donchian channel over `period` bars. */
export function donchian(candles: OhlcvCandle[], period = 20): DonchianResult {
  const ex = rollingExtremes(candles, period);
  const middle = ex.hh.map((h, i) =>
    h !== null && ex.ll[i] !== null ? (h + (ex.ll[i] as number)) / 2 : null,
  );
  return { upper: ex.hh, middle, lower: ex.ll };
}

export interface StochasticResult {
  k: (number | null)[];
  d: (number | null)[];
}

/** Stochastic %K/%D (kPeriod, kSmooth, dPeriod). */
export function stochastic(
  candles: OhlcvCandle[],
  kPeriod = 14,
  kSmooth = 3,
  dPeriod = 3,
): StochasticResult {
  const n = candles.length;
  const ex = rollingExtremes(candles, kPeriod);
  const raw: (number | null)[] = new Array(n).fill(null);
  for (let i = 0; i < n; i++) {
    const h = ex.hh[i];
    const l = ex.ll[i];
    if (h === null || l === null) continue;
    raw[i] = h === l ? 50 : (100 * (candles[i].close - l)) / (h - l);
  }
  const smooth = (vals: (number | null)[], p: number): (number | null)[] => {
    const res: (number | null)[] = new Array(vals.length).fill(null);
    for (let i = 0; i < vals.length; i++) {
      let acc = 0;
      let cnt = 0;
      for (let j = i - p + 1; j <= i; j++) {
        if (j < 0 || vals[j] === null) {
          cnt = 0;
          break;
        }
        acc += vals[j] as number;
        cnt++;
      }
      if (cnt === p) res[i] = acc / p;
    }
    return res;
  };
  const k = smooth(raw, kSmooth);
  const d = smooth(k, dPeriod);
  return { k, d };
}

export interface AdxResult {
  adx: (number | null)[];
  plusDi: (number | null)[];
  minusDi: (number | null)[];
}

/** ADX with +DI/−DI (Wilder). */
export function adx(candles: OhlcvCandle[], period = 14): AdxResult {
  const n = candles.length;
  const adxOut: (number | null)[] = new Array(n).fill(null);
  const plusOut: (number | null)[] = new Array(n).fill(null);
  const minusOut: (number | null)[] = new Array(n).fill(null);
  if (n <= period * 2) return { adx: adxOut, plusDi: plusOut, minusDi: minusOut };
  const tr = trueRange(candles);
  const plusDm: number[] = new Array(n).fill(0);
  const minusDm: number[] = new Array(n).fill(0);
  for (let i = 1; i < n; i++) {
    const upMove = candles[i].high - candles[i - 1].high;
    const downMove = candles[i - 1].low - candles[i].low;
    plusDm[i] = upMove > downMove && upMove > 0 ? upMove : 0;
    minusDm[i] = downMove > upMove && downMove > 0 ? downMove : 0;
  }
  let sTr = 0;
  let sPlus = 0;
  let sMinus = 0;
  for (let i = 1; i <= period; i++) {
    sTr += tr[i];
    sPlus += plusDm[i];
    sMinus += minusDm[i];
  }
  const dx: (number | null)[] = new Array(n).fill(null);
  for (let i = period; i < n; i++) {
    if (i > period) {
      sTr = sTr - sTr / period + tr[i];
      sPlus = sPlus - sPlus / period + plusDm[i];
      sMinus = sMinus - sMinus / period + minusDm[i];
    }
    const pdi = sTr > 0 ? (100 * sPlus) / sTr : 0;
    const mdi = sTr > 0 ? (100 * sMinus) / sTr : 0;
    plusOut[i] = pdi;
    minusOut[i] = mdi;
    dx[i] = pdi + mdi > 0 ? (100 * Math.abs(pdi - mdi)) / (pdi + mdi) : 0;
  }
  let acc = 0;
  for (let i = period; i < period * 2; i++) acc += dx[i] as number;
  let prev = acc / period;
  adxOut[period * 2 - 1] = prev;
  for (let i = period * 2; i < n; i++) {
    prev = (prev * (period - 1) + (dx[i] as number)) / period;
    adxOut[i] = prev;
  }
  return { adx: adxOut, plusDi: plusOut, minusDi: minusOut };
}

/** On-balance volume (cumulative). */
export function obv(candles: OhlcvCandle[]): (number | null)[] {
  const out: (number | null)[] = new Array(candles.length).fill(null);
  let acc = 0;
  for (let i = 0; i < candles.length; i++) {
    if (i > 0) {
      if (candles[i].close > candles[i - 1].close) acc += candles[i].volume;
      else if (candles[i].close < candles[i - 1].close) acc -= candles[i].volume;
    }
    out[i] = acc;
  }
  return out;
}

/** Commodity channel index over `period`. */
export function cci(candles: OhlcvCandle[], period = 20): (number | null)[] {
  const n = candles.length;
  const out: (number | null)[] = new Array(n).fill(null);
  const tp = candles.map((c) => (c.high + c.low + c.close) / 3);
  for (let i = period - 1; i < n; i++) {
    let mean = 0;
    for (let j = i - period + 1; j <= i; j++) mean += tp[j];
    mean /= period;
    let dev = 0;
    for (let j = i - period + 1; j <= i; j++) dev += Math.abs(tp[j] - mean);
    dev /= period;
    out[i] = dev > 0 ? (tp[i] - mean) / (0.015 * dev) : 0;
  }
  return out;
}

/** Money flow index over `period` (volume-weighted RSI cousin). */
export function mfi(candles: OhlcvCandle[], period = 14): (number | null)[] {
  const n = candles.length;
  const out: (number | null)[] = new Array(n).fill(null);
  if (n <= period) return out;
  const tp = candles.map((c) => (c.high + c.low + c.close) / 3);
  const flow: number[] = new Array(n).fill(0); // signed money flow
  for (let i = 1; i < n; i++) {
    const mf = tp[i] * candles[i].volume;
    flow[i] = tp[i] > tp[i - 1] ? mf : tp[i] < tp[i - 1] ? -mf : 0;
  }
  for (let i = period; i < n; i++) {
    let pos = 0;
    let neg = 0;
    for (let j = i - period + 1; j <= i; j++) {
      if (flow[j] > 0) pos += flow[j];
      else neg -= flow[j];
    }
    out[i] = neg === 0 ? 100 : 100 - 100 / (1 + pos / neg);
  }
  return out;
}

/** Williams %R over `period` (0 to −100). */
export function williamsR(candles: OhlcvCandle[], period = 14): (number | null)[] {
  const n = candles.length;
  const out: (number | null)[] = new Array(n).fill(null);
  const ex = rollingExtremes(candles, period);
  for (let i = 0; i < n; i++) {
    const h = ex.hh[i];
    const l = ex.ll[i];
    if (h === null || l === null) continue;
    out[i] = h === l ? -50 : (-100 * (h - candles[i].close)) / (h - l);
  }
  return out;
}

export interface MacdResult {
  macd: (number | null)[];
  signal: (number | null)[];
  histogram: (number | null)[];
}

/** MACD(fast, slow, signal) on closes. */
export function macd(
  values: number[],
  fast = 12,
  slow = 26,
  signalPeriod = 9,
): MacdResult {
  const emaFast = ema(values, fast);
  const emaSlow = ema(values, slow);
  const macdLine: (number | null)[] = new Array(values.length).fill(null);
  for (let i = 0; i < values.length; i++) {
    const f = emaFast[i];
    const s = emaSlow[i];
    if (f !== null && s !== null) macdLine[i] = f - s;
  }
  // Signal = EMA of the macd line, computed over its non-null tail.
  const signal: (number | null)[] = new Array(values.length).fill(null);
  const histogram: (number | null)[] = new Array(values.length).fill(null);
  const start = macdLine.findIndex((v) => v !== null);
  if (start >= 0) {
    const tail = macdLine.slice(start) as number[];
    const sig = ema(tail, signalPeriod);
    for (let i = 0; i < tail.length; i++) {
      const s = sig[i];
      if (s !== null) {
        signal[start + i] = s;
        histogram[start + i] = tail[i] - s;
      }
    }
  }
  return { macd: macdLine, signal, histogram };
}
