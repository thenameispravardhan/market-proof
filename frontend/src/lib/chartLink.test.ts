import { describe, expect, it } from "vitest";
import { chartHref, chartLinkParams } from "./chartLink";

describe("chartHref", () => {
  it("treats a naive API time as UTC, maps bare names to NSE EQ, defaults to 5m", () => {
    const href = chartHref("sbin", "2026-10-12T04:00:00");
    const q = new URLSearchParams(href.split("?")[1]);
    expect(q.get("at")).toBe(String(Date.UTC(2026, 9, 12, 4) / 1000));
    expect(q.get("symbol")).toBe("NSE:SBIN-EQ");
    expect(q.get("iv")).toBe("5");
    window.location.hash = href.split("#")[1];
    expect(chartLinkParams()).toMatchObject({ hit: { symbol: "NSE:SBIN-EQ", short_name: "SBIN" }, iv: "5", at: Date.UTC(2026, 9, 12, 4) / 1000 });
  });
  it("omits at without a time; popout links are not Trade links", () => {
    expect(chartHref("NSE:SBIN-EQ", null)).not.toContain("at=");
    window.location.hash = "/trade?popout=1&symbol=NSE:SBIN-EQ";
    expect(chartLinkParams()).toBeNull();
  });
});
