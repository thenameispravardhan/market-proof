import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AccountManager, localPnl } from "../components/trade/TradePanels";
import { ingestQuote } from "../hooks/useQuotes";
import type { Position } from "../types";

const now = new Date().toISOString();
const pos = (o: Partial<Position>): Position => ({
  id: 1, symbol: "NSE:SBIN-EQ", quantity: 10, average_price: 100, last_price: 100, unrealized_pnl: 0,
  strategy_id: null, product: "INTRADAY", lot_size: 1, opened_at: now, updated_at: now, ...o,
});

function renderManager(positions: Position[]) {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify([]))));
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const noop = () => {};
  render(
    <QueryClientProvider client={qc}>
      <AccountManager
        tab="positions" onTab={noop} positions={positions} managed={[]} pendingOrders={[]}
        onCancel={noop} cancelBusyId={null} accountId={null} privacy={false} connected={false}
        accountLabel="Paper" selected={null} closeFor={() => async () => {}} levelsFor={() => async () => {}}
        orderFor={() => async () => "ok"} qty={1} onQty={noop} onMaximize={noop} maximized={false} onCollapse={noop}
      />
    </QueryClientProvider>,
  );
}

describe("Positions panel", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("unrealized P&L uses the live price, signed for shorts, zero once flat", () => {
    expect(localPnl(pos({}), 105)).toBe(50);
    expect(localPnl(pos({ quantity: -10 }), 105)).toBe(-50);
    expect(localPnl(pos({ unrealized_pnl: 7 }), null)).toBe(7);
    expect(localPnl(pos({ quantity: 0, unrealized_pnl: 99 }), 105)).toBe(0);
  });

  it("the header total ticks with the rows instead of the server's stale mark", async () => {
    renderManager([pos({ unrealized_pnl: 0 })]);
    expect(screen.getByTestId("am-total-pnl").textContent).toMatch(/^0(\.00)?$/);
    act(() => ingestQuote({ symbol: "NSE:SBIN-EQ", last_price: 112 }));
    await waitFor(() => expect(screen.getByTestId("am-total-pnl").textContent).toBe("120.00"));
  });

  it("lists today's closed positions on request, without exit buttons", () => {
    renderManager([pos({}), pos({ id: 2, symbol: "NSE:TCS-EQ", quantity: 0 })]);
    expect(screen.queryByText("NSE:TCS-EQ")).toBeNull();
    fireEvent.click(screen.getByTestId("positions-show-closed"));
    expect(screen.getByText("NSE:TCS-EQ")).toBeTruthy();
    expect(screen.queryByTestId("exit-NSE:TCS-EQ")).toBeNull();
    expect(screen.getByTestId("exit-NSE:SBIN-EQ")).toBeTruthy();
  });

  it("an option exit is checked against its own lot, not the charted symbol's", () => {
    renderManager([pos({ symbol: "NSE:BANKNIFTY25OCT56000CE", quantity: 60, lot_size: 30 })]);
    fireEvent.click(screen.getByTestId("exit-NSE:BANKNIFTY25OCT56000CE"));
    fireEvent.change(screen.getByTestId("exit-qty"), { target: { value: "45" } });
    expect(screen.getByText(/multiple of the lot size \(30\)/)).toBeTruthy();
    fireEvent.change(screen.getByTestId("exit-qty"), { target: { value: "30" } });
    expect(screen.queryByText(/multiple of the lot size/)).toBeNull();
  });
});
