// TradePanels — the Trade page's TradingView-style side and bottom panels:
// the watchlist (several lists, sorting, flags, drag to reorder, import /
// export), symbol details + headlines, the account manager (positions,
// orders, trades, account, basket orders, notifications log) and the
// named-layout manager (save / load / autosave).

import { useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import type { InstrumentHit, Position } from "../../types";
import { useLiveQuote } from "../../hooks/useQuotes";
import { technicalRating, type OhlcvCandle } from "../../lib/indicators";
import { capTier } from "../../lib/marketCap";
import { clearLog, getLog, subscribeLog } from "./chartSync";
import { useOutside } from "./chartUi";
import type { ChartOrder } from "./ChartPanel";

const fmt = (v: number | null | undefined, d = 2) =>
  v == null || !Number.isFinite(v) ? "—" : v.toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });

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
            const idx = list.items.findIndex((i) => i.symbol === h.symbol);
            return (
              <div
                key={h.symbol}
                role="button"
                tabIndex={0}
                className={`tv-watch-row wl-row${selected === h.symbol ? " on" : ""}`}
                onClick={() => onSelect(h)}
                onKeyDown={(e) => { if (e.key === "Enter") onSelect(h); }}
                onContextMenu={(e) => { e.preventDefault(); setMenu({ sym: h.symbol, x: e.clientX, y: e.clientY }); }}
                draggable={!sort}
                onDragStart={() => { dragFrom.current = idx; }}
                onDragOver={(e) => { if (!sort) e.preventDefault(); }}
                onDrop={() => {
                  const from = dragFrom.current;
                  dragFrom.current = null;
                  if (from === null || from === idx) return;
                  const items = [...list.items];
                  const [m] = items.splice(from, 1);
                  items.splice(idx, 0, m);
                  putList({ items });
                }}
                title={`${h.symbol} — right-click for options, drag to reorder`}
                data-testid={`recent-chip-${h.symbol}`}
                style={flag ? { boxShadow: `inset 3px 0 ${flag}` } : undefined}
              >
                <span className="sym">{h.short_name}</span>
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
          {state.lists.length > 1 && <div className="chart-menu-head">Add to list</div>}
          {state.lists.map((l, i) =>
            i === state.active ? null : (
              <button key={i} type="button" className="chart-menu-item" onClick={() => {
                const h = list.items.find((x) => x.symbol === menu.sym);
                if (h) onChange({ ...state, lists: state.lists.map((y, j) => (j === i && !y.items.some((z) => z.symbol === h.symbol) ? { ...y, items: [...y.items, h] } : y)) });
                setMenu(null);
              }}>{l.name}</button>
            ),
          )}
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

export type BottomTab = "positions" | "orders" | "trades" | "account" | "basket" | "log";

interface TradeRow {
  id: number;
  symbol: string;
  side: string;
  quantity: number;
  price: number | null;
  order_type: string;
  status: string;
  broker_order_id: string | null;
  pnl: number | null;
  executed_at: string | null;
  created_at: string | null;
}

function useJson<T>(url: string | null, every = 0): { data: T | null; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    if (!url) return;
    let stop = false;
    const load = () =>
      fetch(url)
        .then((r) => (r.ok ? r.json() : null))
        .then((j) => !stop && setData(j as T))
        .catch(() => undefined);
    void load();
    const id = every ? setInterval(load, every) : undefined;
    return () => {
      stop = true;
      if (id) clearInterval(id);
    };
  }, [url, every, nonce]);
  return { data, reload: () => setNonce((n) => n + 1) };
}

