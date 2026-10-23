// Large universes in the Algo Lab: symbol lists, the live-capacity line and the screen status.
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CapacityLine, ScreenLine, parseSymbolList } from "../pages/Algo";

vi.mock("lightweight-charts", () => ({ createChart: vi.fn(), ColorType: {}, CandlestickSeries: {}, HistogramSeries: {}, LineSeries: {} }));

describe("parseSymbolList", () => {
  it("reads bare tickers as NSE shares and keeps exchange-qualified ones", () => {
    expect(parseSymbolList("RELIANCE, tcs\nNSE:NIFTY50-INDEX;M&M  BAJAJ-AUTO 123.5 reliance")).toEqual([
      "NSE:RELIANCE-EQ", "NSE:TCS-EQ", "NSE:NIFTY50-INDEX", "NSE:M&M-EQ", "NSE:BAJAJ-AUTO-EQ",
    ]);
  });
  it("reads the Symbol column of an NSE index file", () => {
    const csv = 'Company Name,Industry,Symbol,Series,ISIN Code\n"Reliance Industries Ltd.",Oil,RELIANCE,EQ,INE002A01018\nInfosys Ltd.,IT,INFY,EQ,INE009A01021';
    expect(parseSymbolList(csv)).toEqual(["NSE:RELIANCE-EQ", "NSE:INFY-EQ"]);
  });
});

const spec = (n: number, timeframe = 15) => ({ symbols: Array.from({ length: n }, (_, k) => `NSE:S${k}-EQ`), timeframe } as never);

function mount(body: unknown) {
  globalThis.fetch = vi.fn(() => Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }))) as typeof fetch;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><CapacityLine spec={spec(1000)} /></QueryClientProvider>);
}

describe("CapacityLine", () => {
  const original = globalThis.fetch;
  afterEach(() => { globalThis.fetch = original; });

  it("says a large universe is screened and what it costs a day", async () => {
    mount({ plan: "standard", symbols: 1000, exact_capacity: 40, mode: "screened", bulk_per_day: 4000, fits: true, used_today: 12,
            max_symbols: 1000, timeframe: 15, daily_calls: { warm: 1000, polls: 500, confirm: 1250, total: 2750 } });
    const el = await screen.findByTestId("algo-capacity");
    expect(el.textContent).toMatch(/1,000 symbols screened on batch quotes/);
    expect(el.textContent).toMatch(/~2,750 data calls a day \(1,000 warm-up, 500 quote polls, ~1,250 confirmations\) of the 4,000/);
    expect(el.className).not.toMatch(/warn/);
  });

  it("warns past the plan's daily budget with what fits", async () => {
    mount({ plan: "standard", symbols: 1000, exact_capacity: 40, mode: "screened", bulk_per_day: 4000, fits: false, used_today: 0,
            max_symbols: 640, timeframe: 5, daily_calls: { warm: 1000, polls: 1500, confirm: 3750, total: 6250 } });
    const el = await screen.findByTestId("algo-capacity");
    expect(el.textContent).toMatch(/about 640 symbols fit on 5m — fewer symbols, a longer timeframe or Fyers Prime/);
    expect(el.className).toMatch(/warn/);
  });
});

describe("ScreenLine", () => {
  it("shows the day's data budget and each screened automation's last candle", () => {
    render(<ScreenLine nameOf={(id) => `strategy ${id}`} sc={{
      warm: 1000, warming: false, warm_errors: 0,
      budget: { plan: "standard", per_min: 50, per_day: 5000, reserve_pct: 20, used_today: 1234, bulk_left_today: 2766, last_minute: 21, refused_today: 0 },
      strategies: { "3": { bar: 1767240000, symbols: 1000, warm: 1000, screened: 1000, fired: 31, unsure: 2, held: 5, candidates: 38,
                           skipped: 0, deferred: 0, quote_calls: 20, ms: 850 } },
    }} />);
    expect(screen.getByTestId("algo-screen-status").textContent).toMatch(/1,234 of 5,000 calls today · 21\/50 in the last minute · automations have 2,766 left/);
    expect(screen.getByTestId("algo-screen-3").textContent).toMatch(/strategy 3: last candle .* 1000 of 1000 screened, 31 may have fired, 38 checked on exact candles, 2 re-syncing · 20 quote calls, 850 ms/);
  });
});
