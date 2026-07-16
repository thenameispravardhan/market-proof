// WorkflowBar — the clickable pipeline strip. We stub every polled
// endpoint and assert (a) all six stage chips render, (b) the states
// reflect the data (AI OFF must be an error chip — that switch sat off
// silently once and cost thousands of skipped filings), and (c) a chip
// click navigates to its stage's page.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { WorkflowBar } from "../components/common/WorkflowBar";

function makeJsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubApi(overrides: Record<string, unknown> = {}) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    const route = (suffix: string) => url.includes(suffix);
    if (route("/api/settings"))
      return makeJsonResponse(
        overrides["settings"] ?? {
          global: { AI_ANALYSIS_ENABLED: true, FAST_TRACK_ENABLED: true, TRADING_MODE: "paper" },
        }
      );
    if (route("/api/announcements/recent"))
      return makeJsonResponse(
        overrides["announcements"] ?? [
          { id: 1, symbol: "RELIANCE", exchange: "NSE", event_type: "ANNOUNCEMENT",
            headline: "x", received_at: new Date().toISOString() },
        ]
      );
    // NOTE: these two endpoints return WRAPPED objects, matching the
    // real API ({strategies: [...]}, {rules: [...]}).
    if (route("/api/strategies"))
      return makeJsonResponse({
        strategies: overrides["strategies"] ?? [
          { id: 1, name: "default", description: null, enabled: true, config: null, created_at: "" },
        ],
      });
    if (route("/api/rules"))
      return makeJsonResponse({
        rules: overrides["rules"] ?? [
          { id: 1, strategy_id: 1, name: "buy orders", priority: 10, conditions: {},
            action: "BUY", action_params: null, enabled: true, created_at: "" },
        ],
      });
    if (route("/api/risk/state"))
      return makeJsonResponse(
        overrides["risk"] ?? {
          trading_disabled: false, disabled_reason: null, halted_until: null,
          current_risk_pct: null, equity: 1000000, trades_today: 2,
          consecutive_losers: 0, entries_allowed: true, entry_block_reason: null,
        }
      );
    if (route("/api/fyers/status"))
      return makeJsonResponse(
        overrides["fyers"] ?? { connected: false, authorized: true, has_token: true, enabled: false }
      );
    if (route("/api/dataset/backfill/status"))
      return makeJsonResponse(
        overrides["dataset"] ?? {
          progress: { running: false },
          remaining: { signal: 0, shadow: 0 },
          collector: { running: true, last_run_at: null, next_run_at: null,
                       interval_s: 120, runs: 3, enriched_session: 7, last_counts: {} },
        }
      );
    return makeJsonResponse({});
  });
}

function renderBar(navigate = vi.fn(), tab = "dashboard" as const) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <WorkflowBar tab={tab} navigate={navigate} />
    </QueryClientProvider>
  );
  return navigate;
}

describe("WorkflowBar", () => {
  let originalFetch: typeof fetch;
  beforeEach(() => { originalFetch = globalThis.fetch; });
  afterEach(() => { globalThis.fetch = originalFetch; vi.restoreAllMocks(); });

  it("renders all six pipeline chips with healthy states", async () => {
    globalThis.fetch = stubApi() as unknown as typeof fetch;
    renderBar();
    for (const key of ["news", "ai", "rules", "risk", "exec", "data"]) {
      expect(screen.getByTestId(`wf-${key}`)).toBeTruthy();
    }
    await waitFor(() => {
      expect(screen.getByTestId("wf-ai").className).toContain("ok");
      expect(screen.getByTestId("wf-rules").textContent).toContain("1 LIVE");
      expect(screen.getByTestId("wf-risk").textContent).toContain("2 TODAY");
      expect(screen.getByTestId("wf-exec").textContent).toContain("PAPER");
      expect(screen.getByTestId("wf-data").textContent).toContain("+7");
    });
  });

  it("shows AI OFF as an error chip", async () => {
    globalThis.fetch = stubApi({
      settings: { global: { AI_ANALYSIS_ENABLED: false, TRADING_MODE: "paper" } },
    }) as unknown as typeof fetch;
    renderBar();
    await waitFor(() => {
      const chip = screen.getByTestId("wf-ai");
      expect(chip.className).toContain("err");
      expect(chip.textContent).toContain("OFF");
    });
  });

  it("warns when no live rules exist (HOLD-only pipeline)", async () => {
    globalThis.fetch = stubApi({ rules: [] }) as unknown as typeof fetch;
    renderBar();
    await waitFor(() => {
      const chip = screen.getByTestId("wf-rules");
      expect(chip.className).toContain("warn");
      expect(chip.textContent).toContain("NONE");
    });
  });

  it("shows blocked entries with the block reason", async () => {
    globalThis.fetch = stubApi({
      risk: {
        trading_disabled: true, disabled_reason: "daily_loss_limit",
        halted_until: null, current_risk_pct: null, equity: 1000000,
        trades_today: 5, consecutive_losers: 0,
        entries_allowed: false, entry_block_reason: "daily_loss_limit",
      },
    }) as unknown as typeof fetch;
    renderBar();
    await waitFor(() => {
      const chip = screen.getByTestId("wf-risk");
      expect(chip.className).toContain("err");
      expect(chip.textContent).toContain("DAILY_LOSS");
    });
  });

  it("navigates to the stage's page on click", async () => {
    globalThis.fetch = stubApi() as unknown as typeof fetch;
    const navigate = renderBar();
    fireEvent.click(screen.getByTestId("wf-data"));
    expect(navigate).toHaveBeenCalledWith("dataset");
    fireEvent.click(screen.getByTestId("wf-rules"));
    expect(navigate).toHaveBeenCalledWith("rules");
  });
});
