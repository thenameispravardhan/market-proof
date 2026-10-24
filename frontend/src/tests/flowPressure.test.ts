import { describe, expect, it } from "vitest";
import { bookPressure, flowPressure } from "../components/trade/indicatorCatalog";
import { flowByCandle } from "../components/trade/footprint";

// Same fixture as app/tests/test_algo.py::test_flow_pressure_absorption_and_aggression: chart and backtest agree.
describe("flowPressure", () => {
  it("flags absorption where the close refuses the aggressor, aggression where it follows", () => {
    const n = 30;
    const c = Array.from({ length: n }, (_, i) => {
      const close = 100 + (i % 3) * 0.1, open = i ? 100 + ((i - 1) % 3) * 0.1 : close;
      return { time: i * 300, open, high: Math.max(open, close) + 1, low: Math.min(open, close) - 1, close, volume: 0 };
    });
    const flow = new Map<number, [number, number, number]>();
    c.forEach((k, i) => { const b = 500 + (i % 4) * 20, s = 500 + ((i + 2) % 4) * 20; flow.set(k.time, [b, s, b - s]); });
    Object.assign(c[25], { high: 101, low: 99, close: 101 }); flow.set(c[25].time, [flow.get(c[25].time)![0], 3500, 0]);
    Object.assign(c[27], { high: 102, low: 100, close: 102 }); flow.set(c[27].time, [3500, flow.get(c[27].time)![1], 0]);
    const r = flowPressure(c, flow, 20, 3, 2);
    expect(r.absorption[25]).toBe(1);
    expect(r.aggression[25]).toBe(0);
    expect(r.aggression[27]).toBe(1);
    expect(r.absorption.slice(0, 25).every((x) => x === 0)).toBe(true);
    expect(r.deltaZ[25]!).toBeLessThan(-2);
    expect(r.pressure[26]!).toBeLessThan(0);
    expect(r.pressure[29]!).toBeGreaterThan(0);
  });
});

describe("bookPressure", () => {
  it("reads the book's bid vs ask quantity per candle, −100…+100, with an EMA; gaps stay empty", () => {
    const c = [0, 300, 600, 900].map((time) => ({ time }));
    const t = (bid: number | null, ask: number | null) => [0, 0, 0, 0, 0, null, null, 0, 0, bid, ask] as (number | null)[];
    const flow = new Map<number, (number | null)[]>([[0, t(300, 100)], [300, t(100, 300)], [900, t(0, 0)]]);
    const r = bookPressure(c, flow, 1);
    expect(r.raw).toEqual([50, -50, null, null]);              // 600: no candle flow; 900: an empty book
    expect(r.ema).toEqual([50, -50, null, null]);              // smoothing 1 = the raw value
    expect(bookPressure(c, flow, 3).ema[1]).toBe(0);           // 50 + 0.5 × (−50 − 50)
  });

  it("averages the book over a candle's sub-bars that carried it", () => {
    const m = flowByCandle([0], [[0, 1, 1, 0, 1, 0, null, 10, 0, 0, 900, 300], [60, 1, 1, 0, 1, 0, null, 10, 0, 0, 700, 500],
      [120, 1, 1, 0, 1, 0, null, 10, 0, 0, null, null]], 0);
    expect([m.get(0)!.bookBid, m.get(0)!.bookAsk]).toEqual([800, 400]);
  });
});
