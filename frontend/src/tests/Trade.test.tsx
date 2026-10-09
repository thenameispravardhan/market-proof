// Trade page tests — render the page, drive the search and ticket,
// and verify the place-order flow against a stubbed backend.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import Trade, { ticketProblem, ticketWarning, levelPrice, type TicketCheck } from "../pages/Trade";

// lightweight-charts needs a real <canvas>; jsdom has none. Stub the
// whole engine so ChartPanel mounts and its toolbar renders, while the
// chart surface itself is a no-op.
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

function makeFetchStub(handler: (url: string, init?: RequestInit) => Response | Promise<Response>) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    return handler(url, init);
  });
}

function makeJsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function wrapper(qc: QueryClient) {
  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

const REAL_ACCOUNT = {
  id: 7,
  name: "Fyers Live",
  broker: "fyers",
  app_id: "APP123",
  secret_key: "***",
  access_token: "***",
  redirect_uri: null,
  paper_mode: false,
  enabled: true,
  created_at: "2026-06-13T10:00:00Z",
  updated_at: "2026-06-13T10:00:00Z",
};

const PAPER_ACCOUNT = { ...REAL_ACCOUNT, id: 99, name: "Paper Account", paper_mode: true };

const SEARCH_RELIANCE = {
  ok: true,
  count: 1,
  hits: [
    {
      symbol: "NSE:RELIANCE-EQ",
      short_name: "RELIANCE",
      exchange: "NSE",
      segment: "EQ",
      instrument_type: "EQ",
      lot_size: 1,
      tick_size: 0.05,
      expiry: null,
      strike: null,
      underlying: null,
      display: "RELIANCE  ·  NSE:EQ",
    },
  ],
};

function defaultStubs(overrides: Partial<{
  placeResponse: unknown;
  placeStatus: number;
  pending: unknown;
  positions: unknown;
  quote: unknown;
  optionChain: unknown;
  serverInfo: unknown;
}> = {}) {
  return (url: string, init?: RequestInit) => {
    if (url.includes("/api/broker-accounts")) {
      return makeJsonResponse({ accounts: [REAL_ACCOUNT, PAPER_ACCOUNT] });
    }
    if (url.includes("/api/search/symbols")) {
      return makeJsonResponse(SEARCH_RELIANCE);
    }
    if (url.includes("/api/market/history")) {
      return makeJsonResponse({
        ok: true,
        symbol: "NSE:RELIANCE-EQ",
        resolution: "5",
        candles: [
          [1750000000, 2440, 2455, 2438, 2450, 120000],
          [1750000300, 2450, 2460, 2448, 2458, 98000],
        ],
        reason: null,
      });
    }
    if (url.includes("/api/search/option-chain")) {
      return makeJsonResponse(overrides.optionChain ?? {
        ok: true, underlying: "RELIANCE", expiries: [], selected_expiry: null, spot: null, strikes: [],
      });
    }
    if (url.includes("/api/orders/quote")) {
      return makeJsonResponse(overrides.quote ?? {
        ok: true, symbol: "NSE:RELIANCE-EQ", last_price: 2450.0, bid: 2449.5, ask: 2450.5,
      });
    }
    if (url.includes("/api/orders/pending")) {
      return makeJsonResponse(overrides.pending ?? { ok: true, count: 0, orders: [] });
    }
    if (url.includes("/api/positions")) {
      return makeJsonResponse(overrides.positions ?? []);
    }
    if (url.includes("/api/server-info")) {
      return makeJsonResponse(
        overrides.serverInfo ?? {
          public_ip: "103.172.203.1",
          ip_source: "api.ipify.org (cached 5 min)",
        },
      );
    }
    if (url.includes("/api/orders/cancel") && init?.method === "POST") {
      return makeJsonResponse({ ok: true, broker_order_id: "STUB-1", reason: "cancelled" });
    }
    if (url.includes("/api/orders") && init?.method === "POST") {
      return makeJsonResponse(
        overrides.placeResponse ?? {
          ok: true,
          blocked: false,
          bypassed_risk: false,
          risk_codes: [],
          risk_message: "",
          broker_order_id: "STUB-1",
          status: "PENDING",
          error: null,
          entry_used: 2450.0,
          stop_loss_used: 2303.0,
          target_used: 2891.0,
          entry_is_synthetic: false,
        },
        overrides.placeStatus ?? 200,
      );
    }
    if (url.includes("/api/orders") && init?.method === "DELETE") {
      return makeJsonResponse({ ok: true, broker_order_id: "STUB-1" });
    }
    return makeJsonResponse({}, 404);
  };
}

describe("Trade page", () => {
  let originalFetch: typeof fetch;

  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();   // per-tab Trade state (router tabLocal)
    localStorage.setItem("trade:bottomOpen", "true");   // the orders panel open, as an operator would have it
    originalFetch = globalThis.fetch;
  });
  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  function makeQc() {
    return new QueryClient({
      defaultOptions: {
        queries: { retry: false, refetchInterval: false },
        mutations: { retry: false },
      },
    });
  }

  it("silently uses the first real account (paper accounts are hidden from the page)", async () => {
    globalThis.fetch = makeFetchStub(defaultStubs());
    const user = userEvent.setup();
    const qc = makeQc();
    render(<Trade />, { wrapper: wrapper(qc) });

    // No account picker is rendered.
    expect(screen.queryByTestId("trade-account")).not.toBeInTheDocument();

    // No segment picker either.
    expect(screen.queryByTestId("trade-segment")).not.toBeInTheDocument();

    // Pick a symbol and place an order — the real account is used
    // automatically. The fetch stub records the order and we verify
    // the broker account id was the live one (7), never the paper (99).
    const search = await screen.findByTestId("trade-search");
    await user.type(search, "RELI");
    const row = await screen.findByTestId("search-row-NSE:RELIANCE-EQ");
    await user.click(row);

    const submit = await screen.findByTestId("ticket-submit");
    await waitFor(() => expect(submit).not.toBeDisabled());
    await user.click(submit);

    // Find the POST /api/orders call.
    const fetchMock = globalThis.fetch as unknown as ReturnType<typeof makeFetchStub>;
    const orderCall = fetchMock.mock.calls.find(
      (args) => {
        const [url, init] = args as [unknown, RequestInit | undefined];
        return (
          typeof url === "string" &&
          url.includes("/api/orders") &&
          init?.method === "POST"
        );
      },
    );
    expect(orderCall).toBeDefined();
    const body = JSON.parse(String((orderCall![1] as RequestInit).body));
    expect(body.account_id).toBe(7);
  });

  it("disables the ticket when there are no live accounts", async () => {
    globalThis.fetch = makeFetchStub((url) => {
      if (url.includes("/api/broker-accounts")) return makeJsonResponse({ accounts: [PAPER_ACCOUNT] });
      if (url.includes("/api/orders/pending")) return makeJsonResponse({ ok: true, count: 0, orders: [] });
      if (url.includes("/api/positions")) return makeJsonResponse([]);
      return makeJsonResponse({ ok: true, count: 0, hits: [] }, 200);
    });
    const qc = makeQc();
    render(<Trade />, { wrapper: wrapper(qc) });
    // Trade page silently shows the search UI with accountId=null.
    // The ticket submit button should be disabled since no account is selected.
    expect(await screen.findByTestId("ticket-submit")).toBeDisabled();
  });

  it("disables the place button when no symbol is selected", async () => {
    globalThis.fetch = makeFetchStub(defaultStubs());
    const qc = makeQc();
    render(<Trade />, { wrapper: wrapper(qc) });
    const submit = await screen.findByTestId("ticket-submit");
    expect(submit).toBeDisabled();
  });

  it("places a MARKET BUY and shows a success result", async () => {
    globalThis.fetch = makeFetchStub(defaultStubs());
    const user = userEvent.setup();
    const qc = makeQc();
    render(<Trade />, { wrapper: wrapper(qc) });

    // Type into the search box, then click the result.
    const search = await screen.findByTestId("trade-search");
    await user.type(search, "RELI");
    // The result row should appear.
    const row = await screen.findByTestId("search-row-NSE:RELIANCE-EQ");
    await user.click(row);

    // The submit button should now be enabled.
    const submit = await screen.findByTestId("ticket-submit");
    await waitFor(() => expect(submit).not.toBeDisabled());

    // Place the order.
    await user.click(submit);

    // Success result should appear.
    const success = await screen.findByTestId("ticket-result-success");
    expect(success.textContent).toMatch(/PENDING/);
    expect(success.textContent).toMatch(/NSE:RELIANCE-EQ/);
  });

  it("ticket: product choice + default, optional SL / target ride with the order", async () => {
    const posts: Record<string, unknown>[] = [];
    const stubs = defaultStubs();
    globalThis.fetch = makeFetchStub((url, init) => {
      if (/\/api\/orders$/.test(url) && init?.method === "POST") posts.push(JSON.parse(String(init.body)));
      return stubs(url, init);
    });
    const user = userEvent.setup();
    render(<Trade />, { wrapper: wrapper(makeQc()) });
    await user.type(await screen.findByTestId("trade-search"), "RELI");
    await user.click(await screen.findByTestId("search-row-NSE:RELIANCE-EQ"));
    await waitFor(() => expect(screen.getByTestId("ticket-submit")).not.toBeDisabled());
    expect(screen.getByTestId("ticket-sl")).toBeTruthy();   // always shown, optional
    expect(screen.getByTestId("ticket-tp")).toBeTruthy();
    expect(screen.getByTestId("ticket-est")).toBeTruthy();
    await user.click(screen.getByTestId("ticket-product-DELIVERY"));
    await user.click(screen.getByTestId("ticket-product-default"));
    expect(localStorage.getItem("trade:defaultProduct")).toBe(JSON.stringify("DELIVERY"));
    await user.type(screen.getByTestId("ticket-sl"), "95");
    await user.click(screen.getByTestId("ticket-submit"));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({ product_type: "DELIVERY", stop_loss: 95, target: null });
  });

  it("places a LIMIT buy from the chart's right-click menu only after the confirm", async () => {
    const posts: Record<string, unknown>[] = [];
    const stubs = defaultStubs();
    globalThis.fetch = makeFetchStub((url, init) => {
      if (/\/api\/orders$/.test(url) && init?.method === "POST") posts.push(JSON.parse(String(init.body)));
      return stubs(url, init);
    });
    const user = userEvent.setup();
    render(<Trade />, { wrapper: wrapper(makeQc()) });
    await user.type(await screen.findByTestId("trade-search"), "RELI");
    await user.click(await screen.findByTestId("search-row-NSE:RELIANCE-EQ"));
    await waitFor(() => expect(screen.getByTestId("ticket-submit")).not.toBeDisabled());

    // Right-click at a price below the market (the chart stub maps every y to 100).
    const chart = await screen.findByTestId("trade-chart");
    fireEvent.contextMenu(chart.querySelector(".chart-container")!, { clientX: 40, clientY: 40 });
    await user.click(await screen.findByTestId("ctx-trade")); // the Trade ▸ flyout
    await user.click(await screen.findByText(/Buy 1 @ 100(\.00)? limit/));
    expect(posts).toHaveLength(0);                  // nothing reaches the broker before the confirm
    await user.click(screen.getByTestId("chart-ctx-place"));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({
      account_id: 7, symbol: "NSE:RELIANCE-EQ", side: "BUY", quantity: 1,
      order_type: "LIMIT", limit_price: 100, stop_price: null, product_type: "INTRADAY",
    });
  });

  it("places a stop-limit from the Trade ▸ flyout with the trigger and a one-tick limit", async () => {
    const posts: Record<string, unknown>[] = [];
    const stubs = defaultStubs();
    globalThis.fetch = makeFetchStub((url, init) => {
      if (/\/api\/orders$/.test(url) && init?.method === "POST") posts.push(JSON.parse(String(init.body)));
      return stubs(url, init);
    });
    const user = userEvent.setup();
    render(<Trade />, { wrapper: wrapper(makeQc()) });
    await user.type(await screen.findByTestId("trade-search"), "RELI");
    await user.click(await screen.findByTestId("search-row-NSE:RELIANCE-EQ"));
    await waitFor(() => expect(screen.getByTestId("ticket-submit")).not.toBeDisabled());
    const chart = await screen.findByTestId("trade-chart");
    fireEvent.contextMenu(chart.querySelector(".chart-container")!, { clientX: 40, clientY: 40 });
    await user.click(await screen.findByTestId("ctx-trade"));
    // 100 is below the market → the breakdown side: sell stop 100, limit 99.95
    await user.click(await screen.findByText(/Sell 1 @ 100(\.00)? stop 99\.95 limit/));
    await user.click(screen.getByTestId("chart-ctx-place"));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({ side: "SELL", order_type: "STOP_LOSS", stop_price: 100, limit_price: 99.95 });
  });

  it("scalper: BUY CE places a market order for lots x lot size on the ATM call", async () => {
    localStorage.setItem("trade:scalper", "true");
    const leg = (strike: number, t: "CE" | "PE") => ({
      symbol: `NSE:RELIANCE26OCT${strike}${t}`, ltp: 20, bid: null, ask: null, oi: 1, volume: 1, ltpch: 0, lot_size: 250, tick_size: 0.05,
    });
    const posts: Record<string, unknown>[] = [];
    const stubs = defaultStubs();
    globalThis.fetch = makeFetchStub((url, init) => {
      if (url.includes("/api/options/chain")) {
        return makeJsonResponse({
          ok: true, underlying: "RELIANCE", symbol: "NSE:RELIANCE-EQ", spot: 2452, source: "fyers", reason: null,
          expiries: [{ label: "28-10-2026", ts: "1793000000" }], selected_expiry: "1793000000",
          strikes: [2400, 2450, 2500].map((k) => ({ strike: k, ce: leg(k, "CE"), pe: leg(k, "PE") })),
        });
      }
      if (/\/api\/orders$/.test(url) && init?.method === "POST") posts.push(JSON.parse(String(init.body)));
      return stubs(url, init);
    });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<Trade />, { wrapper: wrapper(makeQc()) });
    await user.type(await screen.findByTestId("trade-search"), "RELI");
    await user.click(await screen.findByTestId("search-row-NSE:RELIANCE-EQ"));
    await waitFor(() => expect(screen.getByTestId("ticket-submit")).not.toBeDisabled());   // account loaded

    await user.click(await screen.findByTestId("scalp-buy-CE"));
    expect(confirm).toHaveBeenCalled();                       // 1-click is off: a confirm first
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({
      account_id: 7, symbol: "NSE:RELIANCE26OCT2450CE", side: "BUY", quantity: 250,
      order_type: "MARKET", product_type: "INTRADAY",
    });
  });

  it("surfaces a risk warning on a placed order (override / engine-fault)", async () => {
    // When the backend places an order that still carries a risk note
    // (an operator override, or a risk-engine fault that didn't block),
    // the UI shows it as a warning on the success result.
    const placeResponse = {
      ok: true,
      blocked: false,
      bypassed_risk: true,
      risk_codes: ["RISK_MAX_SINGLE_POSITION_PCT"],
      risk_message: "RISK_MAX_SINGLE_POSITION_PCT",
      risk_warning: "RISK_MAX_SINGLE_POSITION_PCT",
      broker_order_id: "FY-12345",
      status: "PENDING",
      error: null,
    };
    globalThis.fetch = makeFetchStub(defaultStubs({ placeResponse }));
    const user = userEvent.setup();
    const qc = makeQc();
    render(<Trade />, { wrapper: wrapper(qc) });

    const search = await screen.findByTestId("trade-search");
    await user.type(search, "REL");
    const row = await screen.findByTestId("search-row-NSE:RELIANCE-EQ");
    await user.click(row);

    const submit = await screen.findByTestId("ticket-submit");
    await waitFor(() => expect(submit).not.toBeDisabled());
    await user.click(submit);

    // The order succeeded and the risk warning is surfaced.
    const ok = await screen.findByTestId("ticket-result-success");
    expect(ok.textContent).toMatch(/PENDING/);
    const advisory = await screen.findByTestId("ticket-risk-advisory");
    expect(advisory.textContent).toMatch(/RISK_MAX_SINGLE_POSITION_PCT/);
  });

  it("blocks a manual order on a risk breach, then places it on override", async () => {
    // Settings actually take effect on the Trade page: a risk breach
    // BLOCKS the order (REJECTED_RISK). The operator can then click the
    // override button, which re-submits with bypass_risk=true and places.
    const blocked = {
      ok: false,
      blocked: true,
      bypassed_risk: false,
      risk_codes: ["RISK_MAX_SINGLE_POSITION_PCT"],
      risk_message: "RISK_MAX_SINGLE_POSITION_PCT",
      risk_warning: "RISK_MAX_SINGLE_POSITION_PCT",
      broker_order_id: null,
      status: "REJECTED_RISK",
      error: "Blocked by risk limits: RISK_MAX_SINGLE_POSITION_PCT.",
      reason: "risk_block",
    };
    const placed = {
      ok: true,
      blocked: false,
      bypassed_risk: true,
      risk_codes: ["RISK_MAX_SINGLE_POSITION_PCT"],
      risk_message: "RISK_MAX_SINGLE_POSITION_PCT",
      risk_warning: "RISK_MAX_SINGLE_POSITION_PCT",
      broker_order_id: "FY-OVR-1",
      status: "PENDING",
      error: null,
    };
    // Stateful place handler: the bypass re-submit places, otherwise block.
    const base = defaultStubs();
    globalThis.fetch = makeFetchStub((url, init) => {
      if (url.includes("/api/orders") && init?.method === "POST") {
        const body = init?.body ? JSON.parse(String(init.body)) : {};
        return makeJsonResponse(body.bypass_risk ? placed : blocked);
      }
      return base(url, init);
    });
    const user = userEvent.setup();
    const qc = makeQc();
    render(<Trade />, { wrapper: wrapper(qc) });

    const search = await screen.findByTestId("trade-search");
    await user.type(search, "REL");
    const row = await screen.findByTestId("search-row-NSE:RELIANCE-EQ");
    await user.click(row);

    const submit = await screen.findByTestId("ticket-submit");
    await waitFor(() => expect(submit).not.toBeDisabled());
    await user.click(submit);

    // The order was blocked and the override affordance appears.
    const err = await screen.findByTestId("ticket-result-error");
    expect(err.textContent).toMatch(/Blocked by risk limits/);
    const overrideBtn = await screen.findByTestId("ticket-risk-override-btn");

    // Operator overrides → the re-submit places the order.
    await user.click(overrideBtn);
    const ok = await screen.findByTestId("ticket-result-success");
    expect(ok.textContent).toMatch(/PENDING/);
  });

  it("shows the LIMIT price field when order type is LIMIT and refuses submit without it", async () => {
    globalThis.fetch = makeFetchStub(defaultStubs());
    const user = userEvent.setup();
    const qc = makeQc();
    render(<Trade />, { wrapper: wrapper(qc) });

    // Pick a symbol
    const search = await screen.findByTestId("trade-search");
    await user.type(search, "REL");
    const row = await screen.findByTestId("search-row-NSE:RELIANCE-EQ");
    await user.click(row);

    // Switch order type to LIMIT
    const typeSel = await screen.findByTestId("ticket-type");
    fireEvent.change(typeSel, { target: { value: "LIMIT" } });

    // Limit price field appears
    const limitInput = await screen.findByTestId("ticket-limit");
    expect(limitInput).toBeInTheDocument();

    // Without a price, the submit button is disabled.
    const submit = screen.getByTestId("ticket-submit");
    expect(submit).toBeDisabled();

    // Fill the price and the button becomes enabled.
    fireEvent.change(limitInput, { target: { value: "2400" } });
    await waitFor(() => expect(submit).not.toBeDisabled());
  });

  it("renders a clean place-order error when the broker returns HTML (Cloudflare block)", async () => {
    // Regression: the backend used to surface the raw Cloudflare
    // HTML body when Fyers was blocked. The Trade page's banner
    // would show 500 chars of <!DOCTYPE html>…</title>. The fix is
    // on the backend (clean message) AND in the frontend's
    // `cleanError` helper (strip HTML, truncate) — both must hold.
    // We simulate a REJECTED result that still contains HTML so we
    // exercise the frontend sanitizer.
    const cloudflareyError =
      "<!DOCTYPE html><html><head>" +
      "<title>Attention Required! | Cloudflare</title>" +
      "</head><body>captcha</body></html>";
    const placeResponse = {
      ok: false,
      blocked: false,
      bypassed_risk: false,
      risk_codes: [],
      risk_message: "",
      broker_order_id: "",
      status: "REJECTED",
      error: cloudflareyError,
    };
    globalThis.fetch = makeFetchStub(defaultStubs({ placeResponse }));
    const user = userEvent.setup();
    const qc = makeQc();
    render(<Trade />, { wrapper: wrapper(qc) });

    const search = await screen.findByTestId("trade-search");
    await user.type(search, "REL");
    const row = await screen.findByTestId("search-row-NSE:RELIANCE-EQ");
    await user.click(row);

    const submit = await screen.findByTestId("ticket-submit");
    await waitFor(() => expect(submit).not.toBeDisabled());
    await user.click(submit);

    const err = await screen.findByTestId("ticket-result-error");
    // No HTML tags in the rendered error.
    expect(err.textContent).not.toMatch(/<[^>]+>/);
    expect(err.textContent).not.toMatch(/<!DOCTYPE/i);
    // Bounded length — never the full 500-char wall.
    expect((err.textContent || "").length).toBeLessThan(280);
  });

  it("renders an IP-whitelist hint with a copyable public IP when Fyers rejects with the IP-whitelist error", async () => {
    // Fyers' "Algo orders are not allowed from this app" error
    // (code=-50) is actually a server-IP whitelist issue per
    // Fyers support docs. The backend converts it to a clean
    // error string and sets `reason: "ip_whitelist"`; the UI
    // surfaces the public IP so the operator can copy it into
    // the Fyers dashboard's whitelist.
    const placeResponse = {
      ok: false,
      blocked: false,
      bypassed_risk: false,
      risk_codes: [],
      risk_message: "",
      broker_order_id: "",
      status: "REJECTED",
      error: "Fyers rejected the order — this server's outbound IP is not whitelisted on the Fyers app's dashboard.",
      reason: "ip_whitelist",
    };
    globalThis.fetch = makeFetchStub(
      defaultStubs({ placeResponse, serverInfo: { public_ip: "103.172.203.1", ip_source: "test" } }),
    );
    const user = userEvent.setup();
    const qc = makeQc();
    render(<Trade />, { wrapper: wrapper(qc) });

    const search = await screen.findByTestId("trade-search");
    await user.type(search, "REL");
    const row = await screen.findByTestId("search-row-NSE:RELIANCE-EQ");
    await user.click(row);
    const submit = await screen.findByTestId("ticket-submit");
    await waitFor(() => expect(submit).not.toBeDisabled());
    await user.click(submit);

    // The IP-whitelist hint appears with the public IP.
    const hint = await screen.findByTestId("ticket-ip-whitelist");
    expect(hint).toBeInTheDocument();
    const ip = await screen.findByTestId("ticket-server-ip");
    expect(ip.textContent).toBe("103.172.203.1");
    // Copy button is present.
    expect(screen.getByTestId("ticket-server-ip-copy")).toBeInTheDocument();
    // The link to the Fyers dashboard is in the hint.
    const link = hint.querySelector("a");
    expect(link?.getAttribute("href")).toBe("https://myapi.fyers.in/dashboard/");
  });

  it("hides the IP-whitelist hint when the place-order error has no ip_whitelist reason", async () => {
    // A generic broker rejection (e.g. insufficient margin) must
    // NOT trigger the IP-hint UI — the hint is reserved for
    // the specific `reason: "ip_whitelist"` marker. A banner
    // with the IP would be misleading and confusing.
    const placeResponse = {
      ok: false,
      blocked: false,
      bypassed_risk: false,
      risk_codes: [],
      risk_message: "",
      broker_order_id: "",
      status: "REJECTED",
      error: "Insufficient margin in the account.",
      reason: "margin",
    };
    globalThis.fetch = makeFetchStub(defaultStubs({ placeResponse }));
    const user = userEvent.setup();
    const qc = makeQc();
    render(<Trade />, { wrapper: wrapper(qc) });

    const search = await screen.findByTestId("trade-search");
    await user.type(search, "REL");
    const row = await screen.findByTestId("search-row-NSE:RELIANCE-EQ");
    await user.click(row);
    const submit = await screen.findByTestId("ticket-submit");
    await waitFor(() => expect(submit).not.toBeDisabled());
    await user.click(submit);

    const err = await screen.findByTestId("ticket-result-error");
    expect(err.textContent).toMatch(/Insufficient margin/);
    // No IP-whitelist hint for non-whitelist errors.
    expect(screen.queryByTestId("ticket-ip-whitelist")).not.toBeInTheDocument();
  });

  it("chain: drag a price onto a chart cell opens that option there", async () => {
    const leg = { symbol: "NSE:RELIANCE26OCT2500CE", ltp: 42, bid: 41.9, ask: 42.1, oi: 1000, volume: 10, ltpch: 0, lot_size: 500, tick_size: 0.05 };
    const chain = { ok: true, underlying: "RELIANCE", symbol: "", spot: 2450, expiries: [], selected_expiry: null, source: "fyers",
      strikes: [{ strike: 2500, ce: leg, pe: null }] };
    const stubs = defaultStubs();
    globalThis.fetch = makeFetchStub((url, init) => (url.includes("/api/options/chain") ? makeJsonResponse(chain) : stubs(url, init)));
    const user = userEvent.setup();
    render(<Trade />, { wrapper: wrapper(makeQc()) });
    await user.type(await screen.findByTestId("trade-search"), "RELI");
    await user.click(await screen.findByTestId("search-row-NSE:RELIANCE-EQ"));
    if (!screen.queryByTestId("dock-chain")) await user.click(screen.getByTestId("rail-chain"));
    const price = await screen.findByTestId("chain-ce-2500");
    const store: Record<string, string> = {};
    const dataTransfer = { setData: (k: string, v: string) => { store[k] = v; }, getData: (k: string) => store[k] ?? "", get types() { return Object.keys(store); }, effectAllowed: "", dropEffect: "" };
    fireEvent.dragStart(price, { dataTransfer });
    const cell = screen.getByTestId("tv-cell-0");
    fireEvent.dragOver(cell, { dataTransfer });
    fireEvent.drop(cell, { dataTransfer });
    await waitFor(() => expect(JSON.parse(localStorage.getItem("trade:last") ?? "null")?.symbol).toBe(leg.symbol));
  });

  it("chain: an option whose lot size the backend doesn't know can't be ordered", async () => {
    const leg = { symbol: "NSE:RELIANCE26OCT2500CE", ltp: 42, bid: 41.9, ask: 42.1, oi: 1000, volume: 10, ltpch: 0, lot_size: null, tick_size: 0.05 };
    const chain = { ok: true, underlying: "RELIANCE", symbol: "", spot: 2450, expiries: [], selected_expiry: null, source: "fyers",
      strikes: [{ strike: 2500, ce: leg, pe: null }] };
    const stubs = defaultStubs();
    globalThis.fetch = makeFetchStub((url, init) => (url.includes("/api/options/chain") ? makeJsonResponse(chain) : stubs(url, init)));
    const user = userEvent.setup();
    render(<Trade />, { wrapper: wrapper(makeQc()) });
    await user.type(await screen.findByTestId("trade-search"), "RELI");
    await user.click(await screen.findByTestId("search-row-NSE:RELIANCE-EQ"));
    if (!screen.queryByTestId("dock-chain")) await user.click(screen.getByTestId("rail-chain"));
    await user.click(await screen.findByTestId("chain-ce-2500"));
    expect(await screen.findByTestId("ticket-lot-unknown")).toBeInTheDocument();
    expect(screen.getByTestId("ticket-submit")).toBeDisabled();
  });

  it("watchlist: a saved option's lot size is refreshed from the server when NSE revises it", async () => {
    const opt = { symbol: "NSE:NIFTY25DEC25000CE", short_name: "NIFTY 25000 CE", exchange: "NSE", segment: "FO", instrument_type: "CE",
      lot_size: 75, tick_size: 0.05, expiry: null, strike: 25000, underlying: "NIFTY", display: "NIFTY 25000 CE" };
    localStorage.setItem("trade:watchlists", JSON.stringify({ active: 0, lists: [{ name: "Watchlist", items: [opt] }] }));
    localStorage.setItem("trade:last", JSON.stringify(opt));
    const lotCalls: string[] = [];
    const stubs = defaultStubs();
    globalThis.fetch = makeFetchStub((url, init) => {
      if (url.includes("/api/options/lots")) {
        lotCalls.push(url);
        return makeJsonResponse({ ok: true, lots: { "NSE:NIFTY25DEC25000CE": 65 } });
      }
      return stubs(url, init);
    });
    render(<Trade />, { wrapper: wrapper(makeQc()) });
    await waitFor(() => expect(screen.getByTestId("ticket-qty").textContent).toMatch(/\(65\/lot\)/));
    expect(decodeURIComponent(lotCalls[0])).toContain("NSE:NIFTY25DEC25000CE");
    const saved = JSON.parse(localStorage.getItem("trade:watchlists") ?? "null");
    expect(saved.lists[0].items[0].lot_size).toBe(65);
  });

  it("chain: dropping on another chart cell never moves the chain to that cell's stock", async () => {
    const leg = { symbol: "NSE:RELIANCE26OCT2500CE", ltp: 42, bid: 41.9, ask: 42.1, oi: 1000, volume: 10, ltpch: 0, lot_size: 500, tick_size: 0.05 };
    const chain = { ok: true, underlying: "RELIANCE", symbol: "", spot: 2450, expiries: [], selected_expiry: null, source: "fyers",
      strikes: [{ strike: 2500, ce: leg, pe: null }] };
    const tcs = { symbol: "NSE:TCS-EQ", short_name: "TCS", exchange: "NSE", segment: "EQ", instrument_type: "EQ", lot_size: 1, tick_size: 0.05, expiry: null, strike: null, underlying: null, display: "TCS" };
    localStorage.setItem("trade:layout", JSON.stringify("2"));
    localStorage.setItem("trade:cells", JSON.stringify([null, tcs]));
    const stubs = defaultStubs();
    globalThis.fetch = makeFetchStub((url, init) => (url.includes("/api/options/chain") ? makeJsonResponse(chain) : stubs(url, init)));
    const user = userEvent.setup();
    render(<Trade />, { wrapper: wrapper(makeQc()) });
    await user.type(await screen.findByTestId("trade-search"), "RELI");
    await user.click(await screen.findByTestId("search-row-NSE:RELIANCE-EQ"));
    if (!screen.queryByTestId("dock-chain")) await user.click(screen.getByTestId("rail-chain"));
    const price = await screen.findByTestId("chain-ce-2500");
    const store: Record<string, string> = {};
    const dataTransfer = { setData: (k: string, v: string) => { store[k] = v; }, getData: (k: string) => store[k] ?? "", get types() { return Object.keys(store); }, effectAllowed: "", dropEffect: "" };
    fireEvent.dragStart(price, { dataTransfer });
    const cell = await screen.findByTestId("tv-cell-1");
    fireEvent.dragOver(cell, { dataTransfer });
    fireEvent.drop(cell, { dataTransfer });
    await waitFor(() => expect(JSON.parse(localStorage.getItem("trade:last") ?? "null")?.symbol).toBe(leg.symbol));
    expect(JSON.parse(localStorage.getItem("trade:chainBase") ?? "null")?.symbol).toBe("NSE:RELIANCE-EQ");
    // the option ticket asks for lots, not quantity
    const lots = await screen.findByTestId("ticket-lots");
    fireEvent.change(lots, { target: { value: "3" } });
    expect(screen.getByTestId("ticket-qty").textContent).toContain("1500 qty");
  });

  it("ticket: F&O asks for lots; SL / target as price, points or %", async () => {
    const posts: Record<string, unknown>[] = [];
    const stubs = defaultStubs();
    globalThis.fetch = makeFetchStub((url, init) => {
      if (/\/api\/orders$/.test(url) && init?.method === "POST") posts.push(JSON.parse(String(init.body)));
      return stubs(url, init);
    });
    const user = userEvent.setup();
    render(<Trade />, { wrapper: wrapper(makeQc()) });
    await user.type(await screen.findByTestId("trade-search"), "RELI");
    await user.click(await screen.findByTestId("search-row-NSE:RELIANCE-EQ"));
    await waitFor(() => expect(screen.getByTestId("ticket-submit")).not.toBeDisabled());
    // ask = 2450.5 for a BUY: SL 1% below, target 20 points above
    await user.click(within(screen.getByTestId("ticket-sl-mode")).getByText("%"));
    await user.type(screen.getByTestId("ticket-sl"), "1");
    await user.click(within(screen.getByTestId("ticket-tp-mode")).getByText("pts"));
    await user.type(screen.getByTestId("ticket-tp"), "20");
    await user.click(screen.getByTestId("ticket-submit"));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({ stop_loss: 2426, target: 2470.5 });
  });

  it("does not render the option chain for a non-index symbol", async () => {
    globalThis.fetch = makeFetchStub(defaultStubs());
    const user = userEvent.setup();
    const qc = makeQc();
    render(<Trade />, { wrapper: wrapper(qc) });

    const search = await screen.findByTestId("trade-search");
    await user.type(search, "REL");
    const row = await screen.findByTestId("search-row-NSE:RELIANCE-EQ");
    await user.click(row);

    // No chain card for cash
    expect(screen.queryByTestId("trade-chain")).not.toBeInTheDocument();
  });

  it("cancels a pending order: fires POST /api/orders/cancel, shows feedback, and removes the row", async () => {
    // One pending order on the real account, broker_order_id = FX-12345.
    // The stub returns ok=true so the success message + row removal
    // path is exercised end-to-end.
    const cancelCalls: { url: string; method: string; body: unknown }[] = [];
    let pending = {
      ok: true, count: 1,
      orders: [{
        id: 42,
        broker_order_id: "FX-12345",
        broker_account_id: 7,
        symbol: "NSE:RELIANCE-EQ",
        side: "BUY", quantity: 1, price: null,
        order_type: "MARKET", status: "placed",
        created_at: "2026-06-13T10:00:00Z",
      }],
    };

    globalThis.fetch = vi.fn(async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method ?? "GET").toString();
      if (url.includes("/api/broker-accounts")) {
        return makeJsonResponse({ accounts: [REAL_ACCOUNT, PAPER_ACCOUNT] });
      }
      if (url.includes("/api/search/symbols")) {
        return makeJsonResponse(SEARCH_RELIANCE);
      }
      if (url.includes("/api/orders/quote")) {
        return makeJsonResponse({
          ok: true, symbol: "NSE:RELIANCE-EQ",
          last_price: 2450.0, bid: 2449.5, ask: 2450.5,
        });
      }
      if (url.includes("/api/orders/pending")) {
        return makeJsonResponse(pending);
      }
      if (url.includes("/api/positions")) return makeJsonResponse([]);
      if (url.includes("/api/search/option-chain")) {
        return makeJsonResponse({ ok: true, underlying: "RELIANCE",
          expiries: [], selected_expiry: null, spot: null, strikes: [] });
      }
      if (url.includes("/api/orders/cancel") && method === "POST") {
        const body = init?.body ? JSON.parse(String(init.body)) : null;
        cancelCalls.push({ url, method, body });
        // Broker confirms cancel; subsequent /pending returns empty.
        pending = { ok: true, count: 0, orders: [] };
        return makeJsonResponse({
          ok: true, broker_order_id: body?.broker_order_id, reason: "cancelled",
        });
      }
      return makeJsonResponse({}, 404);
    });

    const user = userEvent.setup();
    const qc = makeQc();
    render(<Trade />, { wrapper: wrapper(qc) });

    const cancelBtn = await screen.findByTestId("cancel-FX-12345");
    await user.click(cancelBtn);

    // The cancel POST was sent with the right body.
    await waitFor(() => expect(cancelCalls.length).toBe(1));
    const call = cancelCalls[0];
    expect(call.method).toBe("POST");
    expect(call.url).toMatch(/\/api\/orders\/cancel/);
    expect(call.body).toEqual({ account_id: 7, broker_order_id: "FX-12345" });

    // The UI shows a success banner and the row disappears.
    const banner = await screen.findByTestId("cancel-result");
    expect(banner.textContent).toMatch(/Cancelled FX-12345/);
    await waitFor(() =>
      expect(screen.queryByTestId("cancel-FX-12345")).not.toBeInTheDocument(),
    );
  });

  it("surfaces an info banner when the broker rejects the cancel (order already gone)", async () => {
    // The operator clicks Cancel, the order is no longer on the broker
    // (already filled, already cancelled, or never made it). The
    // backend should still drop it from the local pending list and
    // the UI should say so — otherwise the click looks like a no-op.
    const cancelCalls: { url: string; method: string; body: unknown }[] = [];
    let pending = {
      ok: true, count: 1,
      orders: [{
        id: 42,
        broker_order_id: "FX-99999",
        broker_account_id: 7,
        symbol: "NSE:RELIANCE-EQ",
        side: "BUY", quantity: 1, price: null,
        order_type: "MARKET", status: "placed",
        created_at: "2026-06-13T10:00:00Z",
      }],
    };

    globalThis.fetch = vi.fn(async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method ?? "GET").toString();
      if (url.includes("/api/broker-accounts")) {
        return makeJsonResponse({ accounts: [REAL_ACCOUNT, PAPER_ACCOUNT] });
      }
      if (url.includes("/api/search/symbols")) {
        return makeJsonResponse(SEARCH_RELIANCE);
      }
      if (url.includes("/api/orders/quote")) {
        return makeJsonResponse({
          ok: true, symbol: "NSE:RELIANCE-EQ",
          last_price: 2450.0, bid: 2449.5, ask: 2450.5,
        });
      }
      if (url.includes("/api/orders/pending")) {
        return makeJsonResponse(pending);
      }
      if (url.includes("/api/positions")) return makeJsonResponse([]);
      if (url.includes("/api/search/option-chain")) {
        return makeJsonResponse({ ok: true, underlying: "RELIANCE",
          expiries: [], selected_expiry: null, spot: null, strikes: [] });
      }
      if (url.includes("/api/orders/cancel") && method === "POST") {
        const body = init?.body ? JSON.parse(String(init.body)) : null;
        cancelCalls.push({ url, method, body });
        // Broker said "no"; the backend checked and the order was
        // already cancelled there, so the local row is gone.
        pending = { ok: true, count: 0, orders: [] };
        return makeJsonResponse({
          ok: false,
          broker_order_id: body?.broker_order_id,
          reason: "already_gone",
          message: "the order was already cancelled at the broker",
        });
      }
      return makeJsonResponse({}, 404);
    });

    const user = userEvent.setup();
    const qc = makeQc();
    render(<Trade />, { wrapper: wrapper(qc) });

    const cancelBtn = await screen.findByTestId("cancel-FX-99999");
    await user.click(cancelBtn);

    await waitFor(() => expect(cancelCalls.length).toBe(1));
    const banner = await screen.findByTestId("cancel-result");
    expect(banner.textContent).toMatch(/already cancelled/i);
    // The local pending list refreshes and the row is gone.
    await waitFor(() =>
      expect(screen.queryByTestId("cancel-FX-99999")).not.toBeInTheDocument(),
    );
  });

  it("shows an error banner when the cancel request itself fails (e.g. 5xx)", async () => {
    let pending = {
      ok: true, count: 1,
      orders: [{
        id: 42,
        broker_order_id: "FX-55555",
        broker_account_id: 7,
        symbol: "NSE:RELIANCE-EQ",
        side: "BUY", quantity: 1, price: null,
        order_type: "MARKET", status: "placed",
        created_at: "2026-06-13T10:00:00Z",
      }],
    };

    globalThis.fetch = vi.fn(async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method ?? "GET").toString();
      if (url.includes("/api/broker-accounts")) {
        return makeJsonResponse({ accounts: [REAL_ACCOUNT, PAPER_ACCOUNT] });
      }
      if (url.includes("/api/search/symbols")) {
        return makeJsonResponse(SEARCH_RELIANCE);
      }
      if (url.includes("/api/orders/quote")) {
        return makeJsonResponse({
          ok: true, symbol: "NSE:RELIANCE-EQ",
          last_price: 2450.0, bid: 2449.5, ask: 2450.5,
        });
      }
      if (url.includes("/api/orders/pending")) {
        return makeJsonResponse(pending);
      }
      if (url.includes("/api/positions")) return makeJsonResponse([]);
      if (url.includes("/api/search/option-chain")) {
        return makeJsonResponse({ ok: true, underlying: "RELIANCE",
          expiries: [], selected_expiry: null, spot: null, strikes: [] });
      }
      if (url.includes("/api/orders/cancel") && method === "POST") {
        return makeJsonResponse(
          { detail: "broker unreachable" },
          502,
        );
      }
      return makeJsonResponse({}, 404);
    });

    const user = userEvent.setup();
    const qc = makeQc();
    render(<Trade />, { wrapper: wrapper(qc) });

    const cancelBtn = await screen.findByTestId("cancel-FX-55555");
    await user.click(cancelBtn);

    const banner = await screen.findByTestId("cancel-result");
    expect(banner.textContent).toMatch(/Cancel failed/);
    // The original row is still there (cancel did not happen).
    expect(screen.queryByTestId("cancel-FX-55555")).toBeInTheDocument();
  });

  it("surfaces a 500 with a plain-text body without crashing the fetch wrapper", async () => {
    // Regression: when the backend crashed (unhandled exception in
    // the cancel handler), FastAPI returned a 500 with body
    // "Internal Server Error" and `Content-Type: text/plain`. The
    // old `api/client.ts` called `res.json()` first (which threw
    // because the body wasn't JSON, *and* consumed the body), then
    // tried `res.text()` in the catch and exploded with
    // "Failed to execute 'text' on 'Response': body stream already
    // read". The user saw a confusing wrapper error instead of the
    // real reason. The new client reads the body as text once.
    let pending = {
      ok: true, count: 1,
      orders: [{
        id: 42,
        broker_order_id: "FX-66666",
        broker_account_id: 7,
        symbol: "NSE:RELIANCE-EQ",
        side: "BUY", quantity: 1, price: null,
        order_type: "MARKET", status: "placed",
        created_at: "2026-06-13T10:00:00Z",
      }],
    };

    globalThis.fetch = vi.fn(async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method ?? "GET").toString();
      if (url.includes("/api/broker-accounts")) {
        return makeJsonResponse({ accounts: [REAL_ACCOUNT, PAPER_ACCOUNT] });
      }
      if (url.includes("/api/search/symbols")) {
        return makeJsonResponse(SEARCH_RELIANCE);
      }
      if (url.includes("/api/orders/quote")) {
        return makeJsonResponse({
          ok: true, symbol: "NSE:RELIANCE-EQ",
          last_price: 2450.0, bid: 2449.5, ask: 2450.5,
        });
      }
      if (url.includes("/api/orders/pending")) {
        return makeJsonResponse(pending);
      }
      if (url.includes("/api/positions")) return makeJsonResponse([]);
      if (url.includes("/api/search/option-chain")) {
        return makeJsonResponse({ ok: true, underlying: "RELIANCE",
          expiries: [], selected_expiry: null, spot: null, strikes: [] });
      }
      if (url.includes("/api/orders/cancel") && method === "POST") {
        // FastAPI's default 500 looks exactly like this.
        return new Response("Internal Server Error", {
          status: 500,
          headers: { "Content-Type": "text/plain; charset=utf-8" },
        });
      }
      return makeJsonResponse({}, 404);
    });

    const user = userEvent.setup();
    const qc = makeQc();
    render(<Trade />, { wrapper: wrapper(qc) });

    const cancelBtn = await screen.findByTestId("cancel-FX-66666");
    await user.click(cancelBtn);

    // The UI should show the cancel error banner with the real
    // backend text — not the wrapper "body stream already read"
    // error from a half-consumed response.
    const banner = await screen.findByTestId("cancel-result");
    expect(banner.textContent).toMatch(/Cancel failed/);
    // The text of the response should appear, not "body stream".
    expect(banner.textContent).not.toMatch(/body stream/i);
  });

  // ---- TradingView-style chart page ----

  async function openReliance(user: ReturnType<typeof userEvent.setup>) {
    render(<Trade />, { wrapper: wrapper(makeQc()) });
    await user.type(await screen.findByTestId("trade-search"), "RELI");
    await user.click(await screen.findByTestId("search-row-NSE:RELIANCE-EQ"));
    await screen.findByTestId("trade-chart");
  }

  it("chart: the indicator picker adds an instance to the legend and its settings open", async () => {
    globalThis.fetch = makeFetchStub(defaultStubs());
    const user = userEvent.setup();
    await openReliance(user);
    await user.click(screen.getByTestId("chart-indicators-btn"));
    await user.click(await screen.findByTestId("chart-ind-macd"));
    await user.keyboard("{Escape}");
    expect(await screen.findByTestId("legend-macd")).toBeInTheDocument();
    await user.click(screen.getByTestId("ind-settings-macd"));
    expect(await screen.findByTestId("ind-settings")).toBeInTheDocument();
    await user.click(screen.getByTestId("ind-settings-ok"));
    // persisted for the next visit
    const prefs = JSON.parse(localStorage.getItem("chart:prefs") ?? "{}");
    expect(prefs.indicators.map((i: { type: string }) => i.type)).toContain("macd");
  });

  it("chart: an alert created in the dialog is saved for the symbol", async () => {
    globalThis.fetch = makeFetchStub(defaultStubs());
    const user = userEvent.setup();
    await openReliance(user);
    await user.click(screen.getByTestId("chart-alerts-btn"));
    await user.click(await screen.findByTestId("chart-alert-create"));
    await user.selectOptions(await screen.findByLabelText("Condition"), "crossUp");
    await user.selectOptions(screen.getByLabelText("Trigger"), "oncePerBar");
    await user.click(screen.getByTestId("alert-save"));
    await waitFor(() => {
      const saved = JSON.parse(localStorage.getItem("chart:alerts:NSE:RELIANCE-EQ") ?? "[]");
      expect(saved).toHaveLength(1);
      expect(saved[0]).toMatchObject({ cond: "crossUp", trigger: "oncePerBar", active: true });
    });
  });

  it("chart: settings changes persist and the interval menu switches intervals", async () => {
    globalThis.fetch = makeFetchStub(defaultStubs());
    const user = userEvent.setup();
    await openReliance(user);
    await user.click(screen.getByTestId("chart-settings-btn"));
    await user.click(await screen.findByRole("tab", { name: "Canvas" }));
    await user.selectOptions(screen.getByLabelText("Grid lines"), "horz");
    await user.click(screen.getByTestId("chart-settings-ok"));
    expect(JSON.parse(localStorage.getItem("chart:settings") ?? "{}").grid).toBe("horz");
    await user.click(screen.getByTestId("chart-tf"));
    await user.click(await screen.findByTestId("chart-ivm-75"));
    await waitFor(() => expect(JSON.parse(localStorage.getItem("chart:prefs") ?? "{}").interval).toBe("75"));
  });

  it("account manager: lists open positions with exit / reverse actions", async () => {
    localStorage.setItem("trade:bottomTab", JSON.stringify("positions"));
    globalThis.fetch = makeFetchStub(defaultStubs({
      positions: [{ id: 1, symbol: "NSE:RELIANCE-EQ", quantity: 10, average_price: 2440, last_price: 2450, unrealized_pnl: 100, strategy_id: null, opened_at: "", updated_at: "" }],
    }));
    render(<Trade />, { wrapper: wrapper(makeQc()) });
    const panel = await screen.findByTestId("trade-positions");
    await waitFor(() => expect(panel.textContent).toMatch(/NSE:RELIANCE-EQ/));
    expect(screen.getByText("Exit all")).toBeInTheDocument();
    expect(screen.getByTitle("Reverse at market")).toBeInTheDocument();
  });

  it("cancel the broker refused: says the order may still be working and keeps the row", async () => {
    const pending = {
      ok: true, count: 1,
      orders: [{
        id: 43, broker_order_id: "FX-77777", broker_account_id: 7, symbol: "NSE:RELIANCE-EQ",
        side: "BUY", quantity: 1, price: 2400, order_type: "LIMIT", status: "placed",
        created_at: "2026-06-13T10:00:00Z",
      }],
    };
    const stubs = defaultStubs({ pending });
    globalThis.fetch = makeFetchStub((url, init) => {
      if (url.includes("/api/orders/cancel") && init?.method === "POST") {
        return makeJsonResponse({
          ok: false, broker_order_id: "FX-77777", reason: "broker_refused", rows_updated: 0,
          message: "the broker didn't cancel the order and it may still be working — check the Orders tab and try again",
        });
      }
      return stubs(url, init);
    });
    const user = userEvent.setup();
    render(<Trade />, { wrapper: wrapper(makeQc()) });
    await user.click(await screen.findByTestId("cancel-FX-77777"));
    const banner = await screen.findByTestId("cancel-result");
    expect(banner.textContent).toMatch(/Not cancelled/);
    expect(banner.textContent).toMatch(/may still be working/);
    expect(banner.className).toMatch(/error/);
    expect(screen.getByTestId("cancel-FX-77777")).toBeInTheDocument();
  });

  const OPT = (symbol: string, name: string, lot: number) => ({
    symbol, short_name: name, exchange: "NSE", segment: "FO", instrument_type: "CE",
    lot_size: lot, tick_size: 0.05, expiry: null, strike: 50000, underlying: name.split(" ")[0], display: name,
  });
  const NIFTY_CE = OPT("NSE:NIFTY26JUN24000CE", "NIFTY 24000 CE", 65);
  const BANK_CE = OPT("NSE:BANKNIFTY26JUN51000CE", "BANKNIFTY 51000 CE", 30);

  function searchStubs() {
    const stubs = defaultStubs();
    const posts: Record<string, unknown>[] = [];
    globalThis.fetch = makeFetchStub((url, init) => {
      if (url.includes("/api/search/symbols")) {
        const q = new URL(url, "http://x").searchParams.get("q") ?? "";
        const hits = /^BANK/i.test(q) ? [BANK_CE] : /^NIF/i.test(q) ? [NIFTY_CE] : SEARCH_RELIANCE.hits;
        return makeJsonResponse({ ok: true, count: hits.length, hits });
      }
      if (/\/api\/orders$/.test(url) && init?.method === "POST") posts.push(JSON.parse(String(init.body)));
      return stubs(url, init);
    });
    return posts;
  }

  async function pick(user: ReturnType<typeof userEvent.setup>, q: string, symbol: string) {
    const box = await screen.findByTestId("trade-search");
    await user.clear(box);
    await user.type(box, q);
    await user.click(await screen.findByTestId(`search-row-${symbol}`));
  }

  it("ticket: quantity snaps to the new lot when switching options, and back to shares for cash", async () => {
    searchStubs();
    const user = userEvent.setup();
    render(<Trade />, { wrapper: wrapper(makeQc()) });

    await pick(user, "NIF", NIFTY_CE.symbol);
    await waitFor(() => expect(screen.getByTestId("ticket-qty").textContent).toMatch(/= 65 qty/));
    // NIFTY's 65 is not a BANKNIFTY lot (30): it must become one lot, not stay 65.
    await pick(user, "BANK", BANK_CE.symbol);
    await waitFor(() => expect(screen.getByTestId("ticket-qty").textContent).toMatch(/= 30 qty \(30\/lot\)/));
    // Back to a cash stock: the default quantity, not 30 shares.
    await pick(user, "RELI", "NSE:RELIANCE-EQ");
    await waitFor(() => expect((screen.getByTestId("ticket-qty") as HTMLInputElement).value).toBe("1"));
  });

  it("ticket: a limit price typed for one symbol is cleared when another is picked", async () => {
    searchStubs();
    const user = userEvent.setup();
    render(<Trade />, { wrapper: wrapper(makeQc()) });

    await pick(user, "RELI", "NSE:RELIANCE-EQ");
    await user.selectOptions(screen.getByTestId("ticket-type"), "LIMIT");
    await user.type(screen.getByTestId("ticket-limit"), "2450");
    await user.type(screen.getByTestId("ticket-sl"), "2400");

    await pick(user, "BANK", BANK_CE.symbol);
    await user.selectOptions(screen.getByTestId("ticket-type"), "LIMIT");
    expect((screen.getByTestId("ticket-limit") as HTMLInputElement).value).toBe("");
    expect((screen.getByTestId("ticket-sl") as HTMLInputElement).value).toBe("");
    expect(screen.getByTestId("ticket-submit")).toBeDisabled();
  });

  it("ticket: limit price is sent on the instrument's tick grid", async () => {
    const posts = searchStubs();
    const user = userEvent.setup();
    render(<Trade />, { wrapper: wrapper(makeQc()) });

    await pick(user, "RELI", "NSE:RELIANCE-EQ");
    await user.selectOptions(screen.getByTestId("ticket-type"), "LIMIT");
    await user.type(screen.getByTestId("ticket-limit"), "2450.03");
    await waitFor(() => expect(screen.getByTestId("ticket-submit")).not.toBeDisabled());
    await user.click(screen.getByTestId("ticket-submit"));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].limit_price).toBe(2450.05);
  });

  it("an order Fyers never confirmed is shown as an error with a check-before-retry warning", async () => {
    globalThis.fetch = makeFetchStub(defaultStubs({
      placeResponse: {
        ok: false, blocked: false, risk_codes: [], risk_message: "", broker_order_id: "",
        status: "UNCONFIRMED",
        error: "Fyers didn't confirm this order (transport error). It may still have been placed — check the Orders tab or the Fyers order book before placing it again.",
      },
    }));
    const user = userEvent.setup();
    render(<Trade />, { wrapper: wrapper(makeQc()) });
    await user.type(await screen.findByTestId("trade-search"), "RELI");
    await user.click(await screen.findByTestId("search-row-NSE:RELIANCE-EQ"));
    await waitFor(() => expect(screen.getByTestId("ticket-submit")).not.toBeDisabled());
    await user.click(screen.getByTestId("ticket-submit"));
    const err = await screen.findByTestId("ticket-result-error");
    expect(err.textContent).toMatch(/before placing it again/);
    expect(screen.queryByTestId("ticket-result-success")).not.toBeInTheDocument();
  });

  it("ticket: the quantity box can be cleared and retyped", async () => {
    const posts = searchStubs();
    const user = userEvent.setup();
    render(<Trade />, { wrapper: wrapper(makeQc()) });
    await pick(user, "RELI", "NSE:RELIANCE-EQ");
    const qty = screen.getByTestId("ticket-qty") as HTMLInputElement;
    // Backspacing the "1" used to snap straight back to 1, so this sent 15.
    await user.clear(qty);
    await user.type(qty, "5");
    expect(qty.value).toBe("5");
    await waitFor(() => expect(screen.getByTestId("ticket-submit")).not.toBeDisabled());
    await user.click(screen.getByTestId("ticket-submit"));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].quantity).toBe(5);
  });

  it("ticket: a stop loss above a BUY's entry blocks PLACE and says why", async () => {
    const posts = searchStubs();
    const user = userEvent.setup();
    render(<Trade />, { wrapper: wrapper(makeQc()) });
    await pick(user, "RELI", "NSE:RELIANCE-EQ");
    await user.selectOptions(screen.getByTestId("ticket-type"), "LIMIT");
    await user.type(screen.getByTestId("ticket-limit"), "2450");
    await user.type(screen.getByTestId("ticket-sl"), "2500");
    expect(screen.getByTestId("ticket-submit")).toBeDisabled();
    expect(screen.getByTestId("ticket-problem").textContent).toMatch(/wrong side.*below/);
    await user.clear(screen.getByTestId("ticket-sl"));
    await user.type(screen.getByTestId("ticket-sl"), "2400.02");
    await waitFor(() => expect(screen.getByTestId("ticket-submit")).not.toBeDisabled());
    await user.click(screen.getByTestId("ticket-submit"));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].stop_loss).toBe(2400);    // on the tick grid
  });

  it("ticket: the risk override re-sends the blocked order, not the edited ticket", async () => {
    const posts: Record<string, unknown>[] = [];
    const blocked = { ok: false, blocked: true, risk_codes: ["X"], risk_message: "X", broker_order_id: null, status: "REJECTED_RISK", error: "Blocked by risk limits: X.", reason: "risk_block" };
    const placed = { ok: true, blocked: false, risk_codes: [], risk_message: "", broker_order_id: "FY-1", status: "PENDING", error: null };
    const base = defaultStubs();
    globalThis.fetch = makeFetchStub((url, init) => {
      if (/\/api\/orders$/.test(url) && init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        posts.push(body);
        return makeJsonResponse(body.bypass_risk ? placed : blocked);
      }
      return base(url, init);
    });
    const user = userEvent.setup();
    render(<Trade />, { wrapper: wrapper(makeQc()) });
    await user.type(await screen.findByTestId("trade-search"), "RELI");
    await user.click(await screen.findByTestId("search-row-NSE:RELIANCE-EQ"));
    await waitFor(() => expect(screen.getByTestId("ticket-submit")).not.toBeDisabled());
    await user.click(screen.getByTestId("ticket-submit"));
    const overrideBtn = await screen.findByTestId("ticket-risk-override-btn");
    await user.click(screen.getByTestId("ticket-side-sell"));   // the ticket changes after the block
    await user.click(overrideBtn);
    await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[1]).toMatchObject({ side: "BUY", bypass_risk: true });
    const ok = await screen.findByTestId("ticket-result-success");
    expect(ok.textContent).toMatch(/BUY 1/);
    expect(ok.textContent).toMatch(/order FY-1/);
    expect(ok.textContent).not.toMatch(/@ FY-1/);
  });

  it("search: clicking outside the search box closes its results", async () => {
    globalThis.fetch = makeFetchStub(defaultStubs());
    const user = userEvent.setup();
    render(<Trade />, { wrapper: wrapper(makeQc()) });
    await user.type(await screen.findByTestId("trade-search"), "RELI");
    await screen.findByTestId("search-row-NSE:RELIANCE-EQ");
    fireEvent.mouseDown(document.body);
    await waitFor(() => expect(screen.queryByTestId("trade-search-results")).not.toBeInTheDocument());
  });

  it("search: a failed search says so instead of 'no results'", async () => {
    const stubs = defaultStubs();
    globalThis.fetch = makeFetchStub((url, init) => (url.includes("/api/search/symbols") ? makeJsonResponse({ detail: "boom" }, 500) : stubs(url, init)));
    const user = userEvent.setup();
    render(<Trade />, { wrapper: wrapper(makeQc()) });
    await user.type(await screen.findByTestId("trade-search"), "RELI");
    const box = await screen.findByTestId("trade-search-results");
    await waitFor(() => expect(box.textContent).toMatch(/Search failed/));
    expect(box.textContent).not.toMatch(/no results/);
  });

  describe("watchlist", () => {
    const hit = (s: string) => ({ symbol: `NSE:${s}-EQ`, short_name: s, exchange: "NSE", segment: "EQ", instrument_type: "EQ",
      lot_size: 1, tick_size: 0.05, expiry: null, strike: null, underlying: null, display: s });
    const quotesStub = (quotes: Record<string, unknown>) => {
      const stubs = defaultStubs();
      return makeFetchStub((url, init) => (url.includes("/api/market/quotes") ? makeJsonResponse({ quotes }) : stubs(url, init)));
    };

    it("drops duplicate and broken rows saved by an older build", async () => {
      localStorage.setItem("trade:watchlists", JSON.stringify({ active: 0, lists: [{ name: "Watchlist", items: [hit("TCS"), hit("TCS"), { short_name: "x" }, { symbol: "NSE:INFY-EQ" }] }] }));
      globalThis.fetch = quotesStub({});
      render(<Trade />, { wrapper: wrapper(makeQc()) });
      expect(await screen.findAllByTestId("recent-chip-NSE:TCS-EQ")).toHaveLength(1);
      expect(screen.getByTestId("recent-chip-NSE:INFY-EQ").textContent).toContain("INFY");
    });

    it("removing a row drops its flag, so re-adding starts clean", async () => {
      localStorage.setItem("trade:watchlists", JSON.stringify({ active: 0, lists: [{ name: "Watchlist", items: [hit("TCS")], flags: { "NSE:TCS-EQ": "#F23645" } }] }));
      globalThis.fetch = quotesStub({});
      render(<Trade />, { wrapper: wrapper(makeQc()) });
      const row = await screen.findByTestId("recent-chip-NSE:TCS-EQ");
      fireEvent.click(within(row).getByRole("button", { name: /Remove TCS/ }));
      await waitFor(() => expect(screen.queryByTestId("recent-chip-NSE:TCS-EQ")).not.toBeInTheDocument());
      const saved = JSON.parse(localStorage.getItem("trade:watchlists") ?? "null");
      expect(saved.lists[0].flags).toEqual({});
    });

    it("sorting by price keeps symbols without a quote at the bottom", async () => {
      localStorage.setItem("trade:watchlists", JSON.stringify({ active: 0, lists: [{ name: "Watchlist", items: [hit("AAA"), hit("BBB"), hit("CCC"), hit("DDD")] }] }));
      globalThis.fetch = quotesStub({ "NSE:BBB-EQ": { ltp: 10, change: 1, change_pct: 1 }, "NSE:DDD-EQ": { ltp: 20, change: 1, change_pct: 1 } });
      render(<Trade />, { wrapper: wrapper(makeQc()) });
      const order = () => screen.getAllByTestId(/^recent-chip-/).map((r) => r.getAttribute("data-testid")!.replace("recent-chip-NSE:", "").replace("-EQ", ""));
      await waitFor(() => expect(screen.getByTestId("recent-chip-NSE:DDD-EQ").textContent).toContain("20.00"));
      const sortBtn = screen.getByRole("button", { name: /^Last/ });
      fireEvent.click(sortBtn);
      expect(order()).toEqual(["BBB", "DDD", "AAA", "CCC"]);
      fireEvent.click(sortBtn);
      expect(order()).toEqual(["DDD", "BBB", "AAA", "CCC"]);
    });
  });

  it("chain: an option picked off the chain carries its expiry; a lapsed expiry falls back to the nearest", async () => {
    const leg = { symbol: "NSE:RELIANCE26OCT2500CE", ltp: 42, bid: 41.9, ask: 42.1, oi: 1000, volume: 10, ltpch: 0, lot_size: 500, tick_size: 0.05 };
    const expiries = [{ label: "27-10-2026", ts: "1793097000" }, { label: "24-11-2026", ts: "1795516200" }];
    const asked: string[] = [];
    const stubs = defaultStubs();
    globalThis.fetch = makeFetchStub((url, init) => {
      if (!url.includes("/api/options/chain")) return stubs(url, init);
      const exp = new URL(url, "http://x").searchParams.get("expiry");
      asked.push(exp ?? "");
      // The second expiry lapses: the chain stops listing it and has no strikes for it.
      const lapsed = exp === "1795516200";
      const listed = lapsed ? expiries.slice(0, 1) : expiries;
      return makeJsonResponse({ ok: true, underlying: "RELIANCE", symbol: "NSE:RELIANCE-EQ", spot: 2450, expiries: listed,
        selected_expiry: listed[0].ts, source: "fyers", reason: null, strikes: lapsed ? [] : [{ strike: 2500, ce: leg, pe: null }] });
    });
    const user = userEvent.setup();
    render(<Trade />, { wrapper: wrapper(makeQc()) });
    await user.type(await screen.findByTestId("trade-search"), "RELI");
    await user.click(await screen.findByTestId("search-row-NSE:RELIANCE-EQ"));
    if (!screen.queryByTestId("dock-chain")) await user.click(screen.getByTestId("rail-chain"));
    await user.click(await screen.findByTestId("chain-ce-2500"));
    await waitFor(() => expect(JSON.parse(localStorage.getItem("trade:last") ?? "null")?.symbol).toBe(leg.symbol));
    const picked = JSON.parse(localStorage.getItem("trade:last") ?? "null");
    expect(picked.expiry).toBe("2026-10-27");
    expect(picked.underlying).toBe("RELIANCE");
    expect(picked.display).toContain("27-10-2026");

    await user.selectOptions(screen.getByTestId("chain-expiry"), "1795516200");
    await waitFor(() => expect(asked).toContain("1795516200"));
    // Back on the nearest expiry's ladder rather than stuck on the lapsed one.
    expect(await screen.findByTestId("chain-ce-2500")).toBeInTheDocument();
    expect((screen.getByTestId("chain-expiry") as HTMLSelectElement).value).toBe("1793097000");
  });
});

