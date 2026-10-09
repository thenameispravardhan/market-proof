// Algo Lab workflow tests — unsaved-change tracking, stale backtest notice,
// out-of-sample rounding and the LIVE switch-on confirmation.
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import Algo from "../pages/Algo";

vi.mock("lightweight-charts", () => ({
  createChart: vi.fn(() => ({ addSeries: vi.fn(() => ({ setData: vi.fn() })), panes: vi.fn(() => []), timeScale: vi.fn(() => ({ setVisibleLogicalRange: vi.fn() })), remove: vi.fn() })),
  createSeriesMarkers: vi.fn(),
  ColorType: { Solid: "solid" },
  CandlestickSeries: {}, HistogramSeries: {}, LineSeries: {},
}));

const DEFAULTS = {
  symbols: [], timeframe: 5, bars: { type: "time", per_day: 50 }, direction: "long",
  entry_long: { logic: "AND", conditions: [] }, exit_long: null, entry_short: { logic: "AND", conditions: [] }, exit_short: null,
  instrument: { type: "equity", expiry: "current", expiry_kind: "weekly", legs_long: [], legs_short: [], levels_on: "instrument", iv: { source: "auto", value: 15 } },
  stop_loss: { type: "pct", value: 1 }, target: { type: "pct", value: 2 }, trailing: null, breakeven: null,
  mtm: { stop: null, target: null, trail_start: null, trail_gap: null }, daily: { max_loss: null, max_profit: null },
  session: { start: "09:20", end: "15:00", square_off: "15:15" }, entry_order: { type: "market", offset_pct: 0.1, valid_bars: 3 },
  max_trades_per_day: 3, cooldown_bars: 0, max_bars: null, sizing: { mode: "qty", value: 1 },
  portfolio: { capital: 100000, leverage: 1, max_positions: 10, compounding: true, max_position_pct: null },
  costs: { slippage_pct: 0.02, charges: true },
};
const CATALOG = {
  indicators: [{ name: "PRICE", params: {}, outputs: ["value"], group: "price" }, { name: "EMA", params: { period: 20 }, outputs: ["value"], group: "trend" }],
  operators: [">", "<", "crosses_above", "crosses_below"], timeframes: [1, 5, 15, 60, 1440], cond_timeframes: [15, 60, 1440],
  sources: ["close"], metrics: ["net_pnl"], sizing: ["qty", "lots", "amount", "pct_equity", "risk", "risk_pct"], defaults: DEFAULTS, max_range_days: 1098,
};
const LIVE_STRATEGY = {
  id: 7, name: "Live one", spec: { ...DEFAULTS, symbols: ["NSE:SBIN-EQ"] }, enabled: false, mode: "live", account_id: 1,
  closed_trades: 0, realized_pnl: 0, open_positions: 0, version: 1, versions: 1,
};
const BT = {
  stats: { equity: [], daily: [], monthly: {}, weekday: {}, skipped: {} }, per_symbol: {}, monte_carlo: null, by_reason: {},
  trades: [], trades_total: 0, notes: [], elapsed_s: 0.1, chart: { symbol: "NSE:SBIN-EQ", candles: [], trades: [] }, spec: DEFAULTS,
};

const json = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }));
let calls: { url: string; init?: RequestInit }[] = [];

function route(url: string, init?: RequestInit) {
  calls.push({ url, init });
  if (url.includes("/api/algo/indicators")) return json(CATALOG);
  if (url.includes("/api/algo/strategies")) return json(init?.method === "PUT" ? LIVE_STRATEGY : { strategies: [LIVE_STRATEGY] });
  if (url.includes("/api/algo/status")) return json({ running: true, last_tick: null, last_sync: null, events: [], open: [] });
  if (url.includes("/api/algo/trades")) return json({ trades: [] });
  if (url.includes("/api/broker-accounts")) return json({ accounts: [] });
  if (url.includes("/api/algo/instrument")) return json({ symbol: "NSE:SBIN-EQ", name: "SBIN", fno: false, lot: null, weekly: false, futures: [], coverage: null });
  if (url.includes("/api/algo/backtest")) return json(BT);
  if (url.includes("/api/search/symbols")) return json({ hits: [] });
  return json({});
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><Algo /></QueryClientProvider>);
}

describe("Algo Lab", () => {
  let originalFetch: typeof fetch;
  beforeEach(() => {
    calls = [];
    localStorage.clear();
    sessionStorage.clear();
    originalFetch = globalThis.fetch;
    globalThis.fetch = vi.fn((u: RequestInfo | URL, init?: RequestInit) => route(String(u), init)) as typeof fetch;
  });
  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("asks before a template replaces unsaved edits, and not before", async () => {
    mount();
    const nameBox = await screen.findByDisplayValue(/EMA 9\/21/);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const template = screen.getByDisplayValue("— load a template —");
    fireEvent.change(template, { target: { value: "2" } });   // untouched draft: no question
    expect(confirm).not.toHaveBeenCalled();
    expect(await screen.findByDisplayValue(/Opening range breakout/)).toBeTruthy();
    fireEvent.change(screen.getByDisplayValue(/Opening range breakout/), { target: { value: "My tweak" } });
    fireEvent.change(template, { target: { value: "0" } });
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(screen.getByDisplayValue("My tweak")).toBeTruthy();   // cancelled: the edit stays
    expect(nameBox).toBeTruthy();
  });

  it("flags backtest results as stale after a change and rounds a small out-of-sample % up to 5", async () => {
    mount();
    await screen.findByDisplayValue(/EMA 9\/21/);
    const oos = screen.getByTitle(/split the result/).querySelector("input")!;
    fireEvent.change(oos, { target: { value: "3" } });
    fireEvent.click(screen.getByText("▶ Run backtest"));
    await waitFor(() => expect(calls.some((c) => c.url.includes("/api/algo/backtest"))).toBe(true));
    const body = JSON.parse(String(calls.find((c) => c.url.includes("/api/algo/backtest"))!.init!.body));
    expect(body.oos_pct).toBe(5);
    await screen.findByText("Performance", { exact: false });
    expect(screen.queryByTestId("algo-stale")).toBeNull();
    fireEvent.change(oos, { target: { value: "10" } });
    expect(await screen.findByTestId("algo-stale")).toBeTruthy();
  });

  it("confirms before switching on a LIVE automation", async () => {
    mount();
    await screen.findByDisplayValue(/EMA 9\/21/);
    fireEvent.click(screen.getByText("Automations"));
    const sw = await screen.findByRole("switch");
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    fireEvent.click(sw);
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("REAL MONEY"));
    expect(calls.some((c) => c.init?.method === "PUT")).toBe(false);
    confirm.mockReturnValue(true);
    fireEvent.click(sw);
    await waitFor(() => expect(calls.some((c) => c.init?.method === "PUT" && String(c.init.body).includes('"enabled":true'))).toBe(true));
  });
});
