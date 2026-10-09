// ChartPanel history paging — the first page of a short interval can be
// empty (a weekend, a Monday before the open); the chart must page back to
// the last session instead of reporting "no chart data".

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import ChartPanel from "../components/trade/ChartPanel";

vi.mock("lightweight-charts", () => {
  const stubSeries = () => ({
    setData: vi.fn(),
    update: vi.fn(),
    applyOptions: vi.fn(),
    createPriceLine: vi.fn(() => ({ applyOptions: vi.fn() })),
    removePriceLine: vi.fn(),
    priceScale: vi.fn(() => ({ applyOptions: vi.fn() })),
    coordinateToPrice: vi.fn(() => 100),
    priceToCoordinate: vi.fn(() => 100),
    attachPrimitive: vi.fn(),
    detachPrimitive: vi.fn(),
  });
  return {
    createChart: vi.fn(() => ({
      addSeries: vi.fn(() => stubSeries()),
      removeSeries: vi.fn(),
      panes: vi.fn(() => []),
      removePane: vi.fn(),
      paneSize: vi.fn(() => ({ width: 800, height: 400 })),
      takeScreenshot: vi.fn(() => document.createElement("canvas")),
      priceScale: vi.fn(() => ({ applyOptions: vi.fn() })),
      timeScale: vi.fn(() => ({
        applyOptions: vi.fn(),
        fitContent: vi.fn(),
        scrollToRealTime: vi.fn(),
        coordinateToLogical: vi.fn(() => 0),
        logicalToCoordinate: vi.fn(() => 100),
        getVisibleLogicalRange: vi.fn(() => ({ from: 0, to: 60 })),
        setVisibleLogicalRange: vi.fn(),
        subscribeVisibleLogicalRangeChange: vi.fn(),
        unsubscribeVisibleLogicalRangeChange: vi.fn(),
      })),
      subscribeCrosshairMove: vi.fn(),
      unsubscribeCrosshairMove: vi.fn(),
      subscribeClick: vi.fn(),
      unsubscribeClick: vi.fn(),
      applyOptions: vi.fn(),
      remove: vi.fn(),
    })),
    ColorType: { Solid: "solid" },
    CrosshairMode: { Normal: 0, Magnet: 1 },
    LineStyle: { Solid: 0, Dotted: 1, Dashed: 2, LargeDashed: 3, SparseDotted: 4 },
    PriceScaleMode: { Normal: 0, Logarithmic: 1, Percentage: 2, IndexedTo100: 3 },
    CandlestickSeries: {},
    BarSeries: {},
    LineSeries: {},
    AreaSeries: {},
    BaselineSeries: {},
    HistogramSeries: {},
  };
});


const IST = 19800;
// Sunday 2026-10-04 12:00 IST; the last session was Friday 2026-10-02.
const NOW = Date.UTC(2026, 9, 4, 12, 0) / 1000 - IST;
const FRIDAY_0915 = Date.UTC(2026, 9, 2, 9, 15) / 1000 - IST;

let calls: { url: string; from: number; to: number }[] = [];

beforeEach(() => {
  calls = [];
  localStorage.clear();
  localStorage.setItem("chart:prefs", JSON.stringify({ interval: "5S" }));
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW * 1000);
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.includes("/api/market/history")) {
      const q = new URL(url, "http://x").searchParams;
      const from = Number(q.get("from"));
      const to = Number(q.get("to"));
      calls.push({ url, from, to });
      const candles = from <= FRIDAY_0915 && FRIDAY_0915 <= to
        ? Array.from({ length: 120 }, (_, i) => [FRIDAY_0915 + i * 5, 100, 101, 99, 100.5, 10])
        : [];
      return new Response(JSON.stringify({ ok: true, candles, reason: null }), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    return new Response(JSON.stringify([]), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as unknown as typeof fetch;
});

afterEach(() => {
  vi.useRealTimers();
});

describe("chart history paging", () => {
  it("pages back past a weekend instead of showing an empty chart", async () => {
    render(<ChartPanel symbol="NSE:SBIN-EQ" shortName="SBIN" />);
    // the first page (Saturday + Sunday) is empty; the next one reaches Friday
    await waitFor(() => expect(calls.some((c) => c.from <= FRIDAY_0915 && FRIDAY_0915 <= c.to)).toBe(true));
    await waitFor(() => expect(screen.queryByText("loading chart…")).toBeNull());
    expect(screen.queryByTestId("chart-empty")).toBeNull();
    // each page continues where the previous one ended (no re-fetch of the same days)
    for (let i = 1; i < calls.length; i++) expect(calls[i].to).toBeLessThan(calls[i - 1].from);
  });

  it("asks the trade book for fills only when drawing execution marks", async () => {
    render(<ChartPanel symbol="NSE:SBIN-EQ" shortName="SBIN" />);
    await waitFor(() => {
      const urls = (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls.map((c) => String(c[0]));
      expect(urls.some((u) => u.startsWith("/api/trades?status=filled"))).toBe(true);
    });
  });
});
