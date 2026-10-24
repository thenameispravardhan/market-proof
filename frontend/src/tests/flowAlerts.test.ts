import { describe, expect, it } from "vitest";
import { candleEvents, DEFAULT_FLOW_ALERTS, flowAlertsCfg, needsFootprint, printEvents } from "../components/trade/flowAlerts";

const ON = { ...DEFAULT_FLOW_ALERTS, on: true, stacked: false, absorption: false, divergence: false, deltaZ: 0, book: 0 };
const IMB = { imbalance: 3, imbalanceMin: 0, stacked: 3, rowTicks: 0 };
const fmt = (p: number) => p.toFixed(2);

// 22 closed 5-minute candles; flow per candle: [buy, sell, delta, …, book bid (9), book ask (10)]
const bars = Array.from({ length: 22 }, (_, i) => ({ time: i * 300, open: 100, high: 101, low: 99, close: 100 }));
const flowOf = (deltas: number[], book?: [number, number]) => new Map(bars.map((b, i) => {
  const d = deltas[i] ?? 0;
  const t: (number | null)[] = [Math.max(0, d), Math.max(0, -d), d, 1, 0, null, null, 0, 0, null, null];
  if (book && i === bars.length - 1) [t[9], t[10]] = book;
  return [b.time, t];
}));

describe("order-flow alerts", () => {
  it("fires a delta spike only past the σ threshold, against the previous 20 candles", () => {
    const calm = Array.from({ length: 21 }, (_, i) => (i % 2 ? 100 : -100));
    const spike = candleEvents({ ...ON, deltaZ: 3 }, bars, flowOf([...calm, 900]), new Map(), IMB, "NIFTY");
    expect(spike).toHaveLength(1);
    expect(spike[0].key).toBe(`delta:${bars[21].time}`);
    expect(spike[0].text).toMatch(/NIFTY 01:45: delta spike \+900 \(\d+\.\dσ buying\)/);
    expect(candleEvents({ ...ON, deltaZ: 3 }, bars, flowOf([...calm, 150]), new Map(), IMB, "NIFTY")).toEqual([]);
    expect(candleEvents({ ...ON, deltaZ: 3 }, bars.slice(-5), flowOf([...calm, 900]), new Map(), IMB, "NIFTY")).toEqual([]);  // too little history
  });

  it("fires on a lopsided book", () => {
    const ev = candleEvents({ ...ON, book: 40 }, bars, flowOf([], [3000, 1000]), new Map(), IMB, "NIFTY");
    expect(ev.map((e) => e.text)).toEqual(["NIFTY 01:45: book bid-heavy +50% (bids 3,000 vs offers 1,000)"]);
    expect(candleEvents({ ...ON, book: 60 }, bars, flowOf([], [3000, 1000]), new Map(), IMB, "NIFTY")).toEqual([]);
  });

  it("finds a stacked imbalance and absorption in the closed candle's footprint", () => {
    const last = bars[21];
    // buyers lifting 3 rows in a row at ≥ 3× the selling one row below → a stacked buy zone 100.1–100.4
    const stack = new Map([[last.time, { fp: [[100.0, 0, 10], [100.1, 40, 10], [100.2, 40, 10], [100.3, 40, 10]] as [number, number, number][] }]]);
    const ev = candleEvents({ ...ON, stacked: true }, bars, flowOf([]), stack, IMB, "NIFTY");
    expect(ev.map((e) => e.text)).toEqual(["NIFTY 01:45: stacked BUY imbalance 100.1–100.4"]);
    // heavy buying at the high, closed in the bottom of the range → absorption at the high
    const top = { ...last, open: 100.2, high: 100.4, low: 100.0, close: 100.05 };
    const abs = new Map([[last.time, { fp: [[100.0, 2, 3], [100.1, 3, 2], [100.2, 2, 2], [100.3, 3, 3], [100.4, 30, 5]] as [number, number, number][] }]]);
    const ev2 = candleEvents({ ...ON, absorption: true }, [...bars.slice(0, -1), top], flowOf([]), abs, IMB, "NIFTY");
    expect(ev2.map((e) => e.key)).toEqual([`absH:${last.time}`]);
  });

  it("fires big prints newer than the arming point only", () => {
    const prints = [[1000, 22745.5, 5200, 1], [1001, 22744, 300, -1], [900, 22740, 9000, -1]];
    const ev = printEvents({ ...ON, bigPrint: 5000 }, prints, 950, "NIFTY", fmt);
    expect(ev).toEqual([{ key: "print:1000:22745.5:5200", text: "NIFTY: big BUY print 5,200 @ 22745.50" }]);
    expect(printEvents({ ...ON, bigPrint: 0 }, prints, 0, "NIFTY", fmt)).toEqual([]);
    expect(printEvents({ ...ON, on: false, bigPrint: 1 }, prints, 0, "NIFTY", fmt)).toEqual([]);
  });

  it("keeps saved settings valid and loads the footprint only when a rule needs it", () => {
    expect(flowAlertsCfg(undefined)).toEqual(DEFAULT_FLOW_ALERTS);
    expect(flowAlertsCfg({ deltaZ: -4, book: 250, bigPrint: Number.NaN })).toMatchObject({ deltaZ: 0, book: 100, bigPrint: 0 });
    expect(needsFootprint({ ...ON, deltaZ: 3, book: 50 })).toBe(false);
    expect(needsFootprint({ ...ON, bigPrint: 100 })).toBe(true);
    expect(needsFootprint({ ...DEFAULT_FLOW_ALERTS })).toBe(false);          // off
  });
});
