import { describe, expect, it } from "vitest";
import { SIZING_PRESETS, sizingSummary } from "../pages/Algo";

const base = { portfolio: { capital: 100000, leverage: 1, max_positions: 5, compounding: true, max_position_pct: 40 }, daily: { max_loss: 3000, max_profit: null } };

describe("sizing summary", () => {
  it("explains risk sizing, caps and the daily stop in one sentence", () => {
    const s = sizingSummary({ ...base, sizing: { mode: "risk_pct", value: 1 } }, null);
    expect(s).toContain("stop loses 1% of equity (₹1,000 to start)");
    expect(s).toContain("Up to 5 open");
    expect(s).toContain("40% (₹40,000)");
    expect(s).toContain("−₹3,000");
  });
  it("shows lots with their quantity", () => {
    expect(sizingSummary({ ...base, sizing: { mode: "lots", value: 2 } }, 75)).toContain("2 lots (150 qty)");
  });
  it("presets get stricter from aggressive to conservative", () => {
    const [c, b, a] = SIZING_PRESETS;
    expect(c.riskPct < b.riskPct && b.riskPct < a.riskPct).toBe(true);
    expect(c.maxPositions < a.maxPositions).toBe(true);
  });
});