function PositionRowAM({
  p,
  sl,
  tp,
  privacy,
  onClose,
  onReverse,
  onLevels,
  cols,
}: {
  p: Position;
  sl: number | null;
  tp: number | null;
  privacy: boolean;
  onClose: () => void;
  onReverse: () => void;
  onLevels: (sl: number | null, tp: number | null) => void;
  cols: Record<string, boolean>;
}) {
  const live = useLiveQuote(p.symbol);
  const ltp = live?.last_price ?? p.last_price;
  const pnl = live?.last_price != null ? (live.last_price - p.average_price) * p.quantity : p.unrealized_pnl;
  const money = (v: number | null | undefined) => (privacy ? "•••" : fmt(v));
  const ask = (which: "SL" | "TP") => {
    const cur = which === "SL" ? sl : tp;
    const v = window.prompt(`${which} for ${p.symbol} (blank clears)`, cur != null ? String(cur) : "");
    if (v === null) return;
    const n = v.trim() === "" ? null : Number(v);
    if (n !== null && !Number.isFinite(n)) return;
    onLevels(which === "SL" ? n : sl, which === "TP" ? n : tp);
  };
  return (
    <tr>
      <td className="sym">{p.symbol}</td>
      {cols.side && <td className={p.quantity > 0 ? "up" : "down"}>{p.quantity > 0 ? "BUY" : "SELL"}</td>}
      <td className={p.quantity > 0 ? "up" : "down"}>{p.quantity}</td>
      <td>{fmt(p.average_price)}</td>
      <td>{fmt(ltp)}</td>
      <td className={(pnl ?? 0) >= 0 ? "up" : "down"}>{money(pnl)}</td>
      {cols.levels && <td><button type="button" className="am-link" onClick={() => ask("SL")}>{sl != null ? fmt(sl) : "+ SL"}</button></td>}
      {cols.levels && <td><button type="button" className="am-link" onClick={() => ask("TP")}>{tp != null ? fmt(tp) : "+ TP"}</button></td>}
      <td className="am-acts">
        <button type="button" className="btn-sm" onClick={onReverse} title="Reverse at market">⇅</button>
        <button type="button" className="btn-sm danger" onClick={onClose} title="Close at market">Close</button>
      </td>
    </tr>
  );
}

interface BasketLeg {
  id: number;
  symbol: string;
  name: string;
  side: "BUY" | "SELL";
  qty: number;
  type: "MARKET" | "LIMIT";
  price: number | null;
  result?: string;
}

