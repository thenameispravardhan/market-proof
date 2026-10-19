import { describe, expect, it } from "vitest";
import { inSession, intradayBucket, liveBucket, sessionFor } from "../components/trade/chartData";
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

  it("the ticket says why MCX, currency, crypto and indices can't be ordered", () => {
    expect(untradable("CRYPTO:BTCUSDT", "BTCUSDT")).toMatch(/Binance's public feed/);
    expect(untradable("MCX:GOLD26DECFUT", "GOLD")).toMatch(/orders aren't enabled/);
    expect(untradable("NSE:USDINR26OCTFUT", "USDINR")).toMatch(/currency/);
    expect(untradable("NSE:NIFTY50-INDEX", "NIFTY")).toMatch(/index/);
    expect(untradable("NSE:SBIN-EQ")).toBeNull();
    expect(untradable("NSE:NIFTY26OCTFUT")).toBeNull();
  });
});
