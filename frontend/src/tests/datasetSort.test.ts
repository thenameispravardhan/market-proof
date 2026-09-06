import { describe, expect, it } from "vitest";
import { sortRows } from "../pages/Dataset";

const rows = [
  { symbol: "TCS", ret_15m_pct: 0.8, label_15m: "UP" },
  { symbol: "acme", ret_15m_pct: null, label_15m: "FLAT" },
  { symbol: "INFY", ret_15m_pct: -2.4, label_15m: "DOWN" },
  { symbol: "WIPRO", ret_15m_pct: 3.1, label_15m: null },
];

describe("sortRows", () => {
  it("returns the original array untouched when no sort is active", () => {
    expect(sortRows(rows, null)).toBe(rows);
  });

  it("sorts numerically, not as text", () => {
    const out = sortRows(rows, { key: "ret_15m_pct", dir: -1 });
    expect(out.map((r) => r.ret_15m_pct)).toEqual([3.1, 0.8, -2.4, null]);
  });

  it("puts nulls last in BOTH directions", () => {
    for (const dir of [1, -1] as const) {
      const out = sortRows(rows, { key: "ret_15m_pct", dir });
      expect(out[out.length - 1].ret_15m_pct).toBeNull();
    }
    // …including on a text column.
    const byLabel = sortRows(rows, { key: "label_15m", dir: 1 });
    expect(byLabel[byLabel.length - 1].label_15m).toBeNull();
  });

  it("compares strings case-insensitively so 'acme' does not sort after 'TCS'", () => {
    const out = sortRows(rows, { key: "symbol", dir: 1 });
    expect(out.map((r) => r.symbol)).toEqual(["acme", "INFY", "TCS", "WIPRO"]);
  });

  it("never mutates the caller's array (it is the query cache's)", () => {
    const before = [...rows];
    sortRows(rows, { key: "ret_15m_pct", dir: -1 });
    expect(rows).toEqual(before);
  });

  it("handles an unknown column as all-null rather than throwing", () => {
    expect(sortRows(rows, { key: "nope", dir: -1 })).toHaveLength(rows.length);
  });
});
