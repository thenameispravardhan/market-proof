import { afterEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import { BrokerLive, livePnl } from "../components/trade/TradePanels";

const book = {
  ok: true,
  errors: [],
  orders: [
    { id: "OID-MOBILE", symbol: "NSE:SBIN-EQ", side: "SELL", type: "LIMIT", product: "INTRADAY", qty: 5, filled: 0, remaining: 5, limit_price: 960, stop_price: null, traded_price: null, status: "PENDING", message: "", time: "05-Oct-2026 10:01:00", source: "M", ours: false },
  ],
  positions: [
    { symbol: "NSE:INFY-EQ", product: "INTRADAY", net_qty: 2, avg_price: 1000, buy_qty: 2, buy_avg: 1000, sell_qty: 0, sell_avg: null, ltp: 1010, realized: 0, unrealized: 20, pl: 20 },
  ],
};

describe("Fyers live (orders placed outside the bot)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("live P&L = realized + (ltp − avg) × qty, falling back to the Fyers figure", () => {
    const p = book.positions[0];
    expect(livePnl(p, 1015)).toBe(30);
    expect(livePnl(p, null)).toBe(20);
    expect(livePnl({ ...p, net_qty: 0, realized: 12, pl: 12 }, 1015)).toBe(12);
  });

  it("shows a Fyers-app order and reloads on a broker event", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(book)));
    vi.stubGlobal("fetch", fetchMock);
    render(<BrokerLive privacy={false} />);
    expect(await screen.findByText("Mobile")).toBeTruthy();
    expect(screen.getByText("PENDING")).toBeTruthy();
    expect(screen.getByText("NSE:INFY-EQ")).toBeTruthy();
    const before = fetchMock.mock.calls.length;
    act(() => {
      window.dispatchEvent(new Event("broker:order"));
    });
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(before));
  });
});
