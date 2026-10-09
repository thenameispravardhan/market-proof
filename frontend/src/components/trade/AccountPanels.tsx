// AccountPanels — the account manager's tables, broker tabs and popups:
//   DataTable          sortable table with a column chooser (both remembered)
//   useApiJson         small polling JSON fetch, reloaded on broker events
//   orderRows          one order list from the Fyers book (else the bot's own rows)
//   HoldingsPanel / FundsPanel / GttPanel / AllPositionsPanel / SmartOrderbook
//   ModifyOrderDialog  qty (never above the order's) / type / limit / stop
//   ExitPositionDialog full or partial exit, market or limit
//   Baskets            several named baskets: create / rename popups, CSV upload
//   AccountMenu        the account line's … menu
// Every order still goes through the Trade page's place / cancel / modify
// endpoints, which keep the risk engine and the trading-mode gates.

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { InstrumentHit, PendingOrder } from "../../types";
import { useLiveQuote } from "../../hooks/useQuotes";
import { Modal, Row, Sel, useOutside } from "./chartUi";
import type { ChartOrder } from "./ChartPanel";

export const fmt = (v: number | null | undefined, d = 2) =>
  v == null || !Number.isFinite(v) ? "—" : v.toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });

function loadPref<T>(k: string, d: T): T {
  try {
    const r = localStorage.getItem(k);
    return r ? (JSON.parse(r) as T) : d;
  } catch {
    return d;
  }
}

function savePref(k: string, v: unknown): void {
  try {
    localStorage.setItem(k, JSON.stringify(v));
  } catch {
    /* best-effort */
  }
}

// ---------------------------------------------------------------------------
// Data
// ---------------------------------------------------------------------------

/** GET a JSON endpoint, every `every` ms and on each `broker:order` event
 *  (order socket / postback, dispatched by App). `null` url = idle. */
// Panels that read the same URL at the same moment share ONE request (every
// fetch here can cost a Fyers REST call, which competes with orders).
// A finished request stays shareable for 250ms so panels mounting together
// reuse it; a `fresh` read (after a broker event) only joins one still in flight.
const inflight = new Map<string, { p: Promise<unknown>; done: boolean }>();
function sharedGet(url: string, fresh = false): Promise<unknown> {
  const hit = inflight.get(url);
  if (hit && !(fresh && hit.done)) return hit.p;
  const entry = { p: Promise.resolve<unknown>(null), done: false };
  entry.p = fetch(url)
    .then((r) => (r.ok ? r.json() : null))
    .finally(() => {
      entry.done = true;
      setTimeout(() => inflight.get(url) === entry && inflight.delete(url), 250);
    });
  inflight.set(url, entry);
  return entry.p;
}

export function useApiJson<T>(url: string | null, every = 0, onOrders = true): { data: T | null; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    if (!url) return;
    let stop = false;
    let t: ReturnType<typeof setTimeout> | undefined;
    const load = (fresh = false) =>
      sharedGet(url, fresh)
        .then((j) => !stop && setData(j as T))
        .catch(() => undefined);
    void load();
    const id = every ? setInterval(() => void load(), every) : undefined;
    // one order fires several events (transit → filled, socket + postback): coalesce
    const on = () => {
      clearTimeout(t);
      t = setTimeout(() => void load(true), 200);
    };
    if (onOrders) window.addEventListener("broker:order", on);
    return () => {
      stop = true;
      if (id) clearInterval(id);
      clearTimeout(t);
      window.removeEventListener("broker:order", on);
    };
  }, [url, every, nonce, onOrders]);
  return { data, reload: () => setNonce((n) => n + 1) };
}

export interface BrokerOrder { id: string; symbol: string; side: string; type: string; product: string | null; qty: number; filled: number; remaining: number; limit_price: number | null; stop_price: number | null; traded_price: number | null; status: string; message: string; time: string | null; source: string | null; ours: boolean }
export interface BrokerPosition { symbol: string; product: string | null; net_qty: number; avg_price: number | null; buy_qty: number; buy_avg: number | null; sell_qty: number; sell_avg: number | null; ltp: number | null; realized: number | null; unrealized: number | null; pl: number | null }
export interface BrokerBook { ok: boolean; reason?: string; orders: BrokerOrder[]; positions: BrokerPosition[]; errors?: string[] }

/** A row of the bot's own trade history (`/api/trades`). */
export interface TradeRow {
  id: number;
  symbol: string;
  side: string;
  quantity: number;
  price: number | null;
  order_type: string;
  /** INTRADAY / DELIVERY / MARGIN — absent from older backends. */
  product?: string;
  status: string;
  /** Quantity filled so far (manual orders) — 0 / absent when unknown. */
  filled_qty?: number;
  broker_order_id: string | null;
  pnl: number | null;
  executed_at: string | null;
  created_at: string | null;
}

/** "NSE:SBIN-EQ" → { exchange: NSE, segment: Equity }. */
export function symbolParts(sym: string): { exchange: string; segment: string; ticker: string } {
  const [ex, rest = ""] = sym.includes(":") ? sym.split(":", 2) : ["", sym];
  const t = rest.toUpperCase();
  const segment = ex === "MCX" ? "Commodity"
    : /(CE|PE)$/.test(t) && /\d/.test(t) ? "F&O · Options"
      : /FUT$/.test(t) ? "F&O · Futures"
        : /-INDEX$/.test(t) ? "Index"
          : /-(EQ|BE|BZ|SM|ST)$/.test(t) || /-[A-Z]$/.test(t) ? "Equity"
            : ex === "CDS" || /USDINR|EURINR|GBPINR|JPYINR/.test(t) ? "Currency"
              : "Equity";
  return { exchange: ex || "—", segment, ticker: rest.replace(/-(EQ|BE|INDEX)$/i, "") };
}

export type OrderBucket = "working" | "filled" | "cancelled" | "rejected" | "other";

export function orderBucket(status: string): OrderBucket {
  const s = status.toLowerCase();
  if (/pending|open|transit|trigger|working|placed|amo/.test(s)) return "working";
  if (/fill|complete|traded|executed/.test(s)) return "filled";
  if (/cancel|expire/.test(s)) return "cancelled";
  if (/reject/.test(s)) return "rejected";
  return "other";
}

export interface OrderRow {
  key: string;
  id: string | null;
  symbol: string;
  side: string;
  type: string;
  product: string;
  qty: number;
  filled: number | null;
  remaining: number | null;
  limit: number | null;
  stop: number | null;
  avg: number | null;
  status: string;
  /** Sort key: epoch ms, when known. */
  ts: number | null;
  time: string;
  source: string;
  message: string;
  bucket: OrderBucket;
}

const SOURCES: Record<string, string> = { M: "Mobile", W: "Web", A: "API", ITS: "API", R: "Admin" };
const MONTHS: Record<string, number> = { JAN: 0, FEB: 1, MAR: 2, APR: 3, MAY: 4, JUN: 5, JUL: 6, AUG: 7, SEP: 8, OCT: 9, NOV: 10, DEC: 11 };

/** Fyers "dd-Mon-yyyy HH:MM:SS" (exchange time) → epoch ms. */
export function fyersTime(s: string | null): number | null {
  const m = s ? /^(\d{1,2})-([A-Za-z]{3})-(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})/.exec(s.trim()) : null;
  if (!m) return s ? Date.parse(s) || null : null;
  const mon = MONTHS[m[2].toUpperCase()];
  if (mon === undefined) return null;
  return Date.UTC(+m[3], mon, +m[1], +m[4], +m[5], +m[6]) - 330 * 60000;
}

const clock = (ms: number | null) => (ms == null ? "—" : new Date(ms).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }));

/** One order list: the whole Fyers account when its book is available (the
 *  bot's just-placed orders the book hasn't caught up with are kept), else
 *  the bot's own pending orders + history. */
