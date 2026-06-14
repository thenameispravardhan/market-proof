// Trade page tests — render the page, drive the search and ticket,
// and verify the place-order flow against a stubbed backend.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import Trade from "../pages/Trade";

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

  it("shows a warning when there are no live accounts", async () => {
    globalThis.fetch = makeFetchStub((url) => {
      if (url.includes("/api/broker-accounts")) return makeJsonResponse({ accounts: [PAPER_ACCOUNT] });
      if (url.includes("/api/orders/pending")) return makeJsonResponse({ ok: true, count: 0, orders: [] });
      if (url.includes("/api/positions")) return makeJsonResponse([]);
      return makeJsonResponse({ ok: true, count: 0, hits: [] }, 200);
    });
    const qc = makeQc();
    render(<Trade />, { wrapper: wrapper(qc) });
    expect(await screen.findByText(/No live broker account/i)).toBeInTheDocument();
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

  it("places a manual order despite a risk warning (advisory, never blocked)", async () => {
    // Risk is advisory for manual orders: the backend still places the
    // order and returns a risk_warning the UI shows — no block, no bypass.
    const placeResponse = {
      ok: true,
      blocked: false,
      bypassed_risk: false,
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

    // No bypass UI exists anymore.
    expect(screen.queryByTestId("ticket-bypass")).toBeNull();
    // The order succeeded and the advisory warning is surfaced.
    const ok = await screen.findByTestId("ticket-result-success");
    expect(ok.textContent).toMatch(/PENDING/);
    const advisory = await screen.findByTestId("ticket-risk-advisory");
    expect(advisory.textContent).toMatch(/RISK_MAX_SINGLE_POSITION_PCT/);
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
        // Backend says: broker said "no", but the local row is gone.
        pending = { ok: true, count: 0, orders: [] };
        return makeJsonResponse({
          ok: false,
          broker_order_id: body?.broker_order_id,
          reason: "broker_rejected_already_gone",
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
    expect(banner.textContent).toMatch(/already gone/i);
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
});
