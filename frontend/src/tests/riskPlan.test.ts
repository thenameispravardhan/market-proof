import { describe, expect, it } from "vitest";
import { dailyRisk, riskPlan } from "../components/trade/ProPanels";

describe("risk desk", () => {
  it("sizes to the rupee risk in whole lots, with R-multiple targets on the right side", () => {
    const p = riskPlan({ ltp: 1000, atr: 20, vol: 0.25, risk: 5000, atrMult: 0.5, lot: 75, tick: 0.05, long: true, minsLeft: 375 });
    expect(p.stopDist).toBeCloseTo(10);
    expect(p.qty).toBe(450);                       // 5000 / 10 = 500 shares -> 6 lots of 75
    expect(p.stop).toBeCloseTo(990);
    expect(p.targets.map((t) => Math.round(t))).toEqual([1010, 1020, 1030]);
    expect(p.move).toBeCloseTo(1000 * 0.25 / Math.sqrt(252), 6);   // a whole session = one daily sigma
    const s = riskPlan({ ltp: 1000, atr: 20, vol: 0.25, risk: 5000, atrMult: 0.5, lot: 1, tick: 0.05, long: false, minsLeft: 0 });
    expect(s.qty).toBe(500);
    expect(s.stop).toBeCloseTo(1010);
    expect(s.targets[0]).toBeCloseTo(990);
    expect(s.move).toBe(0);
    expect(riskPlan({ ltp: 1000, atr: 20, vol: 0.2, risk: 100, atrMult: 1, lot: 75, tick: 0.05, long: true, minsLeft: 10 }).qty).toBe(0);
  });

  it("reads ATR and realised vol off daily candles, and needs 21 of them", () => {
    const rows = Array.from({ length: 30 }, (_, i) => [i * 86400, 100, 102, 98, i % 2 ? 101 : 100, 1000]);
    const r = dailyRisk(rows)!;
    expect(r.atr).toBeCloseTo(4);                  // high - low = 4 dominates every true range
    expect(r.vol).toBeGreaterThan(0.1);
    expect(dailyRisk(rows.slice(0, 20))).toBeNull();
  });
});
