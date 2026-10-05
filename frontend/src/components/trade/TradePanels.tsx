// TradePanels — the Trade page's TradingView-style side and bottom panels:
// the watchlist (several lists, sorting, flags, drag to reorder, import /
// export), symbol details + headlines, the account manager (positions,
// orders, trades, account, basket orders, notifications log) and the
// named-layout manager (save / load / autosave).

import { useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import type { InstrumentHit, PendingOrder, Position } from "../../types";
import { useLiveQuote } from "../../hooks/useQuotes";
import { technicalRating, type OhlcvCandle } from "../../lib/indicators";
import { capTier } from "../../lib/marketCap";
import { clearLog, getLog, subscribeLog } from "./chartSync";
import { Modal, useOutside } from "./chartUi";
import type { ChartOrder } from "./ChartPanel";
import {
  AccountMenu,
  AllPositionsPanel,
  Baskets,
  DataTable,
  ExitPositionDialog,
  FundsPanel,
  GttPanel,
  HoldingsPanel,
  LiveLtp,
  ModifyOrderDialog,
  OrdersTable,
  SmartOrderbook,
  TradesTable,
  fmt,
  livePnl,
  orderRows,
  useApiJson,
  type BrokerBook,
  type BrokerPosition,
  type Col,
  type ExitTarget,
  type FundsResp,
  type GttResp,
  type HoldingsResp,
  type OrderRow,
  type TradeRow,
} from "./AccountPanels";
import { DomPanel } from "./DomPanel";

export { livePnl };

function fmtVol(v: number): string {
  const a = Math.abs(v);
  if (a >= 1e7) return (v / 1e7).toFixed(2) + "Cr";
  if (a >= 1e5) return (v / 1e5).toFixed(2) + "L";
  if (a >= 1e3) return (v / 1e3).toFixed(1) + "K";
  return String(Math.round(v));
}

// ---------------------------------------------------------------------------
// Watchlist
// ---------------------------------------------------------------------------

export interface WatchList {
  name: string;
  items: InstrumentHit[];
  flags?: Record<string, string>;
}

export interface WatchState {
  active: number;
  lists: WatchList[];
}

export const FLAG_COLORS = ["#F23645", "#FF9800", "#4CAF50", "#2962FF", "#9C27B0"];

type Quote = { ltp: number; change: number | null; change_pct: number | null };
type SortKey = "symbol" | "last" | "chg" | "chgp";

/** Green while ticks are arriving for the symbol (one in the last 15s). */
function LiveDot({ symbol }: { symbol: string }) {
  const q = useLiveQuote(symbol);
  const [seen, setSeen] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (q && !q.simulated) setSeen(Date.now());
  }, [q]);
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 5000);
    return () => clearInterval(id);
  }, []);
  const live = seen > 0 && now - seen < 15000;
  return <span className={`wl-live${live ? " on" : ""}`} title={live ? "Live — ticking" : seen ? "No tick in the last 15s" : "No live ticks yet"} />;
}

