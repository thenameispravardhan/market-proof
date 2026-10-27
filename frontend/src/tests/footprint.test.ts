import { describe, expect, it } from "vitest";
import { fmtK, footprintRows, niceStep, sessionAround } from "../components/trade/customSeries";

describe("footprint rows", () => {
  it("rounds a row height up to 1 / 2 / 2.5 / 5 × 10^k", () => {
    expect(niceStep(0.03)).toBeCloseTo(0.05);
    expect(niceStep(0.13)).toBeCloseTo(0.2);
    expect(niceStep(0.21)).toBeCloseTo(0.25);
    expect(niceStep(3)).toBeCloseTo(5);
    expect(niceStep(0)).toBe(0.05);
  });
  it("sums buy / sell of every price inside a row, lowest row first", () => {
    const rows = footprintRows([[458.05, 10, 2], [458.0, 5, 1], [457.95, 0, 7], [458.2, 1, 1]], 0.1);
    expect(rows).toEqual([
      { p: 457.9, buy: 0, sell: 7 },
      { p: 458.0, buy: 15, sell: 3 },
      { p: 458.2, buy: 1, sell: 1 },
    ]);
  });
  it("formats like the broker: K / L / Cr", () => {
    expect(fmtK(487)).toBe("487");
    expect(fmtK(1190)).toBe("1.19K");
    expect(fmtK(-35440)).toBe("-35.4K");
    expect(fmtK(250000)).toBe("2.50L");
  });
  it("finds a bar's session, and moves on even when bars carry no day (the Trade page freeze)", () => {
    const days = [1, 1, 2, 2, 2, 3];
    expect(sessionAround((i) => days[i], 3, 6)).toEqual([2, 5]);
    expect(sessionAround((i) => days[i], 3, 4)).toEqual([2, 4]);
    expect(sessionAround(() => NaN, 3, 6)).toEqual([3, 4]);
  });
});
