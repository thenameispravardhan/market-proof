// Cap-tier lookup. The expectations below are read straight off the AMFI
// categorisation workbook (data/amfi/) — the same source the generated
// marketCapData.ts comes from. `python scripts/gen_market_cap.py --check`
// verifies the generated data still matches that workbook; these tests
// verify the lookup on top of it.

import { describe, it, expect } from "vitest";
import { capTier } from "../lib/marketCap";
import { LARGE, MID } from "../lib/marketCapData";

describe("capTier", () => {
  it("ships exactly the SEBI top-250", () => {
    expect(LARGE.size).toBe(100);
    expect(MID.size).toBe(150);
    expect([...LARGE].filter((s) => MID.has(s))).toEqual([]);
  });

  it("classifies by AMFI categorisation", () => {
    expect(capTier("RELIANCE")).toBe("Large");
    expect(capTier("BHEL")).toBe("Large"); // large as of Jun-2026, not mid
    expect(capTier("HEROMOTOCO")).toBe("Mid");
    expect(capTier("GODREJIND")).toBe("Mid"); // rank 250, the boundary
    expect(capTier("JBCHEPHARM")).toBe("Small"); // rank 251+
    expect(capTier("MENONBE")).toBe("Small"); // unlisted in AMFI → Small
  });

  it("normalises broker symbol forms", () => {
    expect(capTier("NSE:SBIN-EQ")).toBe("Large");
    expect(capTier(" nse:lupin-eq ")).toBe("Mid");
    expect(capTier("BSE:RELIANCE-A")).toBe("Large");
    expect(capTier("NSE:BAJAJ-AUTO-EQ")).toBe("Large"); // hyphen in the name itself
  });

  it("survives empty / missing symbols", () => {
    expect(capTier(null)).toBe("Small");
    expect(capTier("")).toBe("Small");
  });
});
