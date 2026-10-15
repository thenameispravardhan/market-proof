import { afterEach, describe, expect, it, vi } from "vitest";
import { chartHref, chartLinkParams, resolveBareHit } from "./chartLink";

describe("chartHref", () => {
  it("treats a naive API time as UTC, keeps bare names bare, defaults to 5m", () => {
    const href = chartHref("sbin", "2026-10-12T04:00:00");
    const q = new URLSearchParams(href.split("?")[1]);
    expect(q.get("at")).toBe(String(Date.UTC(2026, 9, 12, 4) / 1000));
    expect(q.get("symbol")).toBe("SBIN");
    expect(q.get("iv")).toBe("5");
    window.location.hash = href.split("#")[1];
    expect(chartLinkParams()).toMatchObject({ hit: { symbol: "SBIN", short_name: "SBIN", exchange: "" }, iv: "5", at: Date.UTC(2026, 9, 12, 4) / 1000 });
  });
  it("carries the trade's side, qty, entry and exit legs", () => {
    window.location.hash = chartHref("NSE:SBIN-EQ", "2026-10-12T04:00:00", { side: "SELL", qty: 7, price: 812.5, exitPrice: 805, exitTime: "2026-10-12T05:00:00" }).split("#")[1];
    expect(chartLinkParams()).toMatchObject({
      hit: { symbol: "NSE:SBIN-EQ", exchange: "NSE" },
      entry: { side: "SELL", qty: 7, price: 812.5, at: Date.UTC(2026, 9, 12, 4) / 1000, exitPrice: 805, exitAt: Date.UTC(2026, 9, 12, 5) / 1000 },
    });
  });
  it("pairs an option chart with its underlying (Algo Lab), 15m, arrows on the option only", () => {
    const href = chartHref("NSE:NIFTY50-INDEX", "2026-10-15T09:15:03", null, {
      iv: "15", pair: { symbol: "NSE:NIFTY26O1923000PE", trade: { side: "BUY", qty: 65, price: 130.85, exitPrice: 130.75, exitTime: "2026-10-15T09:31:53" } },
    });
    window.location.hash = href.split("#")[1];
    const p = chartLinkParams()!;
    expect(p.iv).toBe("15");
    expect(p.hit.symbol).toBe("NSE:NIFTY50-INDEX");
    expect(p.entry).toBeUndefined();
    expect(p.hit2).toMatchObject({ symbol: "NSE:NIFTY26O1923000PE", lot_size: 0 });   // unknown lot: never trade it as 1
    expect(p.entry2).toMatchObject({ side: "BUY", qty: 65, price: 130.85, exitPrice: 130.75, at: Date.UTC(2026, 9, 15, 9, 15, 3) / 1000, exitAt: Date.UTC(2026, 9, 15, 9, 31, 53) / 1000 });
  });
  it("omits at without a time; popout links are not Trade links", () => {
    expect(chartHref("NSE:SBIN-EQ", null)).not.toContain("at=");
    window.location.hash = "/trade?popout=1&symbol=NSE:SBIN-EQ";
    expect(chartLinkParams()).toBeNull();
  });
});

describe("resolveBareHit", () => {
  afterEach(() => vi.unstubAllGlobals());
  const hit = (symbol: string, short_name: string, exchange: string) => ({ symbol, short_name, exchange });
  const stub = (hits: unknown[]) => vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => ({ hits }) })));

  it("prefers the exact NSE listing, then any exact one (a BSE-only scrip)", async () => {
    stub([hit("BSE:SBIN-A", "SBIN", "BSE"), hit("NSE:SBIN-EQ", "SBIN", "NSE")]);
    expect((await resolveBareHit("sbin")).symbol).toBe("NSE:SBIN-EQ");
    stub([hit("NSE:JSTLX-EQ", "JSTLX", "NSE"), hit("BSE:JSTL-A", "JSTL", "BSE")]);
    expect((await resolveBareHit("JSTL")).symbol).toBe("BSE:JSTL-A");
  });
  it("falls back to NSE EQ when the search finds nothing", async () => {
    stub([]);
    expect((await resolveBareHit("XYZ")).symbol).toBe("NSE:XYZ-EQ");
  });
});
