import { describe, expect, it } from "vitest";
import { inSession, intradayBucket, liveBucket, sessionFor } from "../components/trade/chartData";
import { pointValue, setFx } from "../lib/contract";
import { untradable } from "../pages/Trade";

const IST = 19800;

describe("other markets on the chart", () => {
  it("crypto is always in session, with bars on Binance's UTC grid", () => {
    const s = sessionFor("CRYPTO:BTCUSDT");
    const sunday3am = Date.UTC(2026, 9, 18, 21, 30) / 1000 + IST;      // Sun 18 Oct 2026, 03:00 IST (chart time)
    expect(inSession(sunday3am, s)).toBe(true);
    expect(inSession(sunday3am, sessionFor("NSE:SBIN-EQ"))).toBe(false);
    const utc1900 = Date.UTC(2026, 9, 19, 19, 0) / 1000;                // a Binance 1h candle's open
    expect(intradayBucket(utc1900 + IST + 1800, 3600, s.open)).toBe(utc1900 + IST);   // 19:30 UTC is in the 19:00 bar
    expect(liveBucket({ time: utc1900 + IST, open: 1, high: 1, low: 1, close: 1, volume: 0 }, utc1900 + IST + 3600, "60", s.open))
      .toEqual({ kind: "new", time: utc1900 + IST + 3600 });
    expect(sessionFor("MCX:GOLD26DECFUT")).toEqual({ open: 540, close: 1435 });
  });

  it("MCX, currency and crypto trade on Paper only; indices never", () => {
    for (const sym of ["CRYPTO:BTCUSDT", "MCX:GOLD26DECFUT", "NSE:USDINR26OCTFUT"]) {
      expect(untradable(sym, sym, false)).toMatch(/Paper account only/);
      expect(untradable(sym, sym, true)).toBeNull();
    }
    expect(untradable("NSE:NIFTY50-INDEX", "NIFTY", true)).toMatch(/index/);
    expect(untradable("NSE:SBIN-EQ")).toBeNull();
  });

  it("values a crypto contract in rupees, everything else at 1", () => {
    setFx({ usd_inr: 90, units: { BTCUSDT: 0.001 } });
    expect(pointValue("CRYPTO:BTCUSDT")).toBeCloseTo(0.09);       // 0.001 BTC x Rs 90 per $1 move
    expect(pointValue("MCX:GOLD26DECFUT")).toBe(1);               // its lot is counted in 10 g units already
    expect(pointValue("NSE:SBIN-EQ")).toBe(1);
    setFx(null);
  });
});
