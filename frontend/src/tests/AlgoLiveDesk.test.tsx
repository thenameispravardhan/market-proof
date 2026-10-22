// Live desk: the book panel and the Algo Lab kill switch.
import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BookPanel } from "../components/algo/BookPanel";

const json = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }));
const BOOK = {
  underlyings: [{ symbol: "NSE:NIFTY50-INDEX", positions: 2, margin: 250000, mtm: -4200, delta: -1.35 }],
  positions: 2, margin: 250000, realized_today: -800, open_mtm: -4200, day_pnl: -5000,
  limits: { max_day_loss: 10000, max_margin: 400000 }, halted: false,
};

describe("Book panel", () => {
  const original = globalThis.fetch;
  afterEach(() => { globalThis.fetch = original; vi.restoreAllMocks(); });

  it("shows the book against its limits and kills only after a confirm", async () => {
    const calls: string[] = [];
    globalThis.fetch = vi.fn((u: RequestInfo | URL, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${String(u)}`);
      return json(String(u).includes("/kill") ? { flattened: 2, still_open: 0 } : BOOK);
    }) as typeof fetch;
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={qc}><BookPanel /></QueryClientProvider>);
    const panel = await screen.findByTestId("algo-book");
    expect(panel.textContent).toContain("₹-5,000");
    expect(panel.textContent).toContain("of −₹10,000 limit");
    expect(panel.textContent).toContain("-1.35");
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    fireEvent.click(screen.getByText("Kill all automations"));
    expect(confirm).toHaveBeenCalled();
    expect(calls.some((c) => c.startsWith("POST"))).toBe(false);
    confirm.mockReturnValue(true);
    fireEvent.click(screen.getByText("Kill all automations"));
    await waitFor(() => expect(calls.some((c) => c === "POST /api/algo/book/kill")).toBe(true));
  });
});

describe("Shadow row", () => {
  const original = globalThis.fetch;
  afterEach(() => { globalThis.fetch = original; vi.restoreAllMocks(); });

  it("compares a shadow with its parent and promotes it only after a confirm", async () => {
    const { ShadowLine } = await import("../pages/Algo");
    const calls: string[] = [];
    globalThis.fetch = vi.fn((u: RequestInfo | URL, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${String(u)}`);
      return json({ since: "2026-10-23T03:45:00", shadow: { trades: 4, wins: 3, net: 2100 }, parent: { trades: 5, wins: 2, net: -900 }, parent_mode: "live" });
    }) as typeof fetch;
    const done = vi.fn();
    const s = { id: 9, name: "orb · shadow v2", spec: {} as never, enabled: true, mode: "paper" as const, account_id: null,
      closed_trades: 4, realized_pnl: 2100, open_positions: 0, version: 1, versions: 1, shadow_of: 3 };
    render(<ShadowLine s={s} parent="orb" onDone={done} onErr={() => {}} />);
    fireEvent.click(screen.getByText("compare"));
    expect((await screen.findByText(/shadow 4 trades/)).textContent).toContain("orb 5 trades");
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    fireEvent.click(screen.getByText("promote"));
    expect(confirm.mock.calls[0][0]).toContain("REAL MONEY");
    expect(calls.some((c) => c.startsWith("POST"))).toBe(false);
    confirm.mockReturnValue(true);
    fireEvent.click(screen.getByText("promote"));
    await waitFor(() => expect(calls).toContain("POST /api/algo/strategies/9/promote"));
    expect(done).toHaveBeenCalled();
  });
});
