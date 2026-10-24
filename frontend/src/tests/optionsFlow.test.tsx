import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { optionsFlow, type OptFlowTuple } from "../components/trade/indicatorCatalog";
import { OptionsFlowSection, OptionsStrikeLadder } from "../pages/Trade";

describe("optionsFlow", () => {
  it("nets calls against puts per candle and runs the total per session", () => {
    const day = 86400;
    const c = [0, 300, day, day + 300].map((time) => ({ time, open: 1, high: 1, low: 1, close: 1, volume: 0 }));
    const f = new Map<number, OptFlowTuple>([
      [0, [100, 40, 10, 30, 80, 0, 0, 0, 0]],           // calls +60, puts −20 → +80
      [300, [0, 50, 70, 0, -120, 0, 0, 0, 0]],
      [day + 300, [10, 0, 0, 0, 10, 0, 0, 0, 0]],
    ]);
    const r = optionsFlow(c, f, true);
    expect(r.net).toEqual([80, -120, null, 10]);
    expect(r.cum).toEqual([80, -40, 0, 10]);             // a new session starts from 0
    expect(r.calls[0]).toBe(60);
    expect(r.puts[0]).toBe(-20);
    expect(optionsFlow(c, f, false).cum[3]).toBe(-30);
  });
});

describe("OptionsFlowSection", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows today's call / put premium and the bullish net, hidden when nothing is recorded", async () => {
    const now = Math.floor(Date.now() / 1000);
    const t = now - ((now + 19800) % 300);
    const bars = [[t, 650, 325, 0, 1300, 65000, 32337.5, 0, 104000, 136662.5]];
    const fetchMock = vi.fn(async (url: string) => ({ ok: true, json: async () => ({ name: "NIFTY", recorded: !url.includes("NIFTYBANK"), bars: url.includes("NIFTYBANK") ? [] : bars }) }));
    vi.stubGlobal("fetch", fetchMock);
    const { unmount } = render(<OptionsFlowSection symbol="NSE:NIFTY50-INDEX" />);
    await waitFor(() => expect(screen.getByTestId("trade-options-flow")).toBeTruthy());
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/algo/ticks/options-flow?symbol=NSE%3ANIFTY50-INDEX");
    expect(screen.getByText("₹65.0K")).toBeTruthy();                        // calls bought
    expect(screen.getAllByText("₹1.37L").length).toBeGreaterThan(0);        // net: calls bought, puts written
    unmount();
    render(<OptionsFlowSection symbol="NSE:NIFTYBANK-INDEX" />);
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(1));
    expect(screen.queryByTestId("trade-options-flow")).toBeNull();
  });
});

describe("OptionsStrikeLadder", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("lays calls and puts out strike by strike, highest first, with the bullish net", async () => {
    const rows = [
      [25000, 650, 325, 0, 1300, 65000, 32337.5, 0, 104000, 136662.5],    // calls bought, puts written: bullish
      [25100, 0, 0, 900, 0, 0, 0, 45000, 0, -45000],                       // puts bought: bearish
      [24900, 0, 0, 0, 0, 0, 0, 0, 0, 0],                                  // nothing traded: left out
    ];
    const fetchMock = vi.fn(async (_url: string) => ({ ok: true, json: async () => ({ name: "NIFTY", recorded: true, rows }) }));
    vi.stubGlobal("fetch", fetchMock);
    const day0 = 20746 * 86400 - 19800;
    render(<OptionsStrikeLadder symbol="NSE:NIFTY50-INDEX" expiry="near" dayStart={day0} lastBar={day0 + 33300 + 3000} />);
    await waitFor(() => expect(screen.getByTestId("trade-options-strikes").querySelectorAll("tbody tr").length).toBe(2));
    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain("/api/algo/ticks/options-strikes?symbol=NSE%3ANIFTY50-INDEX");
    expect(url).toContain(`from=${day0}`);                                // the session
    const strikes = [...screen.getByTestId("trade-options-strikes").querySelectorAll("tbody tr td b")].map((b) => b.textContent);
    expect(strikes).toEqual(["25100", "25000"]);
    expect(screen.getByText("₹1.37L")).toBeTruthy();
    expect(screen.getByText("−₹45.0K")).toBeTruthy();
    fireEvent.click(screen.getByText("last 15 min"));
    await waitFor(() => expect(String(fetchMock.mock.calls[fetchMock.mock.calls.length - 1][0])).toContain(`from=${day0 + 33300 + 3000 + 300 - 900}`));
  });
});
