// Trade — the operator-driven single-page manual order ticket.
//
// Real-money only. Paper-mode broker accounts are filtered out of
// the account picker (the backend also rejects them with 400).
//
// Layout (Bloomberg-style, dense, dark):
//
//  ┌─────────────────────────────────────────────────────────────┐
//  │ SEARCH  [_______________]                                    │
//  │   results...                                                  │
//  ├──────────────────┬──────────────────┬──────────────────────┤
//  │ QUOTE            │ TICKET           │ PENDING ORDERS         │
//  │ symbol, ltp,     │ BUY / SELL       │ list of placed orders  │
//  │ bid/ask, vol     │ qty, type,       │ [cancel] per row       │
//  │                  │ product, SL/lim  │                        │
//  ├──────────────────┴──────────────────┴──────────────────────┤
//  │ OPTION CHAIN (only when symbol is an INDEX)                  │
//  │   expiry ▾   strike | CE bid/offer | PE bid/offer            │
//  │           ...                                                  │
//  └─────────────────────────────────────────────────────────────┘
//
// Soft risk: if the risk engine blocks, the ticket surfaces the
// blocking codes and a "BYPASS" field that the operator must type
// "I ACCEPT THE RISK" into before re-submitting.

import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  useBrokerAccounts,
  useCancelOrder,
  useFyersDisconnect,
  useGlobalSettings,
  useOptionChain,
  usePendingOrders,
  usePlaceOrder,
  usePositions,
  useQuote,
  useRefreshInstruments,
  useSearchSymbols,
  useServerInfo,
} from "../hooks/useApi";
import { useLiveQuote } from "../hooks/useQuotes";
import { tabLocal } from "../router";
import ChartPanel, { type BrokerLine, type ChartOrder, type ChartPosition, type HostAction } from "../components/trade/ChartPanel";
import Scalper, { splitDrag } from "../components/trade/Scalper";
import { AccountManager, BOTTOM_TABS, LayoutMenu, SymbolDetails, WatchlistTable, useLayouts, type BottomTab, type WatchState } from "../components/trade/TradePanels";
import { BookPanel, FnoPanel, type DeskTab } from "../components/trade/ProPanels";
import { loadUserPrefs, UserSettingsDialog, type UserPrefs } from "../components/trade/UserSettings";
import type { SyncFlags } from "../components/trade/chartSync";
import { useOutside } from "../components/trade/chartUi";
import { expiryIso, optionRoot } from "../lib/options";
import type {
  BrokerAccount,
  InstrumentHit,
  OptionLeg,
  OrderType,
  PlaceOrderRequest,
  ProductType,
} from "../types";

function fmtMoney(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return v.toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

/** Compact open-interest formatter (Indian units): K / L (lakh) / Cr. */
function fmtOi(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  if (v >= 1e7) return (v / 1e7).toFixed(2) + "Cr";
  if (v >= 1e5) return (v / 1e5).toFixed(2) + "L";
  if (v >= 1e3) return (v / 1e3).toFixed(1) + "K";
  return String(v);
}

/** Make an error string operator-friendly. We never want to dump
 *  a Cloudflare challenge page or a stacktrace into the orange
 *  banner. Strips HTML, collapses whitespace, caps length, and
 *  falls back to a generic message if we somehow ended up with
 *  empty input. Used by both the place-order and cancel flows. */
function cleanError(raw: unknown, fallback: string): string {
  if (raw == null) return fallback;
  let s = typeof raw === "string" ? raw : String(raw);
  // Drop HTML tags + decode the most common entities. A regex is
  // good enough for operator-visible text — we're not sanitising
  // user input, just stripping an upstream CDN's challenge page.
  s = s
    .replace(/<style[^>]*>[\s\S]*?<\/style>/gi, " ")
    .replace(/<script[^>]*>[\s\S]*?<\/script>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/\s+/g, " ")
    .trim();
  if (!s) return fallback;
  // Cap length to keep the banner compact. The HTML-strip above
  // already neutralises CDN challenge pages, so this is only a
  // secondary guard — 320 leaves room for the broker's full
  // diagnostic (e.g. the -50 app-rejection guidance) without
  // truncating it mid-sentence.
  if (s.length > 320) s = s.slice(0, 320) + "…";
  return s;
}

const DRAG_MIME = "application/x-tradebot-instrument";

/** A price snapped to the instrument's tick (Fyers rejects off-grid prices). */
export function roundToTick(v: number, tick = 0.05): number {
  const t = tick > 0 ? tick : 0.05;
  return Math.round(Math.round(v / t) * t * 100) / 100;
}

export type LevelInputMode = "price" | "pts" | "pct";
const LEVEL_MODE_LABEL: Record<LevelInputMode, string> = { price: "₹", pts: "pts", pct: "%" };

/** A stop / target typed as a price, points away, or % away from the entry
 *  reference — always returned as an absolute price on the right side of the
 *  entry for the trade direction, rounded to the tick. null = not set. */
export function levelPrice(v: number, mode: LevelInputMode, ref: number | null | undefined, side: "BUY" | "SELL",
  kind: "sl" | "target", tick = 0.05): number | null {
  if (!(v > 0)) return null;
  // A typed price goes to Fyers too (the SL / target orders), so it sits on
  // the same tick grid as the entry.
  if (mode === "price") return roundToTick(v, tick);
  if (ref == null || !(ref > 0)) return null;
  const dist = mode === "pts" ? v : (ref * v) / 100;
  const up = (side === "BUY") === (kind === "target");   // BUY target / SELL stop sit above the entry
  const raw = up ? ref + dist : ref - dist;
  const t = tick > 0 ? tick : 0.05;
  return raw > 0 ? Math.round(Math.round(raw / t) * t * 100) / 100 : null;
}

export interface TicketCheck {
  side: "BUY" | "SELL";
  orderType: OrderType;
  quantity: number;
  lotSize: number;
  limit: number | null;     // tick-rounded, null when not set / not needed
  stop: number | null;
  ltp: number | null;
  entry: number | null;     // the price the stop / target are measured from
  sl: number | null;
  tp: number | null;
}

/** Why the ticket can't be sent as it stands (null = it can). Each of these
 *  is an order Fyers would reject, or one that would hit its own stop the
 *  moment it filled. */
export function ticketProblem(t: TicketCheck): string | null {
  const needLimit = t.orderType === "LIMIT" || t.orderType === "STOP_LOSS";
  const needStop = t.orderType === "STOP_LOSS" || t.orderType === "SL-M";
  if (!(t.quantity > 0) || !Number.isInteger(t.quantity)) return "Enter a quantity of at least 1.";
  if (t.lotSize > 1 && t.quantity % t.lotSize !== 0) return `Quantity must be a multiple of the lot size (${t.lotSize}).`;
  if (needLimit && t.limit == null) return "Enter a limit price.";
  if (needStop && t.stop == null) return "Enter a trigger (stop) price.";
  // Stop-limit: a BUY triggers at the stop and buys up to the limit, so the
  // limit can't be below the trigger (and the reverse for a SELL).
  if (t.orderType === "STOP_LOSS" && t.limit != null && t.stop != null) {
    if (t.side === "BUY" && t.limit < t.stop) return "For a stop-limit BUY the limit price must be at or above the trigger.";
    if (t.side === "SELL" && t.limit > t.stop) return "For a stop-limit SELL the limit price must be at or below the trigger.";
  }
  if (t.entry != null && t.entry > 0) {
    const long = t.side === "BUY";
    if (t.sl != null && (long ? t.sl >= t.entry : t.sl <= t.entry))
      return `Stop loss ${t.sl} is on the wrong side of the entry (${t.entry}) — for a ${t.side} it must be ${long ? "below" : "above"} it.`;
    if (t.tp != null && (long ? t.tp <= t.entry : t.tp >= t.entry))
      return `Target ${t.tp} is on the wrong side of the entry (${t.entry}) — for a ${t.side} it must be ${long ? "above" : "below"} it.`;
  }
  return null;
}

/** Non-blocking: a stop order whose trigger is already through the market
 *  (Fyers refuses those, but the LTP here can lag, so it's a warning). */
export function ticketWarning(t: TicketCheck): string | null {
  if ((t.orderType === "STOP_LOSS" || t.orderType === "SL-M") && t.stop != null && t.ltp != null && t.ltp > 0) {
    if (t.side === "BUY" && t.stop <= t.ltp) return `A BUY stop triggers above the market — ${t.stop} is at or below the LTP ${t.ltp}, so the broker will likely reject it.`;
    if (t.side === "SELL" && t.stop >= t.ltp) return `A SELL stop triggers below the market — ${t.stop} is at or above the LTP ${t.ltp}, so the broker will likely reject it.`;
  }
  return null;
}

/** A whole-number input that can be cleared and retyped. Committing on every
 *  keystroke snapped an empty box straight back to 1, so backspacing "1" and
 *  typing "5" gave 15. Commits valid values as they're typed; an empty or bad
 *  entry falls back to the last good value on blur. */
function IntField({ value, onCommit, testid }: { value: number; onCommit: (n: number) => void; testid: string }) {
  const [text, setText] = useState(String(value));
  useEffect(() => { setText((t) => (Number(t) === value ? t : String(value))); }, [value]);
  return (
    <input
      type="number"
      min={1}
      step={1}
      value={text}
      onChange={(e) => {
        setText(e.target.value);
        const n = Number(e.target.value);
        if (e.target.value !== "" && Number.isInteger(n) && n >= 1) onCommit(n);
      }}
      onBlur={() => setText(String(value))}
      data-testid={testid}
    />
  );
}

function LevelMode({ mode, onMode, testid }: { mode: LevelInputMode; onMode: (m: LevelInputMode) => void; testid: string }) {
  return (
    <span className="seg level-mode" data-testid={testid}>
      {(["price", "pts", "pct"] as LevelInputMode[]).map((m) => (
        <button key={m} type="button" className={mode === m ? "on" : ""} onClick={() => onMode(m)}
          title={m === "price" ? "an absolute price" : m === "pts" ? "points away from the entry" : "% away from the entry"}>{LEVEL_MODE_LABEL[m]}</button>
      ))}
    </span>
  );
}
const PRODUCT_LABEL: Record<ProductType, string> = { INTRADAY: "Intraday", DELIVERY: "Delivery", MARGIN: "Carry (NRML)" };
const ORDER_TYPE_LABEL: Record<OrderType, string> = { MARKET: "Market", LIMIT: "Limit", STOP_LOSS: "Stop-limit", "SL-M": "Stop-market" };

// Right-dock panels, opened / closed from the icon bar. Several can be open;
// they stack in this order.
type DockId = "watch" | "details" | "trade" | "chain" | "flow" | "book" | "fno" | "data" | "tree" | "alerts";
const DOCK: { id: DockId; label: string; short: string; icon: string; hint: string }[] = [
  { id: "watch", label: "Watchlist", short: "Watch", icon: "☰", hint: "Watchlist — search and switch symbols" },
  { id: "details", label: "Details & news", short: "Details", icon: "ⓘ", hint: "Symbol details, performance, technical rating and headlines" },
  { id: "trade", label: "Trade", short: "Trade", icon: "⇅", hint: "Quote and order ticket (buy / sell)" },
  { id: "chain", label: "Option chain", short: "Chain", icon: "⊞", hint: "Option chain — click a price to trade that option" },
  { id: "flow", label: "Order flow", short: "Flow", icon: "Δ", hint: "Real order flow from recorded ticks" },
  { id: "book", label: "Market depth", short: "Depth", icon: "≣", hint: "DOM price ladder, 5-level depth with book imbalance, and time & sales" },
  { id: "fno", label: "F&O tools", short: "F&O", icon: "⚖", hint: "Futures chain (basis, carry) and options strategy builder (payoff)" },
  { id: "data", label: "Data window", short: "Data", icon: "▤", hint: "Values under the crosshair — OHLC, volume, every indicator" },
  { id: "tree", label: "Object tree", short: "Objects", icon: "⌗", hint: "Series, indicators and drawings by pane" },
  { id: "alerts", label: "Alerts", short: "Alerts", icon: "🔔", hint: "Alerts manager and log" },
];
const DEFAULT_DOCK: DockId[] = ["watch", "trade"];

// Multi-chart layouts (TradingView-style). Click a chart to make it the
// active one: it follows the watchlist / search and drives the ticket.
type Layout = "1" | "2" | "2v" | "3" | "3c" | "3r" | "4" | "4c" | "4r" | "4l" | "5" | "5l" | "6" | "6v" | "7" | "8" | "8v";
const LAYOUTS: { id: Layout; n: number; icon: string; label: string }[] = [
  { id: "1", n: 1, icon: "▢", label: "1 chart" },
  { id: "2", n: 2, icon: "◫", label: "2 side by side" },
  { id: "2v", n: 2, icon: "⊟", label: "2 stacked" },
  { id: "3", n: 3, icon: "◧", label: "1 large + 2" },
  { id: "3c", n: 3, icon: "⫴", label: "3 side by side" },
  { id: "3r", n: 3, icon: "☰", label: "3 stacked" },
  { id: "4", n: 4, icon: "⊞", label: "4 (2 × 2)" },
  { id: "4c", n: 4, icon: "⫴", label: "4 side by side" },
  { id: "4r", n: 4, icon: "☰", label: "4 stacked" },
  { id: "4l", n: 4, icon: "◧", label: "1 large + 3" },
  { id: "5", n: 5, icon: "⊞", label: "5 (2 + 3)" },
  { id: "5l", n: 5, icon: "◧", label: "1 large + 4" },
  { id: "6", n: 6, icon: "▦", label: "6 (3 × 2)" },
  { id: "6v", n: 6, icon: "▤", label: "6 (2 × 3)" },
  { id: "7", n: 7, icon: "▦", label: "7 (3 + 4)" },
  { id: "8", n: 8, icon: "▩", label: "8 (4 × 2)" },
  { id: "8v", n: 8, icon: "▤", label: "8 (2 × 4)" },
];

type LayoutSync = SyncFlags & { symbol: boolean };
const SYNC_DEFAULT: LayoutSync = { symbol: false, interval: false, crosshair: true, time: false, dateRange: false, drawings: true };
const SYNC_LABELS: [keyof LayoutSync, string][] = [
  ["symbol", "Symbol"],
  ["interval", "Interval"],
  ["crosshair", "Crosshair"],
  ["time", "Time (scroll)"],
  ["dateRange", "Date range"],
  ["drawings", "Drawings (same symbol)"],
];

// Trading settings (TradingView's Settings → Trading), kept per browser.
type TradeSet = { instant: boolean; showPos: boolean; showOrders: boolean; plus: boolean };
const TRADE_SET_DEFAULT: TradeSet = { instant: false, showPos: true, showOrders: true, plus: true };

function TradeSettings({ s, onChange }: { s: TradeSet; onChange: (k: keyof TradeSet, v: boolean) => void }) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    const close = () => setOpen(false);
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);
  const row = (k: keyof TradeSet, label: string, hint: string) => (
    <label className="tset-row" title={hint}>
      <input type="checkbox" checked={s[k]} onChange={(e) => onChange(k, e.target.checked)} data-testid={`tset-${k}`} />
      {label}
    </label>
  );
  return (
    <div className="tset" onMouseDown={(e) => e.stopPropagation()}>
      {s.instant && <span className="tset-badge" title="Chart and scalper orders are placed without a confirm">⚡ instant</span>}
      <button type="button" className={`chart-btn${open ? " on" : ""}`} onClick={() => setOpen((o) => !o)} title="Trading settings" data-testid="trade-settings">⚙</button>
      {open && (
        <div className="chart-menu tset-menu">
          <div className="chart-menu-head">Trading</div>
          {row("instant", "Instant orders — no confirm", "Chart / '+' / scalper orders go straight to the broker; also arms the scalper hotkeys")}
          {row("showPos", "Positions on chart (P&L, SL / TP)", "The live position line with draggable stop-loss and target")}
          {row("showOrders", "Pending orders on chart", "A dashed line per working order")}
          {row("plus", "'+' button on the price scale", "Alert / order / line at the hovered price")}
        </div>
      )}
    </div>
  );
}

