// B1 research loop: a research check from Optimise saves a strategy card; the Research tab lists them.
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
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
const CHECKS = [
  { key: "dsr", label: "Deflated Sharpe ≥ 95% after 4 tries", value: 0.61, pass: false, note: null },
  { key: "walk_forward", label: "Walk-forward unseen windows net > 0 on ≥ 10 trades", value: 1200, pass: true, note: null },
  { key: "held_out", label: "Held-out period net > 0 on ≥ 10 trades", value: -300, pass: false, note: null },
  { key: "pbo", label: "PBO ≤ 50%", value: null, pass: null, note: "fixed rules — no other configurations to compare" },
];
const SUM = { trades: 12, win_rate: 50, net_pnl: -300, profit_factor: 0.9, sharpe: -0.2 };
const CARD = {
  id: 7, strategy_id: null, version: null, name: "EMA 9/21 cross", trials: 4, verdict: "kill", checks: CHECKS,
  hypothesis: "trend days follow an EMA cross", note: null, created_at: "2026-10-22T10:00:00Z",
  setup: { start: "2025-01-01", end: "2025-06-30", grid: [], metric: "net_pnl", min_trades: 10, oos_pct: 30, walk_forward: { windows: 4, is_mult: 3, anchored: false } },
  headline: { dsr: 0.61, pbo: null, wf_net: 1200, held_out_net: -300 },
  rules: ["NSE:SBIN-EQ · 5m · long · equity", "Long when EMA(9) crosses above EMA(21)"],
  result: {
    combos: 1, trials: 4, fixed: true,
    split: { params: {}, oos_from: 1735689600, is: { ...SUM, net_pnl: 900 }, oos: SUM, ranked: 1 },
    walk_forward: { windows: 4, anchored: false, wfe_pct: 40, param_sets: 1, oos: { ...SUM, net_pnl: 1200 },
      folds: [{ is_from: 1, oos_from: 1735689600, oos_to: 1736689600, params: {}, oos: { ...SUM, net_pnl: 1200 } }] },
    full: { ...SUM, net_pnl: 600, max_drawdown_pct: 3.2 },
  },
};
const json = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }));
let calls: { url: string; body: any }[] = [];

function route(url: string, init?: RequestInit) {
  const body = init?.body ? JSON.parse(String(init.body)) : undefined;
  calls.push({ url, body });
  if (url.includes("/api/algo/indicators")) return json(CATALOG);
  if (url.includes("/api/algo/strategies")) return json({ strategies: [] });
  if (url.includes("/api/algo/status")) return json({ running: true, last_tick: null, last_sync: null, events: [], open: [] });
  if (url.includes("/api/algo/trades")) return json({ trades: [] });
  if (url.includes("/api/algo/instrument")) return json({ symbol: "NSE:SBIN-EQ", name: "SBIN", fno: false, lot: null, weekly: false, futures: [], coverage: null });
  if (url.includes("/api/algo/research/check")) return json({ ...CARD, trials: (body.trials ?? 0) + 1, elapsed_s: 0.4 });
  if (url.includes("/api/algo/research/cards/7")) return json(CARD);
  if (url.includes("/api/algo/research/cards")) {
    const { result: _r, rules: _x, ...summary } = CARD;
    const pass = { ...summary, id: 8, name: "ORB", verdict: "pass", headline: { dsr: 0.97, pbo: 0.2, wf_net: 5000, held_out_net: 2100 } };
    return json({ cards: url.includes("verdict=kill") ? [summary] : [pass, summary], bar: { dsr: 0.95, pbo: 0.5 } });
  }
  return json({});
}
const checks = () => calls.filter((c) => c.url.includes("/api/algo/research/check"));

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><Algo /></QueryClientProvider>);
}

describe("Algo Lab research loop", () => {
  let originalFetch: typeof fetch;
  beforeEach(() => {
    calls = [];
    localStorage.clear(); sessionStorage.clear();
    originalFetch = globalThis.fetch;
    globalThis.fetch = vi.fn((u: RequestInfo | URL, init?: RequestInit) => route(String(u), init)) as typeof fetch;
  });
  afterEach(() => { globalThis.fetch = originalFetch; vi.restoreAllMocks(); });

  it("runs a research check on the rules as they are and shows the card", async () => {
    mount();
    await screen.findByDisplayValue(/EMA 9\/21/);
    fireEvent.click(screen.getByRole("button", { name: "Optimise" }));
    expect(screen.getByText("Run optimisation").hasAttribute("disabled")).toBe(true);   // no grid: nothing to optimise
    fireEvent.change(screen.getByDisplayValue("one out-of-sample split"), { target: { value: "research" } });
    fireEvent.change(screen.getByTestId("research-idea"), { target: { value: "trend days follow an EMA cross" } });
    const run = screen.getByText("Run research check");
    expect(run.hasAttribute("disabled")).toBe(false);                                   // no grid is fine here
    fireEvent.click(run);
    await waitFor(() => expect(checks().length).toBe(1));
    expect(checks()[0].body).toMatchObject({
      grid: [], oos_pct: 30, walk_forward: { windows: 4, is_mult: 3, anchored: false }, min_trades: 10,
      hypothesis: "trend days follow an EMA cross", trials: 0, start: expect.any(String), end: expect.any(String),
    });
    const card = await screen.findByTestId("strategy-card");
    expect(within(card).getByTestId("card-verdict").textContent).toBe("KILLED");
    expect(within(card).getByTestId("check-dsr").textContent).toContain("61%");
    expect(within(card).getByTestId("check-dsr").textContent).toContain("FAIL");
    expect(within(card).getByTestId("check-pbo").textContent).toContain("n/a");
    expect(within(card).getByText("Long when EMA(9) crosses above EMA(21)")).toBeTruthy();
    expect(within(card).getByText(/Killed: below the bar/)).toBeTruthy();
    // the check's tries count on the draft like any backtest
    expect(JSON.parse(localStorage.getItem("algo.draft.v2")!).trials).toBe(1);
  });

  it("lists every card in the Research tab and opens one", async () => {
    mount();
    await screen.findByDisplayValue(/EMA 9\/21/);
    fireEvent.click(screen.getByRole("button", { name: "Research" }));
    const tab = await screen.findByTestId("research-tab");
    await within(tab).findByText("ORB");
    expect(within(tab).getAllByTestId("card-verdict").map((x) => x.textContent)).toEqual(["PASS", "KILLED"]);
    fireEvent.change(within(tab).getByDisplayValue("all"), { target: { value: "kill" } });
    await waitFor(() => expect(within(tab).queryByText("ORB")).toBeNull());
    fireEvent.click(await within(tab).findByText(/EMA 9\/21 cross/));
    const card = await within(tab).findByTestId("strategy-card");
    expect(within(card).getByText(/trend days follow an EMA cross/)).toBeTruthy();
    expect(calls.some((c) => c.url.endsWith("/api/algo/research/cards/7"))).toBe(true);
  });
});
