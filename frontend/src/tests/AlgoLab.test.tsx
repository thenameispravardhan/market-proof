// Algo Lab workflow tests — unsaved-change tracking, stale backtest notice,
// out-of-sample rounding and the LIVE switch-on confirmation.
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import Algo, { TEMPLATES, capitalInUse, nextDays, parseStrategyJson, pnlHistogram, tradesCsv } from "../pages/Algo";

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

let btReply: unknown = BT;
const PAYOFF = {
  symbol: "NSE:NIFTY50-INDEX", spot: 25000, spot_source: "last close", iv_pct: 13, iv_source: "India VIX", lot: 65, side: "BUY", dte: 6.5, first_expiry: 1790000000,
  legs: [{ label: "NIFTY 27OCT26 25000 CE", kind: "CE", side: "SELL", strike: 25000, expiry: 1790000000, qty: 65, dte: 6.5, price: 190, delta: 0.53, pos_delta: -34.5, theta: 980, vega: -890, gamma: -0.06, margin_alone: 120000, margin_if_bought: 12350, margin_if_sold: 120000 }],
  curve: [{ spot: 24000, expiry: -52650, now: -40000, mid: -45000 }, { spot: 25000, expiry: 12350, now: 0, mid: 6000 }, { spot: 26000, expiry: 12350, now: 9000, mid: 11000 }],
  breakevens: [24810], max_profit: 24700, max_loss: null, pop_pct: 56.1, expected_move: 450,
  greeks: { delta: -4.5, gamma: -0.13, theta: 1980, vega: -2010 }, premium: { net: 24700, kind: "credit" },
  margin: { model: "leverage", used: 406250, span: { span: 88000, exposure: 75000, premium: 0, total: 163000, scan_pct: 6, exposure_pct: 2 }, leverage: 406250, leverage_x: 8, naked: 270000, hedge_benefit: 107000, sets_affordable: 0, capital: 300000 },
  return_on_margin_pct: 6.1,
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
  if (url.includes("/api/algo/backtest")) return json(btReply);
  if (url.includes("/api/algo/payoff")) return json(PAYOFF);
  if (url.includes("/api/algo/guide")) return Promise.resolve(new Response("# Algo Lab guide\n…", { status: 200, headers: { "Content-Type": "text/markdown" } }));
  if (url.includes("/api/algo/validate")) {
    const spec = JSON.parse(String(init?.body)).spec;
    if (!spec.symbols?.length) return Promise.resolve(new Response(JSON.stringify({ detail: "add at least one symbol" }), { status: 422, headers: { "Content-Type": "application/json" } }));
    return json({ ok: true, spec: { ...DEFAULTS, ...spec, symbols: spec.symbols.map((x: string) => (x.includes(":") ? x : `NSE:${x}-EQ`)) } });
  }
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
    btReply = BT;
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

  it("names each trade's strategy and filters trades + log to the ticked strategies", async () => {
    const OTHER = { ...LIVE_STRATEGY, id: 8, name: "Paper two", mode: "paper" };
    const trade = (id: number, sid: number) => ({ id, strategy_id: sid, symbol: "NSE:SBIN-EQ", instrument: "SBIN", side: "BUY", quantity: 1, mode: "paper",
      status: "closed", entry_at: "2026-10-21T04:00:00", entry_price: 800, stop_loss: null, target: null, trail_stop: null, exit_at: "2026-10-21T05:00:00",
      exit_price: 810, exit_reason: "TARGET", net_pnl: 10, charges: 0, note: null, legs: null, version: 3 });
    globalThis.fetch = vi.fn((u: RequestInfo | URL, init?: RequestInit) => {
      const url = String(u);
      calls.push({ url, init });
      if (url.includes("/api/algo/strategies")) return json({ strategies: [LIVE_STRATEGY, OTHER] });
      if (url.includes("/api/algo/trades")) return json({ trades: url.includes("strategy_ids=8") ? [trade(2, 8)] : [trade(1, 7), trade(2, 8)] });
      if (url.includes("/api/algo/status")) return json({ running: true, last_tick: null, last_sync: null, open: [],
        ticks: { seen: 42, last_exit_check: null, watching: 2 },
        events: [{ t: 1, level: "exit", msg: "Live one v1: TARGET SBIN net ₹10", strategy_id: 7 }, { t: 1, level: "exit", msg: "Paper two v3: TARGET SBIN net ₹10", strategy_id: 8 }] });
      return route(url, init);
    }) as typeof fetch;
    mount();
    await screen.findByDisplayValue(/EMA 9\/21/);
    fireEvent.click(screen.getByText("Automations"));
    expect((await screen.findByTestId("algo-trade-strategy-1")).textContent).toBe("Live one v3");
    expect(screen.getByTestId("algo-trade-strategy-2").textContent).toBe("Paper two v3");
    expect(screen.getByTestId("algo-log-panel").textContent).toContain("Live one v1: TARGET");
    fireEvent.click(screen.getByTestId("algo-sel-8"));
    await waitFor(() => expect(screen.queryByTestId("algo-trade-strategy-1")).toBeNull());
    expect(calls.some((c) => c.url.includes("strategy_ids=8"))).toBe(true);
    expect(screen.getByTestId("algo-log-panel").textContent).not.toContain("Live one v1");
    fireEvent.click(screen.getByTestId("algo-sel-7"));             // both ticked
    await waitFor(() => expect(calls.some((c) => c.url.includes("strategy_ids=7,8"))).toBe(true));
    fireEvent.click(screen.getByText("show all"));
    await waitFor(() => expect(screen.getByTestId("algo-log-panel").textContent).toContain("Live one v1"));
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
  it("previews payoff and funds for option legs, and switches a writer to the SPAN margin estimate", async () => {
    mount();
    await screen.findByDisplayValue(/EMA 9\/21/);
    const k = TEMPLATES.findIndex((t) => t.name.includes("9:20 short straddle"));
    fireEvent.change(screen.getByDisplayValue("— load a template —"), { target: { value: String(k) } });
    const panel = await screen.findByTestId("payoff-panel");
    expect(await within(panel).findByText("unlimited")).toBeTruthy();                   // naked straddle: max loss
    expect(within(panel).getByText("₹4.06 L")).toBeTruthy();                             // funds at the leverage model
    expect(within(screen.getByTestId("payoff-legs")).getByText("NIFTY 27OCT26 25000 CE")).toBeTruthy();
    const req = JSON.parse(String(calls.filter((c) => c.url.includes("/api/algo/payoff")).pop()!.init!.body));
    expect(req.side).toBe("BUY");
    expect(req.spec.instrument.legs_long.map((l: { action: string }) => l.action)).toEqual(["SELL", "SELL"]);
    fireEvent.click(within(await screen.findByTestId("margin-warning")).getByText("use the SPAN estimate"));
    expect((within(screen.getByTestId("margin-model")).getByRole("combobox") as HTMLSelectElement).value).toBe("span");
    fireEvent.click(screen.getByText("▶ Run backtest"));
    await waitFor(() => expect(calls.some((c) => c.url.includes("/api/algo/backtest"))).toBe(true));
    const body = JSON.parse(String(calls.find((c) => c.url.includes("/api/algo/backtest"))!.init!.body));
    expect(body.spec.margin).toEqual({ model: "span", scan_pct: null, exposure_pct: null });
  });

  it("sends delta strikes and a leg's own expiry", async () => {
    mount();
    await screen.findByDisplayValue(/EMA 9\/21/);
    const k = TEMPLATES.findIndex((t) => t.name.includes("20-delta short strangle"));
    fireEvent.change(screen.getByDisplayValue("— load a template —"), { target: { value: String(k) } });
    await screen.findByTestId("payoff-panel");
    fireEvent.change(screen.getAllByTestId("leg-expiry")[0], { target: { value: "next" } });
    fireEvent.click(screen.getByText("▶ Run backtest"));
    await waitFor(() => expect(calls.some((c) => c.url.includes("/api/algo/backtest"))).toBe(true));
    const body = JSON.parse(String(calls.find((c) => c.url.includes("/api/algo/backtest"))!.init!.body));
    expect(body.spec.instrument.legs_long).toEqual([
      { right: "CE", action: "SELL", strike: "DELTA", steps: 0, premium: 100, lots: 1, delta: 0.2, expiry: "next" },
      { right: "PE", action: "SELL", strike: "DELTA", steps: 0, premium: 100, lots: 1, delta: 0.2 }]);
    expect(body.spec.margin).toEqual({ model: "span" });
  });

  it("draws trade analytics: capital in use, distribution, hours, excursions, month heatmap", async () => {
    const tr = (entry_t: number, exit_t: number, net: number, margin: number) => ({
      symbol: "NSE:SBIN-EQ", instrument: "NSE:SBIN-EQ", side: "BUY", qty: 1, lots: 1, entry_t, entry: 100, exit_t, exit: 101, u_entry: 100, u_exit: 101,
      reason: "TARGET", gross: net, charges: 0, net, bars: 2, margin, legs: [], mae_pct: -0.4, mfe_pct: 1.2, r: null, minutes: 10 });
    btReply = { ...BT, trades: [tr(1735704000, 1735707600, 500, 40000), tr(1735705000, 1735706000, -200, 30000)], trades_total: 2,
      stats: { ...BT.stats, net_pnl: 300, monthly: { "2025-01": 300 } } };
    mount();
    await screen.findByDisplayValue(/EMA 9\/21/);
    fireEvent.click(screen.getByText("▶ Run backtest"));
    const cap = await screen.findByTestId("capital-in-use");
    expect(within(cap).getByText(/70,000/)).toBeTruthy();                               // both open at once
    expect(screen.getByTestId("pnl-histogram")).toBeTruthy();
    expect(screen.getByTestId("mae-mfe")).toBeTruthy();
    expect(within(screen.getByTestId("month-heatmap")).getByText("2025")).toBeTruthy();
    expect(screen.queryByTestId("analytics-partial")).toBeNull();                      // every trade is here
  });

  it("says when the analytics see only the last trades the server sent", async () => {
    const tr = { symbol: "NSE:SBIN-EQ", instrument: "NSE:SBIN-EQ", side: "BUY", qty: 1, lots: 1, entry_t: 1735704000, entry: 100,
      exit_t: 1735707600, exit: 101, u_entry: 100, u_exit: 101, reason: "TARGET", gross: 1, charges: 0, net: 1, bars: 2, margin: 100,
      legs: [], mae_pct: null, mfe_pct: null, r: null, minutes: 10 };
    btReply = { ...BT, trades: [tr], trades_total: 5000 };
    mount();
    await screen.findByDisplayValue(/EMA 9\/21/);
    fireEvent.click(screen.getByText("▶ Run backtest"));
    expect((await screen.findByTestId("analytics-partial")).textContent).toMatch(/last 1 of 5000 trades/);
  });

  it("analytics helpers: histogram buckets and margin in use over time", () => {
    const h = pnlHistogram([-100, -50, 0, 50, 100], 4);
    expect(h.map((b) => b.count)).toEqual([1, 1, 1, 2]);
    expect(capitalInUse([{ entry_t: 1, exit_t: 5, margin: 10 }, { entry_t: 2, exit_t: 3, margin: 5 }]).map((p) => p.used)).toEqual([10, 15, 10, 0]);
  });
  it("collapses and expands the leg presets, and remembers it", async () => {
    mount();
    await screen.findByDisplayValue(/EMA 9\/21/);
    const k = TEMPLATES.findIndex((t) => t.name.includes("9:20 short straddle"));
    fireEvent.change(screen.getByDisplayValue("— load a template —"), { target: { value: String(k) } });
    const toggle = await screen.findByTestId("presets-toggle");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("Butterfly")).toBeNull();                                   // collapsed by default
    fireEvent.click(toggle);
    fireEvent.click(await screen.findByText("Butterfly"));
    expect(screen.getAllByDisplayValue("SELL").length).toBeGreaterThan(0);
    expect(localStorage.getItem("algo.presets.open")).toBe("1");
    fireEvent.click(screen.getByTestId("presets-toggle"));
    expect(screen.queryByText("Butterfly")).toBeNull();
  });

  it("keeps a combined right-hand value when the operator changes", async () => {
    mount();
    await screen.findByDisplayValue(/EMA 9\/21/);
    fireEvent.change(screen.getAllByDisplayValue("EMA")[1], { target: { value: "@math" } });   // EMA 9 crosses above (EMA 21 − EMA 21)
    fireEvent.change(screen.getAllByDisplayValue("crosses above")[0], { target: { value: ">" } });
    fireEvent.click(screen.getByText("▶ Run backtest"));
    await waitFor(() => expect(calls.some((c) => c.url.includes("/api/algo/backtest"))).toBe(true));
    const c0 = JSON.parse(String(calls.find((c) => c.url.includes("/api/algo/backtest"))!.init!.body)).spec.entry_long.conditions[0];
    expect(c0.op).toBe(">");
    expect(c0.right).toMatchObject({ math: "-", a: { ind: "EMA" }, b: { ind: "EMA" } });      // not replaced by the number 0
  });

  it("drops a leg's option-only settings when it is switched to a future", async () => {
    mount();
    await screen.findByDisplayValue(/EMA 9\/21/);
    const k = TEMPLATES.findIndex((t) => t.name.includes("9:20 short straddle"));
    fireEvent.change(screen.getByDisplayValue("— load a template —"), { target: { value: String(k) } });
    await screen.findByTestId("payoff-panel");
    fireEvent.change(screen.getAllByTestId("leg-sl_pct")[0].querySelector("input")!, { target: { value: "30" } });
    fireEvent.change(screen.getAllByTestId("leg-expiry")[0], { target: { value: "next" } });
    fireEvent.change(screen.getAllByDisplayValue("CE (call)")[0], { target: { value: "FUT" } });
    fireEvent.click(screen.getByText("▶ Run backtest"));
    await waitFor(() => expect(calls.some((c) => c.url.includes("/api/algo/backtest"))).toBe(true));
    const leg = JSON.parse(String(calls.find((c) => c.url.includes("/api/algo/backtest"))!.init!.body)).spec.instrument.legs_long[0];
    expect(leg).toEqual({ right: "FUT", action: "SELL", strike: "ATM", steps: 0, premium: 100, lots: 1 });   // no hidden SL % / own expiry
  });

  it("sends per-leg stops, the on-leg-stop action, MTM % of premium and Greek rules", async () => {
    mount();
    await screen.findByDisplayValue(/EMA 9\/21/);
    const k = TEMPLATES.findIndex((t) => t.name.includes("9:20 short straddle"));
    fireEvent.change(screen.getByDisplayValue("— load a template —"), { target: { value: String(k) } });
    await screen.findByTestId("payoff-panel");
    fireEvent.change(screen.getAllByTestId("leg-sl_pct")[0].querySelector("input")!, { target: { value: "30" } });
    fireEvent.change(within(await screen.findByTestId("on-leg-stop")).getByRole("combobox"), { target: { value: "cost" } });
    fireEvent.change(screen.getByTestId("mtm-target-pct").querySelector("input")!, { target: { value: "50" } });
    fireEvent.click(within(screen.getByTestId("rules-entry")).getByText("+ entry rule"));
    fireEvent.click(within(screen.getByTestId("rules-exit")).getByText("+ exit rule"));
    fireEvent.change(within(screen.getByTestId("rules-exit")).getAllByRole("combobox")[0], { target: { value: "pnl_pct" } });
    fireEvent.click(screen.getByText("▶ Run backtest"));
    await waitFor(() => expect(calls.some((c) => c.url.includes("/api/algo/backtest"))).toBe(true));
    const spec = JSON.parse(String(calls.find((c) => c.url.includes("/api/algo/backtest"))!.init!.body)).spec;
    expect(spec.instrument.legs_long[0].sl_pct).toBe(30);
    expect(spec.instrument.legs_long[1].sl_pct).toBeUndefined();
    expect(spec.instrument.on_leg_stop).toBe("cost");
    expect(spec.mtm.target_pct).toBe(50);
    expect(spec.position_rules).toEqual({ entry: [{ metric: "abs_delta", op: "<=", value: 0.1 }], exit: [{ metric: "pnl_pct", op: ">", value: 0.35 }] });
    fireEvent.click(within(screen.getByTestId("rules-entry")).getByText("✕"));
    fireEvent.click(within(screen.getByTestId("rules-exit")).getByText("✕"));
    fireEvent.click(screen.getByText("▶ Run backtest"));
    await waitFor(() => expect(calls.filter((c) => c.url.includes("/api/algo/backtest")).length).toBe(2));
    expect(JSON.parse(String(calls.filter((c) => c.url.includes("/api/algo/backtest"))[1].init!.body)).spec.position_rules).toBeUndefined();
  });

  it("downloads the Algo guide for an AI", async () => {
    const url = vi.fn(() => "blob:guide");
    (URL as unknown as { createObjectURL: unknown }).createObjectURL = url;
    (URL as unknown as { revokeObjectURL: unknown }).revokeObjectURL = vi.fn();
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    mount();
    await screen.findByDisplayValue(/EMA 9\/21/);
    fireEvent.click(screen.getByTestId("algo-guide"));
    await waitFor(() => expect(click).toHaveBeenCalled());
    expect(calls.some((c) => c.url.includes("/api/algo/guide"))).toBe(true);
    const a = click.mock.instances[0] as unknown as HTMLAnchorElement;
    expect(a.download).toBe("algo-lab-guide.txt");
  });

  it("imports pasted AI JSON after the server checks it, and explains a rejection", async () => {
    mount();
    await screen.findByDisplayValue(/EMA 9\/21/);
    fireEvent.click(screen.getByTestId("paste-json"));
    const box = within(await screen.findByTestId("paste-panel")).getByRole("textbox");
    fireEvent.change(box, { target: { value: 'Here you go:\n```json\n{"name": "AI ORB", "spec": {"symbols": [], "timeframe": 15}}\n```' } });
    fireEvent.click(within(screen.getByTestId("paste-panel")).getByText("Import"));
    expect((await screen.findByTestId("paste-error")).textContent).toContain("add at least one symbol");
    expect(screen.queryByDisplayValue("AI ORB")).toBeNull();                               // not loaded
    fireEvent.change(box, { target: { value: '```json\n{"name": "AI ORB", "spec": {"symbols": ["SBIN"], "timeframe": 15}}\n```' } });
    fireEvent.click(within(screen.getByTestId("paste-panel")).getByText("Import"));
    expect(await screen.findByDisplayValue("AI ORB")).toBeTruthy();
    expect(screen.queryByTestId("paste-panel")).toBeNull();
    const v = calls.filter((c) => c.url.includes("/api/algo/validate")).pop()!;
    expect(JSON.parse(String(v.init!.body)).spec.timeframe).toBe(15);
  });

  it("parses AI replies and writes entry Greeks and leg exits into the trades CSV", () => {
    expect(parseStrategyJson('Sure!\n```json\n{"name": "X", "spec": {"timeframe": 5}}\n```\nAssumed NIFTY.')).toEqual({ name: "X", spec: { timeframe: 5 } });
    expect(parseStrategyJson('{"timeframe": 15, "symbols": ["NSE:SBIN-EQ"]}').spec.timeframe).toBe(15);
    expect(() => parseStrategyJson("no json here")).toThrow(/no JSON object/);
    expect(() => parseStrategyJson("{oops}")).toThrow(/not valid JSON/);
    const csv = tradesCsv([{ symbol: "N", instrument: "CE + PE", side: "BUY", qty: 65, lots: 1, entry_t: 1735704000, entry: 1, exit_t: 1735707600, exit: 1,
      u_entry: 1, u_exit: 1, reason: "LEG_COST", gross: 1, charges: 0, net: 1, bars: 1, margin: 1, mae_pct: null, mfe_pct: null, r: null, minutes: 60,
      greeks: { iv: 13.5, delta: -0.02, theta: 1200, vega: -900, credit: 5000 },
      legs: [{ label: "CE", side: "SELL", qty: 65, entry: 45, exit: 58.5, reason: "LEG_SL" }, { label: "PE", side: "SELL", qty: 65, entry: 35, exit: 35, reason: "LEG_COST" }] }]);
    const [head, row] = csv.split("\n");
    expect(head).toContain("entry_iv_pct");
    expect(row).toContain('"13.5"');
    expect(row).toContain("SELL 65 CE: 45 → 58.5 (LEG_SL) | SELL 65 PE: 35 → 35 (LEG_COST)");
  });
});
