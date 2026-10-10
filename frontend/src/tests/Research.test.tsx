// Research page: renders the funnel and calibration from the API, names an
// inverted confidence as inverted, and shows the optimiser's overfitting line.

import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import Research, { aucVerdict } from "../pages/Research";
import { overfittingLine } from "../pages/Algo";

function json(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

const original = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = original;
});

describe("Research page", () => {
  it("renders funnel and calibration", async () => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/funnel")) {
        return json({ signals: 100, blocked: 99, block_rate: 0.99,
          by_block_reason: [{ reason: "RULE_HOLD", n: 90, share_of_blocked: 0.909 }] });
      }
      if (url.includes("/calibration")) {
        return json({ mover_pct: 1.5, by_month: [],
          overall: { n: 500, ece: 0.31, base_rate: 0.14, auc: 0.42,
            bins: [{ lo: 0.9, hi: 1.0, n: 50, mean_confidence: 0.95, observed_rate: 0.086 }] },
          by_model: { "deepseek-chat": { n: 500, ece: 0.31, base_rate: 0.14, auc: 0.42, bins: [] } } });
      }
      if (url.includes("/shadow")) return json({ paired: 0, shadow_calls: {}, shadow_latency_ms: { p50: null, p90: null } });
      if (url.includes("/windows")) return json({ windows: [] });
      return json({});
    }) as unknown as typeof fetch;
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={qc}><Research /></QueryClientProvider>);
    await waitFor(() => expect(screen.getByText("RULE_HOLD")).toBeTruthy());
    expect(screen.getByText(/blocked \(99.0%\)/)).toBeTruthy();
    await waitFor(() => expect(screen.getAllByText(/INVERTED/).length).toBeGreaterThan(0));
    expect(screen.getByText(/No paired filings yet/)).toBeTruthy();
  });

  it("classifies AUC honestly", () => {
    expect(aucVerdict(0.42)).toMatch(/INVERTED/);
    expect(aucVerdict(0.5)).toMatch(/no ranking/);
    expect(aucVerdict(0.7)).toMatch(/ranks movers/);
    expect(aucVerdict(null)).toMatch(/not enough/);
  });

  it("describes the optimiser's overfitting check", () => {
    expect(overfittingLine({ deflated_sharpe: { dsr: 0.4, n_trials: 120, sharpe_per_period: 0.1, benchmark_sharpe: 0.2 },
      pbo: { pbo: 0.7, splits: 70, n_configs: 120 } })).toMatch(/may be luck.*PBO 70%/);
    expect(overfittingLine({ note: "fewer than 10 trading days" })).toMatch(/fewer than 10/);
    expect(overfittingLine(null)).toBeNull();
  });
});
