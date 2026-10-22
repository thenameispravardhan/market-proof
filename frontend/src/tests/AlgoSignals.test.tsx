// Algo Lab v3, half A: an operand on another symbol, and the tries counter behind the Deflated Sharpe.
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
  indicators: [{ name: "PRICE", params: {}, outputs: ["value"], group: "price" }, { name: "EMA", params: { period: 20 }, outputs: ["value"], group: "trend" },
    { name: "VIX", params: {}, outputs: ["value"], group: "volatility" }, { name: "ROC", params: { period: 10 }, outputs: ["value"], group: "momentum" }],
  operators: [">", "<", "crosses_above", "crosses_below"], timeframes: [1, 5, 15, 60, 1440], cond_timeframes: [15, 60, 1440],
  sources: ["close"], metrics: ["net_pnl"], sizing: ["qty", "lots", "amount", "pct_equity", "risk", "risk_pct"], defaults: DEFAULTS, max_range_days: 1098,
  no_symbol: ["VIX", "DTE", "TIME"],
};
const BT = {
  stats: { equity: [], daily: [], monthly: {}, weekday: {}, skipped: {}, deflated_sharpe: { dsr: 0.42, n_trials: 1, sharpe_per_period: 0.1, benchmark_sharpe: 0 } },
  per_symbol: {}, monte_carlo: null, by_reason: {}, trades: [], trades_total: 0, notes: [], elapsed_s: 0.1,
  chart: { symbol: "NSE:SBIN-EQ", candles: [], trades: [] }, spec: DEFAULTS,
};
const json = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }));
let calls: { url: string; body: any }[] = [];
let tried = 0;

function route(url: string, init?: RequestInit) {
  const body = init?.body ? JSON.parse(String(init.body)) : undefined;
  calls.push({ url, body });
  if (url.includes("/api/algo/indicators")) return json(CATALOG);
  if (url.includes("/api/algo/strategies")) return json({ strategies: [] });
  if (url.includes("/api/algo/status")) return json({ running: true, last_tick: null, last_sync: null, events: [], open: [] });
  if (url.includes("/api/algo/trades")) return json({ trades: [] });
  if (url.includes("/api/algo/instrument")) return json({ symbol: "NSE:SBIN-EQ", name: "SBIN", fno: false, lot: null, weekly: false, futures: [], coverage: null });
  if (url.includes("/api/algo/backtest")) { tried = (body.trials ?? 0) + 1; return json({ ...BT, trials: tried }); }
  return json({});
}
const backtests = () => calls.filter((c) => c.url.includes("/api/algo/backtest"));

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><Algo /></QueryClientProvider>);
}

describe("Algo Lab signals", () => {
  let originalFetch: typeof fetch;
  beforeEach(() => {
    calls = []; tried = 0;
    localStorage.clear(); sessionStorage.clear();
    originalFetch = globalThis.fetch;
    globalThis.fetch = vi.fn((u: RequestInfo | URL, init?: RequestInit) => route(String(u), init)) as typeof fetch;
  });
  afterEach(() => { globalThis.fetch = originalFetch; vi.restoreAllMocks(); });

  it("puts another symbol on an operand, and takes it off again", async () => {
    mount();
    await screen.findByDisplayValue(/EMA 9\/21/);
    const box = screen.getAllByTestId("operand-symbol")[0];
    fireEvent.change(box, { target: { value: "nse:nifty50-index" } });
    fireEvent.click(screen.getByText("▶ Run backtest"));
    await waitFor(() => expect(backtests().length).toBe(1));
    expect(backtests()[0].body.spec.entry_long.conditions[0].left).toMatchObject({ ind: "EMA", symbol: "NSE:NIFTY50-INDEX" });
    fireEvent.change(box, { target: { value: "" } });
    fireEvent.click(screen.getByText("▶ Run backtest"));
    await waitFor(() => expect(backtests().length).toBe(2));
    expect("symbol" in backtests()[1].body.spec.entry_long.conditions[0].left).toBe(false);
  });

  it("counts every backtest of an unsaved idea and keeps the count with the draft", async () => {
    mount();
    await screen.findByDisplayValue(/EMA 9\/21/);
    fireEvent.click(screen.getByText("▶ Run backtest"));
    expect((await screen.findByTestId("algo-trials")).textContent).toBe("1 try so far");
    expect(backtests()[0].body.trials).toBe(0);
    expect(screen.getByText("DSR · 1 try")).toBeTruthy();
    fireEvent.click(screen.getByText("▶ Run backtest"));
    await waitFor(() => expect(screen.getByTestId("algo-trials").textContent).toBe("2 tries so far"));
    expect(backtests()[1].body.trials).toBe(1);
    expect(JSON.parse(localStorage.getItem("algo.draft.v2")!).trials).toBe(2);
  });

  it("ranks same-candle entries once there are several symbols", async () => {
    mount();
    await screen.findByDisplayValue(/EMA 9\/21/);
    expect(screen.queryByTestId("algo-rank")).toBeNull();          // one symbol: nothing to rank
    fireEvent.click(screen.getByText("+ NIFTY"));
    fireEvent.click(await screen.findByText(/on the same candle, enter only the best/));
    fireEvent.click(screen.getByText("▶ Run backtest"));
    await waitFor(() => expect(backtests().length).toBe(1));
    expect(backtests()[0].body.spec.rank).toMatchObject({ by: { ind: "ROC", params: { period: 20 } }, order: "desc", top: 1 });
    fireEvent.click(screen.getByText(/on the same candle, enter only the best/));
    fireEvent.click(screen.getByText("▶ Run backtest"));
    await waitFor(() => expect(backtests().length).toBe(2));
    expect("rank" in backtests()[1].body.spec).toBe(false);          // off = left out
  });
});

describe("Live vs backtest drift", () => {
  let originalFetch: typeof fetch;
  afterEach(() => { globalThis.fetch = originalFetch; vi.restoreAllMocks(); });

  it("lines up live trades with the backtest's and lists the misses", async () => {
    const { DriftPanel } = await import("../components/algo/DriftPanel");
    const row = (entry_t: number, extra = {}) => ({ symbol: "NSE:SBIN-EQ", side: "BUY", entry_t, entry: 800, exit_t: entry_t + 600, exit: 805, reason: "TARGET", net: 40, ...extra });
    const drift = { strategy_id: 7, days: 10, versions: [{ version: 3, from: 0, to: 0, live_net: 40, bt_net: 85, notes: [],
      matched: [{ live: row(1790000005, { entry: 800.8 }), bt: row(1790000000), entry_gap_pct: 0.1, net_gap: -5, same_reason: true }],
      live_only: [row(1790090000, { status: "rejected", reason: null, net: null, exit: null, exit_t: null })], bt_only: [row(1790190000, { net: 45 })] }] };
    const urls: string[] = [];
    originalFetch = globalThis.fetch;
    globalThis.fetch = vi.fn((u: RequestInfo | URL) => { urls.push(String(u)); return json(drift); }) as typeof fetch;
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={qc}><DriftPanel strategies={[{ id: 7, name: "Live one" }]} /></QueryClientProvider>);
    fireEvent.click(screen.getByText("Compare"));
    const v = await screen.findByTestId("algo-drift-v3");
    expect(urls[0]).toContain("/api/algo/strategies/7/drift?days=10");
    expect(v.textContent).toContain("1 matched · 1 only live · 1 only in the backtest");
    expect(v.textContent).toContain("0.10%");
    expect(v.textContent).toContain("rejected");
  });
});
