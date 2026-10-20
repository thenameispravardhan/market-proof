import { describe, expect, it } from "vitest";
import { inSession, intradayBucket, liveBucket, sessionFor } from "../components/trade/chartData";
import { pointValue, setFx } from "../lib/contract";
import { levelPrice, roundToTick, untradable } from "../pages/Trade";
import { snapToTick } from "../components/trade/levelDrag";
import { livePnl } from "../components/trade/AccountPanels";

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

  it("currency and crypto trade on Paper only; MCX goes live with the Settings switch; indices never", () => {
    for (const sym of ["CRYPTO:BTCUSDT", "NSE:USDINR26OCTFUT", "NSE:EURUSD26OCTFUT"]) {
      expect(untradable(sym, sym, false, true)).toMatch(/Paper account only/);
      expect(untradable(sym, sym, true)).toBeNull();
    }
    expect(untradable("MCX:GOLD26DECFUT", "GOLD", false)).toMatch(/live MCX orders are off/);
    expect(untradable("MCX:GOLD26DECFUT", "GOLD", false, true)).toBeNull();
    expect(untradable("MCX:GOLD26DECFUT", "GOLD", true)).toBeNull();
    expect(untradable("NSE:NIFTY50-INDEX", "NIFTY", true)).toMatch(/index/);
    expect(untradable("NSE:SBIN-EQ")).toBeNull();
  });

  it("prices keep their tick's decimals: currency 0.0025, sub-cent crypto 1e-8", () => {
    expect(roundToTick(95.1226, 0.0025)).toBe(95.1225);         // was 95.12 (2 dp)
    expect(snapToTick(95.1226, 0.0025)).toBe(95.1225);          // was 95.123 (3 dp, off the grid)
    expect(roundToTick(0.0000123456, 0.00000001)).toBe(0.00001235);   // SHIB: was 0
    expect(roundToTick(2.45678, 0.0001)).toBe(2.4568);          // XRP
    expect(roundToTick(147266.4, 1)).toBe(147266);              // GOLD
    expect(roundToTick(123.47)).toBe(123.45);                    // NSE default unchanged
    expect(levelPrice(10, "pct", 0.00001234, "BUY", "sl", 0.00000001)).toBe(0.00001111);   // a SHIB stop 10% below
  });

  it("a Fyers MCX position counted in lots is valued with its multiplier", () => {
    const row = { symbol: "MCX:GOLD26DECFUT", product: "INTRADAY", net_qty: 1, avg_price: 147000, buy_qty: 1, buy_avg: 147000,
      sell_qty: 0, sell_avg: null, ltp: 147010, realized: 0, unrealized: null, pl: null };
    expect(livePnl({ ...row, multiplier: 100 }, 147010)).toBe(1000);      // Rs 10 x 1 lot of 100 x 10 g
    expect(livePnl(row, 147010)).toBe(10);                                // no multiplier: units already
  });

  it("values a crypto contract in rupees, everything else at 1", () => {
    setFx({ usd_inr: 90, units: { BTCUSDT: 0.001 } });
    expect(pointValue("CRYPTO:BTCUSDT")).toBeCloseTo(0.09);       // 0.001 BTC x Rs 90 per $1 move
    expect(pointValue("MCX:GOLD26DECFUT")).toBe(1);               // its lot is counted in 10 g units already
    expect(pointValue("NSE:SBIN-EQ")).toBe(1);
    expect(pointValue("NSE:EURUSD26OCTFUT")).toBe(0);              // cross rates not in yet: no rupee figure
    setFx({ usd_inr: 90, units: {}, cross: { EURUSD: 90, GBPUSD: 90, USDJPY: 0.6 } });
    expect(pointValue("NSE:EURUSD26OCTFUT")).toBe(90);             // $1 per EUR per point, in rupees
    expect(pointValue("NSE:USDJPY26OCT164.5CE")).toBe(0.6);        // 1 yen per USD per point
    expect(pointValue("NSE:USDINR26OCTFUT")).toBe(1);              // already rupees
    setFx(null);
  });
});
