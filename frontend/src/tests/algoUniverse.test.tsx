// Large universes in the Algo Lab: the screen status on Automations.
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { ScreenLine } from "../pages/Algo";

vi.mock("lightweight-charts", () => ({ createChart: vi.fn(), ColorType: {}, CandlestickSeries: {}, HistogramSeries: {}, LineSeries: {} }));

describe("ScreenLine", () => {
  it("shows the day's data budget and each screened automation's last candle", () => {
    render(<ScreenLine nameOf={(id) => `strategy ${id}`} sc={{
      warm: 1000, warming: false, warm_errors: 0,
      budget: { plan: "standard", per_min: 50, per_day: 5000, reserve_pct: 20, used_today: 1234, bulk_left_today: 2766, last_minute: 21, refused_today: 0 },
      strategies: { "3": { bar: 1767240000, symbols: 1000, warm: 1000, screened: 1000, fired: 31, sure: 12, unsure: 2, held: 5, candidates: 38,
                           skipped: 0, full: 4, deferred: 0, quote_calls: 20, ms: 850 } },
    }} />);
    expect(screen.getByTestId("algo-screen-status").textContent).toMatch(/1,234 of 5,000 calls today · 21\/50 in the last minute · automations have 2,766 left/);
    expect(screen.getByTestId("algo-screen-3").textContent).toMatch(/strategy 3: last candle .* 1000 of 1000 screened, 31 may have fired \(12 clearly\), 38 checked on exact candles, 4 entries not checked \(max positions open\), 2 re-syncing · 20 quote calls, 850 ms/);
  });
});
