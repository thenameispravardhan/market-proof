// Dashboard widget tests. We mock /api/dashboard/summary and the
// resource endpoints with MSW-style fetch stubs, then assert the
// expected widgets render with the data we fed in. We don't load
// recharts end-to-end in jsdom (it works but adds a lot of layout
// thrash); we just assert the page renders the expected widget
// testids.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import Dashboard from "../pages/Dashboard";

function makeFetchStub(handler: (url: string) => Response | Promise<Response>) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    return handler(url);
  });
}

function wrapper(qc: QueryClient) {
  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

function makeJsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("Dashboard", () => {
  let originalFetch: typeof fetch;

  beforeEach(() => {
    originalFetch = globalThis.fetch;
  });
  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("renders the dashboard widgets (no Risk Metrics) and shows announcement data when the API responds and shows announcement data when the API responds", async () => {
    globalThis.fetch = makeFetchStub((url) => {
      if (url.includes("/api/announcements/recent")) {
        return makeJsonResponse([
          {
            id: 1,
            symbol: "RELIANCE",
            exchange: "BSE",
            event_type: "earnings",
            headline: "Q4 results",
            body: null,
            pdf_url: "https://example.com/r.pdf",
            source: "BSE",
            filed_at: "2026-06-10T10:00:00Z",
            received_at: "2026-06-10T10:00:00Z",
          },
        ]);
      }
      if (url.includes("/api/analyses/recent")) return makeJsonResponse([]);
      if (url.includes("/api/signals/recent")) return makeJsonResponse([]);
      if (url.includes("/api/positions")) return makeJsonResponse([]);
      if (url.includes("/api/trades")) return makeJsonResponse([]);
      if (url.includes("/api/dashboard/summary")) return makeJsonResponse({}, 404);
      return makeJsonResponse({}, 404);
    });

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchInterval: false } },
    });
    render(<Dashboard />, { wrapper: wrapper(qc) });

    // The widgets render with their testids; Risk Metrics was removed.
    expect(screen.getByTestId("news-pipeline")).toBeInTheDocument();
    expect(screen.getByTestId("active-positions")).toBeInTheDocument();
    expect(screen.queryByTestId("risk-metrics")).not.toBeInTheDocument();
    expect(screen.getByTestId("pnl-chart")).toBeInTheDocument();

    // The pipeline widget shows the symbol from the mock.
    await waitFor(() => {
      expect(screen.getByText("RELIANCE")).toBeInTheDocument();
    });
  });

  it("renders a graceful empty state when each endpoint 404s", async () => {
    globalThis.fetch = makeFetchStub(() => makeJsonResponse({ detail: "not found" }, 404));

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchInterval: false } },
    });
    render(<Dashboard />, { wrapper: wrapper(qc) });

    await waitFor(() => {
      // Each widget that hits a 404 shows its own empty-state copy.
      const empty = screen.getAllByText(/Endpoint not available yet/i);
      expect(empty.length).toBeGreaterThan(0);
    });
  });

  it("shows today's P&L in rupees in the stat row from the summary endpoint", async () => {
    globalThis.fetch = makeFetchStub((url) => {
      if (url.includes("/api/dashboard/summary")) {
        return makeJsonResponse({
          open_positions: 2,
          todays_realized_pnl: 1234.5,
          todays_unrealized_pnl: -42.25,
          pnl_series: [],
        });
      }
      return makeJsonResponse([], 404);
    });

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchInterval: false } },
    });
    render(<Dashboard />, { wrapper: wrapper(qc) });

    expect(await screen.findByText("+₹1,234.50")).toBeInTheDocument();
    expect(screen.getByText("−₹42.25")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
  });

  it("asks before squaring off and does nothing when cancelled", async () => {
    const posts: string[] = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (init?.method === "POST") {
        posts.push(url);
        return makeJsonResponse({ ok: true });
      }
      if (url.includes("/api/positions/managed")) return makeJsonResponse([]);
      if (url.includes("/api/positions")) {
        return makeJsonResponse([
          {
            id: 1, symbol: "TCS", quantity: 10, average_price: 100, last_price: 104,
            unrealized_pnl: 40, strategy_id: null, opened_at: null, updated_at: null,
          },
        ]);
      }
      return makeJsonResponse([], 404);
    }) as unknown as typeof fetch;
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchInterval: false } },
    });
    render(<Dashboard />, { wrapper: wrapper(qc) });

    (await screen.findByText("Square off all")).click();
    expect(confirmSpy).toHaveBeenCalledWith("Close all 1 open position at market price?");
    screen.getByText("Close").click();
    expect(confirmSpy).toHaveBeenCalledWith("Close your TCS position at market price?");
    expect(posts).toEqual([]);
  });

  it("renders the AI Analysis toggle and PUTs AI_ANALYSIS_ENABLED=false on click", async () => {
    // Capture the PUT so we can assert the body. GET /api/settings
    // reports the switch ON; clicking must flip it OFF.
    const puts: Array<{ url: string; body: unknown }> = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/api/settings") && init?.method === "PUT") {
        puts.push({ url, body: JSON.parse(String(init.body)) });
        return makeJsonResponse({ global: { AI_ANALYSIS_ENABLED: false } });
      }
      if (url.includes("/api/settings")) {
        return makeJsonResponse({
          global: { TRADING_MODE: "paper", AI_ANALYSIS_ENABLED: true },
        });
      }
      return makeJsonResponse([], 404);
    }) as unknown as typeof fetch;

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchInterval: false } },
    });
    render(<Dashboard />, { wrapper: wrapper(qc) });

    const toggle = await screen.findByTestId("toggle-ai-analysis");
    // Settings loaded with the switch ON.
    await waitFor(() => {
      expect(toggle).toHaveTextContent("ON");
      expect(toggle).not.toBeDisabled();
    });

    toggle.click();

    await waitFor(() => {
      expect(puts.length).toBe(1);
    });
    expect(puts[0].body).toEqual({ global: { AI_ANALYSIS_ENABLED: false } });
  });
});


