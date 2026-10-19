import { describe, expect, it } from "vitest";
import { flowPressure } from "../components/trade/indicatorCatalog";

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