// Which money the ticket trades, always on screen: the Trade page only ever
// sends real orders, so this reads LIVE whenever an order can go out and
// OFF (with the reason under it) when it can't.
function TicketModeBadge({ account, botMode }: { account: BrokerAccount | null; botMode?: string }) {
  if (!account) {
    return (
      <span className="mode-badge off" data-testid="ticket-mode" title={botMode === "paper" ? "The bot is in paper mode; this page only trades real money" : "No logged-in Fyers account is switched on"}>
        ORDERS OFF
      </span>
    );
  }
  return (
    <span className="mode-badge live" data-testid="ticket-mode" title={`Orders go to Fyers (${account.name}) with real money`}>
      LIVE · REAL MONEY · {account.name}
    </span>
  );
}

// Polling a symbol's quote keeps it subscribed on the live socket, so a
// chart that isn't the ticket's symbol still ticks.
function KeepLive({ symbol }: { symbol: string }) {
  useQuote(symbol);
  return null;
}

// An instrument that HAS an option chain (index or cash stock).
const isUnderlying = (h: InstrumentHit | null): h is InstrumentHit =>
  h != null && (h.instrument_type === "IND" || h.instrument_type === "EQ");

function stored<T>(key: string, fallback: T): T {
  try {
    const v = tabLocal.getItem(key);
    return v == null ? fallback : (JSON.parse(v) as T);
  } catch {
    return fallback;
  }
}

// Drag-to-resize. dir 1: dragging toward the chart (up / left) grows the
// panel (bottom bar, dock width); dir -1: dragging down grows it (a dock
// panel's bottom edge). The size persists under `key` when one is given.
function drag(e: React.PointerEvent, axis: "x" | "y", start: number, set: (v: number) => void,
              min: number, max: number, key: string | null, dir = 1) {
  e.preventDefault();
  const p0 = axis === "x" ? e.clientX : e.clientY;
  let v = start;
  const move = (ev: PointerEvent) => {
    v = Math.max(min, Math.min(max, start + dir * (p0 - (axis === "x" ? ev.clientX : ev.clientY))));
    set(v);
  };
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    if (key) try { tabLocal.setItem(key, String(Math.round(v))); } catch { /* best-effort */ }
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
}

// Watchlist (TradingView-style): every instrument picked on this page joins
// it; × removes. Kept local, like the last-open symbol.
const RECENT_KEY = "trade:recent";
const LAST_KEY = "trade:last";
const WATCH_KEY = "trade:watchlists";

function loadRecent(): InstrumentHit[] {
  try {
    const raw = tabLocal.getItem(RECENT_KEY);
    const list = raw ? (JSON.parse(raw) as InstrumentHit[]) : [];
    return Array.isArray(list) ? list.filter((h) => h && h.symbol) : [];
  } catch {
    return [];
  }
}

function loadWatchState(): WatchState {
  try {
    const v = JSON.parse(tabLocal.getItem(WATCH_KEY) ?? "null") as WatchState | null;
    if (v && Array.isArray(v.lists) && v.lists.length) return { active: Math.max(0, Math.min(v.active ?? 0, v.lists.length - 1)), lists: v.lists };
  } catch {
    /* fall through */
  }
  return { active: 0, lists: [{ name: "Watchlist", items: loadRecent() }] };
}

