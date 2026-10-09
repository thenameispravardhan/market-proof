import { describe, expect, it } from "vitest";
import { levelProblem, snapToTick } from "../components/trade/levelDrag";

describe("chart stop-loss / target drag", () => {
  it("snaps a dragged price to the instrument tick", () => {
    expect(snapToTick(123.47, 0.05)).toBe(123.45);
    expect(snapToTick(123.48, 0.05)).toBe(123.5);
    expect(snapToTick(99.996, 0.01)).toBe(100);
    expect(snapToTick(1501.3, 1)).toBe(1501);
    expect(snapToTick(10.02, 0)).toBe(10); // unknown tick falls back to 0.05
  });

  it("refuses levels on the wrong side of the market", () => {
    expect(levelProblem("sl", 101, 100, true)).toMatch(/Stop-loss must be below/);
    expect(levelProblem("tp", 99, 100, true)).toMatch(/Target must be above/);
    expect(levelProblem("sl", 99, 100, false)).toMatch(/Stop-loss must be above/);
    expect(levelProblem("tp", 101, 100, false)).toMatch(/Target must be below/);
    expect(levelProblem("sl", 95, 100, true)).toBeNull();
    expect(levelProblem("tp", 95, 100, false)).toBeNull();
  });

  it("refuses a level at or below zero, even with no LTP", () => {
    expect(levelProblem("sl", 0, null, true)).toMatch(/above zero/);
    expect(levelProblem("sl", 90, null, true)).toBeNull();
  });
});
