import { describe, expect, it } from "vitest";
import { bigTrades, INDICATOR_BY_TYPE, oiChange, optionsPcr, tapeSpeed, type FlowTuple, type OptFlowTuple } from "../components/trade/indicatorCatalog";

const bars = (n: number, step = 300) => Array.from({ length: n }, (_, i) => ({ time: i * step }));

describe("recorded-flow indicators", () => {
  it("big trades: the biggest print per candle at or over the minimum", () => {
    const prints = new Map<number, [number, number, number][]>([[0, [[100, 500, 1], [101, 900, -1]]], [300, [[99, 200, 1]]]]);
    expect(bigTrades(bars(3), prints)).toEqual({ price: [101, 99, null], qty: [900, 200, null], side: [-1, 1, null] });
    expect(bigTrades(bars(3), prints, 600).price).toEqual([101, null, null]);
  });

  it("OI change: from the previous candle that had OI", () => {
    const f = (oi: number | null) => [0, 0, 0, 0, 0, oi] as FlowTuple;
    const flow = new Map<number, FlowTuple>([[0, f(1000)], [300, f(1200)], [900, f(1100)]]);
    expect(oiChange(bars(4), flow)).toEqual({ oi: [1000, 1200, null, 1100], chg: [null, 200, null, -100] });
  });

  it("options PCR: put ÷ call quantity per candle and through the session", () => {
    const q = (cb: number, cs: number, pb: number, ps: number) => [0, 0, 0, 0, 0, cb, cs, pb, ps] as OptFlowTuple;
    const flow = new Map<number, OptFlowTuple>([[0, q(100, 100, 300, 100)], [300, q(50, 50, 0, 0)], [86400, q(10, 0, 5, 0)]]);
    const r = optionsPcr([{ time: 0 }, { time: 300 }, { time: 86400 }], flow);
    expect(r.pcr).toEqual([2, 0, 0.5]);
    expect(r.session).toEqual([2, 400 / 300, 0.5]);              // a new session starts over
  });

  it("tape speed: prints per minute, with a z-score once there is history", () => {
    const f = (t: number) => [0, 0, 0, t] as FlowTuple;
    const flow = new Map<number, FlowTuple>(bars(12).map((b, i) => [b.time, f(i === 11 ? 500 : 50 + (i % 2) * 10)]));
    const r = tapeSpeed(bars(12), flow, 10);
    expect(r.speed[0]).toBe(10);                                   // 50 prints in a 5-minute candle
    expect(r.z.slice(0, 10).every((v) => v === null)).toBe(true);
    expect(r.z[11]!).toBeGreaterThan(5);                           // the burst
  });
});

describe("icebergs indicator", () => {
  it("marks each likely iceberg on its candle, filtered by ratio and size", () => {
    const def = INDICATOR_BY_TYPE.get("icebergs")!;
    const c = [0, 300].map((time) => ({ time, open: 1, high: 2, low: 0, close: 1, volume: 0 }));
    const icebergs = new Map([[300, [{ side: -1 as const, traded: 12000, shown: 1500, ratio: 8, price: 22750 },
      { side: 1 as const, traded: 400, shown: 300, ratio: 1.3, price: 22700 }]]]);
    const r = def.compute(c, { ratio: 3, min: 0 }, { interval: "5", up: "#0f0", down: "#f00", icebergs });
    expect(r.plots[0]).toEqual([null, 1]);
    expect(r.marks).toEqual([expect.objectContaining({ i: 1, pos: "above", shape: "arrowDown", text: "ICE 12.0K", color: "#f00" })]);
    expect(def.compute(c, { ratio: 3, min: 20000 }, { interval: "5", up: "#0f0", down: "#f00", icebergs }).marks).toEqual([]);
  });
});