describe("ticket checks", () => {
  const base: TicketCheck = { side: "BUY", orderType: "MARKET", quantity: 1, lotSize: 1, limit: null, stop: null, ltp: 100, entry: 100, sl: null, tp: null };

  it("passes a plain market order", () => {
    expect(ticketProblem(base)).toBeNull();
  });
  it("wants a whole lot for F&O", () => {
    expect(ticketProblem({ ...base, quantity: 40, lotSize: 30 })).toMatch(/multiple of the lot size \(30\)/);
    expect(ticketProblem({ ...base, quantity: 60, lotSize: 30 })).toBeNull();
  });
  it("needs the prices its order type uses", () => {
    expect(ticketProblem({ ...base, orderType: "LIMIT" })).toMatch(/limit price/);
    expect(ticketProblem({ ...base, orderType: "SL-M" })).toMatch(/trigger/);
    expect(ticketProblem({ ...base, orderType: "STOP_LOSS", limit: 101 })).toMatch(/trigger/);
  });
  it("refuses a stop-limit whose limit is on the wrong side of its trigger", () => {
    expect(ticketProblem({ ...base, orderType: "STOP_LOSS", stop: 105, limit: 104 })).toMatch(/at or above the trigger/);
    expect(ticketProblem({ ...base, orderType: "STOP_LOSS", stop: 105, limit: 106, entry: 106 })).toBeNull();
    expect(ticketProblem({ ...base, side: "SELL", orderType: "STOP_LOSS", stop: 95, limit: 96 })).toMatch(/at or below the trigger/);
  });
  it("keeps the stop loss and target on the right side of the entry", () => {
    expect(ticketProblem({ ...base, sl: 101 })).toMatch(/Stop loss .* below/);
    expect(ticketProblem({ ...base, tp: 99 })).toMatch(/Target .* above/);
    expect(ticketProblem({ ...base, side: "SELL", sl: 99 })).toMatch(/Stop loss .* above/);
    expect(ticketProblem({ ...base, side: "SELL", sl: 102, tp: 95 })).toBeNull();
  });
  it("warns when a stop's trigger is already through the market", () => {
    expect(ticketWarning({ ...base, orderType: "SL-M", stop: 99 })).toMatch(/BUY stop triggers above/);
    expect(ticketWarning({ ...base, orderType: "SL-M", stop: 101 })).toBeNull();
    expect(ticketWarning({ ...base, side: "SELL", orderType: "SL-M", stop: 101 })).toMatch(/SELL stop/);
  });
  it("puts a typed stop / target price on the tick grid", () => {
    expect(levelPrice(2400.02, "price", 2450, "BUY", "sl", 0.05)).toBe(2400);
    expect(levelPrice(101.13, "price", null, "BUY", "target", 0.1)).toBe(101.1);
  });
});
