// Algo Lab v3, half B: fills vs quotes (TCA) panel.
import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TcaPanel } from "../components/algo/TcaPanel";

const json = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }));

describe("Fills vs quotes", () => {
  const original = globalThis.fetch;
  afterEach(() => { globalThis.fetch = original; vi.restoreAllMocks(); });

  it("flags slippage above what the backtests assume, for the shown strategies only", async () => {
    globalThis.fetch = vi.fn(() => json({ days: 30, rows: [
      { strategy_id: 1, name: "Strangle", kind: "CE", fills: 12, median_pct: 0.42, mean_pct: 0.5, rupees: 1830, assumed_pct: 0.02 },
      { strategy_id: 2, name: "Hidden", kind: "EQ", fills: 3, median_pct: 0.01, mean_pct: 0.01, rupees: 12, assumed_pct: 0.02 },
    ] })) as typeof fetch;
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={qc}><TcaPanel shown={(id) => id === 1} /></QueryClientProvider>);
    const cell = await screen.findByText("0.420%");
    expect(cell.className).toContain("pnl-neg");
    expect(screen.queryByText("Hidden")).toBeNull();
    expect(screen.getByText("₹1,830")).toBeTruthy();
  });
});
