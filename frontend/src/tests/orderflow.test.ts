import { describe, expect, it } from "vitest";
import {
  analyze, candleAt, DEFAULT_FOOTPRINT, flowByCandle, footprintByCandle, fpSettings, groupRows, inferTick, rowStep,
  sessionBasis, sessionCvd, type FlowBar,
} from "../components/trade/footprint";
import { INDICATOR_BY_TYPE, type FlowTuple } from "../components/trade/indicatorCatalog";

const S = { imbalance: 3, imbalanceMin: 0, stacked: 3, vaPct: 70 };

describe("footprint analytics", () => {
  it("groups prices into rows of whole ticks and sizes rows to the zoom", () => {
    expect(inferTick([100, 100.05, 100.2])).toBeCloseTo(0.05);
    expect(inferTick([24500, 24500.5, 24502])).toBeCloseTo(0.5);
    expect(rowStep(0.05, 100, 0)).toBeCloseTo(0.2);                        // 4 ticks: 20 px a row
    expect(rowStep(0.05, 1000, 0)).toBeCloseTo(0.05);                      // zoomed in: one tick a row
    expect(rowStep(0.05, 1, 5)).toBeCloseTo(0.25);                         // fixed 5 ticks
    expect(groupRows([[458.05, 10, 2], [458.0, 5, 1], [457.95, 0, 7], [458.2, 1, 1]], 0.1)).toEqual([
      { p: 457.9, buy: 0, sell: 7 }, { p: 458.0, buy: 15, sell: 3 }, { p: 458.2, buy: 1, sell: 1 },
    ]);
  });

  it("finds diagonal imbalances, a stacked zone, the POC and the value area", () => {
    // rows 100.00 .. 100.30 (step 0.05); buyers lift 100.10-100.20 hard against thin selling one row below
    const rows = groupRows([
      [100.0, 10, 70], [100.05, 20, 5], [100.1, 60, 30], [100.15, 120, 8], [100.2, 90, 10], [100.25, 5, 200], [100.3, 4, 6],
    ], 0.05);
    const a = analyze(rows, 0.05, S);
    expect([...a.buyImb].sort()).toEqual([100.1, 100.15, 100.2]);          // 60 >= 3x5, 120 >= 3x30, 90 >= 3x8
    expect([...a.sellImb]).toEqual([100.0, 100.25]);                       // 70 >= 3x20; 200 >= 3x4
    expect(a.zones).toEqual([{ lo: 100.1, hi: 100.25, side: 1 }]);        // three buy imbalances in a row
    expect(a.poc).toBe(100.25);                                            // 205 traded there
    expect(a.total).toBe(638);
    expect(a.val).toBe(100.1);
    expect(a.vah).toBe(100.25);
    expect(a.unfinishedHigh && a.unfinishedLow).toBe(true);               // both sides traded at both extremes
    const off = analyze(rows, 0.05, { ...S, imbalance: 0 });
    expect(off.buyImb.size + off.sellImb.size + off.zones.length).toBe(0);
    const big = analyze(rows, 0.05, { ...S, imbalanceMin: 100 });
    expect([...big.buyImb]).toEqual([100.15]);                             // only the 120 clears 100
  });

  it("folds the server's base bars into the chart's candles with the running delta range", () => {
    const times = [1000, 1240];                                            // a 4-minute chart from 2-minute rows
    const bars = [
      [1000, 30, 10, 20, 3, 0, 500, 100.1, 0, 25],
      [1120, 5, 25, -20, 2, 5, 520, 100.0, -22, 0],
      [1240, 7, 7, 0, 1, 0, 530, 100.05, -1, 3],
    ];
    const m = flowByCandle(times, bars, 0);
    expect(m.get(1000)).toEqual({ buy: 35, sell: 35, delta: 0, trades: 5, unk: 5, oi: 520, close: 100.0, dlo: -2, dhi: 25 });
    expect(m.get(1240)?.dhi).toBe(3);
    expect(candleAt(times, 999)).toBe(-1);
    const fp = footprintByCandle(times, [[1000, 100.1, 3, 1], [1120, 100.1, 2, 2], [1240, 100.0, 1, 0]], 0);
    expect(fp.get(1000)?.get(100.1)).toEqual([5, 3]);
    const cvd = sessionCvd([0, 300, 86400], new Map([[0, { delta: 10 }], [300, { delta: -4 }], [86400, { delta: 7 }]] as [number, FlowBar][]));
    expect([...cvd.values()]).toEqual([10, 6, 7]);                          // resets each session
    const basis = sessionBasis([{ time: 0, close: 25000 }, { time: 300, close: 25010 }, { time: 600, close: 25020 }],
      new Map([[0, { close: 25060 }], [300, { close: 25065 }], [600, { close: 25090 }]] as [number, FlowBar][]));
    expect(basis.get(0)).toBe(60);                                         // the session's median, not each bar's
  });

  it("keeps saved settings valid", () => {
    expect(fpSettings(undefined)).toEqual(DEFAULT_FOOTPRINT);
    expect(fpSettings({ mode: "delta", stats: ["cvd", "nope" as never] })).toMatchObject({ mode: "delta", stats: ["cvd"], imbalance: 3 });
  });
});

describe("CVD candles", () => {
  it("opens each candle where the delta stood and reaches the bar's running extremes", () => {
    const def = INDICATOR_BY_TYPE.get("cvd")!;
    const c = [0, 300, 600, 86400].map((time) => ({ time, open: 1, high: 1, low: 1, close: 1, volume: 0 }));
    const flow = new Map<number, FlowTuple>([
      [0, [60, 40, 20, 5, 0, null, null, -5, 30]],
      [300, [10, 40, -30, 4, 0, null, null, -35, 2]],
      [86400, [5, 0, 5, 1, 0, null, null, 0, 5]],
    ]);
    const r = def.compute(c, { reset: "session" }, { interval: "5", up: "#0f0", down: "#f00", flow });
    expect(r.plots[0]).toEqual([20, -10, null, 5]);
    expect(r.candles?.[0]).toEqual([[0, 30, -5, 20], [20, 22, -15, -10], null, [0, 5, 0, 5]]);
    const run = def.compute(c, { reset: "none" }, { interval: "5", up: "#0f0", down: "#f00", flow });
    expect(run.plots[0]).toEqual([20, -10, null, -5]);
  });
});
