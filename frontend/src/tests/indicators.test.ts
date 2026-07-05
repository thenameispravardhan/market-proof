// Unit tests for the pure indicator math behind the Trade-page chart.
// Fixtures are tiny hand-checkable series; the goal is sanity (warm-up
// nulls, alignment, direction), not bit-exact parity with any vendor.

import { describe, it, expect } from "vitest";
import {
  adx,
  atr,
  bollinger,
  cci,
  donchian,
  ema,
  heikinAshi,
  heikinAshiBar,
  ichimoku,
  macd,
  mfi,
  obv,
  psar,
  rsi,
  sma,
  stochastic,
  supertrend,
  vwap,
  williamsR,
  wma,
  type OhlcvCandle,
} from "../lib/indicators";

/** Build a candle series from closes (high/low straddle the close). */
function candles(closes: number[], volume = 1000): OhlcvCandle[] {
  return closes.map((c, i) => ({
    time: 1_000_000 + i * 300,
    open: i > 0 ? closes[i - 1] : c,
    high: c + 1,
    low: c - 1,
    close: c,
    volume,
  }));
}

describe("moving averages", () => {
  it("sma warms up then averages the window", () => {
    const out = sma([1, 2, 3, 4, 5], 3);
    expect(out.slice(0, 2)).toEqual([null, null]);
    expect(out[2]).toBeCloseTo(2);
    expect(out[4]).toBeCloseTo(4);
  });

  it("ema is seeded with the sma and tracks upward moves", () => {
    const out = ema([1, 2, 3, 4, 5, 6], 3);
    expect(out[1]).toBeNull();
    expect(out[2]).toBeCloseTo(2); // seed = mean(1,2,3)
    expect(out[5]! > out[4]!).toBe(true);
  });

  it("wma weights the newest value hardest", () => {
    const out = wma([1, 2, 3], 3);
    // (1*1 + 2*2 + 3*3) / 6 = 14/6
    expect(out[2]).toBeCloseTo(14 / 6);
  });
});

describe("bands and channels", () => {
  it("bollinger middle equals the sma and bands straddle it", () => {
    const values = [2, 4, 6, 8, 10, 12];
    const b = bollinger(values, 4, 2);
    expect(b.middle[3]).toBeCloseTo(5);
    expect(b.upper[3]! > b.middle[3]!).toBe(true);
    expect(b.lower[3]! < b.middle[3]!).toBe(true);
  });

  it("donchian tracks rolling extremes", () => {
    const cs = candles([10, 20, 30, 25, 15]);
    const d = donchian(cs, 3);
    expect(d.upper[2]).toBeCloseTo(31); // max high of first 3 bars
    expect(d.lower[4]).toBeCloseTo(14); // min low of last 3 bars
    expect(d.middle[2]).toBeCloseTo((31 + 9) / 2);
  });
});

describe("oscillators", () => {
  it("rsi saturates high on a pure uptrend", () => {
    const out = rsi(Array.from({ length: 30 }, (_, i) => 100 + i), 14);
    expect(out[13]).toBeNull();
    expect(out[29]).toBeCloseTo(100);
  });

  it("macd histogram = macd - signal", () => {
    const values = Array.from({ length: 60 }, (_, i) => 100 + Math.sin(i / 5) * 10);
    const m = macd(values, 12, 26, 9);
    const i = 55;
    expect(m.histogram[i]).toBeCloseTo(m.macd[i]! - m.signal[i]!);
  });

  it("stochastic sits near 100 in an uptrend and %D lags %K", () => {
    const cs = candles(Array.from({ length: 30 }, (_, i) => 100 + i * 2));
    const st = stochastic(cs, 14, 3, 3);
    expect(st.k[29]! > 80).toBe(true);
    expect(st.d[29]).not.toBeNull();
  });

  it("cci is positive when price runs above its typical-price mean", () => {
    const cs = candles([...Array.from({ length: 19 }, () => 100), 120]);
    const out = cci(cs, 20);
    expect(out[19]! > 0).toBe(true);
  });

  it("williams %R stays in [-100, 0]", () => {
    const cs = candles(Array.from({ length: 20 }, (_, i) => 100 + Math.sin(i) * 5));
    const out = williamsR(cs, 14);
    for (const v of out) {
      if (v !== null) {
        expect(v).toBeLessThanOrEqual(0);
        expect(v).toBeGreaterThanOrEqual(-100);
      }
    }
  });

  it("mfi saturates at 100 when every bar's typical price rises", () => {
    const cs = candles(Array.from({ length: 20 }, (_, i) => 100 + i));
    const out = mfi(cs, 14);
    expect(out[19]).toBeCloseTo(100);
  });
});

