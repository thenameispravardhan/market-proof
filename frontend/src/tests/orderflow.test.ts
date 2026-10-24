import { describe, expect, it } from "vitest";
import {
  analyze, barSignals, candleAt, cellLayout, DEFAULT_FOOTPRINT, flowByCandle, footprintByCandle, fpSettings, groupRows, heatColor, inferTick, nakedPocs, rowStep,
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

  it("lays the rows out to fill the candle exactly, never past its high or low", () => {
    // 100.00-100.20 on screen from y=100 (high) to y=200 (low): five 0.05 rows of 20 px
    const g = cellLayout(100.0, 100.2, 0.05, 100, 200, 12);
    expect(g.n).toBe(5);
    expect(g.box(100.2)).toEqual([100, 120]);                             // the high's row starts at the high
    expect(g.box(100.0)).toEqual([180, 200]);                             // the low's row ends at the low
    expect(g.rowOf(100.37)).toBe(100.2);                                  // a print outside the bar sits on its extreme
    const grouped = cellLayout(100.0, 100.2, 0.1, 100, 200, 12);          // two-tick rows: 100.0, 100.1, 100.2
    expect(grouped.n).toBe(3);
    expect(grouped.box(100.2)[0]).toBe(100);
    expect(grouped.box(100.0)[1]).toBe(200);
    const flat = cellLayout(50, 50, 0.05, 300, 300, 12);                  // one traded price: one row around it
    expect(flat.box(50)).toEqual([294, 306]);
  });

  it("flags absorption at an extreme and delta divergence", () => {
    // buyers hammer the high (100.2) but the bar closes near its low
    const rows = groupRows([[100.0, 10, 12], [100.05, 9, 10], [100.1, 11, 9], [100.15, 10, 10], [100.2, 90, 20]], 0.05);
    const a = analyze(rows, 0.05, S);
    const sg = barSignals(a, { open: 100.1, high: 100.2, low: 100.0, close: 100.05 }, { high: 100.15, low: 100.0 });
    expect(sg.absorbHigh).toBe(true);
    expect(sg.absorbLow).toBe(false);
    expect(sg.divBear).toBe(false);                                       // delta is positive here
    const neg = analyze(groupRows([[100.0, 5, 40], [100.1, 5, 40], [100.2, 10, 30]], 0.1), 0.1, S);
    expect(barSignals(neg, { open: 100.1, high: 100.2, low: 100.0, close: 100.1 }, { high: 100.15, low: 99.9 }).divBear).toBe(true);
    const flatClose = barSignals(a, { open: 100.1, high: 100.2, low: 100.0, close: 100.18 }, null);
    expect(flatClose.absorbHigh).toBe(false);                             // it closed up there: no rejection
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

describe("footprint views: heatmap, naked POCs, profile options", () => {
  it("keeps only POCs no later bar traded through", () => {
    // [high, low, POC row low]; step 1: a POC row is [p, p + 1)
    const bars: [number, number, number | null][] = [
      [105, 100, 102],   // tested: bar 2 trades 101-103
      [110, 106, 108],   // naked: nothing later reaches 108
      [103, 101, 101],   // naked: bar 3's low is 102 > 101's row [101, 102)
      [104, 102, null],  // no footprint
    ];
    expect(nakedPocs(bars, 1)).toEqual([[1, 108], [2, 101]]);
    expect(nakedPocs([[100, 99, 99.5], [99.5, 98, null]], 0.5)).toEqual([]);   // touched at the row's low edge
  });

  it("ramps the heat colour from blue to red and clamps", () => {
    expect(heatColor(0)).toBe("hsla(230, 85%, 38%, 1)");
    expect(heatColor(1, 0.5)).toBe("hsla(0, 85%, 52%, 0.5)");
    expect(heatColor(7)).toBe(heatColor(1));
    expect(heatColor(-1)).toBe(heatColor(0));
  });

  it("normalises the new settings and keeps stored ones", () => {
    const d = fpSettings(undefined);
    expect([d.mode, d.profileScope, d.profileType, d.npoc, d.numbers]).toEqual(["bidask", "visible", "volume", false, true]);
    expect(fpSettings({ mode: "heatmap", profileScope: "session", profileType: "delta", npoc: true, numbers: false }))
      .toMatchObject({ mode: "heatmap", profileScope: "session", profileType: "delta", npoc: true, numbers: false });
    expect(fpSettings({ mode: "nope" as never, profileScope: "x" as never, profileType: "y" as never }))
      .toMatchObject({ mode: "bidask", profileScope: "visible", profileType: "volume" });
  });
});
