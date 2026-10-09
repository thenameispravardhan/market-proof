import { afterEach, describe, expect, it } from "vitest";
import {
  filterOrders,
  fyersTime,
  groupOrders,
  loadBaskets,
  orderBucket,
  orderRows,
  parseBasketCsv,
  symbolParts,
  tradedQty,
  type BrokerBook,
  type TradeRow,
} from "../components/trade/AccountPanels";
import { ladderKeys } from "../components/trade/DomPanel";
import type { PendingOrder } from "../types";

const pending = (p: Partial<PendingOrder>): PendingOrder => ({
  id: 1,
  broker_order_id: "FX-1",
  broker_account_id: 7,
  symbol: "NSE:SBIN-EQ",
  side: "BUY",
  quantity: 5,
  price: 600,
  order_type: "LIMIT",
  status: "placed",
  created_at: "2026-10-04T04:00:00Z",
  ...p,
});

const trade = (t: Partial<TradeRow>): TradeRow => ({
  id: 9,
  symbol: "NSE:SBIN-EQ",
  side: "BUY",
  quantity: 5,
  price: 600,
  order_type: "MARKET",
  status: "filled",
  broker_order_id: "FX-0",
  pnl: null,
  executed_at: "2026-10-04T03:50:00Z",
  created_at: "2026-10-04T03:50:00Z",
  ...t,
});

describe("order list", () => {
  it("buckets statuses", () => {
    expect(orderBucket("PENDING")).toBe("working");
    expect(orderBucket("TRANSIT")).toBe("working");
    expect(orderBucket("placed")).toBe("working");
    expect(orderBucket("FILLED")).toBe("filled");
    expect(orderBucket("cancelled")).toBe("cancelled");
    expect(orderBucket("REJECTED")).toBe("rejected");
    // an exchange-expired DAY order is gone just like a cancelled one
    expect(orderBucket("EXPIRED")).toBe("cancelled");
  });

  it("parses Fyers order times as IST", () => {
    expect(fyersTime("04-Oct-2026 10:01:00")).toBe(Date.UTC(2026, 9, 4, 4, 31, 0));
    expect(fyersTime(null)).toBeNull();
  });

  it("without the Fyers book: the bot's pending orders + history, no duplicates", () => {
    const rows = orderRows(null, [pending({})], [trade({}), trade({ id: 10, broker_order_id: "FX-1", status: "placed" })]);
    expect(rows.map((r) => r.id)).toEqual(["FX-1", "FX-0"]);
    expect(rows[0]).toMatchObject({ bucket: "working", limit: 600, stop: null, remaining: 5, product: "INTRADAY" });
    expect(rows[1]).toMatchObject({ bucket: "filled", avg: 600 });
  });

  it("with the book: the whole account, keeping just-placed bot orders it hasn't caught up with", () => {
    const book: BrokerBook = {
      ok: true,
      positions: [],
      orders: [
        { id: "FX-2", symbol: "NSE:SBIN-EQ", side: "SELL", type: "SL-L", product: "INTRADAY", qty: 10, filled: 4, remaining: 6, limit_price: 590, stop_price: 592, traded_price: 591, status: "PENDING", message: "", time: "04-Oct-2026 10:05:00", source: "M", ours: false },
      ],
    };
    const rows = orderRows(book, [pending({ broker_order_id: "FX-NEW" })], [trade({})]);
    expect(rows.map((r) => r.id)).toEqual(["FX-2", "FX-NEW"]); // history comes from the book when it's live
    expect(rows[0]).toMatchObject({ type: "STOP_LOSS", limit: 590, stop: 592, remaining: 6, filled: 4, source: "Mobile" });
  });

  it("filters: inactive is everything that stopped working", () => {
    const rows = orderRows(null, [pending({})], [trade({}), trade({ id: 11, broker_order_id: "FX-3", status: "rejected" })]);
    expect(filterOrders(rows, "working")).toHaveLength(1);
    expect(filterOrders(rows, "inactive")).toHaveLength(2);
    expect(filterOrders(rows, "rejected")).toHaveLength(1);
  });

  it("a partly filled pending order shows its filled and remaining quantity", () => {
    const [r] = orderRows(null, [pending({ quantity: 10, filled_qty: 4 })], []);
    expect(r).toMatchObject({ qty: 10, filled: 4, remaining: 6 });
  });

  it("a stop-limit's stored price is its limit, never shown as the stop", () => {
    const [sl] = orderRows(null, [pending({ order_type: "STOP_LOSS", price: 590 })], []);
    expect(sl).toMatchObject({ limit: 590, stop: null });
    const [slm] = orderRows(null, [pending({ order_type: "SL-M", price: 592 })], []);
    expect(slm).toMatchObject({ limit: null, stop: 592 });
  });

  it("history rows carry their product and the quantity that actually traded", () => {
    const rows = orderRows(null, [], [
      trade({ id: 20, broker_order_id: "FX-5", status: "cancelled", quantity: 10, filled_qty: 3, price: 601, order_type: "LIMIT", product: "DELIVERY" }),
    ]);
    expect(rows[0]).toMatchObject({ bucket: "cancelled", product: "DELIVERY", filled: 3, avg: 601, limit: null });
  });

  it("trades tab counts partial fills, not the order size", () => {
    expect(tradedQty(trade({ quantity: 10, filled_qty: 3, status: "cancelled", executed_at: null }))).toBe(3);
    expect(tradedQty(trade({ quantity: 10, status: "filled" }))).toBe(10); // bot fills don't track slices
    expect(tradedQty(trade({ quantity: 10, status: "cancelled", executed_at: null }))).toBe(0);
  });

  it("groups orders by symbol with fill stats (smart orderbook)", () => {
    const rows = orderRows(null, [pending({})], [trade({}), trade({ id: 12, broker_order_id: "FX-4", side: "SELL", quantity: 2, price: 610 })]);
    const [g] = groupOrders(rows);
    expect(g).toMatchObject({ symbol: "NSE:SBIN-EQ", working: 1, filled: 2, net: 3, buyAvg: 600, sellAvg: 610 });
  });
});