export function orderRows(book: BrokerBook | null, pending: PendingOrder[], history: TradeRow[]): OrderRow[] {
  const out: OrderRow[] = [];
  const seen = new Set<string>();
  if (book?.ok) {
    for (const o of book.orders) {
      const ts = fyersTime(o.time);
      seen.add(o.id);
      out.push({
        key: `b-${o.id}`,
        id: o.id,
        symbol: o.symbol,
        side: o.side,
        type: o.type === "SL-L" ? "STOP_LOSS" : o.type,
        product: o.product ?? "—",
        qty: o.qty,
        filled: o.filled,
        remaining: o.remaining,
        limit: o.limit_price || null,
        stop: o.stop_price || null,
        avg: o.traded_price || null,
        status: o.status,
        ts,
        time: clock(ts),
        source: o.ours ? "Bot" : SOURCES[o.source ?? ""] ?? o.source ?? "—",
        message: o.message,
        bucket: orderBucket(o.status),
      });
    }
  }
  for (const p of pending) {
    if (p.broker_order_id && seen.has(p.broker_order_id)) continue;
    if (p.broker_order_id) seen.add(p.broker_order_id);
    const ts = p.created_at ? Date.parse(p.created_at) : null;
    const filled = p.filled_qty ?? 0;
    out.push({
      key: `p-${p.id}`,
      id: p.broker_order_id,
      symbol: p.symbol,
      side: p.side,
      type: p.order_type,
      product: p.product ?? "INTRADAY",
      qty: p.quantity,
      filled: p.filled_qty == null ? null : filled,
      remaining: Math.max(0, p.quantity - filled),
      // `price` is the limit (LIMIT / stop-limit) or the SL-M trigger; a
      // stop-limit's trigger isn't stored locally, so it stays unknown
      // rather than showing the limit as the stop.
      limit: p.order_type === "LIMIT" || p.order_type === "STOP_LOSS" ? p.price || null : null,
      stop: p.order_type === "SL-M" ? p.price || null : null,
      avg: null,
      status: "WORKING",
      ts,
      time: clock(ts),
      source: "Bot",
      message: "",
      bucket: "working",
    });
  }
  if (!book?.ok) {
    for (const t of history) {
      if (t.broker_order_id && seen.has(t.broker_order_id)) continue;
      if (orderBucket(t.status) === "working") continue; // the pending list has it
      const ts = t.created_at ? Date.parse(t.created_at) : null;
      const b = orderBucket(t.status);
      // filled_qty is only tracked for manual orders; a filled bot order
      // reports 0 there, so a fill with no slice count is the whole order.
      const filled = t.filled_qty ? t.filled_qty : b === "filled" ? t.quantity : 0;
      out.push({
        key: `h-${t.id}`,
        id: t.broker_order_id,
        symbol: t.symbol,
        side: t.side,
        type: t.order_type,
        product: t.product ?? "INTRADAY",
        qty: t.quantity,
        filled,
        remaining: 0,
        // a fill overwrites `price` with the traded price
        limit: t.order_type === "LIMIT" && !filled ? t.price : null,
        stop: null,
        avg: filled ? t.price : null,
        status: t.status.toUpperCase(),
        ts,
        time: clock(ts),
        source: "Bot",
        message: "",
        bucket: b,
      });
    }
  }
  return out.sort((a, b) => (b.ts ?? 0) - (a.ts ?? 0));
}

// ---------------------------------------------------------------------------
// DataTable
// ---------------------------------------------------------------------------

export interface Col<T> {
  id: string;
  label: string;
  /** Sort key, and the cell text unless `render` is given. */
  get: (r: T) => string | number | null | undefined;
  render?: (r: T) => ReactNode;
  cls?: (r: T) => string | undefined;
  /** Hidden until picked in the column chooser. */
  optional?: boolean;
  /** Always shown (the symbol). */
  fixed?: boolean;
  num?: boolean;
  title?: string;
}

function cmp(a: string | number | null | undefined, b: string | number | null | undefined): number {
  if (a == null || a === "") return b == null || b === "" ? 0 : 1; // empties last
  if (b == null || b === "") return -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b), "en-IN", { numeric: true });
}

/** A table you can sort (click a header: ascending → descending → off) and
 *  pick columns for (the |||| button); both are remembered per table. */
