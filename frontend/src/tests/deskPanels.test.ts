import { describe, expect, it } from "vitest";
import { carryPct, imbalance, payoffAt, payoffStats, tapeSide, type Leg } from "../components/trade/ProPanels";
import { mergeLayouts } from "../components/trade/TradePanels";
import { INDICATOR_BY_TYPE, newInstance } from "../components/trade/indicatorCatalog";
import type { OhlcvCandle } from "../lib/indicators";

const leg = (type: "CE" | "PE", strike: number, side: 1 | -1, premium: number): Leg => ({ type, strike, side, lots: 1, premium, lot: 50 });

describe("strategy payoff", () => {
  it("long call: limited loss, unlimited profit, breakeven at strike + premium", () => {
    const s = payoffStats([leg("CE", 100, 1, 5)], 80, 130);
    expect(payoffAt([leg("CE", 100, 1, 5)], 120)).toBe((20 - 5) * 50);
    expect(s.maxP).toBeNull();
    expect(s.maxL).toBe(-250);
    expect(s.be[0]).toBeCloseTo(105, 0);
    expect(s.net).toBe(-250);
  });
  it("short put: loss bounded by the underlying at zero", () => {
    const s = payoffStats([leg("PE", 100, -1, 4)], 90, 110);
    expect(s.maxP).toBe(200);
    expect(s.maxL).toBe(-(100 - 4) * 50);
  });
  it("iron condor is bounded both ways with two breakevens", () => {
    const legs = [leg("CE", 110, -1, 3), leg("CE", 120, 1, 1), leg("PE", 90, -1, 3), leg("PE", 80, 1, 1)];
    const s = payoffStats(legs, 70, 130);
    expect(s.maxP).toBeCloseTo(4 * 50, 6);
    expect(s.maxL).toBeCloseTo(-(10 - 4) * 50, 6);
    expect(s.be).toHaveLength(2);
  });
});

describe("desk maths", () => {
  it("book imbalance and tape side", () => {
    expect(imbalance(300, 100)).toBe(0.5);
    expect(imbalance(0, 0)).toBe(0);
    expect(tapeSide(101, 100, 101)).toBe("B");
    expect(tapeSide(100, 100, 101)).toBe("S");
    expect(tapeSide(100.5, 100, 101)).toBe("");
  });
  it("annualised carry", () => {
    expect(carryPct(101, 100, 365 * 86400, 0)).toBeCloseTo(1, 6);
    expect(carryPct(101, 100, 0, 10)).toBeNull();
  });
  it("layout merge keeps the newer copy and the union", () => {
    const a = { id: "A", name: "a", saved: 1, data: {} };
    const b = { id: "B", name: "b", saved: 2, data: {} };
    const a2 = { ...a, name: "a2", saved: 3 };
    expect(mergeLayouts([a, b], [a2]).map((l) => l.name)).toEqual(["b", "a2"]);
  });
});

describe("extra indicators", () => {
  // Two 5-minute sessions; chart time is already IST-shifted (day = t / 86400).
  const day = (d: number, bars: [number, number, number, number][]): OhlcvCandle[] =>
    bars.map(([o, h, l, c], i) => ({ time: d * 86400 + 33300 + i * 300, open: o, high: h, low: l, close: c, volume: 100 }));
  const c = [...day(1, [[100, 110, 95, 105], [105, 108, 100, 102]]), ...day(2, [[103, 104, 101, 103], [103, 112, 102, 111], [111, 113, 109, 112]])];
  const run = (type: string, inputs?: Record<string, number>) => {
    const def = INDICATOR_BY_TYPE.get(type)!;
    return def.compute(c, newInstance(type, inputs)!.inputs, { interval: "5", up: "g", down: "r" }).plots;
  };
  it("previous day OHLC and CPR use yesterday's range", () => {
    const [pdh, pdl, pdc] = run("pdhl");
    expect(pdh[0]).toBeNull();
    expect([pdh[2], pdl[2], pdc[2]]).toEqual([110, 95, 102]);
    const [tc, p, bc] = run("cpr");
    expect(p[3]).toBeCloseTo((110 + 95 + 102) / 3, 6);
    // TC < BC here (close below the midpoint), so the plots swap: TC is always the upper line.
    expect(tc[3]).toBeCloseTo(102.5, 6);
    expect(bc[3]).toBeCloseTo(2 * p[3]! - 102.5, 6);
  });
  it("ORB waits for the range to finish, then holds it all session", () => {
    const [hi, lo] = run("orb", { minutes: 10 });
    expect(hi[2]).toBeNull();
    expect(hi[4]).toBe(112);
    expect(lo[4]).toBe(101);
  });
  it("every extra indicator returns one value per candle", () => {
    for (const t of ["hma", "dema", "tema", "vwapbands", "chandelier", "atrstop", "hl52", "envelopes", "stochrsi", "roc", "mom", "cmf", "chop", "hv", "ao", "bop", "trix", "aroon", "rvol", "squeeze"]) {
      expect(INDICATOR_BY_TYPE.has(t)).toBe(true);
      for (const plot of run(t)) expect(plot).toHaveLength(c.length);
    }
  });
});

import { levelPrice } from "../pages/Trade";

describe("ticket level input", () => {
  it("turns price / points / % into an absolute level on the right side", () => {
    expect(levelPrice(95, "price", 100, "BUY", "sl")).toBe(95);
    expect(levelPrice(2, "pct", 100, "BUY", "sl")).toBe(98);
    expect(levelPrice(2, "pct", 100, "BUY", "target")).toBe(102);
    expect(levelPrice(5, "pts", 100, "SELL", "sl")).toBe(105);
    expect(levelPrice(5, "pts", 100, "SELL", "target")).toBe(95);
    expect(levelPrice(1, "pct", 333.33, "BUY", "sl", 0.05)).toBe(330);
    expect(levelPrice(0, "pct", 100, "BUY", "sl")).toBeNull();
    expect(levelPrice(1, "pct", null, "BUY", "sl")).toBeNull();
  });
});
