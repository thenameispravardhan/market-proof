// Data tab — research recording (B0): the option-chain and short-list switches send
// their own config blocks, and the disk projection says whether it all fits.
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
  indicators: [{ name: "PRICE", params: {}, outputs: ["value"], group: "price" }], operators: [">", "<"], timeframes: [1, 5, 15, 60, 1440],
  cond_timeframes: [15, 60, 1440], sources: ["close"], metrics: ["net_pnl"], sizing: ["qty"], defaults: DEFAULTS, max_range_days: 1098,
};
const UNIVERSE = {
  enabled: true, lists: ["nifty500"], stocks: true, index_futures: true, stock_futures: true, next_futures: "roll", index_options: true,
  index_strikes: 15, index_next_strikes: 10, stock_options: true, stock_strikes: 5, stock_next_expiry: "roll", roll_days: 5,
};
const CAPACITY = {
  per_day_mb: { full: 180, raw: 40, long: 4, long_raw: 25, today_so_far: 60 },
  days: { full: 90, raw: 5, long: 365, long_raw: 120 },
  tiers_gb: { full: 16.2, raw: 0.2, long: 1.1, long_raw: 2.9 },
  projected_gb: 20.5, now_gb: 9.1, disk_total_gb: 58.1, free_gb: 31.4, min_free_gb: 5, room_gb: 35.5,
  fits: true, estimate: ["long", "long_raw"], note: null,
};

let config = {
  enabled: true, symbols: [], retention_days: 90, raw_days: 5, budget: 4500, min_free_gb: 5, universe: UNIVERSE,
  chains: { enabled: false, underlyings: [], strategies: true, strikes: 10, expiries: 2, interval_s: 60 },
  long: { enabled: false, symbols: [], strategies: true, days: 365, raw_days: 120, raw_strikes: 3 },
};
const status = () => ({
  enabled: true, config, config_symbols: [], strategy_symbols: ["NSE:NIFTY50-INDEX"],
  collector: { alive: true, plan: { used: 900, budget: 4500, classes: { focus: 2, chain: 40 }, chains: { names: ["NIFTY"], keys: 42 } },
    chains: { keys: 42, quoted: 41, snapshots: 120, last: null, interval_s: 60 } },
  days: { count: 30, first: "20260901", last: "20261021", retention: 90, long: { count: 0, first: null } },
  disk_mb: 9100, capacity: CAPACITY, lists: { nifty500: "NIFTY 500" }, keys_today: 0, top: [],
});

const json = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }));
let puts: unknown[] = [];

function route(url: string, init?: RequestInit) {
  if (url.includes("/api/algo/ticks/config") && init?.method === "PUT") {
    const patch = JSON.parse(String(init.body));
    puts.push(patch);
    config = { ...config, ...(patch.chains ? { chains: { ...config.chains, ...patch.chains } } : {}),
      ...(patch.long ? { long: { ...config.long, ...patch.long } } : {}) };
    return json(status());
  }
  if (url.includes("/api/algo/ticks/status")) return json(status());
  if (url.includes("/api/algo/indicators")) return json(CATALOG);
  if (url.includes("/api/algo/strategies")) return json({ strategies: [] });
  if (url.includes("/api/algo/status")) return json({ running: true, last_tick: null, last_sync: null, events: [], open: [] });
  if (url.includes("/api/algo/trades")) return json({ trades: [] });
  if (url.includes("/api/broker-accounts")) return json({ accounts: [] });
  return json({});
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><Algo /></QueryClientProvider>);
}

describe("Data tab: research recording", () => {
  let originalFetch: typeof fetch;
  beforeEach(() => {
    puts = [];
    config = { ...config, chains: { ...config.chains, enabled: false, underlyings: [] }, long: { ...config.long, enabled: false } };
    localStorage.clear();
    originalFetch = globalThis.fetch;
    globalThis.fetch = vi.fn((u: RequestInfo | URL, init?: RequestInit) => route(String(u), init)) as typeof fetch;
  });
  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("shows both switches off by default and the disk projection", async () => {
    mount();
    fireEvent.click(await screen.findByText("Data"));
    const chains = (await screen.findByTestId("flow-chains-on")) as HTMLInputElement;
    expect(chains.checked).toBe(false);
    expect((screen.getByTestId("flow-long-on") as HTMLInputElement).checked).toBe(false);
    expect(screen.queryByTestId("flow-chains-state")).toBeNull();
    const cap = screen.getByTestId("flow-capacity");
    expect(cap.textContent).toContain("20.5 GB");
    expect(cap.textContent).toContain("fits");
  });

  it("switches chains on, sets their underlyings, and turns on the short list", async () => {
    mount();
    fireEvent.click(await screen.findByText("Data"));
    fireEvent.click(await screen.findByTestId("flow-chains-on"));
    await waitFor(() => expect(puts).toContainEqual({ chains: { enabled: true } }));
    const names = screen.getByTestId("flow-chains-names");
    fireEvent.change(names, { target: { value: "nifty, banknifty,, nifty" } });
    fireEvent.blur(names);
    await waitFor(() => expect(puts).toContainEqual({ chains: { underlyings: ["NIFTY", "BANKNIFTY"] } }));
    expect((await screen.findByTestId("flow-chains-state")).textContent).toContain("NIFTY · 42 contracts");
    fireEvent.click(screen.getByTestId("flow-long-on"));
    await waitFor(() => expect(puts).toContainEqual({ long: { enabled: true } }));
    const days = screen.getByTestId("flow-long-days");
    fireEvent.change(days, { target: { value: "500" } });
    fireEvent.blur(days);
    await waitFor(() => expect(puts).toContainEqual({ long: { days: 500 } }));
  });
});