// Real order flow for the selected symbol, from the tick recorder: every
// Fyers tick classified buy/sell (Lee-Ready). Today's 5-minute candles of
// buy vs sell volume, delta and cumulative delta. Symbols not on the
// recorder's list can be added from here.
type FlowResp = { symbol: string; key: string; recorded: boolean; bars: [number, number, number, number][] };
function FlowPanel({ symbol }: { symbol: string | null }) {
  const [data, setData] = useState<FlowResp | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const load = async () => {
    if (!symbol) return;
    const now = Math.floor(Date.now() / 1000);
    const day0 = now - ((now + 19800) % 86400);
    try {
      const r = await fetch(`/api/algo/ticks/flow?symbol=${encodeURIComponent(symbol)}&resolution=5&from=${day0 - 86400 * 4}&to=${now + 60}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setData(await r.json()); setErr(null);
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };
  useEffect(() => { setData(null); load(); const id = setInterval(load, 15000); return () => clearInterval(id); }, [symbol]);  // eslint-disable-line react-hooks/exhaustive-deps
  if (!symbol) return <section className="trade-card"><div className="empty">Pick a symbol first.</div></section>;
  const bars = data?.bars ?? [];
  const lastDay = bars.length ? Math.floor((bars[bars.length - 1][0] + 19800) / 86400) : 0;
  const today = bars.filter((b) => Math.floor((b[0] + 19800) / 86400) === lastDay);
  const buy = today.reduce((a, b) => a + b[1], 0), sell = today.reduce((a, b) => a + b[2], 0);
  let cvd = 0;
  const maxAbs = Math.max(1, ...today.map((b) => Math.abs(b[3])));
  const fmt = (v: number) => Math.round(v).toLocaleString("en-IN");
  return (
    <section className="trade-card" data-testid="trade-flow">
      <h2>Real order flow — {symbol} {data?.key && <span className="hint">recorded as {data.key}</span>}</h2>
      {err && <div className="hint warn-text">{err}</div>}
      {data && !data.recorded && (
        <div className="empty">
          No ticks recorded for this symbol yet.{" "}
          <button type="button" className="btn-sm" disabled={adding} onClick={async () => {
            setAdding(true);
            try {
              const st = await (await fetch("/api/algo/ticks/status")).json();
              await fetch("/api/algo/ticks/config", { method: "PUT", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ enabled: true, symbols: [...st.config_symbols, symbol] }) });
              setErr("Added — recording starts with the next tick (market hours).");
            } finally { setAdding(false); }
          }}>record it</button>
        </div>
      )}
      {today.length > 0 && (
        <>
          <div className="quote-row">
            <div className="quote-cell"><div className="k">BUY VOL</div><div className="v up">{fmt(buy)}</div></div>
            <div className="quote-cell"><div className="k">SELL VOL</div><div className="v down">{fmt(sell)}</div></div>
            <div className="quote-cell"><div className="k">DELTA</div><div className={`v ${buy >= sell ? "up" : "down"}`}>{fmt(buy - sell)}</div></div>
            <div className="quote-cell"><div className="k">BUY %</div><div className="v">{buy + sell ? ((buy / (buy + sell)) * 100).toFixed(1) : "—"}</div></div>
          </div>
          <div style={{ maxHeight: 280, overflow: "auto" }}>
            <table className="pending-table">
              <thead><tr><th>Time</th><th>Buy</th><th>Sell</th><th>Delta</th><th style={{ width: "40%" }}></th><th>CVD</th></tr></thead>
              <tbody>{today.map((b) => { cvd += b[3]; return (
                <tr key={b[0]}>
                  <td>{new Date((b[0] + 19800) * 1000).toISOString().slice(11, 16)}</td>
                  <td className="up">{fmt(b[1])}</td><td className="down">{fmt(b[2])}</td>
                  <td className={b[3] >= 0 ? "up" : "down"}>{fmt(b[3])}</td>
                  <td><div style={{ height: 8, width: `${(Math.abs(b[3]) / maxAbs) * 100}%`, background: b[3] >= 0 ? "var(--green)" : "var(--red)", marginLeft: b[3] >= 0 ? "50%" : `${50 - (Math.abs(b[3]) / maxAbs) * 50}%`, maxWidth: "50%" }} /></td>
                  <td className={cvd >= 0 ? "up" : "down"}>{fmt(cvd)}</td>
                </tr>); })}</tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}

export default function Trade() {
  const [bottomTab, setBottomTab] = useState<BottomTab>(() => stored<BottomTab>("trade:bottomTab", "orders"));
  const [bottomOpen, setBottomOpen] = useState(() => stored("trade:bottomOpen", false));
  const openBottom = (tab: BottomTab | null) => {
    if (tab) setBottomTab(tab);
    setBottomOpen(tab != null);
    try {
      tabLocal.setItem("trade:bottomOpen", JSON.stringify(tab != null));
      if (tab) tabLocal.setItem("trade:bottomTab", JSON.stringify(tab));
    } catch { /* best-effort */ }
  };
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<InstrumentHit | null>(() => {
    try { return JSON.parse(tabLocal.getItem(LAST_KEY) ?? "null"); } catch { return null; }
  });
  // The chain stays on the last UNDERLYING picked, so clicking one of its
  // options (which opens that option's chart + ticket) keeps the chain up.
  const [chainBase, setChainBase] = useState<InstrumentHit | null>(() => (isUnderlying(selected) ? selected : stored<InstrumentHit | null>("trade:chainBase", null)));
  const [dock, setDock] = useState<DockId[]>(() => {
    const v = stored<DockId[]>("trade:dock", DEFAULT_DOCK);
    return Array.isArray(v) ? v.filter((x) => DOCK.some((d) => d.id === x)) : DEFAULT_DOCK;
  });
  const [dockW, setDockW] = useState(() => Number(stored("trade:dockW", 340)) || 340);
  const [layout, setLayout] = useState<Layout>(() => stored<Layout>("trade:layout", "1"));
  const [cells, setCells] = useState<(InstrumentHit | null)[]>(() => stored("trade:cells", []));
  // The active cell is persisted with the cells: the last-open symbol belongs
  // to it, so restoring it into cell 0 overwrote that cell's chart on reload.
  const [activeCell, setActiveCellState] = useState(() => Math.max(0, Number(stored("trade:activeCell", 0)) || 0));
  const setActiveCell = (i: number) => {
    setActiveCellState(i);
    try { tabLocal.setItem("trade:activeCell", JSON.stringify(i)); } catch { /* best-effort */ }
  };
  const [layoutOpen, setLayoutOpen] = useState(false);
  const layoutPopRef = useRef<HTMLDivElement | null>(null);
  useOutside(layoutPopRef, layoutOpen, () => setLayoutOpen(false));
  const [tset, setTset] = useState<TradeSet>(() => ({ ...TRADE_SET_DEFAULT, ...stored<Partial<TradeSet>>("trade:settings", {}) }));
  const changeSetting = (k: keyof TradeSet, v: boolean) => {
    if (k === "instant" && v && !window.confirm("Turn on instant orders?\nEvery order from the chart, the '+' menu and the scalper (and its hotkeys) goes to the broker immediately — no confirm.")) return;
    const n = { ...tset, [k]: v };
    setTset(n);
    try { tabLocal.setItem("trade:settings", JSON.stringify(n)); } catch { /* best-effort */ }
  };
  const settingsEl = <TradeSettings s={tset} onChange={changeSetting} />;
  // Layout split positions (fractions), draggable, per layout.
  const [splits, setSplits] = useState<Record<string, { x: number; y: number }>>(() => stored("trade:splits", {}));
  useEffect(() => { try { tabLocal.setItem("trade:splits", JSON.stringify(splits)); } catch { /* best-effort */ } }, [splits]);
  // One top toolbar + one drawing strip for the whole layout: the active
  // chart renders its own controls into these (TradingView-style).
  const [topSlot, setTopSlot] = useState<HTMLDivElement | null>(null);
  const [leftSlot, setLeftSlot] = useState<HTMLDivElement | null>(null);
  const [scalper, setScalper] = useState(() => stored("trade:scalper", false));
  useEffect(() => { try { tabLocal.setItem("trade:scalper", JSON.stringify(scalper)); } catch { /* best-effort */ } }, [scalper]);
  // Sizes dragged on a bigger monitor must not squeeze the chart away on a
  // smaller one: the dock and bottom bar are capped to the current window.
  const [win, setWin] = useState(() => ({ w: window.innerWidth, h: window.innerHeight }));
  useEffect(() => {
    const on = () => setWin({ w: window.innerWidth, h: window.innerHeight });
    window.addEventListener("resize", on);
    return () => window.removeEventListener("resize", on);
  }, []);
  const dockCap = Math.max(280, win.w - 660);   // leaves the chart + rail at least ~600px
  const dockWidth = Math.min(dockCap, dock.includes("chain") ? Math.max(dockW, 400) : dockW);   // the chain's 5 columns need room
  // Panel heights the operator dragged (px); unset = sized by content. The
  // last panel always takes whatever room is left.
  const [sizes, setSizes] = useState<Partial<Record<DockId, number>>>(() => stored("trade:dockSizes", {}));
  useEffect(() => { try { tabLocal.setItem("trade:dockSizes", JSON.stringify(sizes)); } catch { /* best-effort */ } }, [sizes]);
  const saveDock = (next: DockId[]) => {
    setDock(next);
    try { tabLocal.setItem("trade:dock", JSON.stringify(next)); } catch { /* best-effort */ }
  };
  const toggleDock = (id: DockId) => saveDock(dock.includes(id) ? dock.filter((x) => x !== id) : [...dock, id]);
  const moveDock = (id: DockId, by: -1 | 1) => {
    const i = dock.indexOf(id), j = i + by;
    if (j < 0 || j >= dock.length) return;
    const next = [...dock];
    [next[i], next[j]] = [next[j], next[i]];
    saveDock(next);
  };
  const panelStyle = (id: DockId): React.CSSProperties => {
    const last = dock.indexOf(id) === dock.length - 1;
    return { order: dock.indexOf(id), ...(last ? { flex: "1 1 auto" } : sizes[id] ? { flex: `0 0 ${sizes[id]}px` } : {}) };
  };
  const dockHead = (id: DockId, sub?: string) => {
    const label = DOCK.find((d) => d.id === id)!.label;
    const i = dock.indexOf(id);
    return (
      <header className="dock-head">
        <span>{label}</span>
        {sub && <span className="sub">{sub}</span>}
        <span className="dock-tools">
          <button type="button" disabled={i === 0} onClick={() => moveDock(id, -1)} title="Move panel up" aria-label={`Move ${label} up`}>▲</button>
          <button type="button" disabled={i === dock.length - 1} onClick={() => moveDock(id, 1)} title="Move panel down" aria-label={`Move ${label} down`}>▼</button>
          <button type="button" className="dock-x" onClick={() => toggleDock(id)} title="Close panel" aria-label={`Close ${label}`}>×</button>
        </span>
      </header>
    );
  };
  // Bottom edge of every panel but the last: drag to resize, double-click to
  // hand the height back to the content.
  const dockGrip = (id: DockId) =>
    dock.indexOf(id) < dock.length - 1 && (
      <div
        className="dock-grip"
        title="Drag to resize · double-click to reset"
        onPointerDown={(e) => {
          const panel = e.currentTarget.parentElement!;
          const room = panel.parentElement!.clientHeight - (dock.length - 1) * 148;   // the others keep their 140px minimum
          drag(e, "y", panel.offsetHeight, (v) => setSizes((z) => ({ ...z, [id]: v })), 140, Math.max(140, room), null, -1);
        }}
        onDoubleClick={() => setSizes((z) => { const n = { ...z }; delete n[id]; return n; })}
      />
    );
  const [sync, setSyncState] = useState<LayoutSync>(() => ({ ...SYNC_DEFAULT, ...stored<Partial<LayoutSync>>("trade:sync", {}) }));
  const setSync = (k: keyof LayoutSync, v: boolean) => {
    const n = { ...sync, [k]: v };
    setSyncState(n);
    try { tabLocal.setItem("trade:sync", JSON.stringify(n)); } catch { /* best-effort */ }
  };
  const nCells = LAYOUTS.find((l) => l.id === layout)?.n ?? 1;
  useEffect(() => { if (activeCell >= nCells) setActiveCell(0); }, [activeCell, nCells]);  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    setCells((c) => {
      if (sync.symbol && selected) {
        if (Array.from({ length: nCells }, (_, i) => c[i]?.symbol).every((x) => x === selected.symbol)) return c;
        const n = Array.from({ length: Math.max(nCells, c.length) }, () => selected);
        try { tabLocal.setItem("trade:cells", JSON.stringify(n)); } catch { /* best-effort */ }
        return n;
      }
      if (c[activeCell]?.symbol === selected?.symbol) return c;
      const n = [...c];
      n[activeCell] = selected;
      try { tabLocal.setItem("trade:cells", JSON.stringify(n)); } catch { /* best-effort */ }
      return n;
    });
  }, [selected, activeCell, sync.symbol, nCells]);
  const [showResults, setShowResults] = useState(false);
  // Clicking anywhere outside the search box closes its results.
  const searchRef = useRef<HTMLDivElement | null>(null);
  useOutside(searchRef, showResults, () => setShowResults(false));
  // Keyboard cursor into the search results (-1 = nothing highlighted).
  const [highlightIdx, setHighlightIdx] = useState(-1);
  const [wl, setWlState] = useState<WatchState>(loadWatchState);
  const updateWl = (fn: (w: WatchState) => WatchState) =>
    setWlState((w) => {
      const next = fn(w);
      try {
        tabLocal.setItem(WATCH_KEY, JSON.stringify(next));
        tabLocal.setItem(RECENT_KEY, JSON.stringify(next.lists[next.active]?.items ?? []));
      } catch { /* best-effort */ }
      return next;
    });
  const recent = wl.lists[wl.active]?.items ?? [];
  const addToWatch = (h: InstrumentHit) =>
    updateWl((w) => ({
      ...w,
      lists: w.lists.map((l, i) => (i === w.active && !l.items.some((r) => r.symbol === h.symbol) ? { ...l, items: [h, ...l.items].slice(0, 200) } : l)),
    }));
  const [bottomH, setBottomH] = useState(() => Number(stored("trade:bottomH", 260)) || 260);
  const [bottomMax, setBottomMax] = useState(false);
  const [maxCell, setMaxCell] = useState<number | null>(null);
  const [privacy, setPrivacy] = useState(() => stored("trade:privacy", false));
  const [layoutMenuOpen, setLayoutMenuOpen] = useState(false);
  const L = useLayouts();
  const [rangeSlot, setRangeSlot] = useState<HTMLDivElement | null>(null);
  const [dataSlot, setDataSlot] = useState<HTMLDivElement | null>(null);
  const [treeSlot, setTreeSlot] = useState<HTMLDivElement | null>(null);
  const [alertsSlot, setAlertsSlot] = useState<HTMLDivElement | null>(null);
  const watchSyms = [...new Set(wl.lists.flatMap((l) => l.items.map((h) => h.symbol)))].slice(0, 50).join(",");
  const { data: watchQuotes } = useQuery<{ quotes: Record<string, { ltp: number; change: number | null; change_pct: number | null }> }>({
    queryKey: ["watch-quotes", watchSyms],
    queryFn: async () => {
      const r = await fetch(`/api/market/quotes?symbols=${encodeURIComponent(watchSyms)}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    },
    enabled: watchSyms.length > 0,
    refetchInterval: 5000,
  });

  // Lot sizes of saved F&O entries (watchlist, the open ticket) are refreshed
  // from the server's F&O scrip master: a lot stored when an option was added
  // goes stale when NSE revises lot sizes, and the ticket would send it.
  const isFno = (h: InstrumentHit | null | undefined) =>
    !!h && (h.segment === "FO" || ["CE", "PE", "FUT"].includes(h.instrument_type));
  const fnoSyms = [...new Set([...wl.lists.flatMap((l) => l.items), selected].filter(isFno).map((h) => h!.symbol))]
    .sort().slice(0, 200).join(",");
  const { data: freshLots } = useQuery<{ lots: Record<string, number | null> }>({
    queryKey: ["fno-lots", fnoSyms],
    queryFn: async () => {
      const r = await fetch(`/api/options/lots?symbols=${encodeURIComponent(fnoSyms)}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    },
    enabled: fnoSyms.length > 0,
    staleTime: 5 * 60_000,
  });
  useEffect(() => {
    const lots = freshLots?.lots;
    if (!lots) return;
    const fix = (h: InstrumentHit): InstrumentHit => {
      if (!(h.symbol in lots)) return h;
      const lot = lots[h.symbol] ?? 0;   // 0 = unknown: the ticket blocks it
      return h.lot_size === lot ? h : { ...h, lot_size: lot };
    };
    if (wl.lists.some((l) => l.items.some((h) => fix(h) !== h)))
      updateWl((w) => ({ ...w, lists: w.lists.map((l) => ({ ...l, items: l.items.map(fix) })) }));
    setSelected((cur) => (cur && fix(cur) !== cur ? fix(cur) : cur));
  }, [freshLots]);  // eslint-disable-line react-hooks/exhaustive-deps

  const { data: accounts } = useBrokerAccounts();
  const { data: globalSettings } = useGlobalSettings();
  // Trade page is REAL-MONEY ONLY. Filter out paper-mode rows.
  // We pick the first switched-on real account, preferring one that is
  // logged in (the backend 400s an order on an account with no token).
  const realAccounts = useMemo<BrokerAccount[]>(
    () => (accounts?.filter((a) => !a.paper_mode && a.enabled) ?? [])
      .sort((a, b) => Number(!!b.access_token) - Number(!!a.access_token)),
    [accounts],
  );
  const account = realAccounts[0]?.access_token ? realAccounts[0] : null;
  const accountId: number | null = account?.id ?? null;
  // Why this page can't place orders, in words the operator can act on.
  // The Fyers switch on the Dashboard is the paper/live master switch:
  // off means the bot is in paper mode and this page has no account.
  const noAccountReason: string | null = account ? null
    : accounts === undefined ? "Loading the broker account…"
    : !accounts.some((a) => !a.paper_mode) ? "No Fyers account yet. Add and log in to one on the Accounts page to place orders."
    : realAccounts.length === 0 ? "Paper mode: Fyers trading is switched off. Turn the Fyers switch on in the Dashboard to place real orders here."
    : "Fyers is not logged in. Log in on the Accounts page to place orders.";
  const botMode = globalSettings?.global?.TRADING_MODE;
  // Server identity (public IP). Surfaced in the place-order
  // error banner when Fyers rejects with the IP-whitelist
  // error so the operator can copy the IP into the Fyers app
  // dashboard's whitelist.
  const { data: serverInfo } = useServerInfo();

  const { data: searchData, isFetching: searching } = useSearchSymbols(query);
  const refreshInstruments = useRefreshInstruments();

  const { data: quote } = useQuote(selected?.symbol ?? "");
  // Sub-second overlay: the `/ws` push stream (touched live by the quote
  // endpoint above) overrides the 3s REST poll the instant ticks arrive.
  const live = useLiveQuote(selected?.symbol);
  const ltp = live?.last_price ?? (quote?.ok ? quote.last_price : null);
  const bid = live?.bid ?? (quote?.ok ? quote.bid : null);
  const ask = live?.ask ?? (quote?.ok ? quote.ask : null);
  // Selected option-chain expiry (epoch ts). Reset on each new symbol.
  const [selectedExpiry, setSelectedExpiry] = useState<string | null>(null);
  // The chain is available for index underlyings AND F&O stocks. We fetch
  // for both indices and cash equities; a non-F&O stock just returns no
  // strikes and the panel stays hidden.
  const chainOn = chainBase != null && (dock.includes("chain") || dock.includes("fno") || scalper);
  // User settings → order window defaults, chain strikes
  const [userPrefs, setUserPrefs] = useState<UserPrefs>(loadUserPrefs);
  const [userSettingsOpen, setUserSettingsOpen] = useState(false);
  const { data: chain } = useOptionChain(
    chainOn ? chainBase!.symbol : "",
    chainOn ? chainBase!.short_name : "",
    selectedExpiry,
    userPrefs.chainStrikes,
  );
  // An expiry the chain no longer lists (it lapsed, or the chain switched
  // between live epochs and static dates) drops back to the nearest one,
  // instead of a select showing one expiry while every poll asks for another.
  useEffect(() => {
    if (selectedExpiry && chain && chain.expiries.length > 0 && !chain.expiries.some((e) => e.ts === selectedExpiry)) {
      setSelectedExpiry(null);
    }
  }, [chain, selectedExpiry]);
  const { data: pending } = usePendingOrders(accountId);
  const { data: positions } = usePositions();

  // --- Ticket state ---
  const [side, setSide] = useState<"BUY" | "SELL">("BUY");
  const [quantity, setQuantity] = useState<number>(() => loadUserPrefs().defaultQty);
  const [orderType, setOrderType] = useState<OrderType>(() => loadUserPrefs().defaultOrderType);
  const fyersDisconnect = useFyersDisconnect();
  const [limitPrice, setLimitPrice] = useState<string>("");
  const [stopPrice, setStopPrice] = useState<string>("");
  const [lastResult, setLastResult] = useState<{
    type: "success" | "error";
    message: string;
    warning?: string | null;
    /** The order is at the broker but wasn't saved here — never re-place it. */
    recordWarning?: string | null;
    detail?: PlaceOrderRequest;
    // Diagnostic reason from the Fyers backend. Set when the
    // place-order rejection has a known cause the UI can
    // surface as a richer banner — currently just
    // "ip_whitelist" (Fyers code -50: "Algo orders are not
    // allowed from this app", which is the misleading message
    // Fyers uses when the server's IP isn't on the app's
    // IP-whitelist). When set, the banner also shows the
    // bot's public IP so the operator can copy it into the
    // Fyers dashboard.
    reason?: string | null;
  } | null>(null);
  // Tracks the most recent cancel attempt so the operator gets
  // feedback in the UI. The pending list disappearing is implicit
  // confirmation; an explicit message covers the case where the
  // broker says "already gone" (the row is no longer in the list,
  // but the operator wants to know why their click "did nothing").
  const [cancelMessage, setCancelMessage] = useState<{
    type: "success" | "info" | "error";
    text: string;
  } | null>(null);

  // Lot-size aware quantity. F&O trades in whole lots, so when the
  // instrument changes a quantity that isn't a multiple of the new lot snaps
  // to one lot (NIFTY's 65 carried over to a BANKNIFTY option, lot 30, and
  // Fyers rejected it; a default qty other than 1 was never snapped at all).
  // Leaving F&O for a cash symbol drops back to the default quantity rather
  // than carrying a lot-sized share count into an equity ticket.
  const prevLot = useRef(1);
  useEffect(() => {
    const lot = selected && selected.lot_size > 1 ? selected.lot_size : 1;
    const was = prevLot.current;
    prevLot.current = lot;
    if (lot > 1) {
      if (quantity % lot !== 0) setQuantity(lot);
    } else if (was > 1) {
      setQuantity(loadUserPrefs().defaultQty);
    }
  }, [selected?.symbol, selected?.lot_size]);  // eslint-disable-line react-hooks/exhaustive-deps

  const placeOrder = usePlaceOrder();
  const chartPlace = usePlaceOrder();   // chart / DOM / scalper orders never lock the ticket button
  const cancelOrder = useCancelOrder();

  // Broker state drawn on the chart: open position average + pending
  // order prices for the charted symbol.
  const brokerLines = useMemo<BrokerLine[]>(() => {
    if (!selected) return [];
    const lines: BrokerLine[] = [];
    for (const o of pending?.orders ?? []) {
      if (o.symbol === selected.symbol && o.price > 0) {
        lines.push({
          price: o.price,
          title: `${o.side} ${o.quantity} ${o.order_type}`,
          kind: "order",
        });
      }
    }
    return lines;
  }, [selected, pending]);

  // The open position on the charted symbol + the exits the trade manager
  // is watching for it: drawn as a live-P&L line with draggable SL / TP.
  const qc = useQueryClient();
  type Managed = { symbol: string; stop_loss: number | null; target: number | null };
  const { data: managed } = useQuery<Managed[]>({
    queryKey: ["managed-positions"],
    queryFn: async () => {
      const r = await fetch("/api/positions/managed");
      return r.ok ? r.json() : [];      // 503 when the trade manager isn't running
    },
    refetchInterval: 3000,
  });
  // Position line / exits / close for ANY charted symbol (every layout
  // cell, the scalper's legs).
  const positionFor = (sym: string): ChartPosition | null => {
    const pos = positions?.find((p) => p.symbol === sym && p.quantity !== 0);
    if (!pos) return null;
    const m = managed?.find((x) => x.symbol === sym);
    return { qty: pos.quantity, avg: pos.average_price, sl: m?.stop_loss ?? null, tp: m?.target ?? null };
  };
  const levelsFor = (sym: string) => async (sl: number | null, tp: number | null) => {
    const r = await fetch(`/api/positions/${encodeURIComponent(sym)}/levels`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stop_loss: sl, target: tp }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail ?? `HTTP ${r.status}`);
    qc.setQueryData<Managed[]>(["managed-positions"], (old) => [...(old ?? []).filter((x) => x.symbol !== j.managed.symbol), j.managed]);
  };
  const closeFor = (sym: string) => async () => {
    const r = await fetch(`/api/positions/${encodeURIComponent(sym)}/close`, { method: "POST" });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail ?? `HTTP ${r.status}`);
    void qc.invalidateQueries({ queryKey: ["positions"] });
    void qc.invalidateQueries({ queryKey: ["managed-positions"] });
  };

  const [deskTab, setDeskTab] = useState<Record<"book" | "fno", DeskTab>>({ book: "depth", fno: "futures" });
  // DOM ladder click → limit ticket on that side; Buy/Sell Mkt → market ticket.
  const onBookPrice = (price: number, s: "BUY" | "SELL") => {
    setSide(s);
    onPickPrice(price);
  };
  const onBookMarket = (s: "BUY" | "SELL") => {
    if (!dock.includes("trade")) toggleDock("trade");
    setSide(s);
    setOrderType("MARKET");
  };

  // Chart "⤷ Ticket" tool → prefill the ticket as a LIMIT order.
  const onPickPrice = (price: number) => {
    if (!dock.includes("trade")) toggleDock("trade");
    setOrderType("LIMIT");
    setLimitPrice(String(roundToTick(price, selected?.tick_size ?? 0.05)));
  };

  const isOption =
    selected != null && (selected.instrument_type === "CE" || selected.instrument_type === "PE");
  const isFuture = selected != null && selected.instrument_type === "FUT";
  // An F&O contract whose lot the backend doesn't know: never guess 1 (the
  // exchange rejects a non-lot quantity); the backend refuses it too.
  const lotUnknown = (isOption || isFuture) && !(selected!.lot_size > 0);

  // Fyers v3 price rules: LIMIT and STOP_LOSS (SL-L, stop-limit) both
  // need a limit price; STOP_LOSS (SL-L) and SL-M both need a stop /
  // trigger price. MARKET needs neither.
  const requiresLimit = orderType === "LIMIT" || orderType === "STOP_LOSS";
  const requiresStop = orderType === "STOP_LOSS" || orderType === "SL-M";

  const onSelect = (h: InstrumentHit, fromChain = false, keepChain = false) => {
    if (h.symbol !== selected?.symbol) {
      // Prices belong to the instrument they were typed for. Carrying a
      // RELIANCE limit / SL into an option ticket sent a wildly marketable
      // limit, or armed a stop at another instrument's price on fill.
      // Points / % levels are relative to the entry, so those stay.
      setLimitPrice("");
      setStopPrice("");
      if (slMode === "price") setSlPrice("");
      if (tpMode === "price") setTpPrice("");
    }
    setSelected(h);
    setShowResults(false);
    setQuery(""); // the watchlist row + chart header show the pick; the box is for the next search
    setLastResult(null);
    if (isUnderlying(h) && !keepChain) {
      setChainBase(h);
      try { tabLocal.setItem("trade:chainBase", JSON.stringify(h)); } catch { /* best-effort */ }
      setSelectedExpiry(null); // load the nearest expiry for the new underlying
    }
    // An option picked off the chain opens its chart + ticket without
    // flooding the watchlist; a searched symbol joins it.
    if (!fromChain) addToWatch(h);
    try { tabLocal.setItem(LAST_KEY, JSON.stringify(h)); } catch { /* best-effort */ }
    // Intraday-only bot — F&O included. Every ticket is MIS/INTRADAY; the
    // backend rejects anything else, so there is nothing per-instrument to set.
    setOrderType(loadUserPrefs().defaultOrderType);
  };

  const sp = splits[layout] ?? { x: layout === "3" ? 0.6 : 0.5, y: 0.5 };
  const hasX = layout === "2" || layout === "3" || layout === "4";
  const hasY = layout === "2v" || layout === "3" || layout === "4";
  const gridStyle: React.CSSProperties = {
    ...(hasX ? { gridTemplateColumns: `${sp.x}fr ${1 - sp.x}fr` } : {}),
    ...(hasY ? { gridTemplateRows: `${sp.y}fr ${1 - sp.y}fr` } : {}),
  };

  // A new layout keeps the active symbol in the first cell and fills new
  // cells from the watchlist (then the active symbol).
  const pickLayout = (id: Layout) => {
    const n = LAYOUTS.find((l) => l.id === id)?.n ?? 1;
    const out: (InstrumentHit | null)[] = [selected, ...cells.filter((_, i) => i !== activeCell)].slice(0, n);
    const used = new Set(out.filter(Boolean).map((h) => h!.symbol));
    for (let i = 0; i < n; i++) {
      if (out[i]) continue;
      const next = recent.find((r) => !used.has(r.symbol)) ?? selected;
      out[i] = next ?? null;
      if (next) used.add(next.symbol);
    }
    setCells(out);
    setActiveCell(0);
    setLayout(id);
    setScalper(false);
    setLayoutOpen(false);
    setMaxCell(null);
    try {
      tabLocal.setItem("trade:layout", JSON.stringify(id));
      tabLocal.setItem("trade:cells", JSON.stringify(out));
    } catch { /* best-effort */ }
  };
  const activate = (i: number) => {
    if (i === activeCell) return;
    setActiveCell(i);
    const h = cells[i];
    if (h && h.symbol !== selected?.symbol) onSelect(h, true);
  };

  // The strike nearest the spot — highlighted as ATM in the chain.
  const atmStrike = useMemo<number | null>(() => {
    if (!chain || chain.spot == null || chain.strikes.length === 0) return null;
    let best = chain.strikes[0].strike;
    let bestDist = Infinity;
    for (const s of chain.strikes) {
      const d = Math.abs(s.strike - chain.spot);
      if (d < bestDist) {
        bestDist = d;
        best = s.strike;
      }
    }
    return best;
  }, [chain]);

  // Largest OI on the ladder: the OI bars' 100%. Once per chain, not per row.
  const chainMaxOi = useMemo(
    () => Math.max(1, ...(chain?.strikes ?? []).map((x) => Math.max(x.ce?.oi ?? 0, x.pe?.oi ?? 0))),
    [chain],
  );

  const atmRef = useRef<HTMLTableRowElement | null>(null);
  useEffect(() => {
    atmRef.current?.scrollIntoView?.({ block: "center" });
  }, [chainOn, chainBase?.symbol, chain?.selected_expiry, chain != null]);  // eslint-disable-line react-hooks/exhaustive-deps

  // Click a CE/PE cell in the chain → load that option into the ticket.
  const optionHit = (row: { strike: number; ce: OptionLeg | null; pe: OptionLeg | null }, type: "CE" | "PE"): InstrumentHit | null => {
    const leg = type === "CE" ? row.ce : row.pe;
    if (!leg || !chainBase) return null;
    // Name the option by its F&O root (NIFTY, not the index's NIFTY50) and
    // carry the chain's expiry, so two expiries of one strike tell apart.
    const root = optionRoot(chainBase);
    const exp = chain?.expiries.find((e) => e.ts === chain.selected_expiry)?.label ?? null;
    const name = `${root} ${row.strike} ${type}`;
    return {
      symbol: leg.symbol,
      short_name: name,
      exchange: leg.symbol.includes(":") ? leg.symbol.split(":")[0] : chainBase.exchange,
      segment: "FO",
      instrument_type: type,
      lot_size: leg.lot_size ?? 0,
      tick_size: leg.tick_size,
      expiry: expiryIso(exp),
      strike: row.strike,
      underlying: root,
      display: exp ? `${name}  ·  ${exp}` : name,
    };
  };
  const onSelectOption = (
    row: { strike: number; ce: OptionLeg | null; pe: OptionLeg | null },
    type: "CE" | "PE",
  ) => {
    const h = optionHit(row, type);
    if (!h) return;
    onSelect(h, true);
    if (!dock.includes("trade")) toggleDock("trade");   // the ticket is where the click is going
  };
  // Drag an option off the chain onto any chart cell to open it there.
  const dragOption = (row: { strike: number; ce: OptionLeg | null; pe: OptionLeg | null }, type: "CE" | "PE") => (e: React.DragEvent) => {
    const h = optionHit(row, type);
    if (!h) return;
    e.dataTransfer.setData(DRAG_MIME, JSON.stringify(h));
    e.dataTransfer.effectAllowed = "copy";
  };
  const dropOnCell = (i: number) => (e: React.DragEvent) => {
    const raw = e.dataTransfer.getData(DRAG_MIME);
    if (!raw) return;
    e.preventDefault();
    try {
      const h = JSON.parse(raw) as InstrumentHit;
      // Switch the target cell straight to the dropped option. Going through
      // activate(i) would first select that cell's OLD stock — which moved
      // the option chain to it. A drop never changes the chain.
      setActiveCell(i);
      onSelect(h, true, true);
    } catch { /* not ours */ }
  };
  // The chain's own search: changes only the chain, never a chart.
  const [chainQuery, setChainQuery] = useState("");
  const { data: chainHits } = useSearchSymbols(chainQuery);
  const pickChainBase = (h: InstrumentHit) => {
    setChainBase(h);
    setChainQuery("");
    setSelectedExpiry(null);
    try { tabLocal.setItem("trade:chainBase", JSON.stringify(h)); } catch { /* best-effort */ }
  };

  // An order from a chart (right-click menu, scalper buttons) on the ticket's
  // account. Risk-blocked or rejected orders come back as errors (the ticket
  // is where a risk override is typed).
  // `product` defaults to intraday; exits and reversals of a carry position
  // pass its own product so the order nets it off at Fyers.
  const orderFor = (sym: string, name: string, qty: number, productType: ProductType = "INTRADAY") => async (o: ChartOrder): Promise<string> => {
    if (!accountId) throw new Error(noAccountReason ?? "no live Fyers account connected");
    if (!(qty > 0)) throw new Error("quantity must be more than 0");
    const r = await chartPlace.mutateAsync({
      account_id: accountId,
      symbol: sym,
      side: o.side,
      quantity: qty,
      order_type: o.type,
      limit_price: o.type === "LIMIT" ? o.price : o.type === "STOP_LOSS" ? o.limit ?? o.price : null,
      stop_price: o.type === "SL-M" || o.type === "STOP_LOSS" ? o.price : null,
      product_type: productType,
      bypass_risk: false,
      operator: "ui_chart",
    });
    if (r.status === "REJECTED" || r.status === "REJECTED_RISK" || r.ok === false) {
      throw new Error(cleanError(r.error || r.risk_message, "broker rejected the order"));
    }
    return `${o.side} ${qty} ${name} ${o.type === "STOP_LOSS" ? "STOP-LIMIT" : o.type}${o.price != null ? ` @ ${o.price}` : ""}${o.type === "STOP_LOSS" && o.limit != null ? ` lmt ${o.limit}` : ""} → ${r.status}`;
  };

  const isFnoSel = selected != null && selected.segment !== "EQ" && selected.instrument_type !== "EQ";
  const carryProduct: ProductType = isFnoSel ? "MARGIN" : "DELIVERY";
  const [defaultProduct, setDefaultProduct] = useState<ProductType>(() => stored<ProductType>("trade:defaultProduct", "INTRADAY"));
  const [productPick, setProduct] = useState<ProductType>(defaultProduct);
  // DELIVERY and MARGIN are the same choice ("carry") on cash vs F&O.
  const product: ProductType = productPick === "INTRADAY" ? "INTRADAY" : carryProduct;
  const saveDefaultProduct = (p: ProductType) => {
    setDefaultProduct(p);
    try { tabLocal.setItem("trade:defaultProduct", JSON.stringify(p)); } catch { /* best-effort */ }
  };
  const [slPrice, setSlPrice] = useState("");
  const [tpPrice, setTpPrice] = useState("");
  const tick = selected?.tick_size ?? 0.05;
  const limitNum = requiresLimit && Number(limitPrice) > 0 ? roundToTick(Number(limitPrice), tick) : null;
  const stopNum = requiresStop && Number(stopPrice) > 0 ? roundToTick(Number(stopPrice), tick) : null;
  // The price the order should fill near: its limit, an SL-M's trigger, else
  // the touch on the side being hit.
  const estPrice = limitNum ?? stopNum ?? (side === "BUY" ? ask : bid) ?? ltp;
  const [slMode, setSlMode] = useState<LevelInputMode>(() => stored<LevelInputMode>("trade:slMode", "price"));
  const [tpMode, setTpMode] = useState<LevelInputMode>(() => stored<LevelInputMode>("trade:tpMode", "price"));
  useEffect(() => { try { tabLocal.setItem("trade:slMode", JSON.stringify(slMode)); tabLocal.setItem("trade:tpMode", JSON.stringify(tpMode)); } catch { /* best-effort */ } }, [slMode, tpMode]);
  const slAbs = levelPrice(Number(slPrice), slMode, estPrice, side, "sl", tick);
  const tpAbs = levelPrice(Number(tpPrice), tpMode, estPrice, side, "target", tick);
  const check: TicketCheck = {
    side, orderType, quantity,
    lotSize: selected && selected.lot_size > 1 ? selected.lot_size : 1,
    limit: limitNum, stop: stopNum, ltp: ltp ?? null, entry: estPrice ?? null, sl: slAbs, tp: tpAbs,
  };
  const problem = !selected ? "Pick a symbol." : !accountId ? noAccountReason
    : lotUnknown ? "Lot size unknown for this contract." : ticketProblem(check);
  const canSubmit = problem == null;
  const ticketWarn = canSubmit ? ticketWarning(check) : null;
  const { data: funds } = useQuery<{ ok: boolean; available: number | null }>({
    queryKey: ["market-funds"],
    queryFn: () => fetch("/api/market/funds").then((r) => r.json()),
    refetchInterval: 30000,
  });

  const onSubmit = async (opts?: { bypassRisk?: boolean; resend?: PlaceOrderRequest }) => {
    if (!opts?.resend && (!selected || !accountId || !canSubmit)) return;
    // The risk override re-sends the order that was blocked, not whatever the
    // ticket holds now (the side / qty / symbol may have been changed since).
    const body: PlaceOrderRequest = opts?.resend ? { ...opts.resend, bypass_risk: true } : {
      account_id: accountId!,
      symbol: selected!.symbol,
      side,
      quantity: Number(quantity),
      order_type: orderType,
      // Fyers rejects a price off the instrument's tick grid.
      limit_price: limitNum,
      stop_price: stopNum,
      product_type: product,
      stop_loss: slAbs,
      target: tpAbs,
      bypass_risk: opts?.bypassRisk ?? false,
      operator: "ui_trade_page",
    };
    try {
      const r = await placeOrder.mutateAsync(body);
      if (r.status === "REJECTED" || r.status === "REJECTED_RISK" || r.ok === false) {
        setLastResult({
          type: "error",
          message: cleanError(
            r.error || r.risk_message,
            "broker rejected the order",
          ),
          detail: body,
          reason: r.reason ?? null,
        });
      } else {
        // What was sent, in the words a trader reads it: side, size, name,
        // type and price, then the broker's status and order id.
        const name = selected?.symbol === body.symbol ? selected.short_name || body.symbol : body.symbol;
        const px = body.order_type === "LIMIT" ? ` @ ${body.limit_price}`
          : body.order_type === "SL-M" ? ` trigger ${body.stop_price}`
          : body.order_type === "STOP_LOSS" ? ` trigger ${body.stop_price} limit ${body.limit_price}` : "";
        const levels = [body.stop_loss != null ? `SL ${body.stop_loss}` : "", body.target != null ? `target ${body.target}` : ""].filter(Boolean).join(", ");
        setLastResult({
          type: "success",
          // The order id is not a price — "@ 2406..." read as a fill price.
          message: `${r.status} · ${body.side} ${body.quantity} ${name} · ${ORDER_TYPE_LABEL[body.order_type]}${px}`
            + `${levels ? ` · ${levels} set once it fills` : ""}${r.broker_order_id ? ` · order ${r.broker_order_id}` : ""}`,
          // Risk is advisory for manual orders — surface it without blocking.
          warning: r.risk_warning ?? (r.risk_message ? r.risk_message : null),
          recordWarning: r.warning ?? null,
          detail: body,
        });
        // The stop loss / target belonged to this order. Left in the boxes
        // they'd ride along on the next one, an exit included.
        setSlPrice("");
        setTpPrice("");
      }
    } catch (e) {
      setLastResult({
        type: "error",
        message: cleanError(
          (e as Error).message,
          "place-order request failed",
        ),
        detail: body,
      });
    }
  };

  const onCancel = async (brokerOrderId: string, label?: string) => {
    // "BUY 10 NSE:SBIN-EQ (2410…)": the order id alone means nothing to a person
    const what = label ? `${label} (${brokerOrderId})` : brokerOrderId;
    if (!accountId) {
      // Cancels go through a live account; without one the click used to do nothing at all.
      setCancelMessage({ type: "error", text: `Not cancelled — ${what}: connect a live Fyers account to cancel orders.` });
      return;
    }
    setCancelMessage(null);
    try {
      const r = await cancelOrder.mutateAsync({
        account_id: accountId,
        broker_order_id: brokerOrderId,
      });
      // ok:false means the broker didn't cancel it. The backend then
      // checks what the order really is: already cancelled / rejected
      // (dropped from the list), already filled (now a position), or
      // still working — the one case the operator must act on.
      if (r.ok) {
        setCancelMessage({ type: "success", text: `Cancelled ${what}` });
      } else if (r.reason === "already_gone" || r.reason === "already_filled") {
        setCancelMessage({
          type: "info",
          text: `${what}: ${r.message ?? "the order was no longer working at the broker"}.`,
        });
      } else {
        setCancelMessage({
          type: "error",
          text: `Not cancelled — ${what}: ${r.message ?? "the broker refused the cancel; the order may still be working"}.`,
        });
      }
    } catch (e) {
      setCancelMessage({
        type: "error",
        text: `Cancel failed for ${what}: ${cleanError(
          (e as Error).message,
          "request failed",
        )}`,
      });
    }
  };

  // Requests from the chart (open a panel, save the layout, maximize …).
  const hostAction = (a: HostAction) => {
    const desk: Partial<Record<HostAction, [DockId, DeskTab]>> = {
      "panel:depth": ["book", "depth"], "panel:tape": ["book", "tape"], "panel:futures": ["fno", "futures"], "panel:strategy": ["fno", "strategy"],
    };
    const dk = desk[a];
    if (dk) {
      setDeskTab((t) => ({ ...t, [dk[0]]: dk[1] }));
      if (!dock.includes(dk[0])) saveDock([...dock, dk[0]]);
      return;
    }
    if (a.startsWith("panel:")) {
      const id = a.slice(6) as DockId;
      if (DOCK.some((d) => d.id === id) && !dock.includes(id)) saveDock([...dock, id]);
      return;
    }
    if (a.startsWith("bottom:")) {
      openBottom(a.slice(7) as BottomTab);
      return;
    }
    switch (a) {
      case "usersettings": setUserSettingsOpen(true); return;
      case "ticket:buy": onBookMarket("BUY"); return;
      case "ticket:sell": onBookMarket("SELL"); return;
      case "logout": {
        if (window.confirm("Log out of the Fyers trading session? Orders already at the broker stay working; reconnect from Accounts.")) {
          fyersDisconnect.mutate(undefined, { onSuccess: () => setLastResult({ type: "success", message: "Logged out of Fyers — reconnect from Accounts" }) });
        }
        return;
      }
      case "scalper": setScalper(true); return;
      case "layouts": setLayoutMenuOpen(true); return;
      case "save": L.save(); return;
      case "maximize": setMaxCell((m) => (m === null && nCells > 1 ? activeCell : null)); return;
      case "watch:add": if (selected) addToWatch(selected); return;
      case "privacy": {
        const v = !privacy;
        setPrivacy(v);
        try { tabLocal.setItem("trade:privacy", JSON.stringify(v)); } catch { /* best-effort */ }
        return;
      }
    }
  };
  const tradingFlags = { instant: tset.instant, showPos: tset.showPos, showOrders: tset.showOrders, plus: tset.plus };
  const shownCells = maxCell !== null && maxCell < nCells ? [maxCell] : Array.from({ length: nCells }, (_, i) => i);

  // -------- render --------

  return (
    <div className="trade-page tv">
      <div className="tv-center">
        <div className="tv-chart">
          {scalper ? (
            <Scalper
              base={chainBase}
              underlyings={[...(chainBase ? [chainBase] : []), ...recent.filter((h) => isUnderlying(h) && h.symbol !== chainBase?.symbol)]}
              onBase={(h) => onSelect(h, true)}
              chain={chain}
              expiry={selectedExpiry}
              onExpiry={setSelectedExpiry}
              atmStrike={atmStrike}
              positions={positions}
              helpers={{ positionFor: (sym) => (tset.showPos ? positionFor(sym) : null), levelsFor, closeFor, orderFor }}
              instant={tset.instant}
              onInstant={(v) => changeSetting("instant", v)}
              settings={settingsEl}
              showPlus={tset.plus}
            />
          ) : (
            <div className="tv-layout">
            <div className="tv-topbar"><div className="tv-topslot" ref={setTopSlot} /><LayoutMenu L={L} open={layoutMenuOpen} onOpen={setLayoutMenuOpen} />{settingsEl}</div>
            <div className="tv-chartrow">
            <div className="tv-leftbar" ref={setLeftSlot} />
            <div className={`tv-grid l${maxCell !== null ? "1" : layout}`} style={maxCell !== null ? undefined : gridStyle}>
              {shownCells.map((i) => {
                const h = i === activeCell ? selected : cells[i] ?? null;
                const active = i === activeCell;
                return (
                  <div key={i} className={`tv-cell${active && nCells > 1 ? " active" : ""}`} onMouseDownCapture={() => activate(i)}
                    onDragOver={(e) => { if (e.dataTransfer.types.includes(DRAG_MIME)) { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; } }}
                    onDrop={dropOnCell(i)} data-testid={`tv-cell-${i}`}>
                    {/* key={symbol} remounts the panel per symbol so its candle
                        store, drawings and pagination reset cleanly. */}
                    {h && (
                      <ChartPanel
                        key={h.symbol}
                        symbol={h.symbol}
                        shortName={h.short_name}
                        brokerLines={active && tset.showOrders ? brokerLines : undefined}
                        onPickPrice={active ? onPickPrice : undefined}
                        position={tset.showPos ? positionFor(h.symbol) : null}
                        onLevels={levelsFor(h.symbol)}
                        onClosePosition={closeFor(h.symbol)}
                        onChartOrder={active ? orderFor(h.symbol, h.short_name, Number(quantity)) : undefined}
                        orderQty={active ? Number(quantity) : undefined}
                        onOrderQty={active ? (n) => setQuantity(n) : undefined}
                        toolbarSlot={active ? topSlot : undefined}
                        toolsSlot={active ? leftSlot : undefined}
                        rangeSlot={active ? rangeSlot : undefined}
                        widgetSlots={active ? { data: dataSlot, tree: treeSlot, alerts: alertsSlot } : undefined}
                        chrome={active}
                        instant={tset.instant}
                        showPlus={tset.plus}
                        trading={tradingFlags}
                        onTrading={changeSetting}
                        instrument={h}
                        onSymbolChange={active ? (x) => onSelect(x) : undefined}
                        recentSymbols={recent}
                        onAction={hostAction}
                        syncId={`cell${i}`}
                        sync={sync}
                        multi={nCells > 1}
                        maximized={maxCell === i}
                        privacy={privacy}
                      />
                    )}
                    {h && !active && <KeepLive symbol={h.symbol} />}
              {!h && (
                <section className="trade-card tv-empty">
                  <div className="empty">
                    No symbol open.{" "}
                    <button type="button" className="btn-sm" onClick={() => { if (!dock.includes("watch")) toggleDock("watch"); setTimeout(() => document.querySelector<HTMLInputElement>('[data-testid="trade-search"]')?.focus(), 0); }}>
                      Search a symbol
                    </button>
                  </div>
                </section>
              )}
                  </div>
                );
              })}
              {maxCell === null && hasX && (
                <div className="split-x" style={{ left: `calc(${sp.x * 100}% - 3px)` }} title="Drag to resize · double-click to reset"
                  onPointerDown={(e) => splitDrag(e, "x", (f) => setSplits((z) => ({ ...z, [layout]: { ...sp, ...z[layout], x: f } })))}
                  onDoubleClick={() => setSplits((z) => ({ ...z, [layout]: { ...sp, x: 0.5 } }))} />
              )}
              {maxCell === null && hasY && (
                <div className="split-y" style={{ top: `calc(${sp.y * 100}% - 3px)`, left: layout === "3" ? `${sp.x * 100}%` : 0 }} title="Drag to resize · double-click to reset"
                  onPointerDown={(e) => splitDrag(e, "y", (f) => setSplits((z) => ({ ...z, [layout]: { ...sp, ...z[layout], y: f } })))}
                  onDoubleClick={() => setSplits((z) => ({ ...z, [layout]: { ...sp, y: 0.5 } }))} />
              )}
            </div>
            </div>
            <div className="tv-rangeslot" ref={setRangeSlot} />
            </div>
          )}
        </div>

        {/* ---- bottom bar: click a tab to open it, click it again (or ▾) to close; drag the top edge to resize ---- */}
        <div className={`tv-bottom${bottomOpen ? "" : " closed"}`} style={bottomOpen ? { height: bottomMax ? "70vh" : Math.min(bottomH, Math.max(120, win.h * 0.6)) } : undefined}>
          {bottomOpen && <div className="tv-resize" onPointerDown={(e) => { setBottomMax(false); drag(e, "y", Math.min(bottomH, Math.max(120, win.h * 0.6)), setBottomH, 120, window.innerHeight * 0.6, "trade:bottomH"); }} title="Drag to resize" />}
          {!bottomOpen && (
            <div className="tabs trade-tabs" role="tablist">
              <button type="button" role="tab" aria-selected={false} className="tab am-trade-tab" onClick={() => openBottom("trade")} title="DOM — trade from the price ladder">Trade</button>
              {BOTTOM_TABS.map(([k, l]) => {
                const n = k === "positions" ? positions?.filter((p) => p.quantity !== 0).length ?? 0 : k === "orders" ? pending?.count ?? 0 : 0;
                return <button key={k} type="button" role="tab" aria-selected={false} className="tab" onClick={() => openBottom(k)}>{l}{n ? ` (${n})` : ""}</button>;
              })}
              <button type="button" className="tab tv-collapse" onClick={() => openBottom(bottomTab)} title="Open panel">▴</button>
            </div>
          )}
          {bottomOpen && (
            <AccountManager
              tab={bottomTab}
              onTab={(t) => openBottom(t)}
              positions={positions}
              managed={managed}
              pendingOrders={pending?.orders ?? []}
              onCancel={(id, label) => void onCancel(id, label)}
              cancelBusyId={cancelOrder.isPending ? cancelOrder.variables?.broker_order_id ?? null : null}
              cancelBanner={cancelMessage && (
                <div className={`result ${cancelMessage.type}`} data-testid="cancel-result" onClick={() => setCancelMessage(null)} title="Click to dismiss" style={{ cursor: "pointer" }}>
                  {cancelMessage.text}
                </div>
              )}
              accountId={accountId}
              privacy={privacy}
              connected={accountId != null}
              accountLabel={account ? `${account.name} · INR` : ""}
              selected={selected}
              closeFor={closeFor}
              levelsFor={levelsFor}
              orderFor={orderFor}
              instant={tset.instant}
              qty={Number(quantity)}
              onQty={(n) => setQuantity(n)}
              onLogout={() => hostAction("logout")}
              maximized={bottomMax}
              onMaximize={() => setBottomMax((v) => !v)}
              onCollapse={() => openBottom(null)}
            />
          )}
        </div>
      </div>

      {/* ---- right dock: the panels opened from the icon bar, stacked; each scrolls inside itself ---- */}
      {dock.length > 0 && (
        <div className="tv-dock" style={{ width: dockWidth }}>
          <div className="tv-dock-resize" onPointerDown={(e) => drag(e, "x", dockWidth, setDockW, 280, Math.min(760, dockCap), "trade:dockW")} title="Drag to resize" />
          {dock.includes("watch") && (
            <section className="dock-panel" data-testid="dock-watch" style={panelStyle("watch")}>
              {dockGrip("watch")}
              {dockHead("watch")}
              <div className="dock-body">
                <div className="trade-search" ref={searchRef}>
                <div className="trade-search-bar">
                  <input
                    type="text"
                    value={query}
                    onChange={(e) => {
                      setQuery(e.target.value);
                      setShowResults(true);
                      setHighlightIdx(-1);
                      setLastResult(null);
                    }}
                    onFocus={() => {
                      // Reopen only for a genuine query — not for the display
                      // string a selection left behind (which has no hits and
                      // would show a stale "no results" box).
                      if (query && query !== selected?.display) setShowResults(true);
                    }}
                    onKeyDown={(e) => {
                      const hits = searchData?.hits ?? [];
                      if (e.key === "Escape") {
                        setShowResults(false);
                        setHighlightIdx(-1);
                        return;
                      }
                      if (!showResults || hits.length === 0) return;
                      if (e.key === "ArrowDown") {
                        e.preventDefault();
                        setHighlightIdx((i) => (i + 1) % hits.length);
                      } else if (e.key === "ArrowUp") {
                        e.preventDefault();
                        setHighlightIdx((i) => (i <= 0 ? hits.length - 1 : i - 1));
                      } else if (e.key === "Enter") {
                        e.preventDefault();
                        const pick = hits[highlightIdx >= 0 ? highlightIdx : 0];
                        if (pick) {
                          onSelect(pick);
                          setHighlightIdx(-1);
                        }
                      }
                    }}
                    placeholder="+ Add symbol — RELIANCE, NIFTY…"
                    data-testid="trade-search"
                    autoComplete="off"
                  />
                  <button
                    type="button"
                    className="btn-sm"
                    onClick={() => refreshInstruments.mutate()}
                    disabled={refreshInstruments.isPending}
                    data-testid="refresh-instruments"
                    title="Download the full NSE + BSE stock list from Fyers"
                  >
                    {refreshInstruments.isPending
                      ? "loading…"
                      : refreshInstruments.isSuccess
                      ? `✓ ${refreshInstruments.data?.instrument_count?.toLocaleString() ?? ""} symbols`
                      : "↻ NSE/BSE"}
                  </button>
                </div>
                {showResults && query && (
                  <div className="trade-search-results" data-testid="trade-search-results">
                    {searching && <div className="hint">searching…</div>}
                    {!searching && (searchData?.count ?? 0) === 0 && (
                      <div className="hint">no results for "{query}"</div>
                    )}
                    {(searchData?.hits ?? []).map((h, i) => (
                      <button
                        key={h.symbol}
                        type="button"
                        className={`trade-search-row${i === highlightIdx ? " hl" : ""}`}
                        onClick={() => onSelect(h)}
                        onMouseEnter={() => setHighlightIdx(i)}
                        data-testid={`search-row-${h.symbol}`}
                      >
                        <span className="sym">{h.short_name}</span>
                        <span className="exch">{h.exchange}:{h.segment}</span>
                        <span className="disp">{h.display}</span>
                        {h.lot_size > 1 && <span className="lot">lot {h.lot_size}</span>}
                      </button>
                    ))}
                  </div>
                )}
                </div>
                <WatchlistTable
                  state={wl}
                  quotes={watchQuotes?.quotes}
                  selected={selected?.symbol ?? null}
                  onSelect={(h) => onSelect(h)}
                  onChange={(next) => updateWl(() => next)}
                />
              </div>
            </section>
          )}
          {dock.includes("trade") && (
            <section className="dock-panel dock-trade" data-testid="dock-trade" style={panelStyle("trade")}>
              {dockGrip("trade")}
              {dockHead("trade", selected?.short_name)}
              <div className="dock-body">
                {/* QUOTE */}
                <section className="trade-card" data-testid="trade-quote">
                  <h2>Quote</h2>
                  {!selected ? (
                    <div className="empty">Pick a symbol above to start.</div>
                  ) : (
                    <div>
                      <div className="quote-sym">
                        <span className="big">{selected.short_name}</span>
                        <span className="exch">{selected.exchange}:{selected.segment}</span>
                        {isOption && <span className="badge opt">OPTION</span>}
                        {isFuture && <span className="badge fut">FUTURE</span>}
                        {!isOption && !isFuture && (selected.segment === "INDEX"
                          ? <span className="badge idx">INDEX</span>
                          : <span className="badge cash">CASH</span>)}
                      </div>
                      <div className="quote-fullsym">{selected.symbol}</div>
                      <div className="quote-row">
                        <div className="quote-cell">
                          <div className="k">LTP</div>
                          <div className="v big" data-testid="quote-ltp">
                            {ltp != null ? fmtMoney(ltp) : "—"}
                          </div>
                        </div>
                        <div className="quote-cell">
                          <div className="k">BID</div>
                          <div className="v">{bid != null ? fmtMoney(bid) : "—"}</div>
                        </div>
                        <div className="quote-cell">
                          <div className="k">ASK</div>
                          <div className="v">{ask != null ? fmtMoney(ask) : "—"}</div>
                        </div>
                        <div className="quote-cell">
                          <div className="k">LOT</div>
                          <div className="v">{selected.lot_size > 0 ? selected.lot_size : "—"}</div>
                        </div>
                      </div>
                      {!quote?.ok && (
                        <div className="hint warn-text">
                          {quote?.reason ?? "no quote cached for this symbol"}
                        </div>
                      )}
                    </div>
                  )}
                </section>
                {/* TICKET */}
                <section className={`trade-card${account ? " ticket-live" : ""}`} data-testid="trade-ticket">
                  <h2>
                    Ticket{" "}
                    <TicketModeBadge account={account} botMode={botMode} />
                  </h2>
                  {!account && noAccountReason && (
                    <div className="hint warn-text" data-testid="ticket-mode-note">{noAccountReason}</div>
                  )}
                  {account && botMode === "paper" && (
                    // The bot's TRADING_MODE only steers its own signals; a
                    // manual order here always goes to the broker.
                    <div className="hint warn-text" data-testid="ticket-mode-note">
                      The bot is in paper mode, but orders from this page still go to Fyers with real money.
                    </div>
                  )}
                  <div className="ticket-side" role="group" aria-label="side">
                    <button
                      className={`ticket-side-btn buy ${side === "BUY" ? "on" : ""}`}
                      onClick={() => setSide("BUY")}
                      data-testid="ticket-side-buy"
                    >
                      BUY
                    </button>
                    <button
                      className={`ticket-side-btn sell ${side === "SELL" ? "on" : ""}`}
                      onClick={() => setSide("SELL")}
                      data-testid="ticket-side-sell"
                    >
                      SELL
                    </button>
                  </div>
  
                  {lotUnknown && (
                    <div className="hint warn-text" data-testid="ticket-lot-unknown">
                      Lot size unknown for this contract (the F&amp;O scrip master hasn&apos;t loaded), so it can&apos;t be ordered yet.
                    </div>
                  )}
                  {selected && selected.lot_size > 1 ? (
                    /* F&O trades in whole lots: enter lots, the quantity follows. */
                    <label className="ticket-row">
                      <span>Lots</span>
                      <IntField
                        value={Math.max(1, Math.round(quantity / selected.lot_size))}
                        onCommit={(n) => setQuantity(n * selected.lot_size)}
                        testid="ticket-lots"
                      />
                      <span className="hint" data-testid="ticket-qty">= {quantity} qty ({selected.lot_size}/lot)</span>
                    </label>
                  ) : (
                    <label className="ticket-row">
                      <span>Quantity</span>
                      <IntField value={quantity} onCommit={setQuantity} testid="ticket-qty" />
                    </label>
                  )}
  
                  <label className="ticket-row">
                    <span>Order type</span>
                    <select
                      value={orderType}
                      onChange={(e) => setOrderType(e.target.value as OrderType)}
                      data-testid="ticket-type"
                    >
                      <option value="MARKET">Market</option>
                      <option value="LIMIT">Limit</option>
                      <option value="STOP_LOSS">Stop-limit (SL)</option>
                      <option value="SL-M">Stop-market (SL-M)</option>
                    </select>
                  </label>
  
                  {requiresLimit && (
                    <label className="ticket-row">
                      <span>Limit price</span>
                      <input
                        type="number"
                        step={tick}
                        value={limitPrice}
                        onChange={(e) => setLimitPrice(e.target.value)}
                        data-testid="ticket-limit"
                      />
                      <span className="px-quick">
                        {bid != null && (
                          <button
                            type="button"
                            className="px-quick-btn"
                            onClick={() => setLimitPrice(String(roundToTick(bid, tick)))}
                            title={`bid ${fmtMoney(bid)}`}
                            data-testid="limit-fill-bid"
                          >
                            bid
                          </button>
                        )}
                        {ltp != null && (
                          <button
                            type="button"
                            className="px-quick-btn"
                            onClick={() => setLimitPrice(String(roundToTick(ltp, tick)))}
                            title={`LTP ${fmtMoney(ltp)}`}
                            data-testid="limit-fill-ltp"
                          >
                            ltp
                          </button>
                        )}
                        {ask != null && (
                          <button
                            type="button"
                            className="px-quick-btn"
                            onClick={() => setLimitPrice(String(roundToTick(ask, tick)))}
                            title={`ask ${fmtMoney(ask)}`}
                            data-testid="limit-fill-ask"
                          >
                            ask
                          </button>
                        )}
                      </span>
                    </label>
                  )}
  
                  {requiresStop && (
                    <label className="ticket-row">
                      <span>Trigger price</span>
                      <input
                        type="number"
                        step={tick}
                        value={stopPrice}
                        onChange={(e) => setStopPrice(e.target.value)}
                        data-testid="ticket-stop"
                      />
                      {ltp != null && (
                        <span className="px-quick">
                          <button
                            type="button"
                            className="px-quick-btn"
                            onClick={() => setStopPrice(String(roundToTick(ltp, tick)))}
                            title={`LTP ${fmtMoney(ltp)}`}
                            data-testid="stop-fill-ltp"
                          >
                            ltp
                          </button>
                        </span>
                      )}
                    </label>
                  )}
  
                  {/* Product: the bot itself is intraday-only; a manual order may carry
                      forward. The star saves the choice as the ticket default. */}
                  <div className="ticket-row">
                    <span>Product</span>
                    <span className="seg" data-testid="ticket-product">
                      {(["INTRADAY", carryProduct] as ProductType[]).map((p) => (
                        <button key={p} type="button" className={product === p ? "on" : ""} onClick={() => setProduct(p)} data-testid={`ticket-product-${p}`}
                          title={p === "INTRADAY" ? "MIS — squared off before the close" : p === "DELIVERY" ? "CNC — delivery, kept overnight" : "NRML — F&O carry-forward"}>
                          {PRODUCT_LABEL[p]}
                        </button>
                      ))}
                    </span>
                    <button type="button" className="px-quick-btn" onClick={() => saveDefaultProduct(product)} title="make this the ticket default"
                      data-testid="ticket-product-default">{defaultProduct === product ? "★ default" : "☆ set default"}</button>
                  </div>

                  <label className="ticket-row">
                    <span>Stop loss</span>
                    <input type="number" step={tick} min={0} placeholder="optional" value={slPrice} onChange={(e) => setSlPrice(e.target.value)} data-testid="ticket-sl" />
                    <LevelMode mode={slMode} onMode={setSlMode} testid="ticket-sl-mode" />
                    {slMode !== "price" && slAbs != null && <span className="hint" data-testid="ticket-sl-abs">→ {fmtMoney(slAbs)}</span>}
                  </label>
                  <label className="ticket-row">
                    <span>Target</span>
                    <input type="number" step={tick} min={0} placeholder="optional" value={tpPrice} onChange={(e) => setTpPrice(e.target.value)} data-testid="ticket-tp" />
                    <LevelMode mode={tpMode} onMode={setTpMode} testid="ticket-tp-mode" />
                    {tpMode !== "price" && tpAbs != null && <span className="hint" data-testid="ticket-tp-abs">→ {fmtMoney(tpAbs)}</span>}
                  </label>
                  <div className="ticket-row ticket-est" data-testid="ticket-est">
                    <span>Est. amount</span>
                    <span>{estPrice != null ? fmtMoney(estPrice * quantity) : "—"}</span>
                    <span className="hint">funds {funds?.ok && funds.available != null ? fmtMoney(funds.available) : "—"}</span>
                  </div>
  
                  {/* Submit */}
                  <div className="ticket-submit">
                    <button
                      className={`btn ${side === "BUY" ? "buy" : "sell"}`}
                      disabled={!canSubmit || placeOrder.isPending}
                      onClick={() => onSubmit()}
                      data-testid="ticket-submit"
                      title={account ? `Sends a real ${side} order to Fyers (${account.name})` : undefined}
                    >
                      {placeOrder.isPending ? "placing…" : selected ? `${side} ${quantity} ${selected.short_name || selected.symbol}` : `PLACE ${side}`}
                    </button>
                  </div>
                  {/* Say why PLACE is greyed out instead of leaving a dead button. */}
                  {problem && <div className="hint ticket-problem" data-testid="ticket-problem">{problem}</div>}
                  {ticketWarn && <div className="result-warning" data-testid="ticket-warning">⚠ {ticketWarn}</div>}
  
                  {lastResult && lastResult.type === "success" && (
                    <div className="result success" data-testid="ticket-result-success">
                      {lastResult.message}
                      {lastResult.recordWarning && (
                        <div className="result-warning" data-testid="ticket-record-warning">
                          ⚠ {lastResult.recordWarning}
                        </div>
                      )}
                      {lastResult.warning && (
                        <div className="result-warning" data-testid="ticket-risk-advisory">
                          ⚠ risk advisory: {lastResult.warning}
                        </div>
                      )}
                    </div>
                  )}
                  {lastResult && lastResult.type === "error" && (
                    <div className="result error" data-testid="ticket-result-error">
                      {lastResult.message}
                      {lastResult.reason === "risk_block" && (
                        <div
                          className="risk-override-hint"
                          data-testid="ticket-risk-override"
                          style={{
                            marginTop: 10,
                            padding: "8px 10px",
                            border: "1px solid var(--amber)",
                            borderRadius: 4,
                            background: "rgba(255, 200, 80, 0.08)",
                            fontSize: 12,
                          }}
                        >
                          <div style={{ marginBottom: 8 }}>
                            This order breaches a limit set in <b>Settings</b>. Reduce
                            the size or relax the limit — or override the check and
                            place it anyway.
                          </div>
                          <button
                            className="btn-sm sell"
                            onClick={() => lastResult.detail && onSubmit({ resend: lastResult.detail })}
                            disabled={placeOrder.isPending}
                            data-testid="ticket-risk-override-btn"
                          >
                            {placeOrder.isPending
                              ? "placing…"
                              : "⚠ Override risk & place anyway"}
                          </button>
                        </div>
                      )}
                      {lastResult.reason === "ip_whitelist" && serverInfo?.public_ip && (
                        <div
                          className="ip-whitelist-hint"
                          data-testid="ticket-ip-whitelist"
                          style={{
                            marginTop: 10,
                            padding: "8px 10px",
                            border: "1px solid var(--amber)",
                            borderRadius: 4,
                            background: "rgba(255, 200, 80, 0.08)",
                            fontSize: 12,
                            fontFamily: "var(--mono)",
                          }}
                        >
                          <div style={{ marginBottom: 6 }}>
                            Add this server's public IP to the Fyers app's
                            IP-whitelist on{" "}
                            <a
                              href="https://myapi.fyers.in/dashboard/"
                              target="_blank"
                              rel="noreferrer"
                              style={{ color: "var(--amber)" }}
                            >
                              myapi.fyers.in/dashboard
                            </a>
                            , then place the order again.
                          </div>
                          <div
                            style={{
                              display: "flex",
                              alignItems: "center",
                              gap: 8,
                            }}
                          >
                            <div
                              style={{
                                fontSize: 14,
                                fontWeight: 700,
                                letterSpacing: "0.05em",
                                userSelect: "all",
                                flex: 1,
                              }}
                              data-testid="ticket-server-ip"
                            >
                              {serverInfo.public_ip}
                            </div>
                            <button
                              className="btn-sm"
                              onClick={() => {
                                if (serverInfo.public_ip && navigator.clipboard) {
                                  navigator.clipboard
                                    .writeText(serverInfo.public_ip)
                                    .catch(() => {
                                      /* ignore — the IP is selectable */
                                    });
                                }
                              }}
                              data-testid="ticket-server-ip-copy"
                              title="Copy IP"
                            >
                              Copy
                            </button>
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                </section>
              </div>
            </section>
          )}
          {dock.includes("chain") && (
            <section className="dock-panel" data-testid="dock-chain" style={panelStyle("chain")}>
              {dockGrip("chain")}
              {dockHead("chain", chainBase?.short_name)}
              <div className="dock-body">
                <div className="chain-search">
                  <input type="search" placeholder="Search underlying — NIFTY, RELIANCE… (chart stays as is)" value={chainQuery}
                    onChange={(e) => setChainQuery(e.target.value)} data-testid="chain-search" />
                  {chainQuery.trim() && (
                    <div className="chain-search-hits">
                      {(chainHits?.hits ?? []).filter((h) => h.instrument_type === "IND" || h.instrument_type === "EQ").slice(0, 8).map((h) => (
                        <button type="button" key={h.symbol} onClick={() => pickChainBase(h)} data-testid={`chain-search-${h.short_name}`}>
                          <b>{h.short_name}</b> <span className="hint">{h.exchange} · {h.instrument_type === "IND" ? "index" : "stock"}</span>
                        </button>
                      ))}
                    </div>
                  )}
                  <span className="hint">drag a price onto a chart to open it there</span>
                </div>
                {/* ---- option chain (index underlyings + F&O stocks) ----
                     Indices always show the panel; a cash stock shows it only when
                     it actually has options (F&O stock), so non-F&O names stay clean. */}
                {chainBase &&
                  (chainBase.instrument_type === "IND" ||
                    (chainBase.instrument_type === "EQ" &&
                      (chain?.strikes?.length ?? 0) > 0)) && (
                  <section className="trade-card chain" data-testid="trade-chain">
                    <div className="chain-head">
                      <div className="chain-meta">
                        <span
                          className={`badge ${chain?.source === "fyers" ? "cash" : "neutral"}`}
                          title={chain?.source === "fyers" ? "live Fyers prices" : "static ladder"}
                        >
                          {chain?.source === "fyers" ? "LIVE" : "STATIC"}
                        </span>
                        {chain?.spot != null && (
                          <span data-testid="chain-spot">spot {fmtMoney(chain.spot)}</span>
                        )}
                        {(chain?.expiries?.length ?? 0) > 0 && (
                          <label className="chain-expiry">
                            <span>expiry</span>
                            <select
                              value={selectedExpiry ?? chain?.selected_expiry ?? ""}
                              onChange={(e) => setSelectedExpiry(e.target.value || null)}
                              data-testid="chain-expiry"
                            >
                              {chain!.expiries.map((ex) => (
                                <option key={ex.ts} value={ex.ts}>
                                  {ex.label}
                                </option>
                              ))}
                            </select>
                          </label>
                        )}
                      </div>
                    </div>
                    {!chain ? (
                      <div className="empty">Loading chain…</div>
                    ) : chain.strikes.length === 0 ? (
                      <div className="empty" data-testid="chain-empty">
                        {chain.reason ?? "No option chain available for this underlying."}
                      </div>
                    ) : (
                      <table className="chain-table">
                        <thead>
                          <tr>
                            <th colSpan={2} className="ce-head">CALLS (CE)</th>
                            <th className="strike-head">Strike</th>
                            <th colSpan={2} className="pe-head">PUTS (PE)</th>
                          </tr>
                          <tr className="chain-subhead">
                            <th>OI</th>
                            <th>LTP</th>
                            <th> </th>
                            <th>LTP</th>
                            <th>OI</th>
                          </tr>
                        </thead>
                        <tbody>
                          {chain.strikes.map((s) => {
                            const atm = s.strike === atmStrike;
                            const oiBar = (v: number | null | undefined, side: "ce" | "pe") =>
                              userPrefs.chainOiBars && v ? { background: `linear-gradient(to ${side === "ce" ? "left" : "right"}, ${side === "ce" ? "rgba(239,83,80,0.22)" : "rgba(38,166,154,0.22)"} ${(v / chainMaxOi) * 100}%, transparent 0)` } : undefined;
                            return (
                              <tr key={s.strike} className={atm ? "atm" : ""} ref={atm ? atmRef : undefined}>
                                <td className="oi" style={oiBar(s.ce?.oi, "ce")}>{fmtOi(s.ce?.oi)}</td>
                                <td className="ce-cell">
                                  {s.ce ? (
                                    <button
                                      className={`chain-ltp ce${selected?.symbol === s.ce.symbol ? " on" : ""}`}
                                      onClick={() => onSelectOption(s, "CE")}
                                      draggable
                                      onDragStart={dragOption(s, "CE")}
                                      data-testid={`chain-ce-${s.strike}`}
                                      title={s.ce.symbol}
                                    >
                                      {s.ce.ltp != null ? fmtMoney(s.ce.ltp) : "—"}
                                    </button>
                                  ) : (
                                    "—"
                                  )}
                                </td>
                                <th className="strike">
                                  {s.strike}
                                  {atm && <span className="atm-tag">ATM</span>}
                                </th>
                                <td className="pe-cell">
                                  {s.pe ? (
                                    <button
                                      className={`chain-ltp pe${selected?.symbol === s.pe.symbol ? " on" : ""}`}
                                      onClick={() => onSelectOption(s, "PE")}
                                      draggable
                                      onDragStart={dragOption(s, "PE")}
                                      data-testid={`chain-pe-${s.strike}`}
                                      title={s.pe.symbol}
                                    >
                                      {s.pe.ltp != null ? fmtMoney(s.pe.ltp) : "—"}
                                    </button>
                                  ) : (
                                    "—"
                                  )}
                                </td>
                                <td className="oi" style={oiBar(s.pe?.oi, "pe")}>{fmtOi(s.pe?.oi)}</td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    )}
                  </section>
                )}
                      {!(chainBase && (chainBase.instrument_type === "IND" || (chainBase.instrument_type === "EQ" && (chain?.strikes?.length ?? 0) > 0))) && (
                        <section className="trade-card"><div className="empty">{!chainBase ? "Open an index or F&O stock to see its option chain." : !chain ? "Loading chain…" : "No options listed for this symbol."}</div></section>
                      )}
              </div>
            </section>
          )}
          {dock.includes("flow") && (
            <section className="dock-panel" data-testid="dock-flow" style={panelStyle("flow")}>
              {dockGrip("flow")}
              {dockHead("flow", selected?.short_name)}
              <div className="dock-body"><FlowPanel symbol={selected?.symbol ?? null} /></div>
            </section>
          )}
          {dock.includes("book") && (
            <section className="dock-panel" data-testid="dock-book" style={panelStyle("book")}>
              {dockGrip("book")}
              {dockHead("book", selected?.short_name)}
              <div className="dock-body">
                <BookPanel symbol={selected?.symbol ?? null} tick={selected?.tick_size ?? 0.05} tab={deskTab.book} onTab={(t) => setDeskTab((x) => ({ ...x, book: t }))} onPrice={onBookPrice} onMarket={onBookMarket} />
              </div>
            </section>
          )}
          {dock.includes("fno") && (
            <section className="dock-panel" data-testid="dock-fno" style={panelStyle("fno")}>
              {dockGrip("fno")}
              {dockHead("fno", chainBase?.short_name)}
              <div className="dock-body">
                <FnoPanel base={chainBase ?? selected} chain={chain} tab={deskTab.fno} onTab={(t) => setDeskTab((x) => ({ ...x, fno: t }))} onOpen={(h) => onSelect(h, true)} />
              </div>
            </section>
          )}
          {dock.includes("details") && (
            <section className="dock-panel" data-testid="dock-details" style={panelStyle("details")}>
              {dockGrip("details")}
              {dockHead("details", selected?.short_name)}
              <div className="dock-body"><SymbolDetails hit={selected} /></div>
            </section>
          )}
          {dock.includes("data") && (
            <section className="dock-panel" data-testid="dock-data" style={panelStyle("data")}>
              {dockGrip("data")}
              {dockHead("data")}
              <div className="dock-body"><div ref={setDataSlot} />{scalper && <div className="hint">Available on the chart layout.</div>}</div>
            </section>
          )}
          {dock.includes("tree") && (
            <section className="dock-panel" data-testid="dock-tree" style={panelStyle("tree")}>
              {dockGrip("tree")}
              {dockHead("tree")}
              <div className="dock-body"><div ref={setTreeSlot} />{scalper && <div className="hint">Available on the chart layout.</div>}</div>
            </section>
          )}
          {dock.includes("alerts") && (
            <section className="dock-panel" data-testid="dock-alerts" style={panelStyle("alerts")}>
              {dockGrip("alerts")}
              {dockHead("alerts", selected?.short_name)}
              <div className="dock-body"><div ref={setAlertsSlot} />{scalper && <div className="hint">Available on the chart layout.</div>}</div>
            </section>
          )}
        </div>
      )}

      {/* ---- icon bar: each button opens / closes its panel ---- */}
      {userSettingsOpen && (
        <UserSettingsDialog
          onClose={() => setUserSettingsOpen(false)}
          host={{
            tset,
            onTset: changeSetting,
            privacy,
            onPrivacy: (v) => {
              setPrivacy(v);
              try { tabLocal.setItem("trade:privacy", JSON.stringify(v)); } catch { /* best-effort */ }
            },
            autosave: L.autosave,
            onAutosave: L.setAutosave,
            onPrefs: setUserPrefs,
          }}
        />
      )}
      <nav className="tv-rail" aria-label="Panels">
        {DOCK.map((d) => (
          <button
            key={d.id}
            type="button"
            className={`tv-rail-btn${dock.includes(d.id) ? " on" : ""}`}
            aria-pressed={dock.includes(d.id)}
            title={`${d.hint} — click to ${dock.includes(d.id) ? "close" : "open"}`}
            onClick={() => toggleDock(d.id)}
            data-testid={`rail-${d.id}`}
          >
            <span className="ico">{d.icon}</span>
            <span className="lbl">{d.short}</span>
          </button>
        ))}
        <div className="tv-rail-sep" />
        <div className="tv-rail-pop-wrap" ref={layoutPopRef}>
          <button
            type="button"
            className={`tv-rail-btn${layoutOpen || (!scalper && layout !== "1") ? " on" : ""}`}
            aria-expanded={layoutOpen}
            title="Chart layout — several charts at once"
            onClick={() => setLayoutOpen((o) => !o)}
            data-testid="rail-layout"
          >
            <span className="ico">{LAYOUTS.find((l) => l.id === layout)?.icon ?? "▢"}</span>
            <span className="lbl">Layout</span>
          </button>
          {layoutOpen && (
            <div className="tv-rail-pop" role="menu">
              {LAYOUTS.map((l) => (
                <button key={l.id} type="button" role="menuitem" className={`tv-layout-btn${!scalper && layout === l.id ? " on" : ""}`} onClick={() => pickLayout(l.id)}>
                  <span className="ico">{l.icon}</span>
                  {l.label}
                </button>
              ))}
              <div className="tv-sync">
                <div className="chart-menu-head">Sync in layout</div>
                {SYNC_LABELS.map(([k, label]) => (
                  <label key={k} className="tset-row">
                    <input type="checkbox" checked={!!sync[k]} onChange={(e) => setSync(k, e.target.checked)} data-testid={`sync-${k}`} />
                    {label}
                  </label>
                ))}
              </div>
            </div>
          )}
        </div>
        <button
          type="button"
          className={`tv-rail-btn${scalper ? " on" : ""}`}
          aria-pressed={scalper}
          title="Option scalper — CE, underlying and PE charts with one-click buy / sell"
          onClick={() => { setScalper((v) => !v); setLayoutOpen(false); }}
          data-testid="rail-scalper"
        >
          <span className="ico">⚡</span>
          <span className="lbl">Scalp</span>
        </button>
      </nav>
    </div>
  );
}
