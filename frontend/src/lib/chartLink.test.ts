import { describe, expect, it } from "vitest";
import { chartHref } from "./chartLink";

describe("chartHref", () => {
  it("treats a naive API time as UTC and defaults to 5m", () => {
    const q = new URLSearchParams(chartHref("NSE:SBIN-EQ", "2026-10-12T04:00:00").split("?")[1]);
    expect(q.get("at")).toBe(String(Date.UTC(2026, 9, 12, 4) / 1000));
    expect(q.get("iv")).toBe("5");
    expect(q.get("popout")).toBe("1");
  });
  it("omits at without a time", () => {
    expect(chartHref("SBIN", null)).not.toContain("at=");
  });
});
