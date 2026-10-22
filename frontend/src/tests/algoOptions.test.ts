import { describe, expect, it } from "vitest";
import { DEFAULT_ROLL, withReentries, withRoll, type RollCfg } from "../components/algo/AdjustEditors";
import { slippageVerdict } from "../components/algo/TcaPanel";

type TestLeg = { right: string; action: string; strike: string; delta: number; lots: number; reentries?: number; roll?: RollCfg };
const leg: TestLeg = { right: "CE", action: "SELL", strike: "DELTA", delta: 0.2, lots: 1 };

describe("option adjustment editors", () => {
  it("leaves re-entries out of the leg when off and clamps them to 0-5", () => {
    expect(withReentries(leg, 2)).toEqual({ ...leg, reentries: 2 });
    expect(withReentries({ ...leg, reentries: 2 }, 0)).toEqual(leg);
    expect(withReentries({ ...leg, reentries: 2 }, null)).toEqual(leg);
    expect(withReentries(leg, 9).reentries).toBe(5);
  });

  it("keeps a roll valid: 0 < to < above < 1, max 1-5, and removes it with null", () => {
    expect(withRoll(leg, {}).roll).toEqual(DEFAULT_ROLL);
    expect(withRoll(leg, { above: 0.3, to: 0.5 }).roll).toEqual({ above: 0.3, to: 0.29, max: 1 });
    expect(withRoll(leg, { above: 1.4, max: 12 }).roll).toEqual({ above: 0.99, to: 0.25, max: 5 });
    expect(withRoll({ ...leg, roll: DEFAULT_ROLL }, null)).toEqual(leg);
  });
});

describe("TCA", () => {
  it("flags measured slippage above what the backtest assumes", () => {
    expect(slippageVerdict(0.05, 0.02)).toBe("above");
    expect(slippageVerdict(0.02, 0.02)).toBe("within");
    expect(slippageVerdict(-0.01, 0.02)).toBe("within");
  });
});
