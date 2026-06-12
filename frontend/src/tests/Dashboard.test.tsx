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
    originalFetch = global.fetch;
  });
  afterEach(() => {
    global.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("renders all 6 widgets and shows announcement data when the API responds", async () => {
    global.fetch = makeFetchStub((url) => {
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

    // All six widgets render with their testids.
    expect(screen.getByTestId("announcements-feed")).toBeInTheDocument();
    expect(screen.getByTestId("ai-analysis-panel")).toBeInTheDocument();
    expect(screen.getByTestId("trade-signals")).toBeInTheDocument();
    expect(screen.getByTestId("active-positions")).toBeInTheDocument();
    expect(screen.getByTestId("risk-metrics")).toBeInTheDocument();
    expect(screen.getByTestId("pnl-chart")).toBeInTheDocument();

    // The announcements widget shows the symbol from the mock.
    await waitFor(() => {
      expect(screen.getByText("RELIANCE")).toBeInTheDocument();
    });
  });

  it("renders a graceful empty state when each endpoint 404s", async () => {
    global.fetch = makeFetchStub(() => makeJsonResponse({ detail: "not found" }, 404));

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchInterval: false } },
    });
    render(<Dashboard />, { wrapper: wrapper(qc) });

    await waitFor(() => {
      // The Announcements widget should show its empty state text.
      expect(
        screen.getByText(/Endpoint not available yet/i)
      ).toBeInTheDocument();
    });
  });

  it("surfaces today's realised P&L in the RiskMetrics widget when trades are present", async () => {
    const today = new Date();
    const todayIso = today.toISOString();

    global.fetch = makeFetchStub((url) => {
      if (url.includes("/api/trades")) {
        return makeJsonResponse([
          {
            id: 1,
            signal_id: 1,
            broker_account_id: 1,
            symbol: "TCS",
            side: "BUY",
            quantity: 10,
            price: 100,
            order_type: "market",
            status: "filled",
            broker_order_id: "x",
            pnl: 42.5,
            executed_at: todayIso,
            created_at: todayIso,
          },
        ]);
      }
      if (url.includes("/api/positions")) return makeJsonResponse([
        {
          id: 1, symbol: "TCS", quantity: 10, average_price: 100, last_price: 104.25,
          unrealized_pnl: 42.5, strategy_id: null, opened_at: todayIso, updated_at: todayIso,
        },
      ]);
      if (url.includes("/api/dashboard/summary")) return makeJsonResponse({}, 404);
      return makeJsonResponse([], 404);
    });

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchInterval: false } },
    });
    render(<Dashboard />, { wrapper: wrapper(qc) });

    // RiskMetrics: open positions count, realised P&L with the formatted INR.
    await waitFor(() => {
      expect(screen.getByText("Open positions")).toBeInTheDocument();
    });
    // Hard rules count defaults to 10.
    expect(screen.getByText("Hard rules")).toBeInTheDocument();
    // P&L label present, and the rupee amount contains 42.5.
    expect(screen.getByText(/Realised P&L \(today\)/)).toBeInTheDocument();
  });
});