export function WatchlistTable({
  state,
  quotes,
  selected,
  onSelect,
  onChange,
}: {
  state: WatchState;
  quotes: Record<string, Quote> | undefined;
  selected: string | null;
  onSelect: (h: InstrumentHit) => void;
  onChange: (s: WatchState) => void;
}) {
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 } | null>(null);
  const [menu, setMenu] = useState<{ sym: string; x: number; y: number } | null>(null);
  const [listMenu, setListMenu] = useState(false);
  const dragFrom = useRef<number | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  useOutside(menuRef, !!menu, () => setMenu(null));
  useOutside(listRef, listMenu, () => setListMenu(false));
  const list = state.lists[state.active] ?? state.lists[0];
  const q = (h: InstrumentHit) => quotes?.[h.symbol.toUpperCase()];
  const rows = useMemo(() => {
    const items = [...(list?.items ?? [])];
    if (!sort) return items;
    const val = (h: InstrumentHit): number | string => {
      const x = quotes?.[h.symbol.toUpperCase()];
      if (sort.key === "symbol") return h.short_name;
      if (sort.key === "last") return x?.ltp ?? -Infinity;
      if (sort.key === "chg") return x?.change ?? -Infinity;
      return x?.change_pct ?? -Infinity;
    };
    return items.sort((a, b) => {
      const va = val(a), vb = val(b);
      return (typeof va === "string" ? va.localeCompare(String(vb)) : (va as number) - (vb as number)) * sort.dir;
    });
  }, [list, sort, quotes]);
  const putList = (patch: Partial<WatchList>) => onChange({ ...state, lists: state.lists.map((l, i) => (i === state.active ? { ...l, ...patch } : l)) });
  const head = (k: SortKey, label: string) => (
    <button type="button" className={`wl-sort${sort?.key === k ? " on" : ""}`} onClick={() => setSort((s) => (s?.key === k ? (s.dir === 1 ? { key: k, dir: -1 } : null) : { key: k, dir: 1 }))} title="Sort">
      {label}{sort?.key === k ? (sort.dir === 1 ? " ▴" : " ▾") : ""}
    </button>
  );
  if (!list) return null;
  return (
    <div className="wl" data-testid="watchlist">
      <div className="wl-bar">
        <div className="chart-menu-wrap" ref={listRef}>
          <button type="button" className="wl-name" onClick={() => setListMenu((o) => !o)} data-testid="watchlist-name">{list.name} ▾</button>
          {listMenu && (
            <div className="chart-menu">
              {state.lists.map((l, i) => (
                <button key={i} type="button" className={`chart-menu-item${i === state.active ? " on" : ""}`} onClick={() => { onChange({ ...state, active: i }); setListMenu(false); }}>
                  {l.name} <span className="hint">{l.items.length}</span>
                </button>
              ))}
              <div className="chart-menu-sep" />
              <button type="button" className="chart-menu-item" onClick={() => {
                const name = window.prompt("New list name");
                if (name) onChange({ active: state.lists.length, lists: [...state.lists, { name, items: [] }] });
                setListMenu(false);
              }}>+ Create new list…</button>
              <button type="button" className="chart-menu-item" onClick={() => {
                const name = window.prompt("Rename list", list.name);
                if (name) putList({ name });
                setListMenu(false);
              }}>Rename…</button>
              <button type="button" className="chart-menu-item" onClick={() => {
                onChange({ active: state.lists.length, lists: [...state.lists, { ...list, name: `${list.name} copy` }] });
                setListMenu(false);
              }}>Make a copy</button>
              <button type="button" className="chart-menu-item" disabled={state.lists.length < 2} onClick={() => {
                if (!window.confirm(`Delete the list "${list.name}"?`)) return;
                const lists = state.lists.filter((_, i) => i !== state.active);
                onChange({ active: 0, lists });
                setListMenu(false);
              }}>Delete list</button>
              <div className="chart-menu-sep" />
              <button type="button" className="chart-menu-item" onClick={() => {
                const blob = new Blob([JSON.stringify(list, null, 2)], { type: "application/json" });
                const a = document.createElement("a");
                a.href = URL.createObjectURL(blob);
                a.download = `watchlist-${list.name.replace(/\W+/g, "_")}.json`;
                a.click();
                setListMenu(false);
              }}>Export list…</button>
              <button type="button" className="chart-menu-item" onClick={() => { fileRef.current?.click(); setListMenu(false); }}>Import list…</button>
            </div>
          )}
        </div>
        <input
          ref={fileRef}
          type="file"
          accept="application/json,.json"
          hidden
          onChange={async (e) => {
            const f = e.target.files?.[0];
            e.target.value = "";
            if (!f) return;
            try {
              const raw = JSON.parse(await f.text()) as Partial<WatchList> | InstrumentHit[];
              const items = (Array.isArray(raw) ? raw : raw.items ?? []).filter((h) => h && typeof h.symbol === "string");
              const name = (!Array.isArray(raw) && raw.name) || f.name.replace(/\.json$/i, "");
              onChange({ active: state.lists.length, lists: [...state.lists, { name, items, flags: Array.isArray(raw) ? {} : raw.flags }] });
            } catch {
              window.alert("That file isn't an exported watchlist.");
            }
          }}
        />
        <span className="hint">{list.items.length} symbols</span>
      </div>
      {rows.length > 0 && (
        <div className="tv-watch-list" data-testid="trade-recent">
          <div className="tv-watch-head wl-row">
            <span>{head("symbol", "Symbol")}</span>
            <span>{head("last", "Last")}</span>
            <span>{head("chg", "Chg")}</span>
            <span>{head("chgp", "Chg%")}</span>
            <span />
          </div>
          {rows.map((h) => {
            const x = q(h);
            const pct = x?.change_pct ?? null;
            const chg = x?.change ?? null;
            const flag = list.flags?.[h.symbol];
            const idx = rows.findIndex((i) => i.symbol === h.symbol);
            return (
              <div
                key={h.symbol}
                role="button"
                tabIndex={0}
                className={`tv-watch-row wl-row${selected === h.symbol ? " on" : ""}`}
                onClick={() => onSelect(h)}
                onKeyDown={(e) => { if (e.key === "Enter") onSelect(h); }}
                onContextMenu={(e) => { e.preventDefault(); setMenu({ sym: h.symbol, x: e.clientX, y: e.clientY }); }}
                draggable
                onDragStart={() => { dragFrom.current = idx; }}
                onDragOver={(e) => e.preventDefault()}
                onDrop={() => {
                  const from = dragFrom.current;
                  dragFrom.current = null;
                  if (from === null || from === idx) return;
                  // Reorder what's on screen: a sorted list adopts that order as its own.
                  const items = [...rows];
                  const [m] = items.splice(from, 1);
                  items.splice(idx, 0, m);
                  putList({ items });
                  setSort(null);
                }}
                title={`${h.symbol} — right-click for options, drag ⋮ to reorder`}
                data-testid={`recent-chip-${h.symbol}`}
                style={flag ? { boxShadow: `inset 3px 0 ${flag}` } : undefined}
              >
                <span className="sym"><span className="wl-grip" aria-hidden="true">⋮</span><LiveDot symbol={h.symbol} />{h.short_name}</span>
                <span>{x ? x.ltp.toFixed(2) : "—"}</span>
                <span className={chg == null ? "" : chg >= 0 ? "up" : "down"}>{chg == null ? "—" : `${chg >= 0 ? "+" : ""}${chg.toFixed(2)}`}</span>
                <span className={pct == null ? "" : pct >= 0 ? "up" : "down"}>{pct == null ? "—" : `${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%`}</span>
                <button type="button" className="x" title="Remove from watchlist" onClick={(e) => { e.stopPropagation(); putList({ items: list.items.filter((i) => i.symbol !== h.symbol) }); }}>×</button>
              </div>
            );
          })}
        </div>
      )}
      {menu && (
        <div ref={menuRef} className="chart-menu wl-menu" style={{ position: "fixed", left: menu.x, top: menu.y }}>
          <div className="chart-menu-head">Flag</div>
          <div className="wl-flags">
            {FLAG_COLORS.map((c) => (
              <button key={c} type="button" style={{ background: c }} aria-label={`Flag ${c}`} onClick={() => { putList({ flags: { ...(list.flags ?? {}), [menu.sym]: c } }); setMenu(null); }} />
            ))}
            <button type="button" className="none" onClick={() => { const f = { ...(list.flags ?? {}) }; delete f[menu.sym]; putList({ flags: f }); setMenu(null); }}>none</button>
          </div>
          {state.lists.length > 1 && (["add", "move"] as const).map((how) => (
            <div key={how}>
              <div className="chart-menu-head">{how === "add" ? "Add to list" : "Move to list"}</div>
              {state.lists.map((l, i) =>
                i === state.active ? null : (
                  <button key={i} type="button" className="chart-menu-item" data-testid={`wl-${how}-${i}`} onClick={() => {
                    const h = list.items.find((x) => x.symbol === menu.sym);
                    if (h) {
                      onChange({
                        ...state,
                        lists: state.lists.map((y, j) =>
                          j === i && !y.items.some((z) => z.symbol === h.symbol) ? { ...y, items: [...y.items, h] }
                            : how === "move" && j === state.active ? { ...y, items: y.items.filter((z) => z.symbol !== h.symbol) }
                              : y),
                      });
                    }
                    setMenu(null);
                  }}>{l.name}</button>
                ),
              )}
            </div>
          ))}
          <div className="chart-menu-sep" />
          <button type="button" className="chart-menu-item ctx-sell" onClick={() => { putList({ items: list.items.filter((i) => i.symbol !== menu.sym) }); setMenu(null); }}>Remove from list</button>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Symbol details + headlines
// ---------------------------------------------------------------------------

interface Ann {
  id: number;
  headline: string;
  pdf_url: string | null;
  filed_at: string | null;
  received_at: string;
}

function Gauge({ score, label }: { score: number; label: string }) {
  const a = Math.PI * (1 - (Math.max(-1, Math.min(1, score)) + 1) / 2);
  const x = 60 + 44 * Math.cos(a);
  const y = 60 - 44 * Math.sin(a);
  const color = score > 0.1 ? "var(--green)" : score < -0.1 ? "var(--red)" : "var(--text-dim)";
  return (
    <svg viewBox="0 0 120 70" className="gauge" role="img" aria-label={`Technical rating: ${label}`}>
      <path d="M10 60 A50 50 0 0 1 110 60" fill="none" stroke="var(--border-hi)" strokeWidth="8" />
      <path d="M10 60 A50 50 0 0 1 35 17" fill="none" stroke="var(--red)" strokeWidth="8" opacity="0.8" />
      <path d="M85 17 A50 50 0 0 1 110 60" fill="none" stroke="var(--green)" strokeWidth="8" opacity="0.8" />
      <line x1="60" y1="60" x2={x} y2={y} stroke={color} strokeWidth="3" strokeLinecap="round" />
      <circle cx="60" cy="60" r="4" fill={color} />
    </svg>
  );
}

export function SymbolDetails({ hit }: { hit: InstrumentHit | null }) {
  const live = useLiveQuote(hit?.symbol);
  const [daily, setDaily] = useState<OhlcvCandle[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [news, setNews] = useState<Ann[] | null>(null);
  useEffect(() => {
    setDaily(null);
    setErr(null);
    setNews(null);
    if (!hit) return;
    let stop = false;
    const now = Math.floor(Date.now() / 1000);
    void fetch(`/api/market/history?symbol=${encodeURIComponent(hit.symbol)}&resolution=D&from=${now - 400 * 86400}&to=${now}`)
      .then((r) => r.json())
      .then((j: { ok?: boolean; candles?: number[][]; reason?: string }) => {
        if (stop) return;
        if (!j.ok) {
          setErr(j.reason ?? "no daily history");
          return;
        }
        setDaily((j.candles ?? []).filter((c) => Array.isArray(c) && c.length >= 5).map(([t, o, h, l, c, v]) => ({ time: t, open: o, high: h, low: l, close: c, volume: v ?? 0 })));
      })
      .catch((e) => !stop && setErr(String(e)));
    const tk = hit.short_name.split(/\s+/)[0].toUpperCase();
    void fetch(`/api/announcements/recent?symbol=${encodeURIComponent(tk)}&limit=30`)
      .then((r) => (r.ok ? r.json() : []))
      .then((rows) => !stop && setNews(Array.isArray(rows) ? rows : []))
      .catch(() => !stop && setNews([]));
    return () => {
      stop = true;
    };
  }, [hit?.symbol]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!hit) return <div className="empty">Pick a symbol first.</div>;
  const d = daily ?? [];
  const n = d.length;
  const last = live?.last_price ?? d[n - 1]?.close ?? null;
  const prevClose = live?.prev_close ?? (n > 1 ? d[n - 2].close : null);
  const chg = last != null && prevClose ? last - prevClose : null;
  const today = d[n - 1];
  const yr = d.slice(-252);
  const hi52 = yr.length ? Math.max(...yr.map((c) => c.high)) : null;
  const lo52 = yr.length ? Math.min(...yr.map((c) => c.low)) : null;
  const avgVol = d.length >= 20 ? d.slice(-20).reduce((a, c) => a + c.volume, 0) / 20 : null;
  const perf = (bars: number) => (n > bars && last != null ? ((last - d[n - 1 - bars].close) / d[n - 1 - bars].close) * 100 : null);
  const yStart = n ? d.findIndex((c) => new Date((c.time + 19800) * 1000).getUTCFullYear() === new Date((d[n - 1].time + 19800) * 1000).getUTCFullYear()) : -1;
  const ytd = yStart > 0 && last != null ? ((last - d[yStart - 1].close) / d[yStart - 1].close) * 100 : null;
  const rating = n ? technicalRating(d) : null;
  const range = (lo: number | null, hi: number | null) => {
    if (lo == null || hi == null || last == null || hi <= lo) return null;
    const p = Math.max(0, Math.min(100, ((last - lo) / (hi - lo)) * 100));
    return (
      <div className="det-range">
        <span>{fmt(lo)}</span>
        <div className="det-bar"><i style={{ left: `${p}%` }} /></div>
        <span>{fmt(hi)}</span>
      </div>
    );
  };
  const tier = hit.instrument_type === "EQ" ? capTier(hit.symbol) : null;
  return (
    <div className="det" data-testid="symbol-details">
      <div className="det-head">
        <span className="det-sym">{hit.short_name}</span>
        <span className="hint">{hit.exchange}:{hit.segment}</span>
        {tier && <span className="badge neutral">{tier} cap</span>}
      </div>
      <div className="det-desc">{hit.display}</div>
      <div className="det-price">
        <b>{fmt(last)}</b>
        {chg != null && prevClose ? (
          <span className={chg >= 0 ? "up" : "down"}>{chg >= 0 ? "+" : ""}{fmt(chg)} ({chg >= 0 ? "+" : ""}{((chg / prevClose) * 100).toFixed(2)}%)</span>
        ) : null}
      </div>
      {err && <div className="hint warn-text">{err}</div>}
      <div className="det-grid">
        <span>Day's range</span>{range(live?.last_price != null && today ? Math.min(today.low, live.last_price) : today?.low ?? null, live?.last_price != null && today ? Math.max(today.high, live.last_price) : today?.high ?? null) ?? <span>—</span>}
        <span>52-week range</span>{range(lo52, hi52) ?? <span>—</span>}
        <span>Volume</span><b>{today ? fmtVol(today.volume) : "—"}</b>
        <span>Avg volume (20D)</span><b>{avgVol != null ? fmtVol(avgVol) : "—"}</b>
        <span>Prev close</span><b>{fmt(prevClose)}</b>
        <span>Lot size</span><b>{hit.lot_size}</b>
        {hit.expiry && (<><span>Expiry</span><b>{hit.expiry}</b></>)}
        {hit.strike != null && (<><span>Strike</span><b>{hit.strike}</b></>)}
      </div>
      <div className="det-sec">Performance</div>
      <div className="det-perf">
        {([["1W", perf(5)], ["1M", perf(21)], ["3M", perf(63)], ["6M", perf(126)], ["YTD", ytd], ["1Y", perf(250)]] as [string, number | null][]).map(([k, v]) => (
          <div key={k} className={`det-perf-cell ${v == null ? "" : v >= 0 ? "up" : "down"}`}>
            <b>{v == null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`}</b>
            <span>{k}</span>
          </div>
        ))}
      </div>
      <div className="det-sec">Technicals (daily)</div>
      {rating ? (
        <div className="det-tech">
          <Gauge score={rating.score} label={rating.rating} />
          <div>
            <b className={rating.score > 0.1 ? "up" : rating.score < -0.1 ? "down" : ""}>{rating.rating}</b>
            <div className="hint">MAs {rating.ma >= 0 ? "+" : ""}{rating.ma.toFixed(2)} · oscillators {rating.osc >= 0 ? "+" : ""}{rating.osc.toFixed(2)}</div>
            <div className="hint">buy {rating.votes.buy} · neutral {rating.votes.neutral} · sell {rating.votes.sell}</div>
          </div>
        </div>
      ) : (
        <div className="hint">{daily === null && !err ? "loading…" : "not enough daily history"}</div>
      )}
      <div className="det-sec">Headlines</div>
      {news === null ? (
        <div className="hint">loading…</div>
      ) : news.length === 0 ? (
        <div className="hint">No filed announcements for {hit.short_name}.</div>
      ) : (
        <div className="det-news" data-testid="symbol-news">
          {news.map((a) => {
            const t = new Date(a.filed_at ?? a.received_at);
            const when = `${t.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" })} · ${t.toLocaleDateString("en-IN", { day: "2-digit", month: "short" })}`;
            return (
              <a key={a.id} className="det-news-row" href={a.pdf_url ?? undefined} target="_blank" rel="noreferrer" onClick={(e) => { if (!a.pdf_url) e.preventDefault(); }}>
                <span className="hint">{when}</span>
                <span>{a.headline}</span>
              </a>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Account manager
// ---------------------------------------------------------------------------

export type BottomTab =
  | "trade" | "positions" | "allpositions" | "orders" | "smart" | "trades" | "holdings" | "gtt" | "funds"
  | "account" | "basket" | "broker" | "log";

/** Tab labels (the collapsed bar shows them too). "Trade" is the DOM, beside the Account Manager. */
export const BOTTOM_TABS: [BottomTab, string][] = [
  ["positions", "Positions"],
  ["allpositions", "All positions"],
  ["orders", "Orders"],
  ["smart", "Smart orderbook"],
  ["trades", "Trades"],
  ["holdings", "Holdings"],
  ["gtt", "GTT"],
  ["funds", "Funds"],
  ["account", "Account"],
  ["basket", "Basket"],
  ["broker", "Fyers live"],
  ["log", "Notifications"],
];

interface ProfileResp { ok: boolean; client_id?: string | null; name?: string | null; email?: string | null; reason?: string }

function LocalPnl({ p, privacy }: { p: Position; privacy: boolean }) {
  const live = useLiveQuote(p.symbol)?.last_price ?? null;
  const v = live != null ? (live - p.average_price) * p.quantity : p.unrealized_pnl;
  return <span className={(v ?? 0) >= 0 ? "up" : "down"}>{privacy ? "•••" : fmt(v)}</span>;
}

/** Intraday ⇄ carry: DELIVERY (CNC) for cash, MARGIN (NRML) for F&O. */
const isFnoSymbol = (sym: string) => !/-(EQ|BE|INDEX)$/i.test(sym);
export const convertTarget = (p: { symbol: string; product?: string }) =>
  (p.product ?? "INTRADAY") !== "INTRADAY" ? "INTRADAY" : isFnoSymbol(p.symbol) ? "MARGIN" : "DELIVERY";
const convertTitle = (p: { symbol: string; product?: string }) =>
  convertTarget(p) === "INTRADAY" ? "Convert back to intraday — it will be squared off before the close" : "Convert to carry-forward — the EOD square-off will leave it open";

export function AccountManager({
  tab,
  onTab,
  positions,
  managed,
  pendingOrders,
  onCancel,
  cancelBusyId,
  cancelBanner,
  accountId,
  privacy,
  connected,
  accountLabel,
  selected,
  closeFor,
  levelsFor,
  orderFor,
  instant = false,
  qty,
  onQty,
  onLogout,
  onMaximize,
  maximized,
  onCollapse,
}: {
  tab: BottomTab;
  onTab: (t: BottomTab | null) => void;
  positions: Position[] | undefined;
  managed: { symbol: string; stop_loss: number | null; target: number | null }[] | undefined;
  /** The bot's working orders (`/api/orders/pending`). */
  pendingOrders: PendingOrder[];
  onCancel: (brokerOrderId: string) => void;
  cancelBusyId: string | null;
  cancelBanner?: ReactNode;
  accountId: number | null;
  privacy: boolean;
  connected: boolean;
  accountLabel: string;
  selected: InstrumentHit | null;
  closeFor: (sym: string) => () => Promise<void>;
  levelsFor: (sym: string) => (sl: number | null, tp: number | null) => Promise<void>;
  orderFor: (sym: string, name: string, qty: number) => (o: ChartOrder) => Promise<string>;
  instant?: boolean;
  /** The ticket quantity (shared with the DOM). */
  qty: number;
  onQty: (n: number) => void;
  onLogout?: () => void;
  onMaximize: () => void;
  maximized: boolean;
  onCollapse: () => void;
}) {
  const [msg, setMsg] = useState<string | null>(null);
  const [modify, setModify] = useState<OrderRow | null>(null);
  const [exit, setExit] = useState<{ pos: ExitTarget; local: boolean } | null>(null);
  const bookTabs: BottomTab[] = ["orders", "smart", "allpositions", "positions", "trade"];
  const book = useApiJson<BrokerBook>(connected && bookTabs.includes(tab) ? "/api/broker/book" : null, 15000);
  const trades = useApiJson<TradeRow[]>(["orders", "trades", "account", "smart", "positions"].includes(tab) ? "/api/trades?limit=200" : null, 10000);
  const funds = useApiJson<{ ok: boolean; available: number | null; reason?: string | null }>(tab === "account" || tab === "positions" ? "/api/market/funds" : null, 30000);
  const fundsFull = useApiJson<FundsResp>(tab === "funds" ? "/api/broker/funds" : null, 30000);
  const holdings = useApiJson<HoldingsResp>(tab === "holdings" || tab === "account" ? "/api/broker/holdings" : null, 60000);
  const gtt = useApiJson<GttResp>(tab === "gtt" ? "/api/broker/gtt" : null, 30000);
  const profile = useApiJson<ProfileResp>(connected ? "/api/broker/profile" : null);
  const log = useSyncExternalStore(subscribeLog, getLog);
  const open = (positions ?? []).filter((p) => p.quantity !== 0);
  const totalPnl = open.reduce((a, p) => a + (p.unrealized_pnl ?? 0), 0);
  const money = (v: number | null | undefined) => (privacy ? "•••" : fmt(v));
  const tradeRows = Array.isArray(trades.data) ? trades.data : [];
  const today = new Date().toDateString();
  const realizedBy = (sym: string) => {
    const bp = book.data?.ok ? book.data.positions.filter((x) => x.symbol === sym) : [];
    if (bp.length) return bp.reduce((a, x) => a + (x.realized ?? 0), 0);
    const ts = tradeRows.filter((t) => t.symbol === sym && t.pnl != null && t.executed_at && new Date(t.executed_at).toDateString() === today);
    return ts.length ? ts.reduce((a, t) => a + (t.pnl ?? 0), 0) : null;
  };
  const productOf = (sym: string) => (book.data?.ok ? book.data.positions.find((x) => x.symbol === sym && x.net_qty !== 0)?.product : null) ?? "INTRADAY";
  const realized = tradeRows.filter((t) => t.pnl != null && t.executed_at && new Date(t.executed_at).toDateString() === today).reduce((a, t) => a + (t.pnl ?? 0), 0);
  const rows = orderRows(book.data, pendingOrders, tradeRows);
  const workingCount = rows.filter((r) => r.bucket === "working").length;
  const run = async (label: string, f: () => Promise<unknown>) => {
    try {
      const r = await f();
      setMsg(`${label}: ${typeof r === "string" ? r : "done"}`);
    } catch (e) {
      setMsg(`${label} failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  };
  const lotOf = (sym: string) => (selected?.symbol === sym && selected.lot_size > 1 ? selected.lot_size : 1);
  /** Move a working order to `price` (the DOM's drag): the stop for stop
   *  orders (a stop-limit's limit keeps its offset), else the limit. */
  const modifyPrice = async (o: OrderRow, price: number): Promise<string> => {
    if (!accountId || !o.id) throw new Error("no live account");
    const stopType = o.type === "SL-M" || o.type === "STOP_LOSS";
    const body: Record<string, unknown> = { account_id: accountId, broker_order_id: o.id, order_type: o.type };
    if (stopType) body.stop_price = price;
    if (o.type === "STOP_LOSS") body.limit_price = Math.round(((o.limit ?? price) + (price - (o.stop ?? price))) * 100) / 100;
    if (!stopType) body.limit_price = price;
    const r = await fetch("/api/orders/modify", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : `HTTP ${r.status}`);
    if (j.ok === false) throw new Error(j.message || "the broker refused the change");
    book.reload();
    return `Moved ${o.side} ${o.remaining ?? o.qty} ${o.symbol} to ${fmt(price)}`;
  };
  const prof = profile.data?.ok ? profile.data : null;
  const acctText = !connected ? "No live account" : prof?.client_id ? `Account: ${prof.client_id}${prof.name ? ` : ${prof.name}` : ""} INR` : accountLabel;
  const label = (k: BottomTab, l: string) =>
    k === "positions" && open.length ? `${l} (${open.length})` : k === "orders" && workingCount ? `${l} (${workingCount})` : k === "log" && log.length ? `${l} (${log.length})` : l;
  const posCols: Col<Position>[] = [
    { id: "symbol", label: "Symbol", get: (p) => p.symbol, fixed: true, cls: () => "sym" },
    { id: "side", label: "Buy/Sell", get: (p) => (p.quantity > 0 ? "BUY" : "SELL"), cls: (p) => (p.quantity > 0 ? "up" : "down") },
    { id: "product", label: "Product", get: (p) => productOf(p.symbol) },
    { id: "qty", label: "Net qty", get: (p) => p.quantity, render: (p) => p.quantity, cls: (p) => (p.quantity > 0 ? "up" : "down"), num: true },
    { id: "avg", label: "Avg", get: (p) => p.average_price, num: true },
    { id: "ltp", label: "LTP", get: (p) => p.last_price, render: (p) => <LiveLtp symbol={p.symbol} fallback={p.last_price} />, num: true },
    { id: "pnl", label: "Unrealized P&L", get: (p) => p.unrealized_pnl, render: (p) => <LocalPnl p={p} privacy={privacy} />, num: true },
    { id: "realized", label: "Realized P&L", get: (p) => realizedBy(p.symbol), render: (p) => { const v = realizedBy(p.symbol); return <span className={v == null ? "" : v >= 0 ? "up" : "down"}>{money(v)}</span>; }, num: true },
    {
      id: "sl", label: "SL", get: (p) => managed?.find((x) => x.symbol === p.symbol)?.stop_loss ?? null,
      render: (p) => { const m = managed?.find((x) => x.symbol === p.symbol); return <button type="button" className="am-link" onClick={() => askLevel(p.symbol, "SL", m?.stop_loss ?? null, m?.target ?? null)}>{m?.stop_loss != null ? fmt(m.stop_loss) : "+ SL"}</button>; },
    },
    {
      id: "tp", label: "TP", get: (p) => managed?.find((x) => x.symbol === p.symbol)?.target ?? null,
      render: (p) => { const m = managed?.find((x) => x.symbol === p.symbol); return <button type="button" className="am-link" onClick={() => askLevel(p.symbol, "TP", m?.stop_loss ?? null, m?.target ?? null)}>{m?.target != null ? fmt(m.target) : "+ TP"}</button>; },
    },
  ];
  function askLevel(sym: string, which: "SL" | "TP", sl: number | null, tp: number | null): void {
    const cur = which === "SL" ? sl : tp;
    const v = window.prompt(`${which} for ${sym} (blank clears)`, cur != null ? String(cur) : "");
    if (v === null) return;
    const n = v.trim() === "" ? null : Number(v);
    if (n !== null && !Number.isFinite(n)) return;
    void run(`Exits ${sym}`, () => levelsFor(sym)(which === "SL" ? n : sl, which === "TP" ? n : tp));
  }
  const domPos = selected ? open.find((p) => p.symbol === selected.symbol) : undefined;
  return (
    <>
      <div className="tabs trade-tabs am-tabs" role="tablist">
        <button type="button" role="tab" aria-selected={tab === "trade"} className={`tab am-trade-tab ${tab === "trade" ? "active" : ""}`} onClick={() => onTab("trade")} title="DOM — trade from the price ladder" data-testid="am-tab-trade">Trade</button>
        <span className="am-title">Account Manager</span>
        <div className="am-tab-scroll">
          {BOTTOM_TABS.map(([k, l]) => (
            <button key={k} type="button" role="tab" aria-selected={tab === k} className={`tab ${tab === k ? "active" : ""}`} onClick={() => onTab(k)} data-testid={`am-tab-${k}`}>{label(k, l)}</button>
          ))}
        </div>
        <span className="am-status">
          <span className={`lg-dot ${connected ? "open" : "closed"}`} /> <span data-testid="am-account-line">{acctText}</span>
          {funds.data?.available != null && <> · Funds <b>{money(funds.data.available)}</b></>}
          {open.length > 0 && <> · P&L <b className={totalPnl >= 0 ? "up" : "down"}>{money(totalPnl)}</b></>}
        </span>
        <AccountMenu connected={connected} onLogout={onLogout} onTab={(t) => onTab(t)} onRefresh={() => { book.reload(); trades.reload(); funds.reload(); fundsFull.reload(); holdings.reload(); gtt.reload(); profile.reload(); setMsg("Account data refreshed"); }} />
        <button type="button" className="tab tv-collapse" onClick={onMaximize} title={maximized ? "Restore panel" : "Maximize panel"}>{maximized ? "❐" : "⬚"}</button>
        <button type="button" className="tab" onClick={onCollapse} title="Close panel">▾</button>
      </div>
      <div className="tv-bottom-body">
        {msg && <div className="result info am-msg" onClick={() => setMsg(null)}>{msg}</div>}
        {tab === "trade" && (
          <DomPanel
            symbol={selected}
            position={domPos ? { qty: domPos.quantity, avg: domPos.average_price } : null}
            orders={rows.filter((r) => r.bucket === "working" && selected != null && r.symbol === selected.symbol)}
            qty={qty}
            onQty={onQty}
            instant={instant}
            onOrder={(o, q) => (selected ? orderFor(selected.symbol, selected.short_name, q)(o) : Promise.reject(new Error("no symbol")))}
            onCancel={onCancel}
            onModify={modifyPrice}
            onFlatten={() => (selected ? closeFor(selected.symbol)() : Promise.resolve())}
            onClose={onCollapse}
            onMsg={setMsg}
          />
        )}
        {tab === "positions" && (
          <section className="trade-card" data-testid="trade-positions">
            <DataTable
              id="positions"
              cols={posCols}
              rows={open}
              rowKey={(p) => p.symbol}
              empty="There are no open positions in your trading account yet."
              toolbar={
                <button type="button" className="btn-sm danger" disabled={!open.length} onClick={() => {
                  if (!window.confirm(`Exit all ${open.length} open position(s) at market?`)) return;
                  void run("Exit all", async () => {
                    const r = await fetch("/api/positions/close-all", { method: "POST" });
                    const j = await r.json().catch(() => ({}));
                    if (!r.ok) throw new Error(j.detail ?? `HTTP ${r.status}`);
                    return "sent";
                  });
                }}>Exit all</button>
              }
              actions={(p) => (
                <>
                  <button type="button" className="btn-sm" title="Reverse at market" onClick={() => {
                    const q = Math.abs(p.quantity) * 2;
                    if (window.confirm(`Reverse ${p.symbol}: ${p.quantity > 0 ? "SELL" : "BUY"} ${q} at market?`)) {
                      void run(`Reverse ${p.symbol}`, () => orderFor(p.symbol, p.symbol, q)({ side: p.quantity > 0 ? "SELL" : "BUY", type: "MARKET", price: null }));
                    }
                  }}>⇅</button>
                  <button type="button" className="btn-sm" title={convertTitle(p)} data-testid={`convert-${p.symbol}`} onClick={() => {
                    const to = convertTarget(p);
                    if (window.confirm(`Convert ${p.symbol} (${p.quantity}) from ${p.product ?? "INTRADAY"} to ${to}?`)) {
                      void run(`Convert ${p.symbol} → ${to}`, async () => {
                        const r = await fetch(`/api/positions/${encodeURIComponent(p.symbol)}/convert`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ to }) });
                        const j = await r.json().catch(() => ({}));
                        if (!r.ok) throw new Error(j.detail ?? `HTTP ${r.status}`);
                        return `${p.symbol} is now ${j.product}`;
                      });
                    }
                  }}>{(p.product ?? "INTRADAY") === "INTRADAY" ? "→ Carry" : "→ Intraday"}</button>
                  <button type="button" className="btn-sm danger" title="Exit — full or partial, market or limit" onClick={() => setExit({ pos: { symbol: p.symbol, name: p.symbol, qty: p.quantity, avg: p.average_price, ltp: p.last_price, lot: lotOf(p.symbol) }, local: true })} data-testid={`exit-${p.symbol}`}>Exit</button>
                </>
              )}
            />
          </section>
        )}
        {tab === "allpositions" && (
          <AllPositionsPanel book={book.data} privacy={privacy} onExit={(p) => setExit({ pos: { symbol: p.symbol, name: p.symbol, qty: p.net_qty, avg: p.avg_price, ltp: p.ltp, lot: lotOf(p.symbol) }, local: false })} />
        )}
        {tab === "orders" && (
          <OrdersTable rows={rows} privacy={privacy} onCancel={onCancel} cancelBusyId={cancelBusyId} onModify={setModify} banner={cancelBanner} live={!!book.data?.ok} />
        )}
        {tab === "smart" && <SmartOrderbook rows={rows} privacy={privacy} onCancel={onCancel} onModify={setModify} />}
        {tab === "trades" && <TradesTable rows={tradeRows} privacy={privacy} />}
        {tab === "holdings" && <HoldingsPanel data={holdings.data} privacy={privacy} />}
        {tab === "funds" && <FundsPanel data={fundsFull.data} privacy={privacy} />}
        {tab === "gtt" && <GttPanel data={gtt.data} accountId={accountId} privacy={privacy} onDone={(m) => { setMsg(m); gtt.reload(); }} />}
        {tab === "account" && (
          <section className="trade-card am-account">
            <div className="quote-row">
              <div className="quote-cell"><div className="k">FUNDS AVAILABLE</div><div className="v">{funds.data?.available != null ? money(funds.data.available) : "—"}</div></div>
              <div className="quote-cell"><div className="k">POSITIONS P&amp;L</div><div className={`v ${totalPnl >= 0 ? "up" : "down"}`}>{money(totalPnl)}</div></div>
              <div className="quote-cell"><div className="k">REALIZED TODAY</div><div className={`v ${realized >= 0 ? "up" : "down"}`}>{money(realized)}</div></div>
              <div className="quote-cell" title="Demat holdings: overall P&L"><div className="k">HOLDINGS P&amp;L</div><div className={`v ${(holdings.data?.overall?.pnl ?? 0) >= 0 ? "up" : "down"}`}>{holdings.data?.ok ? money(holdings.data.overall?.pnl ?? null) : "—"}</div></div>
              <div className="quote-cell"><div className="k">OPEN POSITIONS</div><div className="v">{open.length}</div></div>
              <div className="quote-cell"><div className="k">WORKING ORDERS</div><div className="v">{workingCount}</div></div>
            </div>
            {prof && (
              <div className="am-profile">
                <span>Client ID <b>{prof.client_id ?? "—"}</b></span>
                <span>Name <b>{prof.name ?? "—"}</b></span>
                {prof.email && <span>Email <b>{privacy ? "•••" : prof.email}</b></span>}
                <span>Currency <b>INR</b></span>
              </div>
            )}
            {funds.data && !funds.data.ok && <div className="hint warn-text">{funds.data.reason ?? "funds unavailable — connect a Fyers account"}</div>}
          </section>
        )}
        {tab === "basket" && <Baskets selected={selected} orderFor={orderFor} />}
        {tab === "broker" && <BrokerLive privacy={privacy} />}
        {tab === "log" && (
          <section className="trade-card">
            {log.length === 0 ? (
              <div className="empty">No notifications yet — orders and alerts from the charts show up here.</div>
            ) : (
              <>
                <button type="button" className="btn-sm" onClick={clearLog}>Clear</button>
                <table className="pending-table">
                  <tbody>
                    {log.map((l) => (
                      <tr key={l.id}>
                        <td>{new Date(l.ts).toLocaleTimeString("en-IN")}</td>
                        <td className={l.kind === "error" ? "down" : l.kind === "alert" ? "warn-text" : ""}>{l.kind}</td>
                        <td>{l.text}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
          </section>
        )}
      </div>
      {modify && <ModifyOrderDialog order={modify} accountId={accountId} onClose={() => setModify(null)} onDone={(m) => { setMsg(m); book.reload(); }} />}
      {exit && (
        <ExitPositionDialog
          pos={exit.pos}
          onClose={() => setExit(null)}
          onExit={async (q, o, full) => {
            const r = full && exit.local ? await closeFor(exit.pos.symbol)().then(() => `Closed ${exit.pos.symbol} at market`) : await orderFor(exit.pos.symbol, exit.pos.name, q)(o);
            setMsg(`Exit ${exit.pos.symbol}: ${r}`);
            book.reload();
            return r;
          }}
        />
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Fyers live — the whole broker account, including orders placed from the
// Fyers app / web. Reloads on every `broker` event (order WebSocket or
// postback, dispatched by App as "broker:order"); P&L ticks with live quotes.
// ---------------------------------------------------------------------------

const SOURCES: Record<string, string> = { M: "Mobile", W: "Web", A: "API", ITS: "API", R: "Admin" };

function BrokerPositionRow({ p, privacy, onPnl }: { p: BrokerPosition; privacy: boolean; onPnl: (sym: string, v: number) => void }) {
  const live = useLiveQuote(p.net_qty ? p.symbol : null)?.last_price ?? null;
  const ltp = live ?? p.ltp;
  const pnl = livePnl(p, ltp);
  useEffect(() => onPnl(`${p.symbol}|${p.product}`, pnl ?? 0), [p.symbol, p.product, pnl]); // eslint-disable-line react-hooks/exhaustive-deps
  const m = (v: number | null) => (privacy ? "•••" : fmt(v));
  return (
    <tr>
      <td>{p.symbol}</td>
      <td>{p.product ?? "—"}</td>
      <td className={p.net_qty > 0 ? "up" : p.net_qty < 0 ? "down" : ""}>{p.net_qty}</td>
      <td>{fmt(p.avg_price)}</td>
      <td title={live != null ? "live tick" : "Fyers snapshot"}>{fmt(ltp)}{live != null && <span className="live-dot" />}</td>
      <td className={(p.realized ?? 0) >= 0 ? "up" : "down"}>{m(p.realized)}</td>
      <td className={(pnl ?? 0) >= 0 ? "up" : "down"}>{m(pnl)}</td>
    </tr>
  );
}

export function BrokerLive({ privacy }: { privacy: boolean }) {
  // events drive it (useApiJson reloads on each `broker:order`); the poll is a fallback
  const book = useApiJson<BrokerBook>("/api/broker/book", 15000);
  const [pnl, setPnl] = useState<Record<string, number>>({});
  const [flash, setFlash] = useState(false);
  useEffect(() => {
    let t: ReturnType<typeof setTimeout> | undefined;
    // One order fires several events (transit → rejected, socket + postback): one flash.
    const on = () => {
      clearTimeout(t);
      t = setTimeout(() => {
        setFlash(true);
        setTimeout(() => setFlash(false), 800);
      }, 150);
    };
    window.addEventListener("broker:order", on);
    return () => {
      window.removeEventListener("broker:order", on);
      clearTimeout(t);
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const d = book.data;
  if (!d) return <section className="trade-card"><div className="empty">Loading the Fyers account…</div></section>;
  if (!d.ok) return <section className="trade-card"><div className="empty">{d.reason ?? "Fyers account unavailable."}</div></section>;
  const working = d.orders.filter((o) => o.status === "PENDING" || o.status === "TRANSIT");
  const total = Object.values(pnl).reduce((a, b) => a + b, 0);
  const onPnl = (k: string, v: number) => setPnl((x) => (x[k] === v ? x : { ...x, [k]: v }));
  return (
    <section className={`trade-card broker-live${flash ? " flash" : ""}`} data-testid="broker-live">
      <div className="quote-row">
        <div className="quote-cell"><div className="k">ACCOUNT P&amp;L (LIVE)</div><div className={`v ${total >= 0 ? "up" : "down"}`}>{privacy ? "•••" : fmt(total)}</div></div>
        <div className="quote-cell"><div className="k">OPEN POSITIONS</div><div className="v">{d.positions.filter((p) => p.net_qty !== 0).length}</div></div>
        <div className="quote-cell"><div className="k">WORKING ORDERS</div><div className="v">{working.length}</div></div>
        <div className="quote-cell"><div className="k">ORDERS TODAY</div><div className="v">{d.orders.length}</div></div>
      </div>
      {(d.errors ?? []).length > 0 && <div className="hint warn-text">{(d.errors ?? []).join(" · ")}</div>}
      <div className="chart-menu-head">Positions — whole account (app, web and bot)</div>
      {d.positions.length === 0 ? (
        <div className="empty">No positions today.</div>
      ) : (
        <table className="pending-table">
          <thead><tr><th>Symbol</th><th>Product</th><th>Net qty</th><th>Avg</th><th>LTP</th><th>Realized</th><th>P&amp;L</th></tr></thead>
          <tbody>{d.positions.map((p) => <BrokerPositionRow key={`${p.symbol}-${p.product}`} p={p} privacy={privacy} onPnl={onPnl} />)}</tbody>
        </table>
      )}
      <div className="chart-menu-head">Orders today</div>
      {d.orders.length === 0 ? (
        <div className="empty">No orders today.</div>
      ) : (
        <table className="pending-table">
          <thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Type</th><th>Qty</th><th>Price</th><th>Status</th><th>From</th><th>ID</th></tr></thead>
          <tbody>
            {d.orders.map((o) => (
              <tr key={o.id} title={o.message}>
                <td>{o.time?.split(" ").pop() ?? "—"}</td>
                <td>{o.symbol}</td>
                <td className={o.side === "BUY" ? "up" : "down"}>{o.side}</td>
                <td>{o.type}</td>
                <td>{o.filled ? `${o.filled}/${o.qty}` : o.qty}</td>
                <td>{fmt(o.traded_price || o.limit_price || o.stop_price)}</td>
                <td className={o.status === "FILLED" ? "up" : o.status === "REJECTED" || o.status === "CANCELLED" ? "down" : "warn-text"}>{o.status}</td>
                <td>{o.ours ? "Bot" : SOURCES[o.source ?? ""] ?? o.source ?? "—"}</td>
                <td className="dim">{o.id}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Named layouts
// ---------------------------------------------------------------------------

export interface SavedLayout {
  id: string;
  name: string;
  saved: number;
  data: Record<string, string>;
  /** Starred layouts sort first in "Open layout". */
  starred?: boolean;
}

const LAYOUTS_KEY = "trade:layouts";
const CURRENT_KEY = "trade:layoutCurrent";
const AUTOSAVE_KEY = "trade:layoutAutosave";
const META = new Set([LAYOUTS_KEY, CURRENT_KEY, AUTOSAVE_KEY]);

function captureState(): Record<string, string> {
  const out: Record<string, string> = {};
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (!k || META.has(k) || !(k.startsWith("chart:") || k.startsWith("trade:"))) continue;
      out[k] = localStorage.getItem(k) ?? "";
    }
  } catch { /* storage disabled */ }
  return out;
}

function readLayouts(): SavedLayout[] {
  try {
    const v = JSON.parse(localStorage.getItem(LAYOUTS_KEY) ?? "[]");
    return Array.isArray(v) ? v : [];
  } catch {
    return [];
  }
}

const SERVER_KEY = "trade_layouts";

/** Union of two layout lists by id; the copy saved later wins.
 *  ponytail: no tombstones, so a layout deleted here can come back from a
 *  second browser's stale list; add deleted-ids if two browsers ever race. */
export function mergeLayouts(local: SavedLayout[], remote: SavedLayout[]): SavedLayout[] {
  const by = new Map<string, SavedLayout>();
  for (const l of [...local, ...remote]) {
    const have = by.get(l.id);
    if (!have || (l.saved ?? 0) > (have.saved ?? 0)) by.set(l.id, l);
  }
  return [...by.values()].sort((a, b) => a.saved - b.saved);
}

export function useLayouts() {
  const [layouts, setLayouts] = useState<SavedLayout[]>(readLayouts);
  const [current, setCurrent] = useState<string | null>(() => localStorage.getItem(CURRENT_KEY));
  const [autosave, setAutosaveState] = useState(() => localStorage.getItem(AUTOSAVE_KEY) !== "false");
  const [dirty, setDirty] = useState(false);
  const persist = (list: SavedLayout[], cur: string | null) => {
    setLayouts(list);
    setCurrent(cur);
    try {
      localStorage.setItem(LAYOUTS_KEY, JSON.stringify(list));
      if (cur) localStorage.setItem(CURRENT_KEY, cur);
      else localStorage.removeItem(CURRENT_KEY);
    } catch {
      /* quota — layouts are best-effort */
    }
    // Mirror to the server so layouts follow the operator to another browser.
    void fetch(`/api/settings/ui/${SERVER_KEY}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ value: list }) }).catch(() => undefined);
  };
  // Pull the server copy once: union by id, the newer save wins.
  useEffect(() => {
    void fetch(`/api/settings/ui/${SERVER_KEY}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => {
        const remote = Array.isArray(j?.value) ? (j.value as SavedLayout[]) : [];
        if (!remote.length) return;
        const merged = mergeLayouts(readLayouts(), remote);
        if (JSON.stringify(merged) !== JSON.stringify(readLayouts())) persist(merged, localStorage.getItem(CURRENT_KEY));
      })
      .catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const cur = layouts.find((l) => l.id === current) ?? null;
  // Dirty check + autosave every few seconds.
  useEffect(() => {
    const id = setInterval(() => {
      const list = readLayouts();
      const c = list.find((l) => l.id === localStorage.getItem(CURRENT_KEY));
      if (!c) {
        setDirty(true);
        return;
      }
      const now = captureState();
      const same = JSON.stringify(now) === JSON.stringify(c.data);
      if (!same && autosave) {
        persist(list.map((l) => (l.id === c.id ? { ...l, data: now, saved: Date.now() } : l)), c.id);
        setDirty(false);
      } else setDirty(!same);
    }, 4000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autosave]);
  const saveAs = (name: string) => {
    const l: SavedLayout = { id: `L${Date.now().toString(36)}`, name, saved: Date.now(), data: captureState() };
    persist([...layouts, l], l.id);
    setDirty(false);
  };
  const save = () => {
    if (!cur) {
      const name = window.prompt("Layout name", "My layout");
      if (name) saveAs(name);
      return;
    }
    persist(layouts.map((l) => (l.id === cur.id ? { ...l, data: captureState(), saved: Date.now() } : l)), cur.id);
    setDirty(false);
  };
  const load = (id: string) => {
    const l = layouts.find((x) => x.id === id);
    if (!l) return;
    try {
      const drop: string[] = [];
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (k && !META.has(k) && (k.startsWith("chart:") || k.startsWith("trade:"))) drop.push(k);
      }
      drop.forEach((k) => localStorage.removeItem(k));
      for (const [k, v] of Object.entries(l.data)) localStorage.setItem(k, v);
      localStorage.setItem(CURRENT_KEY, id);
    } catch {
      /* best-effort */
    }
    window.location.reload();
  };
  const rename = (id: string, name: string) => persist(layouts.map((l) => (l.id === id ? { ...l, name } : l)), current);
  const copy = (id: string) => {
    const l = layouts.find((x) => x.id === id);
    if (!l) return;
    const c = { ...l, id: `L${Date.now().toString(36)}`, name: `${l.name} copy`, saved: Date.now() };
    persist([...layouts, c], c.id);
  };
  const remove = (id: string) => persist(layouts.filter((l) => l.id !== id), current === id ? null : current);
  const star = (id: string) => persist(layouts.map((l) => (l.id === id ? { ...l, starred: !l.starred } : l)), current);
  const setAutosave = (v: boolean) => {
    setAutosaveState(v);
    try { localStorage.setItem(AUTOSAVE_KEY, String(v)); } catch { /* best-effort */ }
  };
  const newLayout = () => {
    if (!window.confirm("Start a new, empty layout? Unsaved changes to this one are lost.")) return;
    try {
      const drop: string[] = [];
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (k && !META.has(k) && (k.startsWith("chart:prefs") || k === "trade:layout" || k === "trade:cells" || k === "trade:splits")) drop.push(k);
      }
      drop.forEach((k) => localStorage.removeItem(k));
      localStorage.removeItem(CURRENT_KEY);
    } catch { /* best-effort */ }
    window.location.reload();
  };
  return { layouts, current: cur, autosave, dirty, save, saveAs, load, rename, copy, remove, star, setAutosave, newLayout };
}

/** "Open layout…": every saved layout, searchable, starred ones first. */
export function OpenLayoutDialog({ L, onClose }: { L: ReturnType<typeof useLayouts>; onClose: () => void }) {
  const [q, setQ] = useState("");
  const list = L.layouts
    .filter((l) => !q || l.name.toLowerCase().includes(q.trim().toLowerCase()))
    .sort((a, b) => Number(!!b.starred) - Number(!!a.starred) || b.saved - a.saved);
  return (
    <Modal title="Open layout" onClose={onClose} width={480} testid="open-layout">
      <input className="chart-menu-input" placeholder="Search layouts" value={q} onChange={(e) => setQ(e.target.value)} autoFocus aria-label="Search layouts" />
      {list.length === 0 && <div className="hint">{L.layouts.length ? "Nothing matches." : "No saved layouts yet — Save layout (Ctrl+S) to keep this one."}</div>}
      <div className="open-layout-list">
        {list.map((l) => (
          <div key={l.id} className={`open-layout-row${l.id === L.current?.id ? " on" : ""}`}>
            <button type="button" className={`ind-star${l.starred ? " on" : ""}`} onClick={() => L.star(l.id)} title={l.starred ? "Unstar" : "Star"} aria-label={`Star ${l.name}`}>★</button>
            <button type="button" className="open-layout-name" onClick={() => (l.id === L.current?.id ? onClose() : L.load(l.id))} data-testid={`open-layout-${l.name}`}>
              {l.name}
              <span className="hint">{new Date(l.saved).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}{l.id === L.current?.id ? " · open" : ""}</span>
            </button>
            <button type="button" className="chart-menu-x" onClick={() => { if (window.confirm(`Delete layout "${l.name}"?`)) L.remove(l.id); }} title="Delete">✕</button>
          </div>
        ))}
      </div>
    </Modal>
  );
}

export function LayoutMenu({ L, open, onOpen }: { L: ReturnType<typeof useLayouts>; open: boolean; onOpen: (o: boolean) => void }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [browse, setBrowse] = useState(false);
  useOutside(ref, open, () => onOpen(false));
  return (
    <div className="chart-menu-wrap layout-menu" ref={ref}>
      <button type="button" className="chart-btn" onClick={() => onOpen(!open)} title="Manage layouts" data-testid="layout-menu">
        💾 {L.current?.name ?? "Unsaved layout"} ▾
      </button>
      <span className={`layout-status${L.dirty ? " dirty" : ""}`} title={L.autosave ? "Autosave is on" : "Autosave is off"}>
        {L.current ? (L.dirty ? "Unsaved changes" : "All changes saved") : ""}
      </span>
      {open && (
        <div className="chart-menu cdrop right">
          <button type="button" className="chart-menu-item" onClick={() => { L.save(); onOpen(false); }}>Save layout<span className="kbd">Ctrl+S</span></button>
          <button type="button" className="chart-menu-item" onClick={() => { const n = window.prompt("Layout name", L.current ? `${L.current.name} 2` : "My layout"); if (n) L.saveAs(n); onOpen(false); }}>Save as…</button>
          {L.current && <button type="button" className="chart-menu-item" onClick={() => { const n = window.prompt("Rename layout", L.current!.name); if (n) L.rename(L.current!.id, n); onOpen(false); }}>Rename…</button>}
          {L.current && <button type="button" className="chart-menu-item" onClick={() => { L.copy(L.current!.id); onOpen(false); }}>Make a copy</button>}
          <button type="button" className="chart-menu-item" onClick={() => { setBrowse(true); onOpen(false); }} data-testid="open-layout-btn">Open layout…</button>
          <button type="button" className="chart-menu-item" onClick={() => L.newLayout()}>New layout…</button>
          <label><input type="checkbox" checked={L.autosave} onChange={(e) => L.setAutosave(e.target.checked)} />Autosave</label>
          {L.layouts.length > 0 && <div className="chart-menu-head">Saved layouts</div>}
          {[...L.layouts].sort((a, b) => Number(!!b.starred) - Number(!!a.starred)).slice(0, 8).map((l) => (
            <div key={l.id} className={`chart-menu-row${l.id === L.current?.id ? " on" : ""}`}>
              <button type="button" className="chart-menu-item" onClick={() => (l.id === L.current?.id ? onOpen(false) : L.load(l.id))} title={`saved ${new Date(l.saved).toLocaleString("en-IN")}`}>{l.starred ? "★ " : ""}{l.name}</button>
              <button type="button" className="chart-menu-x" onClick={() => { if (window.confirm(`Delete layout "${l.name}"?`)) L.remove(l.id); }} title="Delete">✕</button>
            </div>
          ))}
          {L.layouts.length > 8 && <button type="button" className="chart-menu-item hint" onClick={() => { setBrowse(true); onOpen(false); }}>All {L.layouts.length} layouts…</button>}
        </div>
      )}
      {browse && <OpenLayoutDialog L={L} onClose={() => setBrowse(false)} />}
    </div>
  );
}