describe("symbols", () => {
  it("splits exchange and segment", () => {
    expect(symbolParts("NSE:SBIN-EQ")).toMatchObject({ exchange: "NSE", segment: "Equity", ticker: "SBIN" });
    expect(symbolParts("NSE:NIFTY25OCT24500CE").segment).toBe("F&O · Options");
    expect(symbolParts("NSE:NIFTY25OCTFUT").segment).toBe("F&O · Futures");
    expect(symbolParts("MCX:CRUDEOIL25OCTFUT").segment).toBe("Commodity");
    expect(symbolParts("NSE:NIFTY50-INDEX").segment).toBe("Index");
  });
});

describe("baskets", () => {
  afterEach(() => localStorage.clear());

  it("parses CSV legs and reports bad lines", () => {
    const { legs, errors } = parseBasketCsv("symbol,side,qty,type,price\nNSE:SBIN-EQ,BUY,10,LIMIT,612.5\nnse:tcs-eq,sell,2,,\nBAD,BUY,1,MARKET,\nNSE:INFY-EQ,BUY,0,MARKET,\nNSE:INFY-EQ,BUY,1,LIMIT,\n");
    expect(legs.map((l) => [l.symbol, l.side, l.qty, l.type, l.price, l.name])).toEqual([
      ["NSE:SBIN-EQ", "BUY", 10, "LIMIT", 612.5, "SBIN"],
      ["NSE:TCS-EQ", "SELL", 2, "MARKET", null, "TCS"],
    ]);
    expect(errors).toHaveLength(3);
  });

  it("turns the old single basket into the first of several", () => {
    localStorage.setItem("trade:basket", JSON.stringify([{ id: 1, symbol: "NSE:SBIN-EQ", name: "SBIN", side: "BUY", qty: 1, type: "MARKET", price: null }]));
    localStorage.setItem("trade:basketName", "Morning");
    const [b] = loadBaskets();
    expect(b.name).toBe("Morning");
    expect(b.legs).toHaveLength(1);
  });
});

describe("DOM ladder rows", () => {
  const info = { bestBid: 99, bestAsk: 103, ltp: 101, has: (k: number) => k === 96 || k === 110 };
  it("centres n rows on the price", () => {
    expect(ladderKeys(100, 5, { between: true, zeroVol: true }, info)).toEqual([102, 101, 100, 99, 98]);
  });
  it("can drop the prices between the best bid and ask (but never the LTP)", () => {
    expect(ladderKeys(100, 5, { between: false, zeroVol: true }, info)).toEqual([103, 101, 99, 98, 97]);
  });
  it("can drop prices nothing traded at", () => {
    expect(ladderKeys(100, 3, { between: true, zeroVol: false }, info)).toEqual([110, 101, 96]);
  });
});