describe("volume + volatility", () => {
  it("obv accumulates volume with the close direction", () => {
    const cs = candles([10, 11, 10, 12], 500);
    const out = obv(cs);
    expect(out).toEqual([0, 500, 0, 500]);
  });

  it("atr is positive once warmed up", () => {
    const cs = candles(Array.from({ length: 20 }, (_, i) => 100 + (i % 3)));
    const out = atr(cs, 14);
    expect(out[13]).toBeNull();
    expect(out[19]! > 0).toBe(true);
  });

  it("vwap resets per (IST) day and follows volume-weighted price", () => {
    const day1 = candles([10, 20]).map((c, i) => ({ ...c, time: i * 300 }));
    const day2 = candles([100]).map((c) => ({ ...c, time: 86400 + 300 }));
    const out = vwap([...day1, ...day2]);
    expect(out[1]).toBeCloseTo(15); // mean of the two typical prices (equal vol)
    expect(out[2]).toBeCloseTo(100); // fresh session
  });
});

describe("heikin ashi", () => {
  it("smooths candles with the classic recurrence", () => {
    const cs: OhlcvCandle[] = [
      { time: 0, open: 100, high: 110, low: 95, close: 105, volume: 10 },
      { time: 300, open: 105, high: 115, low: 100, close: 112, volume: 20 },
    ];
    const ha = heikinAshi(cs);
    // bar 0: haClose = (100+110+95+105)/4 = 102.5, haOpen = (100+105)/2 = 102.5
    expect(ha[0].close).toBeCloseTo(102.5);
    expect(ha[0].open).toBeCloseTo(102.5);
    // bar 1: haOpen = (102.5+102.5)/2 = 102.5, haClose = (105+115+100+112)/4 = 108
    expect(ha[1].open).toBeCloseTo(102.5);
    expect(ha[1].close).toBeCloseTo(108);
    expect(ha[1].high).toBeCloseTo(115); // max(h, haOpen, haClose)
    // times and volumes pass through untouched
    expect(ha.map((c) => c.time)).toEqual([0, 300]);
    expect(ha[1].volume).toBe(20);
  });

  it("heikinAshiBar matches the full transform for the last bar", () => {
    const cs: OhlcvCandle[] = [
      { time: 0, open: 100, high: 110, low: 95, close: 105, volume: 10 },
      { time: 300, open: 105, high: 115, low: 100, close: 112, volume: 20 },
      { time: 600, open: 112, high: 118, low: 108, close: 110, volume: 15 },
    ];
    const full = heikinAshi(cs);
    const incremental = heikinAshiBar(cs[2], {
      open: full[1].open,
      close: full[1].close,
    });
    expect(incremental.open).toBeCloseTo(full[2].open);
    expect(incremental.close).toBeCloseTo(full[2].close);
    expect(incremental.high).toBeCloseTo(full[2].high);
    expect(incremental.low).toBeCloseTo(full[2].low);
  });
});

describe("trend systems", () => {
  it("supertrend flags a sustained uptrend and sits below price", () => {
    const cs = candles(Array.from({ length: 40 }, (_, i) => 100 + i * 3));
    const st = supertrend(cs, 10, 3);
    expect(st.up[39]).toBe(true);
    expect(st.value[39]! < cs[39].close).toBe(true);
  });

  it("psar stays below price in an uptrend", () => {
    const cs = candles(Array.from({ length: 30 }, (_, i) => 100 + i * 2));
    const out = psar(cs);
    expect(out[29]! < cs[29].close).toBe(true);
  });

  it("adx grows in a strong trend and +DI leads -DI", () => {
    const cs = candles(Array.from({ length: 60 }, (_, i) => 100 + i * 2));
    const a = adx(cs, 14);
    expect(a.adx[59]! > 20).toBe(true);
    expect(a.plusDi[59]! > a.minusDi[59]!).toBe(true);
  });

  it("ichimoku aligns tenkan/kijun midpoints", () => {
    const cs = candles(Array.from({ length: 60 }, (_, i) => 100 + i));
    const ic = ichimoku(cs);
    // steady +1/bar: tenkan (9) mid = close - 4, kijun (26) mid = close - 12.5
    expect(ic.tenkan[59]).toBeCloseTo(cs[59].close - 4);
    expect(ic.kijun[59]).toBeCloseTo(cs[59].close - 12.5);
    expect(ic.spanA[59]).toBeCloseTo(((ic.tenkan[59] as number) + (ic.kijun[59] as number)) / 2);
    expect(ic.chikou[59]).toBeCloseTo(cs[59].close);
  });
});
