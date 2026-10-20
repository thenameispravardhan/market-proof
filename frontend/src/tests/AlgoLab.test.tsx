// Algo Lab workflow tests — unsaved-change tracking, stale backtest notice,
// out-of-sample rounding and the LIVE switch-on confirmation.
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import Algo, { nextDays } from "../pages/Algo";

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
  operators: [">", "<", "crosses_above", "crosses_below", "between", "outside"], timeframes: [1, 5, 15, 60, 1440], cond_timeframes: [15, 60, 1440],
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

const P1 = "entry_long.conditions.0.left.params.period";
const P2 = "entry_long.conditions.0.right.params.period";
const OPT = {
  combos: 4, ranked: [], too_few_trades: 1, metric: "net_pnl", notes: [], elapsed_s: 0.2, oos_from: null, overfitting: null,
  all: [
    { params: { [P1]: 9, [P2]: 21 }, value: 1200, trades: 30 }, { params: { [P1]: 9, [P2]: 26 }, value: -300, trades: 25 },
    { params: { [P1]: 11, [P2]: 21 }, value: 800, trades: 28 }, { params: { [P1]: 11, [P2]: 26 }, value: 50, trades: 2 },
  ],
};
const WF = {
  combos: 25, metric: "net_pnl", windows: 2, anchored: false, wfe_pct: 62.5, elapsed_s: 0.4, notes: [], oos_from: null,
  folds: [
    { is_from: 1735689600, oos_from: 1738368000, oos_to: 1739577600, params: { [P1]: 9, [P2]: 21 }, is: null, is_value: 900, oos: { trades: 12, net_pnl: 400, profit_factor: 1.4 } },
    { is_from: 1736899200, oos_from: 1739577600, oos_to: 1740787200, params: null, is: null, oos: null },
  ],
  oos: { trades: 12, win_rate: 50, net_pnl: 400, profit_factor: 1.4, sharpe: 1.1, max_drawdown: 150, max_drawdown_pct: 0.2, equity: [[1738400000, 100100], [1738500000, 100400]] },
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
  if (url.includes("/api/algo/optimize")) return json(JSON.parse(String(init?.body)).walk_forward ? WF : OPT);
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

  it("sends scale-out steps and entry weekdays with the backtest", async () => {
    mount();
    await screen.findByDisplayValue(/EMA 9\/21/);
    fireEvent.click(within(screen.getByTestId("scale-out")).getByText("+ step"));       // the template has a stop: an R step
    fireEvent.click(within(screen.getByTestId("algo-days")).getByLabelText("Mon"));     // untick Monday
    fireEvent.click(screen.getByText("▶ Run backtest"));
    await waitFor(() => expect(calls.some((c) => c.url.includes("/api/algo/backtest"))).toBe(true));
    const body = JSON.parse(String(calls.find((c) => c.url.includes("/api/algo/backtest"))!.init!.body));
    expect(body.spec.scale_out).toEqual([{ type: "rr", value: 1, qty_pct: 50, atr_period: 14 }]);
    expect(body.spec.session.days).toEqual([2, 3, 4, 5]);
  });

  it("builds an angle range and condition modifiers", async () => {
    mount();
    await screen.findByDisplayValue(/EMA 9\/21/);
    fireEvent.change(screen.getAllByTestId("operand-xf")[0], { target: { value: "angle" } });     // EMA 9 as its angle
    const opSel = screen.getAllByDisplayValue("crosses above")[0] as HTMLSelectElement;
    fireEvent.change(opSel, { target: { value: "between" } });
    fireEvent.click(screen.getAllByTestId("cond-more")[0]);
    fireEvent.click(within(screen.getAllByTestId("cond-modifiers")[0]).getByLabelText("NOT"));
    fireEvent.click(screen.getByText("▶ Run backtest"));
    await waitFor(() => expect(calls.some((c) => c.url.includes("/api/algo/backtest"))).toBe(true));
    const body = JSON.parse(String(calls.find((c) => c.url.includes("/api/algo/backtest"))!.init!.body));
    const c0 = body.spec.entry_long.conditions[0];
    expect(c0.left).toMatchObject({ ind: "EMA", xf: "angle", xn: 3 });
    expect(c0.op).toBe("between");
    expect(c0.right2).toEqual({ value: 0 });            // the high bound appears with between
    expect(c0.negate).toBe(true);
  });

  it("weekday picker: all five = every day (null), and never empty", () => {
    expect(nextDays(null, 1, false)).toEqual([2, 3, 4, 5]);
    expect(nextDays([2, 3, 4, 5], 1, true)).toBeNull();
    expect(nextDays([3], 3, false)).toEqual([3]);
  });

  it("draws a heatmap for two swept parameters and runs a walk-forward", async () => {
    mount();
    await screen.findByDisplayValue(/EMA 9\/21/);
    fireEvent.click(screen.getByRole("button", { name: "Optimise" }));
    fireEvent.click(screen.getByText("+ parameter"));
    fireEvent.click(screen.getByText("+ parameter"));
    fireEvent.click(screen.getByText("Run optimisation"));
    const heat = await screen.findByTestId("opt-heatmap");
    expect(within(heat).getByText("1200")).toBeTruthy();
    expect(within(heat).getAllByText("·").length).toBe(1);                              // the 2-trade combo is not ranked
    fireEvent.click(within(heat).getByText("800"));
    expect(await screen.findByText(/Optimiser values applied/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Optimise" }));
    fireEvent.change(screen.getByDisplayValue("one out-of-sample split"), { target: { value: "wf" } });
    fireEvent.click(screen.getByText("Run optimisation"));
    const wf = await screen.findByTestId("walk-forward");
    const body = JSON.parse(String(calls.filter((c) => c.url.includes("/api/algo/optimize")).pop()!.init!.body));
    expect(body.walk_forward).toEqual({ windows: 4, is_mult: 3, anchored: false });
    expect(body.oos_pct).toBeUndefined();
    expect(within(wf).getByText("63%")).toBeTruthy();                                   // WF efficiency
    expect(within(wf).getByText(/not traded/)).toBeTruthy();                            // the empty window says so
  });
});