export function AccountManager({
  tab,
  onTab,
  positions,
  managed,
  pendingSection,
  pendingCount,
  privacy,
  connected,
  accountLabel,
  selected,
  closeFor,
  levelsFor,
  orderFor,
  onMaximize,
  maximized,
  onCollapse,
}: {
  tab: BottomTab;
  onTab: (t: BottomTab | null) => void;
  positions: Position[] | undefined;
  managed: { symbol: string; stop_loss: number | null; target: number | null }[] | undefined;
  pendingSection: ReactNode;
  pendingCount: number;
  privacy: boolean;
  connected: boolean;
  accountLabel: string;
  selected: InstrumentHit | null;
  closeFor: (sym: string) => () => Promise<void>;
  levelsFor: (sym: string) => (sl: number | null, tp: number | null) => Promise<void>;
  orderFor: (sym: string, name: string, qty: number) => (o: ChartOrder) => Promise<string>;
  onMaximize: () => void;
  maximized: boolean;
  onCollapse: () => void;
}) {
  const [msg, setMsg] = useState<string | null>(null);
  const [orderFilter, setOrderFilter] = useState<"all" | "working" | "filled" | "cancelled" | "rejected">("all");
  const [cols, setCols] = useState<Record<string, boolean>>({ side: true, levels: true });
  const [colMenu, setColMenu] = useState(false);
  const colRef = useRef<HTMLDivElement | null>(null);
  useOutside(colRef, colMenu, () => setColMenu(false));
  const trades = useJson<TradeRow[]>(tab === "orders" || tab === "trades" || tab === "account" ? "/api/trades?limit=200" : null, 10000);
  const funds = useJson<{ ok: boolean; available: number | null; reason?: string | null }>(tab === "account" || tab === "positions" ? "/api/market/funds" : null, 30000);
  const log = useSyncExternalStore(subscribeLog, getLog);
  const open = (positions ?? []).filter((p) => p.quantity !== 0);
  const totalPnl = open.reduce((a, p) => a + (p.unrealized_pnl ?? 0), 0);
  const money = (v: number | null | undefined) => (privacy ? "•••" : fmt(v));
  const tradeRows = Array.isArray(trades.data) ? trades.data : [];
  const statusOf = (s: string) => s.toLowerCase();
  const filteredTrades = tradeRows.filter((t) => {
    const s = statusOf(t.status);
    if (orderFilter === "all") return true;
    if (orderFilter === "working") return /pending|open|trigger|working/.test(s);
    if (orderFilter === "filled") return /fill|complete|traded|executed/.test(s);
    if (orderFilter === "cancelled") return /cancel/.test(s);
    return /reject/.test(s);
  });
  const realized = tradeRows.filter((t) => t.pnl != null && t.executed_at && new Date(t.executed_at).toDateString() === new Date().toDateString()).reduce((a, t) => a + (t.pnl ?? 0), 0);
  const run = async (label: string, f: () => Promise<unknown>) => {
    try {
      const r = await f();
      setMsg(`${label}: ${typeof r === "string" ? r : "done"}`);
    } catch (e) {
      setMsg(`${label} failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  };
  const tabs: [BottomTab, string][] = [
    ["positions", `Positions${open.length ? ` (${open.length})` : ""}`],
    ["orders", `Orders${pendingCount ? ` (${pendingCount})` : ""}`],
    ["trades", "Trades"],
    ["account", "Account"],
    ["basket", "Basket"],
    ["log", `Notifications${log.length ? ` (${log.length})` : ""}`],
  ];
  return (
    <>
      <div className="tabs trade-tabs am-tabs" role="tablist">
        <span className="am-title">Account Manager</span>
        {tabs.map(([k, label]) => (
          <button key={k} type="button" role="tab" aria-selected={tab === k} className={`tab ${tab === k ? "active" : ""}`} onClick={() => onTab(k)}>{label}</button>
        ))}
        <span className="am-status">
          <span className={`lg-dot ${connected ? "open" : "closed"}`} /> {connected ? accountLabel : "No live account"}
          {funds.data?.available != null && <> · Funds <b>{money(funds.data.available)}</b></>}
          {open.length > 0 && <> · P&L <b className={totalPnl >= 0 ? "up" : "down"}>{money(totalPnl)}</b></>}
        </span>
        <button type="button" className="tab tv-collapse" onClick={onMaximize} title={maximized ? "Restore panel" : "Maximize panel"}>{maximized ? "❐" : "⬚"}</button>
        <button type="button" className="tab" onClick={onCollapse} title="Close panel">▾</button>
      </div>
      <div className="tv-bottom-body">
        {msg && <div className="result info am-msg" onClick={() => setMsg(null)}>{msg}</div>}
        {tab === "positions" && (
          <section className="trade-card" data-testid="trade-positions">
            {open.length === 0 ? (
              <div className="empty">There are no open positions in your trading account yet.</div>
            ) : (
              <>
                <div className="am-toolbar">
                  <button type="button" className="btn-sm danger" onClick={() => {
                    if (!window.confirm(`Exit all ${open.length} open position(s) at market?`)) return;
                    void run("Exit all", async () => {
                      const r = await fetch("/api/positions/close-all", { method: "POST" });
                      const j = await r.json().catch(() => ({}));
                      if (!r.ok) throw new Error(j.detail ?? `HTTP ${r.status}`);
                      return "sent";
                    });
                  }}>Exit all</button>
                  <div className="chart-menu-wrap" ref={colRef}>
                    <button type="button" className="btn-sm" onClick={() => setColMenu((o) => !o)} title="Columns">||||</button>
                    {colMenu && (
                      <div className="chart-menu cdrop right">
                        {([["side", "Buy/Sell"], ["levels", "Stop-loss / target"]] as const).map(([k, l]) => (
                          <label key={k}><input type="checkbox" checked={cols[k]} onChange={(e) => setCols((c) => ({ ...c, [k]: e.target.checked }))} />{l}</label>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
                <table className="positions-table">
                  <thead>
                    <tr>
                      <th>Symbol</th>
                      {cols.side && <th>Buy/Sell</th>}
                      <th>Net qty</th>
                      <th>Avg</th>
                      <th>LTP</th>
                      <th>Unrealized P&amp;L</th>
                      {cols.levels && <th>SL</th>}
                      {cols.levels && <th>TP</th>}
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {open.map((p) => {
                      const m = managed?.find((x) => x.symbol === p.symbol);
                      return (
                        <PositionRowAM
                          key={p.symbol}
                          p={p}
                          sl={m?.stop_loss ?? null}
                          tp={m?.target ?? null}
                          privacy={privacy}
                          cols={cols}
                          onClose={() => {
                            if (window.confirm(`Close ${p.symbol} at market?`)) void run(`Close ${p.symbol}`, closeFor(p.symbol));
                          }}
                          onReverse={() => {
                            const q = Math.abs(p.quantity) * 2;
                            if (window.confirm(`Reverse ${p.symbol}: ${p.quantity > 0 ? "SELL" : "BUY"} ${q} at market?`)) {
                              void run(`Reverse ${p.symbol}`, () => orderFor(p.symbol, p.symbol, q)({ side: p.quantity > 0 ? "SELL" : "BUY", type: "MARKET", price: null }));
                            }
                          }}
                          onLevels={(sl, tp) => void run(`Exits ${p.symbol}`, () => levelsFor(p.symbol)(sl, tp))}
                        />
                      );
                    })}
                  </tbody>
                </table>
              </>
            )}
          </section>
        )}
        {tab === "orders" && (
          <>
            <div className="sym-filters">
              {(["all", "working", "filled", "cancelled", "rejected"] as const).map((f) => (
                <button key={f} type="button" className={`chip${orderFilter === f ? " on" : ""}`} onClick={() => setOrderFilter(f)}>{f[0].toUpperCase() + f.slice(1)}</button>
              ))}
            </div>
            {(orderFilter === "all" || orderFilter === "working") && pendingSection}
            {orderFilter !== "working" && (
              <section className="trade-card">
                <h2>Order history</h2>
                {filteredTrades.length === 0 ? (
                  <div className="empty">No {orderFilter === "all" ? "" : orderFilter} orders.</div>
                ) : (
                  <table className="pending-table">
                    <thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Type</th><th>Qty</th><th>Price</th><th>Status</th><th>ID</th></tr></thead>
                    <tbody>
                      {filteredTrades.map((t) => (
                        <tr key={t.id}>
                          <td>{t.created_at ? new Date(t.created_at).toLocaleString("en-IN") : "—"}</td>
                          <td className="sym">{t.symbol}</td>
                          <td className={t.side === "BUY" ? "up" : "down"}>{t.side}</td>
                          <td>{t.order_type}</td>
                          <td>{t.quantity}</td>
                          <td>{fmt(t.price)}</td>
                          <td>{t.status}</td>
                          <td className="broker-id">{t.broker_order_id ?? "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </section>
            )}
          </>
        )}
        {tab === "trades" && (
          <section className="trade-card">
            {tradeRows.filter((t) => t.executed_at).length === 0 ? (
              <div className="empty">No executed trades yet.</div>
            ) : (
              <table className="pending-table">
                <thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Traded price</th><th>P&amp;L</th></tr></thead>
                <tbody>
                  {tradeRows.filter((t) => t.executed_at).map((t) => (
                    <tr key={t.id}>
                      <td>{new Date(t.executed_at!).toLocaleString("en-IN")}</td>
                      <td className="sym">{t.symbol}</td>
                      <td className={t.side === "BUY" ? "up" : "down"}>{t.side}</td>
                      <td>{t.quantity}</td>
                      <td>{fmt(t.price)}</td>
                      <td className={(t.pnl ?? 0) >= 0 ? "up" : "down"}>{t.pnl == null ? "—" : money(t.pnl)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        )}
        {tab === "account" && (
          <section className="trade-card am-account">
            <div className="quote-row">
              <div className="quote-cell"><div className="k">FUNDS AVAILABLE</div><div className="v">{funds.data?.available != null ? money(funds.data.available) : "—"}</div></div>
              <div className="quote-cell"><div className="k">POSITIONS P&amp;L</div><div className={`v ${totalPnl >= 0 ? "up" : "down"}`}>{money(totalPnl)}</div></div>
              <div className="quote-cell"><div className="k">REALIZED TODAY</div><div className={`v ${realized >= 0 ? "up" : "down"}`}>{money(realized)}</div></div>
              <div className="quote-cell"><div className="k">OPEN POSITIONS</div><div className="v">{open.length}</div></div>
              <div className="quote-cell"><div className="k">WORKING ORDERS</div><div className="v">{pendingCount}</div></div>
            </div>
            {funds.data && !funds.data.ok && <div className="hint warn-text">{funds.data.reason ?? "funds unavailable — connect a Fyers account"}</div>}
          </section>
        )}
        {tab === "basket" && <Basket selected={selected} orderFor={orderFor} />}
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
    </>
  );
}

function Basket({ selected, orderFor }: { selected: InstrumentHit | null; orderFor: (sym: string, name: string, qty: number) => (o: ChartOrder) => Promise<string> }) {
  const [name, setName] = useState(() => localStorage.getItem("trade:basketName") ?? "Basket 1");
  const [legs, setLegs] = useState<BasketLeg[]>(() => {
    try { return JSON.parse(localStorage.getItem("trade:basket") ?? "[]"); } catch { return []; }
  });
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    try {
      localStorage.setItem("trade:basket", JSON.stringify(legs.map(({ result: _r, ...l }) => l)));
      localStorage.setItem("trade:basketName", name);
    } catch { /* best-effort */ }
  }, [legs, name]);
  const put = (id: number, patch: Partial<BasketLeg>) => setLegs((l) => l.map((x) => (x.id === id ? { ...x, ...patch } : x)));
  const placeAll = async () => {
    if (!legs.length || !window.confirm(`Place ${legs.length} order(s) in "${name}" now? These are real orders.`)) return;
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
  return (
    <section className="trade-card" data-testid="basket">
      <div className="am-toolbar">
        <input className="basket-name" value={name} onChange={(e) => setName(e.target.value)} aria-label="Basket name" />
        <button type="button" className="btn-sm" disabled={!selected} onClick={() => selected && setLegs((l) => [...l, { id: Date.now(), symbol: selected.symbol, name: selected.short_name, side: "BUY", qty: selected.lot_size || 1, type: "MARKET", price: null }])}>
          + Add {selected?.short_name ?? "the chart symbol"}
        </button>
        <span className="grow" />
        <button type="button" className="btn-sm" disabled={!legs.length} onClick={() => setLegs([])}>Clear</button>
        <button type="button" className="btn-sm primary" disabled={!legs.length || busy} onClick={() => void placeAll()} data-testid="basket-place">{busy ? "placing…" : `Place all (${legs.length})`}</button>
      </div>
      {legs.length === 0 ? (
        <div className="empty">Add legs from the charted symbol, then place them together. Every leg is an intraday order on the live account.</div>
      ) : (
        <table className="pending-table">
          <thead><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Type</th><th>Price</th><th>Result</th><th /></tr></thead>
          <tbody>
            {legs.map((l) => (
              <tr key={l.id}>
                <td className="sym">{l.name}</td>
                <td>
                  <button type="button" className={`btn-sm ${l.side === "BUY" ? "primary" : "danger"}`} onClick={() => put(l.id, { side: l.side === "BUY" ? "SELL" : "BUY" })}>{l.side}</button>
                </td>
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
  return { layouts, current: cur, autosave, dirty, save, saveAs, load, rename, copy, remove, setAutosave, newLayout };
}

export function LayoutMenu({ L, open, onOpen }: { L: ReturnType<typeof useLayouts>; open: boolean; onOpen: (o: boolean) => void }) {
  const ref = useRef<HTMLDivElement | null>(null);
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
          <button type="button" className="chart-menu-item" onClick={() => L.newLayout()}>New layout…</button>
          <label><input type="checkbox" checked={L.autosave} onChange={(e) => L.setAutosave(e.target.checked)} />Autosave</label>
          {L.layouts.length > 0 && <div className="chart-menu-head">Saved layouts</div>}
          {L.layouts.map((l) => (
            <div key={l.id} className={`chart-menu-row${l.id === L.current?.id ? " on" : ""}`}>
              <button type="button" className="chart-menu-item" onClick={() => (l.id === L.current?.id ? onOpen(false) : L.load(l.id))} title={`saved ${new Date(l.saved).toLocaleString("en-IN")}`}>{l.name}</button>
              <button type="button" className="chart-menu-x" onClick={() => { if (window.confirm(`Delete layout "${l.name}"?`)) L.remove(l.id); }} title="Delete">✕</button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
