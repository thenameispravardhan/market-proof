// The Trade page's Orders tab: the order table's actions and the Modify popup.
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ModifyOrderDialog, OrdersTable, orderRows, type OrderRow } from "../components/trade/AccountPanels";

const row = (o: Partial<OrderRow> = {}): OrderRow => ({
  key: "b-FX-1", id: "FX-1", symbol: "NSE:SBIN-EQ", side: "BUY", type: "LIMIT", product: "INTRADAY",
  qty: 10, filled: 0, remaining: 10, limit: 600, stop: null, avg: null, status: "PENDING",
  ts: null, time: "—", source: "Bot", message: "", bucket: "working", ...o,
});

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
  localStorage.clear();
});

describe("Orders table", () => {
  it("labels the bot's own working orders PENDING, like the Fyers book", () => {
    const [r] = orderRows(null, [{
      id: 1, broker_order_id: "FX-9", broker_account_id: 7, symbol: "NSE:SBIN-EQ", side: "BUY",
      quantity: 5, price: 600, order_type: "LIMIT", status: "placed", filled_qty: 0, created_at: "2026-10-09T04:00:00Z",
    } as never], []);
    expect(r.status).toBe("PENDING");
  });

  it("names the order when cancelling, and says when an order is still being confirmed", async () => {
    const onCancel = vi.fn();
    render(
      <OrdersTable
        rows={[row(), row({ key: "p-2", id: null, symbol: "NSE:TCS-EQ" })]}
        privacy={false}
        onCancel={onCancel}
        cancelBusyId={null}
        onModify={() => undefined}
        live
      />,
    );
    await userEvent.click(screen.getByTestId("cancel-FX-1"));
    expect(onCancel).toHaveBeenCalledWith("FX-1", "BUY 10 NSE:SBIN-EQ");
    expect(screen.getByText("confirming…")).toBeInTheDocument();
  });
});

describe("Modify order popup", () => {
  it("won't send an unchanged order, then sends only what changed", async () => {
    const posts: unknown[] = [];
    globalThis.fetch = vi.fn(async (_u: RequestInfo | URL, init?: RequestInit) => {
      posts.push(JSON.parse(String(init?.body)));
      return new Response(JSON.stringify({ ok: true, message: "Order modified" }), { status: 200 });
    }) as typeof fetch;
    const onDone = vi.fn();
    render(<ModifyOrderDialog order={row()} accountId={7} onClose={() => undefined} onDone={onDone} />);
    expect(screen.getByTestId("modify-order-ok")).toBeDisabled();
    expect(screen.getByText(/Change the quantity, type or price/)).toBeInTheDocument();

    const limit = screen.getByTestId("modify-limit");
    await userEvent.clear(limit);
    await userEvent.type(limit, "605");
    await userEvent.click(screen.getByTestId("modify-order-ok"));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(posts).toEqual([{ account_id: 7, broker_order_id: "FX-1", order_type: "LIMIT", limit_price: 605 }]);
    expect(onDone.mock.calls[0][0]).toBe("Modified BUY 10 NSE:SBIN-EQ (FX-1) — Order modified");
  });

  it("says why it can't modify without a live account", () => {
    render(<ModifyOrderDialog order={row()} accountId={null} onClose={() => undefined} onDone={() => undefined} />);
    expect(screen.getByText(/Connect a live Fyers account/)).toBeInTheDocument();
    expect(screen.getByTestId("modify-order-ok")).toBeDisabled();
  });
});