export function DataTable<T>({
  id,
  cols,
  rows,
  rowKey,
  empty,
  actions,
  toolbar,
  rowTitle,
  testid,
}: {
  id: string;
  cols: Col<T>[];
  rows: T[];
  rowKey: (r: T) => string;
  empty: ReactNode;
  actions?: (r: T) => ReactNode;
  toolbar?: ReactNode;
  rowTitle?: (r: T) => string | undefined;
  testid?: string;
}) {
  const defaults = cols.filter((c) => !c.optional && !c.fixed).map((c) => c.id);
  const [shown, setShown] = useState<string[]>(() => loadPref(`am:cols:${id}`, defaults));
  const [sort, setSort] = useState<{ id: string; dir: 1 | -1 } | null>(() => loadPref(`am:sort:${id}`, null));
  const [chooser, setChooser] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  useOutside(ref, chooser, () => setChooser(false));
  useEffect(() => savePref(`am:cols:${id}`, shown), [id, shown]);
  useEffect(() => savePref(`am:sort:${id}`, sort), [id, sort]);
  const visible = cols.filter((c) => c.fixed || shown.includes(c.id));
  const sorted = useMemo(() => {
    const c = sort && cols.find((x) => x.id === sort.id);
    if (!c || !sort) return rows;
    return [...rows].sort((a, b) => cmp(c.get(a), c.get(b)) * sort.dir);
  }, [rows, sort, cols]);
  const head = (cid: string) => setSort((s) => (!s || s.id !== cid ? { id: cid, dir: 1 } : s.dir === 1 ? { id: cid, dir: -1 } : null));
  return (
    <div className="am-table" data-testid={testid}>
      <div className="am-toolbar">
        {toolbar}
        <span className="grow" />
        <div className="chart-menu-wrap" ref={ref}>
          <button type="button" className="btn-sm am-cols-btn" onClick={() => setChooser((o) => !o)} title="Columns" aria-label="Columns" data-testid={`${id}-cols`}>||||</button>
          {chooser && (
            <div className="chart-menu cdrop right am-chooser" data-testid={`${id}-chooser`}>
              <div className="chart-menu-head">Columns</div>
              {cols.filter((c) => !c.fixed).map((c) => (
                <label key={c.id}>
                  <input type="checkbox" checked={shown.includes(c.id)} onChange={(e) => setShown((l) => (e.target.checked ? [...l, c.id] : l.filter((x) => x !== c.id)))} />
                  {c.label}
                </label>
              ))}
              <div className="chart-menu-sep" />
              <button type="button" className="chart-menu-item" onClick={() => setShown(defaults)}>Reset columns</button>
            </div>
          )}
        </div>
      </div>
      {rows.length === 0 ? (
        <div className="empty">{empty}</div>
      ) : (
        <table className="pending-table am-dt">
          <thead>
            <tr>
              {visible.map((c) => (
                <th
                  key={c.id}
                  className={`am-th${c.num ? " num" : ""}${sort?.id === c.id ? " sorted" : ""}`}
                  onClick={() => head(c.id)}
                  aria-sort={sort?.id === c.id ? (sort.dir === 1 ? "ascending" : "descending") : "none"}
                  title={c.title ?? `Sort by ${c.label}`}
                >
                  {c.label}
                  {sort?.id === c.id && <span className="am-sort">{sort.dir === 1 ? "▲" : "▼"}</span>}
                </th>
              ))}
              {actions && <th />}
            </tr>
          </thead>
          <tbody>
            {sorted.map((r) => (
              <tr key={rowKey(r)} title={rowTitle?.(r)}>
                {visible.map((c) => {
                  const cl = [c.num ? "num" : "", c.cls?.(r) ?? ""].join(" ").trim();
                  const v = c.get(r);
                  return <td key={c.id} className={cl || undefined}>{c.render ? c.render(r) : typeof v === "number" ? fmt(v) : v ?? "—"}</td>;
                })}
                {actions && <td className="am-acts">{actions(r)}</td>}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

const sideCls = (s: string) => (s === "BUY" ? "up" : s === "SELL" ? "down" : undefined);
const pnlCls = (v: number | null | undefined) => (v == null ? undefined : v >= 0 ? "up" : "down");

/** LTP cell with the live tick (and a live dot) when one arrives. */
export function LiveLtp({ symbol, fallback }: { symbol: string; fallback: number | null }) {
  const live = useLiveQuote(symbol)?.last_price ?? null;
  return <span title={live != null ? "live tick" : "last snapshot"}>{fmt(live ?? fallback)}{live != null && <span className="live-dot" />}</span>;
}

// ---------------------------------------------------------------------------
// Orders
// ---------------------------------------------------------------------------

export type OrderFilter = "all" | "working" | "inactive" | "filled" | "cancelled" | "rejected";
const ORDER_FILTERS: [OrderFilter, string, string][] = [
  ["all", "All", "Every order today"],
  ["working", "Working", "Pending at the exchange"],
  ["inactive", "Inactive", "No longer working — filled, cancelled, rejected or expired"],
  ["filled", "Filled", "Executed"],
  ["cancelled", "Cancelled", "Cancelled or expired"],
  ["rejected", "Rejected", "Rejected by the broker or exchange"],
];

export function filterOrders(rows: OrderRow[], f: OrderFilter): OrderRow[] {
  if (f === "all") return rows;
  if (f === "inactive") return rows.filter((r) => r.bucket !== "working");
  return rows.filter((r) => r.bucket === f);
}

export function OrdersTable({
  rows,
  privacy,
  onCancel,
  cancelBusyId,
  onModify,
  banner,
  live,
}: {
  rows: OrderRow[];
  privacy: boolean;
  onCancel: (id: string) => void;
  cancelBusyId: string | null;
  onModify: (r: OrderRow) => void;
  banner?: ReactNode;
  /** True when the rows are the whole Fyers account. */
  live: boolean;
}) {
  const [filter, setFilter] = useState<OrderFilter>(() => loadPref("am:orderFilter", "all"));
  useEffect(() => savePref("am:orderFilter", filter), [filter]);
  const shown = filterOrders(rows, filter);
  const count = (f: OrderFilter) => filterOrders(rows, f).length;
  const m = (v: number | null) => (privacy ? "•••" : fmt(v));
  const cols: Col<OrderRow>[] = [
    { id: "symbol", label: "Symbol", get: (r) => r.symbol, fixed: true, cls: () => "sym" },
    { id: "side", label: "Side", get: (r) => r.side, cls: (r) => sideCls(r.side) },
    { id: "type", label: "Type", get: (r) => (r.type === "STOP_LOSS" ? "STOP-LIMIT" : r.type) },
    { id: "product", label: "Product Type", get: (r) => r.product },
    { id: "qty", label: "Qty", get: (r) => r.qty, render: (r) => r.qty, num: true },
    { id: "filled", label: "Filled Qty", get: (r) => r.filled, render: (r) => r.filled ?? "—", num: true, optional: true },
    { id: "remaining", label: "Rem Qty", get: (r) => r.remaining, render: (r) => r.remaining ?? "—", num: true },
    { id: "limit", label: "Limit Price", get: (r) => r.limit, render: (r) => m(r.limit), num: true },
    { id: "stop", label: "Stop Price", get: (r) => r.stop, render: (r) => m(r.stop), num: true },
    { id: "avg", label: "Avg Fill Price", get: (r) => r.avg, render: (r) => m(r.avg), num: true, optional: true },
    { id: "status", label: "Status", get: (r) => r.status, cls: (r) => (r.bucket === "filled" ? "up" : r.bucket === "rejected" || r.bucket === "cancelled" ? "down" : r.bucket === "working" ? "warn-text" : undefined) },
    { id: "time", label: "Time", get: (r) => r.ts, render: (r) => r.time },
    { id: "source", label: "Placed from", get: (r) => r.source, optional: true },
    { id: "id", label: "Order ID", get: (r) => r.id, cls: () => "broker-id", optional: true },
  ];
  return (
    <section className="trade-card" data-testid="trade-pending">
      {banner}
      <DataTable
        id="orders"
        testid="am-orders"
        cols={cols}
        rows={shown}
        rowKey={(r) => r.key}
        rowTitle={(r) => r.message || undefined}
        empty={filter === "all" ? "No orders today." : `No ${filter} orders.`}
        toolbar={
          <div className="sym-filters am-filters">
            {ORDER_FILTERS.map(([f, l, hint]) => (
              <button key={f} type="button" className={`chip${filter === f ? " on" : ""}`} title={hint} onClick={() => setFilter(f)} data-testid={`orders-filter-${f}`}>
                {l}{count(f) ? ` ${count(f)}` : ""}
              </button>
            ))}
            <span className="hint am-src">{live ? "whole Fyers account" : "this terminal's orders"}</span>
          </div>
        }
        actions={(r) =>
          r.bucket === "working" && r.id ? (
            <>
              <button type="button" className="btn-sm" onClick={() => onModify(r)} data-testid={`modify-${r.id}`}>Modify</button>
              <button type="button" className="btn small" onClick={() => onCancel(r.id!)} disabled={cancelBusyId === r.id} data-testid={`cancel-${r.id}`}>
                {cancelBusyId === r.id ? "cancelling…" : "Cancel"}
              </button>
            </>
          ) : null
        }
      />
    </section>
  );
}

// ---------------------------------------------------------------------------
// Trades
// ---------------------------------------------------------------------------

/** Shares actually traded on a trade row: the filled slices of a manual
 *  order (a partly filled order that was then cancelled traded only those),
 *  else the whole order once it's filled. */
export function tradedQty(t: TradeRow): number {
  if (t.filled_qty) return t.filled_qty;
  return orderBucket(t.status) === "filled" || t.executed_at ? t.quantity : 0;
}

export function TradesTable({ rows, privacy }: { rows: TradeRow[]; privacy: boolean }) {
  const done = rows.filter((t) => tradedQty(t) > 0);
  const when = (t: TradeRow) => t.executed_at ?? t.created_at;
  const cols: Col<TradeRow>[] = [
    { id: "time", label: "Time", get: (t) => (when(t) ? Date.parse(when(t)!) : null), render: (t) => (when(t) ? new Date(when(t)!).toLocaleString("en-IN") : "—") },
    { id: "symbol", label: "Symbol", get: (t) => t.symbol, fixed: true, cls: () => "sym" },
    { id: "exchange", label: "Exchange", get: (t) => symbolParts(t.symbol).exchange },
    { id: "segment", label: "Segment", get: (t) => symbolParts(t.symbol).segment },
    { id: "product", label: "Product Type", get: (t) => t.product ?? "INTRADAY" },
    { id: "side", label: "Side", get: (t) => t.side, cls: (t) => sideCls(t.side) },
    { id: "qty", label: "Qty", get: (t) => tradedQty(t), render: (t) => tradedQty(t), num: true },
    { id: "price", label: "Traded price", get: (t) => t.price, num: true },
    { id: "value", label: "Trade value", get: (t) => (t.price != null ? t.price * tradedQty(t) : null), render: (t) => (privacy ? "•••" : fmt(t.price != null ? t.price * tradedQty(t) : null)), num: true, optional: true },
    { id: "pnl", label: "P&L", get: (t) => t.pnl, render: (t) => (t.pnl == null ? "—" : privacy ? "•••" : fmt(t.pnl)), cls: (t) => pnlCls(t.pnl), num: true },
    { id: "id", label: "Order ID", get: (t) => t.broker_order_id, cls: () => "broker-id", optional: true },
  ];
  return (
    <section className="trade-card">
      <DataTable id="trades" testid="am-trades" cols={cols} rows={done} rowKey={(t) => String(t.id)} empty="No executed trades yet." />
    </section>
  );
}

// ---------------------------------------------------------------------------
// Holdings
// ---------------------------------------------------------------------------

export interface Holding {
  symbol: string;
  isin?: string | null;
  qty: number;
  t1_qty?: number | null;
  remaining_qty?: number | null;
  pledged_qty?: number | null;
  collateral_qty?: number | null;
  avg_price: number | null;
  ltp: number | null;
  market_value?: number | null;
  cost_value?: number | null;
  pnl: number | null;
  pnl_pct?: number | null;
  holding_type?: string | null;
  prev_close?: number | null;
  day_pnl?: number | null;
}
export interface HoldingsResp {
  ok: boolean;
  reason?: string;
  holdings?: Holding[];
  overall?: { count?: number; investment?: number | null; current_value?: number | null; pnl?: number | null; pnl_pct?: number | null; day_pnl?: number | null };
}

export function HoldingsPanel({ data, privacy }: { data: HoldingsResp | null; privacy: boolean }) {
  const m = (v: number | null | undefined) => (privacy ? "•••" : fmt(v));
  if (!data) return <section className="trade-card"><div className="empty">Loading holdings…</div></section>;
  if (!data.ok) return <section className="trade-card"><div className="empty">{data.reason ?? "Holdings unavailable."}</div></section>;
  const rows = data.holdings ?? [];
  const o = data.overall ?? {};
  const cols: Col<Holding>[] = [
    { id: "symbol", label: "Symbol", get: (h) => h.symbol, fixed: true, cls: () => "sym" },
    { id: "qty", label: "Net Quantity", get: (h) => h.qty, render: (h) => h.qty, num: true },
    { id: "t1", label: "T1 Quantity", get: (h) => h.t1_qty ?? null, render: (h) => h.t1_qty ?? "—", num: true },
    { id: "remaining", label: "Remaining Qty", get: (h) => h.remaining_qty ?? null, render: (h) => h.remaining_qty ?? "—", num: true },
    { id: "avg", label: "Avg. Cost Price", get: (h) => h.avg_price, num: true },
    { id: "ltp", label: "LTP", get: (h) => h.ltp, render: (h) => <LiveLtp symbol={h.symbol} fallback={h.ltp} />, num: true },
    { id: "day", label: "Day's P&L", get: (h) => h.day_pnl ?? null, render: (h) => m(h.day_pnl), cls: (h) => pnlCls(h.day_pnl), num: true },
    { id: "pnl", label: "Overall P&L", get: (h) => h.pnl, render: (h) => m(h.pnl), cls: (h) => pnlCls(h.pnl), num: true },
    { id: "pct", label: "Overall %", get: (h) => h.pnl_pct ?? null, render: (h) => (h.pnl_pct == null ? "—" : `${h.pnl_pct >= 0 ? "+" : ""}${h.pnl_pct.toFixed(2)}%`), cls: (h) => pnlCls(h.pnl_pct), num: true },
    { id: "value", label: "Current value", get: (h) => h.market_value ?? null, render: (h) => m(h.market_value), num: true, optional: true },
    { id: "cost", label: "Invested", get: (h) => h.cost_value ?? null, render: (h) => m(h.cost_value), num: true, optional: true },
    { id: "pledged", label: "Pledged", get: (h) => h.pledged_qty ?? null, render: (h) => h.pledged_qty ?? "—", num: true, optional: true },
    { id: "isin", label: "ISIN", get: (h) => h.isin ?? null, optional: true },
  ];
  return (
    <section className="trade-card" data-testid="am-holdings">
      <div className="quote-row">
        <div className="quote-cell"><div className="k">INVESTED</div><div className="v">{m(o.investment)}</div></div>
        <div className="quote-cell"><div className="k">CURRENT VALUE</div><div className="v">{m(o.current_value)}</div></div>
        <div className="quote-cell"><div className="k">DAY'S P&amp;L</div><div className={`v ${pnlCls(o.day_pnl) ?? ""}`}>{m(o.day_pnl)}</div></div>
        <div className="quote-cell"><div className="k">HOLDINGS P&amp;L</div><div className={`v ${pnlCls(o.pnl) ?? ""}`}>{m(o.pnl)}{o.pnl_pct != null && !privacy ? ` (${o.pnl_pct >= 0 ? "+" : ""}${o.pnl_pct.toFixed(2)}%)` : ""}</div></div>
        <div className="quote-cell"><div className="k">HOLDINGS</div><div className="v">{o.count ?? rows.length}</div></div>
      </div>
      <DataTable id="holdings" cols={cols} rows={rows} rowKey={(h) => h.symbol} empty="No holdings in the demat account." />
    </section>
  );
}

// ---------------------------------------------------------------------------
// Funds
// ---------------------------------------------------------------------------

export interface FundsResp {
  ok: boolean;
  reason?: string;
  rows?: { id: number; title: string; equity: number | null; commodity: number | null }[];
  summary?: Partial<Record<"total_balance" | "utilized" | "clear_balance" | "realized_pnl" | "collateral" | "fund_transfer" | "receivables" | "adhoc_limit" | "limit_start" | "available", number | null>>;
}

export function FundsPanel({ data, privacy }: { data: FundsResp | null; privacy: boolean }) {
  const m = (v: number | null | undefined) => (privacy ? "•••" : fmt(v));
  if (!data) return <section className="trade-card"><div className="empty">Loading funds…</div></section>;
  if (!data.ok) return <section className="trade-card"><div className="empty">{data.reason ?? "Funds unavailable."}</div></section>;
  const s = data.summary ?? {};
  const cells: [string, number | null | undefined, string?][] = [
    ["AVAILABLE BALANCE", s.available],
    ["LIMIT AT START OF THE DAY", s.limit_start],
    ["FUND TRANSFER (PAY-IN − WITHDRAWN)", s.fund_transfer, "Fyers reports today's pay-ins and withdrawals as one net figure"],
    ["UTILISED MARGIN", s.utilized],
    ["REALIZED P&L", s.realized_pnl],
    ["COLLATERAL", s.collateral],
  ];
  return (
    <section className="trade-card" data-testid="am-funds">
      <div className="quote-row">
        {cells.map(([k, v, t]) => (
          <div key={k} className="quote-cell" title={t}><div className="k">{k}</div><div className="v">{m(v)}</div></div>
        ))}
      </div>
      <DataTable
        id="funds"
        cols={[
          { id: "title", label: "Item", get: (r) => r.title, fixed: true },
          { id: "equity", label: "Equity", get: (r) => r.equity, render: (r) => m(r.equity), num: true },
          { id: "commodity", label: "Commodity", get: (r) => r.commodity, render: (r) => m(r.commodity), num: true },
        ]}
        rows={data.rows ?? []}
        rowKey={(r) => `${r.id}-${r.title}`}
        empty="Fyers returned no fund rows."
      />
      <div className="hint">MTF (pay later) funding is a Fyers product feature and isn't shown here.</div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// GTT
// ---------------------------------------------------------------------------

export interface GttOrder { id: string; symbol: string; side: string; gtt_type: string; product: string | null; status: string; qty: number | null; trigger: number | null; limit: number | null; trigger2?: number | null; limit2?: number | null; created?: string | null }
export interface GttResp { ok: boolean; reason?: string; orders?: GttOrder[] }

export function GttPanel({ data, accountId, privacy, onDone }: { data: GttResp | null; accountId: number | null; privacy: boolean; onDone: (msg: string) => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const m = (v: number | null | undefined) => (privacy ? "•••" : fmt(v));
  if (!data) return <section className="trade-card"><div className="empty">Loading GTT orders…</div></section>;
  if (!data.ok) return <section className="trade-card"><div className="empty">{data.reason ?? "GTT orders unavailable."}</div></section>;
  const cancel = async (g: GttOrder) => {
    if (!accountId || !window.confirm(`Cancel the GTT ${g.side} ${g.qty ?? ""} ${g.symbol}?`)) return;
    setBusy(g.id);
    try {
      const r = await fetch("/api/broker/gtt/cancel", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ account_id: accountId, id: g.id }) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : `The server refused it (${r.status}).`);
      onDone(j.ok ? `GTT ${g.id} cancelled` : `Fyers did not cancel GTT ${g.id}`);
    } catch (e) {
      onDone(`GTT cancel failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
    }
  };
  return (
    <section className="trade-card" data-testid="am-gtt">
      <DataTable
        id="gtt"
        cols={[
          { id: "symbol", label: "Symbol", get: (g) => g.symbol, fixed: true, cls: () => "sym" },
          { id: "side", label: "Buy/Sell", get: (g) => g.side, cls: (g) => sideCls(g.side) },
          { id: "type", label: "GTT Type", get: (g) => g.gtt_type },
          { id: "product", label: "Product Type", get: (g) => g.product ?? "—" },
          { id: "status", label: "Status", get: (g) => g.status },
          { id: "qty", label: "Qty", get: (g) => g.qty, render: (g) => g.qty ?? "—", num: true },
          { id: "trigger", label: "Trigger", get: (g) => g.trigger, render: (g) => m(g.trigger), num: true },
          { id: "limit", label: "Limit", get: (g) => g.limit, render: (g) => m(g.limit), num: true },
          { id: "trigger2", label: "Trigger 2 (OCO)", get: (g) => g.trigger2 ?? null, render: (g) => m(g.trigger2), num: true, optional: true },
          { id: "limit2", label: "Limit 2 (OCO)", get: (g) => g.limit2 ?? null, render: (g) => m(g.limit2), num: true, optional: true },
          { id: "created", label: "Created", get: (g) => g.created ?? null, optional: true },
        ]}
        rows={data.orders ?? []}
        rowKey={(g) => g.id}
        empty="No GTT orders. GTTs are created from the Fyers app or web."
        actions={(g) => (orderBucket(g.status) === "cancelled" ? null : <button type="button" className="btn-sm danger" disabled={busy !== null || !accountId} onClick={() => void cancel(g)}>{busy === g.id ? "…" : "Cancel"}</button>)}
      />
    </section>
  );
}

// ---------------------------------------------------------------------------
// All positions (the whole Fyers account: day + carry-forward)
// ---------------------------------------------------------------------------

/** Live P&L: realized + (live LTP − avg) × net qty; the Fyers figure until a tick arrives. */
export function livePnl(p: BrokerPosition, ltp: number | null | undefined): number | null {
  if (ltp == null || p.net_qty === 0 || p.avg_price == null) return p.pl;
  return (p.realized ?? 0) + (ltp - p.avg_price) * p.net_qty;
}

function LivePositionPnl({ p, privacy }: { p: BrokerPosition; privacy: boolean }) {
  const live = useLiveQuote(p.net_qty ? p.symbol : null)?.last_price ?? null;
  const v = livePnl(p, live ?? p.ltp);
  return <span className={pnlCls(v)}>{privacy ? "•••" : fmt(v)}</span>;
}

export function AllPositionsPanel({ book, privacy, onExit }: { book: BrokerBook | null; privacy: boolean; onExit: (p: BrokerPosition) => void }) {
  const [openOnly, setOpenOnly] = useState(false);
  const m = (v: number | null | undefined) => (privacy ? "•••" : fmt(v));
  if (!book) return <section className="trade-card"><div className="empty">Loading the Fyers account…</div></section>;
  if (!book.ok) return <section className="trade-card"><div className="empty">{book.reason ?? "Fyers account unavailable."}</div></section>;
  const rows = book.positions.filter((p) => !openOnly || p.net_qty !== 0);
  return (
    <section className="trade-card" data-testid="am-allpositions">
      <DataTable
        id="allpositions"
        cols={[
          { id: "symbol", label: "Symbol", get: (p) => p.symbol, fixed: true, cls: () => "sym" },
          { id: "product", label: "Product", get: (p) => p.product ?? "—" },
          { id: "net", label: "Net Qty", get: (p) => p.net_qty, render: (p) => p.net_qty, cls: (p) => (p.net_qty > 0 ? "up" : p.net_qty < 0 ? "down" : undefined), num: true },
          { id: "avg", label: "Avg Price", get: (p) => p.avg_price, num: true },
          { id: "ltp", label: "LTP", get: (p) => p.ltp, render: (p) => <LiveLtp symbol={p.symbol} fallback={p.ltp} />, num: true },
          { id: "buyq", label: "Buy Qty", get: (p) => p.buy_qty, render: (p) => p.buy_qty, num: true, optional: true },
          { id: "buya", label: "Buy Avg", get: (p) => p.buy_avg, num: true, optional: true },
          { id: "sellq", label: "Sell Qty", get: (p) => p.sell_qty, render: (p) => p.sell_qty, num: true, optional: true },
          { id: "sella", label: "Sell Avg", get: (p) => p.sell_avg, num: true, optional: true },
          { id: "realized", label: "Realized P&L", get: (p) => p.realized, render: (p) => m(p.realized), cls: (p) => pnlCls(p.realized), num: true },
          { id: "pnl", label: "P&L", get: (p) => p.pl, render: (p) => <LivePositionPnl p={p} privacy={privacy} />, num: true },
        ]}
        rows={rows}
        rowKey={(p) => `${p.symbol}-${p.product}`}
        empty="No positions on the account today."
        toolbar={<label className="am-check"><input type="checkbox" checked={openOnly} onChange={(e) => setOpenOnly(e.target.checked)} /> Open only</label>}
        actions={(p) =>
          p.net_qty === 0 ? null : (p.product ?? "INTRADAY").toUpperCase() === "INTRADAY" ? (
            <button type="button" className="btn-sm danger" onClick={() => onExit(p)}>Exit</button>
          ) : (
            <span className="hint" title="This terminal only places intraday orders — exit delivery / carry-forward positions from Fyers">Exit in Fyers</span>
          )
        }
      />
    </section>
  );
}

// ---------------------------------------------------------------------------
// Smart Orderbook — orders grouped by symbol, with fill / reject stats
// ---------------------------------------------------------------------------

export interface OrderGroup {
  symbol: string;
  orders: OrderRow[];
  working: number;
  filled: number;
  rejected: number;
  cancelled: number;
  /** Filled buy − sell quantity. */
  net: number;
  buyAvg: number | null;
  sellAvg: number | null;
  last: number | null;
}

export function groupOrders(rows: OrderRow[]): OrderGroup[] {
  const by = new Map<string, OrderRow[]>();
  for (const r of rows) by.set(r.symbol, [...(by.get(r.symbol) ?? []), r]);
  return [...by.entries()].map(([symbol, orders]) => {
    let bq = 0, bv = 0, sq = 0, sv = 0;
    for (const o of orders) {
      const q = o.filled ?? (o.bucket === "filled" ? o.qty : 0);
      const px = o.avg ?? o.limit;
      if (!q || px == null) continue;
      if (o.side === "BUY") { bq += q; bv += q * px; } else { sq += q; sv += q * px; }
    }
    return {
      symbol,
      orders,
      working: orders.filter((o) => o.bucket === "working").length,
      filled: orders.filter((o) => o.bucket === "filled").length,
      rejected: orders.filter((o) => o.bucket === "rejected").length,
      cancelled: orders.filter((o) => o.bucket === "cancelled").length,
      net: bq - sq,
      buyAvg: bq ? bv / bq : null,
      sellAvg: sq ? sv / sq : null,
      last: Math.max(...orders.map((o) => o.ts ?? 0)) || null,
    };
  }).sort((a, b) => (b.last ?? 0) - (a.last ?? 0));
}

export function SmartOrderbook({ rows, privacy, onCancel, onModify }: { rows: OrderRow[]; privacy: boolean; onCancel: (id: string) => void; onModify: (r: OrderRow) => void }) {
  const [open, setOpen] = useState<string[]>([]);
  const [q, setQ] = useState("");
  const groups = groupOrders(rows.filter((r) => !q.trim() || r.symbol.toLowerCase().includes(q.trim().toLowerCase())));
  const total = rows.length;
  const filled = rows.filter((r) => r.bucket === "filled").length;
  const rejected = rows.filter((r) => r.bucket === "rejected");
  const reasons = Object.entries(rejected.reduce<Record<string, number>>((a, r) => ({ ...a, [r.message || "no reason given"]: (a[r.message || "no reason given"] ?? 0) + 1 }), {})).sort((a, b) => b[1] - a[1]).slice(0, 3);
  const m = (v: number | null) => (privacy ? "•••" : fmt(v));
  const cancelAll = (g: OrderGroup) => {
    const w = g.orders.filter((o) => o.bucket === "working" && o.id);
    if (w.length && window.confirm(`Cancel ${w.length} working order(s) on ${g.symbol}?`)) w.forEach((o) => onCancel(o.id!));
  };
  return (
    <section className="trade-card smart-ob" data-testid="am-smart">
      <div className="quote-row">
        <div className="quote-cell"><div className="k">ORDERS</div><div className="v">{total}</div></div>
        <div className="quote-cell"><div className="k">WORKING</div><div className="v warn-text">{rows.filter((r) => r.bucket === "working").length}</div></div>
        <div className="quote-cell"><div className="k">FILL RATE</div><div className="v">{total ? `${Math.round((filled / total) * 100)}%` : "—"}</div></div>
        <div className="quote-cell"><div className="k">REJECTED</div><div className={`v ${rejected.length ? "down" : ""}`}>{rejected.length}</div></div>
        <div className="quote-cell"><div className="k">SYMBOLS</div><div className="v">{groupOrders(rows).length}</div></div>
      </div>
      {reasons.length > 0 && (
        <div className="hint warn-text">Top rejection reasons: {reasons.map(([r, n]) => `${r} (${n})`).join(" · ")}</div>
      )}
      <div className="am-toolbar">
        <input className="basket-name" placeholder="Filter symbol…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Filter symbol" />
        <span className="grow" />
        <button type="button" className="btn-sm" onClick={() => setOpen(groups.map((g) => g.symbol))}>Expand all</button>
        <button type="button" className="btn-sm" onClick={() => setOpen([])}>Collapse all</button>
      </div>
      {groups.length === 0 ? (
        <div className="empty">No orders today.</div>
      ) : (
        <table className="pending-table am-dt smart-table">
          <thead><tr><th /><th>Symbol</th><th className="num">Orders</th><th className="num">Working</th><th className="num">Filled</th><th className="num">Rejected</th><th className="num">Net filled</th><th className="num">Buy avg</th><th className="num">Sell avg</th><th /></tr></thead>
          <tbody>
            {groups.map((g) => {
              const isOpen = open.includes(g.symbol);
              return [
                <tr key={g.symbol} className="smart-group" onClick={() => setOpen((l) => (isOpen ? l.filter((x) => x !== g.symbol) : [...l, g.symbol]))}>
                  <td>{isOpen ? "▾" : "▸"}</td>
                  <td className="sym">{g.symbol}</td>
                  <td className="num">{g.orders.length}</td>
                  <td className="num warn-text">{g.working || ""}</td>
                  <td className="num up">{g.filled || ""}</td>
                  <td className="num down">{g.rejected || ""}</td>
                  <td className={`num ${g.net > 0 ? "up" : g.net < 0 ? "down" : ""}`}>{g.net}</td>
                  <td className="num">{m(g.buyAvg)}</td>
                  <td className="num">{m(g.sellAvg)}</td>
                  <td className="am-acts" onClick={(e) => e.stopPropagation()}>
                    {g.working > 0 && <button type="button" className="btn-sm danger" onClick={() => cancelAll(g)}>Cancel {g.working}</button>}
                  </td>
                </tr>,
                ...(isOpen
                  ? g.orders.map((o) => (
                      <tr key={o.key} className="smart-order" title={o.message || undefined}>
                        <td />
                        <td className="dim">{o.time}</td>
                        <td className={sideCls(o.side)}>{o.side}</td>
                        <td colSpan={2}>{o.type === "STOP_LOSS" ? "STOP-LIMIT" : o.type} · {o.qty}{o.filled ? ` (${o.filled} filled)` : ""}</td>
                        <td className="num">{m(o.limit ?? o.stop)}</td>
                        <td colSpan={2} className={o.bucket === "filled" ? "up" : o.bucket === "rejected" || o.bucket === "cancelled" ? "down" : "warn-text"}>{o.status}</td>
                        <td className="num">{m(o.avg)}</td>
                        <td className="am-acts">
                          {o.bucket === "working" && o.id && (
                            <>
                              <button type="button" className="btn-sm" onClick={() => onModify(o)}>Modify</button>
                              <button type="button" className="btn-sm danger" onClick={() => onCancel(o.id!)}>Cancel</button>
                            </>
                          )}
                        </td>
                      </tr>
                    ))
                  : []),
              ];
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Popups: modify order, exit position
// ---------------------------------------------------------------------------

export function ModifyOrderDialog({ order, accountId, onClose, onDone }: { order: OrderRow; accountId: number | null; onClose: () => void; onDone: (msg: string) => void }) {
  // Fyers modifies the order's TOTAL quantity (filled + open)
  const [qty, setQty] = useState(String(order.qty));
  const [type, setType] = useState(order.type === "SL-L" ? "STOP_LOSS" : order.type);
  const [limit, setLimit] = useState(order.limit != null ? String(order.limit) : "");
  const [stop, setStop] = useState(order.stop != null ? String(order.stop) : "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const needLimit = type === "LIMIT" || type === "STOP_LOSS";
  const needStop = type === "STOP_LOSS" || type === "SL-M";
  const q = Number(qty);
  const bad = !(q > 0) ? "Quantity must be more than 0"
    : !Number.isInteger(q) ? "Quantity must be a whole number"
    : q > order.qty ? `At most ${order.qty} — add size with a new order so the risk checks see it`
      : order.filled && q < order.filled ? `At least ${order.filled} — already filled`
      : needLimit && !(Number(limit) > 0) ? "Enter a limit price"
        : needStop && !(Number(stop) > 0) ? "Enter a stop (trigger) price"
          : null;
  const submit = async () => {
    if (bad || !accountId || !order.id) return;
    setBusy(true);
    setErr(null);
    try {
      const body: Record<string, unknown> = { account_id: accountId, broker_order_id: order.id, order_type: type };
      if (q !== order.qty) body.quantity = q; // only a real change: the server must be able to verify it
      if (needLimit) body.limit_price = Number(limit);
      if (needStop) body.stop_price = Number(stop);
      const r = await fetch("/api/orders/modify", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : `HTTP ${r.status}`);
      if (j.ok === false) throw new Error(j.message || "the broker refused the change");
      onDone(`Modified ${order.id}${j.message ? ` — ${j.message}` : ""}`);
      onClose();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      title={`Modify ${order.side} ${order.symbol}`}
      onClose={onClose}
      width={400}
      testid="modify-order"
      footer={<><span className="grow" /><button type="button" className="cbtn" onClick={onClose}>Cancel</button><button type="button" className="cbtn primary" disabled={!!bad || busy || !accountId} onClick={() => void submit()} data-testid="modify-order-ok">{busy ? "Sending…" : "Modify"}</button></>}
    >
      <Row label="Order"><span className="dim">{order.id} · {order.qty} {order.type === "STOP_LOSS" ? "STOP-LIMIT" : order.type}{order.filled ? ` · ${order.filled} filled` : ""}</span></Row>
      <Row label="Quantity (total)"><input className="cform-input" type="number" min={Math.max(1, order.filled ?? 0)} max={order.qty} value={qty} onChange={(e) => setQty(e.target.value)} aria-label="Quantity" data-testid="modify-qty" /></Row>
      <Row label="Order type">
        <Sel value={type} options={[{ v: "LIMIT", l: "Limit" }, { v: "MARKET", l: "Market" }, { v: "STOP_LOSS", l: "Stop-limit" }, { v: "SL-M", l: "Stop-market" }]} onChange={setType} ariaLabel="Order type" />
      </Row>
      {needLimit && <Row label="Limit price"><input className="cform-input" type="number" step="0.05" value={limit} onChange={(e) => setLimit(e.target.value)} aria-label="Limit price" data-testid="modify-limit" /></Row>}
      {needStop && <Row label="Stop price"><input className="cform-input" type="number" step="0.05" value={stop} onChange={(e) => setStop(e.target.value)} aria-label="Stop price" data-testid="modify-stop" /></Row>}
      {(bad || err) && <div className={`hint ${err ? "down" : "warn-text"}`}>{err ?? bad}</div>}
    </Modal>
  );
}

/** The position an exit acts on (the bot's, or the Fyers account's). */
export interface ExitTarget { symbol: string; name: string; qty: number; avg: number | null; ltp: number | null; lot: number }

export function ExitPositionDialog({
  pos,
  onClose,
  onExit,
}: {
  pos: ExitTarget;
  onClose: () => void;
  /** Full market exits go to the server's close (it also stops the trade manager watching it). */
  onExit: (qty: number, o: ChartOrder, full: boolean) => Promise<string>;
}) {
  const abs = Math.abs(pos.qty);
  const side: "BUY" | "SELL" = pos.qty > 0 ? "SELL" : "BUY";
  const live = useLiveQuote(pos.symbol)?.last_price ?? pos.ltp;
  const [qty, setQty] = useState(String(abs));
  const [type, setType] = useState<"MARKET" | "LIMIT">("MARKET");
  const [price, setPrice] = useState(live != null ? live.toFixed(2) : "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const q = Math.floor(Number(qty));
  const bad = !(q > 0) ? "Quantity must be more than 0" : q > abs ? `At most ${abs} — the open quantity` : pos.lot > 1 && q % pos.lot ? `Use a multiple of the lot size (${pos.lot})` : type === "LIMIT" && !(Number(price) > 0) ? "Enter a limit price" : null;
  const pnl = live != null && pos.avg != null ? (live - pos.avg) * q * Math.sign(pos.qty) : null;
  const submit = async () => {
    if (bad) return;
    setBusy(true);
    setErr(null);
    try {
      await onExit(q, { side, type, price: type === "LIMIT" ? Number(price) : null }, q === abs && type === "MARKET");
      onClose();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      title={`Exit ${pos.name}`}
      onClose={onClose}
      width={420}
      testid="exit-position"
      footer={<><span className="grow" /><button type="button" className="cbtn" onClick={onClose}>Cancel</button><button type="button" className="cbtn danger" disabled={!!bad || busy} onClick={() => void submit()} data-testid="exit-position-ok">{busy ? "Sending…" : `${side} ${q > 0 ? q : ""} to exit`}</button></>}
    >
      <Row label="Position"><span className={pos.qty > 0 ? "up" : "down"}>{pos.qty > 0 ? "LONG" : "SHORT"} {abs} @ {fmt(pos.avg)}</span><span className="dim"> · LTP {fmt(live)}</span></Row>
      <Row label="Quantity">
        <input className="cform-input" type="number" min={pos.lot} step={pos.lot} max={abs} value={qty} onChange={(e) => setQty(e.target.value)} aria-label="Exit quantity" data-testid="exit-qty" />
        <span className="am-qty-btns">
          {[0.25, 0.5, 1].map((f) => (
            <button key={f} type="button" className="btn-sm" onClick={() => setQty(String(f === 1 ? abs : Math.min(abs, Math.max(pos.lot, Math.round((abs * f) / pos.lot) * pos.lot))))}>{f === 1 ? "All" : `${f * 100}%`}</button>
          ))}
        </span>
      </Row>
      <Row label="Order type">
        <span className="seg">
          <button type="button" className={type === "MARKET" ? "on" : ""} onClick={() => setType("MARKET")}>Market</button>
          <button type="button" className={type === "LIMIT" ? "on" : ""} onClick={() => setType("LIMIT")} data-testid="exit-limit">Limit</button>
        </span>
      </Row>
      {type === "LIMIT" && <Row label="Limit price"><input className="cform-input" type="number" step="0.05" value={price} onChange={(e) => setPrice(e.target.value)} aria-label="Limit price" data-testid="exit-price" /></Row>}
      <div className="hint">
        {q === abs ? "Full exit" : q > 0 && q < abs ? `Partial exit — ${abs - q} stay open` : ""}
        {pnl != null && q > 0 ? ` · est. P&L ${pnl >= 0 ? "+" : ""}${fmt(pnl)}` : ""}
      </div>
      {(bad || err) && <div className={`hint ${err ? "down" : "warn-text"}`}>{err ?? bad}</div>}
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Baskets
// ---------------------------------------------------------------------------

export interface BasketLeg {
  id: number;
  symbol: string;
  name: string;
  side: "BUY" | "SELL";
  qty: number;
  type: "MARKET" | "LIMIT";
  price: number | null;
  result?: string;
}
export interface BasketDef { id: string; name: string; legs: BasketLeg[] }

const BASKETS_KEY = "trade:baskets";

/** Saved baskets; the single basket of older versions becomes the first. */
export function loadBaskets(): BasketDef[] {
  const list = loadPref<BasketDef[] | null>(BASKETS_KEY, null);
  if (Array.isArray(list) && list.length) return list;
  const legs = loadPref<BasketLeg[]>("trade:basket", []);
  let name = "Basket 1";
  try {
    name = localStorage.getItem("trade:basketName") ?? name;
  } catch {
    /* best-effort */
  }
  return [{ id: "b1", name, legs: Array.isArray(legs) ? legs : [] }];
}

/** CSV → basket legs. Columns: symbol, side, qty, type, price (a header row
 *  is optional; type defaults to MARKET, price only for LIMIT). */
export function parseBasketCsv(text: string): { legs: BasketLeg[]; errors: string[] } {
  const legs: BasketLeg[] = [];
  const errors: string[] = [];
  const lines = text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  lines.forEach((line, i) => {
    const cells = line.split(",").map((c) => c.trim().replace(/^"|"$/g, ""));
    if (i === 0 && /symbol/i.test(cells[0] ?? "")) return; // header
    const [symRaw, sideRaw, qtyRaw, typeRaw, priceRaw] = cells;
    const symbol = (symRaw ?? "").toUpperCase();
    const side = (sideRaw ?? "").toUpperCase();
    const qty = Math.floor(Number(qtyRaw));
    const type = ((typeRaw ?? "MARKET").toUpperCase() || "MARKET") as BasketLeg["type"];
    const price = priceRaw ? Number(priceRaw) : null;
    if (!/^[A-Z]+:[A-Z0-9&_.-]+$/.test(symbol)) return void errors.push(`line ${i + 1}: symbol "${symRaw ?? ""}" should look like NSE:SBIN-EQ`);
    if (side !== "BUY" && side !== "SELL") return void errors.push(`line ${i + 1}: side must be BUY or SELL`);
    if (!(qty > 0)) return void errors.push(`line ${i + 1}: quantity must be more than 0`);
    if (type !== "MARKET" && type !== "LIMIT") return void errors.push(`line ${i + 1}: type must be MARKET or LIMIT`);
    if (type === "LIMIT" && !(price != null && price > 0)) return void errors.push(`line ${i + 1}: LIMIT needs a price`);
    legs.push({ id: Date.now() + i, symbol, name: symbolParts(symbol).ticker, side, qty, type, price: type === "LIMIT" ? price : null });
  });
  return { legs, errors };
}

function NameDialog({ title, initial, okLabel, onOk, onClose, testid }: { title: string; initial: string; okLabel: string; onOk: (name: string) => void; onClose: () => void; testid: string }) {
  const [name, setName] = useState(initial);
  const ok = () => {
    if (!name.trim()) return;
    onOk(name.trim());
    onClose();
  };
  return (
    <Modal title={title} onClose={onClose} width={360} testid={testid} footer={<><span className="grow" /><button type="button" className="cbtn" onClick={onClose}>Cancel</button><button type="button" className="cbtn primary" disabled={!name.trim()} onClick={ok} data-testid={`${testid}-ok`}>{okLabel}</button></>}>
      <Row label="Basket name"><input className="cform-input" autoFocus value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && ok()} aria-label="Basket name" data-testid={`${testid}-name`} /></Row>
    </Modal>
  );
}

function UploadDialog({ onImport, onClose }: { onImport: (legs: BasketLeg[]) => void; onClose: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [parsed, setParsed] = useState<{ legs: BasketLeg[]; errors: string[] } | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const pick = (f: File | null) => {
    setFile(f);
    setParsed(null);
    if (f) void f.text().then((t) => setParsed(parseBasketCsv(t)));
  };
  return (
    <Modal
      title="Upload basket (CSV)"
      onClose={onClose}
      width={480}
      testid="basket-upload"
      footer={<><span className="grow" /><button type="button" className="cbtn" onClick={onClose}>Cancel</button><button type="button" className="cbtn primary" disabled={!parsed?.legs.length} onClick={() => { onImport(parsed!.legs); onClose(); }} data-testid="basket-upload-ok">Add {parsed?.legs.length ?? 0} leg(s)</button></>}
    >
      <div className="hint">One order per line: <code>symbol,side,qty,type,price</code> — e.g. <code>NSE:SBIN-EQ,BUY,10,LIMIT,612.5</code>. A header row is optional.</div>
      {!file ? (
        <div className="upload-drop" onClick={() => inputRef.current?.click()} onDragOver={(e) => e.preventDefault()} onDrop={(e) => { e.preventDefault(); pick(e.dataTransfer.files[0] ?? null); }}>
          Drop a .csv file here or <button type="button" className="am-link">browse</button>
          <input ref={inputRef} type="file" accept=".csv,text/csv" hidden onChange={(e) => pick(e.target.files?.[0] ?? null)} data-testid="basket-upload-file" />
        </div>
      ) : (
        <div className="upload-file" data-testid="basket-upload-picked">
          <span>📄 {file.name} <span className="dim">· {(file.size / 1024).toFixed(1)} KB</span></span>
          <button type="button" className="btn-sm danger" onClick={() => pick(null)} data-testid="basket-upload-remove">Remove file</button>
        </div>
      )}
      {parsed && (
        <>
          {parsed.legs.length > 0 && (
            <table className="pending-table">
              <thead><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Type</th><th>Price</th></tr></thead>
              <tbody>{parsed.legs.slice(0, 8).map((l) => <tr key={l.id}><td className="sym">{l.symbol}</td><td className={sideCls(l.side)}>{l.side}</td><td>{l.qty}</td><td>{l.type}</td><td>{fmt(l.price)}</td></tr>)}</tbody>
            </table>
          )}
          {parsed.legs.length > 8 && <div className="hint">…and {parsed.legs.length - 8} more</div>}
          {parsed.errors.length > 0 && <div className="hint down">{parsed.errors.slice(0, 5).join(" · ")}{parsed.errors.length > 5 ? ` · +${parsed.errors.length - 5} more` : ""}</div>}
        </>
      )}
    </Modal>
  );
}

export function Baskets({ selected, orderFor }: { selected: InstrumentHit | null; orderFor: (sym: string, name: string, qty: number) => (o: ChartOrder) => Promise<string> }) {
  const [baskets, setBaskets] = useState<BasketDef[]>(loadBaskets);
  const [activeId, setActiveId] = useState<string>(() => loadPref("trade:basketActive", "b1"));
  const [popup, setPopup] = useState<"create" | "rename" | "upload" | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => savePref(BASKETS_KEY, baskets.map((b) => ({ ...b, legs: b.legs.map(({ result: _r, ...l }) => l) }))), [baskets]);
  useEffect(() => savePref("trade:basketActive", activeId), [activeId]);
  const active = baskets.find((b) => b.id === activeId) ?? baskets[0];
  const legs = active?.legs ?? [];
  const setLegs = (f: (l: BasketLeg[]) => BasketLeg[]) => setBaskets((bs) => bs.map((b) => (b.id === active.id ? { ...b, legs: f(b.legs) } : b)));
  const put = (id: number, patch: Partial<BasketLeg>) => setLegs((l) => l.map((x) => (x.id === id ? { ...x, ...patch } : x)));
  const placeAll = async () => {
    if (!legs.length || !window.confirm(`Place ${legs.length} order(s) in "${active.name}" now? These are real orders.`)) return;
    setBusy(true);
    for (const leg of legs) {
      try {
        const r = await orderFor(leg.symbol, leg.name, leg.qty)({ side: leg.side, type: leg.type, price: leg.type === "LIMIT" ? leg.price : null });
        put(leg.id, { result: `✓ ${r}` });
      } catch (e) {
        put(leg.id, { result: `✕ ${e instanceof Error ? e.message : String(e)}` });
      }
    }
    setBusy(false);
  };
  const exportCsv = () => {
    const csv = ["symbol,side,qty,type,price", ...legs.map((l) => [l.symbol, l.side, l.qty, l.type, l.price ?? ""].join(","))].join("\n");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    a.download = `${active.name.replace(/[^\w-]+/g, "_")}.csv`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  };
  return (
    <section className="trade-card" data-testid="basket">
      <div className="am-toolbar">
        <select className="cform-sel" value={active?.id} onChange={(e) => setActiveId(e.target.value)} aria-label="Basket" data-testid="basket-select">
          {baskets.map((b) => <option key={b.id} value={b.id}>{b.name} ({b.legs.length})</option>)}
        </select>
        <button type="button" className="btn-sm" onClick={() => setPopup("create")} data-testid="basket-new">+ New basket</button>
        <button type="button" className="btn-sm" onClick={() => setPopup("rename")} data-testid="basket-rename">Rename</button>
        <button type="button" className="btn-sm" disabled={baskets.length < 2} onClick={() => {
          if (!window.confirm(`Delete the basket "${active.name}"?`)) return;
          const rest = baskets.filter((b) => b.id !== active.id);
          setBaskets(rest);
          setActiveId(rest[0].id);
        }}>Delete</button>
        <span className="am-sep" />
        <button type="button" className="btn-sm" disabled={!selected} onClick={() => selected && setLegs((l) => [...l, { id: Date.now(), symbol: selected.symbol, name: selected.short_name, side: "BUY", qty: selected.lot_size || 1, type: "MARKET", price: null }])}>
          + Add {selected?.short_name ?? "the chart symbol"}
        </button>
        <button type="button" className="btn-sm" onClick={() => setPopup("upload")} data-testid="basket-upload-btn">⇪ Upload CSV</button>
        <button type="button" className="btn-sm" disabled={!legs.length} onClick={exportCsv}>⇩ CSV</button>
        <span className="grow" />
        <button type="button" className="btn-sm" disabled={!legs.length} onClick={() => setLegs(() => [])}>Clear</button>
        <button type="button" className="btn-sm primary" disabled={!legs.length || busy} onClick={() => void placeAll()} data-testid="basket-place">{busy ? "placing…" : `Place all (${legs.length})`}</button>
      </div>
      {legs.length === 0 ? (
        <div className="empty">Add legs from the charted symbol or upload a CSV, then place them together. Every leg is an intraday order on the live account.</div>
      ) : (
        <table className="pending-table">
          <thead><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Type</th><th>Price</th><th>Result</th><th /></tr></thead>
          <tbody>
            {legs.map((l) => (
              <tr key={l.id}>
                <td className="sym" title={l.symbol}>{l.name}</td>
                <td><button type="button" className={`btn-sm ${l.side === "BUY" ? "primary" : "danger"}`} onClick={() => put(l.id, { side: l.side === "BUY" ? "SELL" : "BUY" })}>{l.side}</button></td>
                <td><input type="number" min={1} value={l.qty} onChange={(e) => put(l.id, { qty: Math.max(1, Math.floor(Number(e.target.value) || 1)) })} style={{ width: 70 }} aria-label="Quantity" /></td>
                <td>
                  <select value={l.type} onChange={(e) => put(l.id, { type: e.target.value as BasketLeg["type"] })} aria-label="Order type">
                    <option value="MARKET">MARKET</option>
                    <option value="LIMIT">LIMIT</option>
                  </select>
                </td>
                <td>{l.type === "LIMIT" ? <input type="number" step="0.05" value={l.price ?? ""} onChange={(e) => put(l.id, { price: e.target.value === "" ? null : Number(e.target.value) })} style={{ width: 90 }} aria-label="Limit price" /> : "—"}</td>
                <td className="hint">{l.result ?? ""}</td>
                <td><button type="button" className="chart-menu-x" onClick={() => setLegs((x) => x.filter((y) => y.id !== l.id))} title="Remove leg">✕</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {popup === "create" && (
        <NameDialog title="Create basket" initial={`Basket ${baskets.length + 1}`} okLabel="Create" testid="basket-create" onClose={() => setPopup(null)} onOk={(name) => {
          const b = { id: `b${Date.now()}`, name, legs: [] };
          setBaskets((bs) => [...bs, b]);
          setActiveId(b.id);
        }} />
      )}
      {popup === "rename" && active && (
        <NameDialog title="Rename basket" initial={active.name} okLabel="Rename" testid="basket-rename-dlg" onClose={() => setPopup(null)} onOk={(name) => setBaskets((bs) => bs.map((b) => (b.id === active.id ? { ...b, name } : b)))} />
      )}
      {popup === "upload" && <UploadDialog onClose={() => setPopup(null)} onImport={(add) => setLegs((l) => [...l, ...add])} />}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Account line … menu
// ---------------------------------------------------------------------------

export function AccountMenu({ onRefresh, onTab, onLogout, connected }: { onRefresh: () => void; onTab: (t: "funds" | "holdings" | "allpositions" | "gtt") => void; onLogout?: () => void; connected: boolean }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  useOutside(ref, open, () => setOpen(false));
  const item = (label: string, f: () => void, testid?: string) => (
    <button type="button" className="chart-menu-item" onClick={() => { setOpen(false); f(); }} data-testid={testid}>{label}</button>
  );
  return (
    <div className="chart-menu-wrap am-acct-menu" ref={ref}>
      <button type="button" className="tab" onClick={() => setOpen((o) => !o)} title="Account" aria-label="Account menu" data-testid="am-account-menu">…</button>
      {open && (
        <div className="chart-menu cdrop right">
          {item("↻ Refresh account data", onRefresh, "am-refresh")}
          <div className="chart-menu-sep" />
          {item("Funds", () => onTab("funds"))}
          {item("Holdings", () => onTab("holdings"))}
          {item("All positions", () => onTab("allpositions"))}
          {item("GTT orders", () => onTab("gtt"))}
          <div className="chart-menu-sep" />
          <a className="chart-menu-item" href="https://trade.fyers.in/" target="_blank" rel="noreferrer" onClick={() => setOpen(false)}>Open FYERS Web ↗</a>
          {connected && onLogout && item("Log out of Fyers", onLogout)}
        </div>
      )}
    </div>
  );
}
