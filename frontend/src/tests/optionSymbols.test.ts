import { describe, it, expect } from "vitest";
import { expiryIso, isOptionOf, optionRoot } from "../lib/options";

const hit = (symbol: string, short_name: string) => ({ symbol, short_name });

describe("option symbol helpers", () => {
  it("maps an index to the root its options trade under", () => {
    expect(optionRoot(hit("NSE:NIFTY50-INDEX", "NIFTY50"))).toBe("NIFTY");
    expect(optionRoot(hit("NSE:NIFTYBANK-INDEX", "NIFTYBANK"))).toBe("BANKNIFTY");
    expect(optionRoot(hit("NSE:FINNIFTY-INDEX", "FINNIFTY"))).toBe("FINNIFTY");
    expect(optionRoot(hit("BSE:SENSEX-INDEX", "SENSEX"))).toBe("SENSEX");
  });

  it("keeps a stock's ticker, hyphens and ampersands included", () => {
    expect(optionRoot(hit("NSE:BAJAJ-AUTO-EQ", "BAJAJ-AUTO"))).toBe("BAJAJ-AUTO");
    expect(optionRoot(hit("NSE:M&M-EQ", "M&M"))).toBe("M&M");
  });

  it("matches only that root's options", () => {
    expect(isOptionOf("NSE:NIFTY25O1425000CE", "NSE", "NIFTY")).toBe(true);
    expect(isOptionOf("NSE:NIFTY25OCT25000PE", "NSE", "NIFTY")).toBe(true);
    expect(isOptionOf("NSE:NIFTYNXT5025OCT70000CE", "NSE", "NIFTY")).toBe(false);
    expect(isOptionOf("NSE:NIFTY25OCTFUT", "NSE", "NIFTY")).toBe(false);
    expect(isOptionOf("NSE:BANKNIFTY25OCT55000CE", "NSE", "NIFTY")).toBe(false);
  });

  it("reads Fyers and master expiry labels as ISO dates", () => {
    expect(expiryIso("27-10-2026")).toBe("2026-10-27");
    expect(expiryIso("2026-10-27")).toBe("2026-10-27");
    expect(expiryIso("Oct 27")).toBeNull();
    expect(expiryIso(null)).toBeNull();
  });
});
