// DomPanel — the account manager's "Trade" tab: a DOM price ladder for the
// charted symbol with the position, its working orders (drag one to another
// price to modify it), the volume traded at each price since the panel
// opened, and the Flatten / CXL All / Reverse / Buy Mkt / Sell Mkt buttons.
// Orders go through the Trade page (risk checks, trading-mode gates); a
// click asks for confirmation unless one-click (instant) trading is on.

import { useEffect, useMemo, useRef, useState } from "react";
import type { InstrumentHit } from "../../types";
import { useLiveQuote } from "../../hooks/useQuotes";
import { useOutside } from "./chartUi";
import type { ChartOrder } from "./ChartPanel";
import { fmt, type OrderRow } from "./AccountPanels";

const fmtQty = (v: number) => (v >= 1e7 ? `${(v / 1e7).toFixed(2)}Cr` : v >= 1e5 ? `${(v / 1e5).toFixed(2)}L` : v >= 1e3 ? `${(v / 1e3).toFixed(1)}K` : String(Math.round(v)));

interface Depth {
  ok: boolean;
  reason?: string;
  bids?: [number, number, number][];
  asks?: [number, number, number][];
  total_buy?: number;
  total_sell?: number;
  ltp?: number | null;
}

export interface DomSettings {
  /** Rows strictly between the best bid and the best ask. */
  between: boolean;
  /** Prices nothing traded at (and no book / orders) — off compacts the ladder. */
  zeroVol: boolean;
  volume: boolean;
  rows: number;
}

const DOM_KEY = "trade:domSettings";
const DEFAULT_DOM: DomSettings = { between: true, zeroVol: true, volume: true, rows: 21 };

function loadDom(): DomSettings {
  try {
    return { ...DEFAULT_DOM, ...(JSON.parse(localStorage.getItem(DOM_KEY) ?? "{}") as Partial<DomSettings>) };
  } catch {
    return DEFAULT_DOM;
  }
}

/** Ladder rows (price keys, high → low) nearest `center`, skipping the ones
 *  the settings hide (the LTP row always stays). */
export function ladderKeys(
  center: number,
  n: number,
  s: Pick<DomSettings, "between" | "zeroVol">,
  info: { bestBid: number | null; bestAsk: number | null; ltp: number | null; has: (k: number) => boolean },
): number[] {
  const keep = (k: number) => {
    if (info.ltp != null && k === info.ltp) return true;
    if (!s.between && info.bestBid != null && info.bestAsk != null && k > info.bestBid && k < info.bestAsk) return false;
    return s.zeroVol || info.has(k);
  };
  const out: number[] = keep(center) ? [center] : [];
  for (let d = 1; out.length < n && d <= n * 20; d++) {
    if (keep(center + d)) out.push(center + d);
    if (out.length < n && keep(center - d)) out.push(center - d);
  }
  return out.sort((x, y) => y - x);
}

export function DomPanel({
  symbol,
  position,
  orders,
  qty,
  onQty,
  instant,
  onOrder,
  onCancel,
  onModify,
  onFlatten,
  onClose,
  onMsg,
}: {
  symbol: InstrumentHit | null;
  position: { qty: number; avg: number } | null;
  /** Working orders on this symbol. */
  orders: OrderRow[];
  qty: number;
  onQty: (n: number) => void;
  instant: boolean;
  onOrder: (o: ChartOrder, qty: number) => Promise<string>;
  onCancel: (id: string) => void;
  onModify: (o: OrderRow, price: number) => Promise<string>;
  onFlatten: () => Promise<void>;
  onClose: () => void;
  onMsg: (m: string) => void;
}) {
  const sym = symbol?.symbol ?? null;
  const tick = symbol?.tick_size && symbol.tick_size > 0 ? symbol.tick_size : 0.05;
  const lot = symbol?.lot_size && symbol.lot_size > 1 ? symbol.lot_size : 1;
  const [d, setD] = useState<Depth | null>(null);
  const [s, setS] = useState<DomSettings>(loadDom);
  const [menu, setMenu] = useState(false);
  const [pinned, setPinned] = useState<number | null>(null);
  const [vap, setVap] = useState<Map<number, number>>(new Map());
  const [drag, setDrag] = useState<string | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  useOutside(menuRef, menu, () => setMenu(false));
  useEffect(() => {
    try {
      localStorage.setItem(DOM_KEY, JSON.stringify(s));
    } catch {
      /* best-effort */
    }
  }, [s]);
  const live = useLiveQuote(sym);
  const key = (p: number) => Math.round(p / tick);

  useEffect(() => {
    setD(null);
    setPinned(null);
    setVap(new Map());
    if (!sym) return;
    let stop = false;
    const load = () => {
      if (document.hidden) return;
      void fetch(`/api/market/depth?symbol=${encodeURIComponent(sym)}`)
        .then((r) => r.json())
        .then((j: Depth) => !stop && setD(j))
        .catch(() => !stop && setD({ ok: false, reason: "depth request failed" }));
    };
    load();
    const id = setInterval(load, 2000); // Fyers caps data calls (~200/min): a 2s poll
    return () => {
      stop = true;
      clearInterval(id);
    };
  }, [sym]);

  // volume at price: each tick's volume increase, booked at its last price
  const lastVol = useRef<number | null>(null);
  useEffect(() => {
    lastVol.current = null;
  }, [sym]);
  useEffect(() => {
    if (!live || live.last_price == null || live.volume == null) return;
    const prev = lastVol.current;
    lastVol.current = live.volume;
    if (prev == null || live.volume <= prev) return;
    const k = key(live.last_price);
    const add = live.volume - prev;
    setVap((m) => new Map(m).set(k, (m.get(k) ?? 0) + add));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live]);

  const bids = d?.ok ? d.bids ?? [] : [];
  const asks = d?.ok ? d.asks ?? [] : [];
  const ltp = live?.last_price ?? d?.ltp ?? null;
  const bestBid = bids[0]?.[0] ?? live?.bid ?? null;
  const bestAsk = asks[0]?.[0] ?? live?.ask ?? null;
  const bidAt = useMemo(() => new Map(bids.map((r) => [key(r[0]), r[1]])), [bids]); // eslint-disable-line react-hooks/exhaustive-deps
  const askAt = useMemo(() => new Map(asks.map((r) => [key(r[0]), r[1]])), [asks]); // eslint-disable-line react-hooks/exhaustive-deps
  const ordAt = useMemo(() => {
    const m = new Map<number, OrderRow[]>();
    for (const o of orders) {
      const px = o.type === "SL-M" || o.type === "STOP_LOSS" ? o.stop : o.limit;
      if (px == null) continue;
      m.set(key(px), [...(m.get(key(px)) ?? []), o]);
    }
    return m;
  }, [orders]); // eslint-disable-line react-hooks/exhaustive-deps
  const maxQ = Math.max(1, ...bids.map((r) => r[1]), ...asks.map((r) => r[1]));
  const maxV = Math.max(1, ...vap.values());
  const mid = bestBid != null && bestAsk != null ? (bestBid + bestAsk) / 2 : ltp;
  const center = pinned ?? (mid != null ? key(mid) : null);
  const rows = center == null ? [] : ladderKeys(center, s.rows, s, {
    bestBid: bestBid != null ? key(bestBid) : null,
    bestAsk: bestAsk != null ? key(bestAsk) : null,
    ltp: ltp != null ? key(ltp) : null,
    has: (k) => (vap.get(k) ?? 0) > 0 || bidAt.has(k) || askAt.has(k) || ordAt.has(k),
  });

  const confirmGo = (text: string) => instant || window.confirm(`${text}?`);
  const send = (o: ChartOrder, text: string) => {
    if (!confirmGo(text)) return;
    onOrder(o, qty).then((m) => onMsg(m), (e) => onMsg(`Order failed: ${e instanceof Error ? e.message : String(e)}`));
  };
  // TradingView's DOM: a buy below the market rests as a limit, above it as a stop
  const clickBid = (p: number) => {
    const above = bestAsk != null ? p > bestAsk : ltp != null && p > ltp;
    send(above ? { side: "BUY", type: "SL-M", price: p } : { side: "BUY", type: "LIMIT", price: p }, `Buy ${qty} ${symbol?.short_name} ${above ? "stop" : "limit"} @ ${fmt(p)}`);
  };
  const clickAsk = (p: number) => {
    const below = bestBid != null ? p < bestBid : ltp != null && p < ltp;
    send(below ? { side: "SELL", type: "SL-M", price: p } : { side: "SELL", type: "LIMIT", price: p }, `Sell ${qty} ${symbol?.short_name} ${below ? "stop" : "limit"} @ ${fmt(p)}`);
  };
  const dropAt = (p: number) => {
    const o = orders.find((x) => x.id === drag);
    setDrag(null);
    if (!o) return;
    const cur = o.type === "SL-M" || o.type === "STOP_LOSS" ? o.stop : o.limit;
    if (cur != null && key(cur) === key(p)) return;
    if (!confirmGo(`Move ${o.side} ${o.remaining ?? o.qty} to ${fmt(p)}`)) return;
    onModify(o, p).then((m) => onMsg(m), (e) => onMsg(`Modify failed: ${e instanceof Error ? e.message : String(e)}`));
  };
  const pos = position && position.qty !== 0 ? position : null;
  const pnl = pos && ltp != null ? (ltp - pos.avg) * pos.qty : null;

  if (!symbol) return <section className="trade-card"><div className="empty">Open a symbol to trade from its DOM.</div></section>;
  return (
    <section className="trade-card dom-trade" data-testid="dom-trade">
      <div className="dom-head">
        <b>{symbol.short_name}, Trading</b>
        <span className="hint">{instant ? "⚡ one-click" : "clicks ask to confirm"}</span>
        <span className="grow" />
        <button type="button" className="btn-sm" onClick={() => setPinned(null)} title="Recenter on the market" data-testid="dom-recenter">⌖</button>
        <button type="button" className="btn-sm" onClick={() => { setVap(new Map()); setPinned(null); }} title="Clear the traded volume and recenter" data-testid="dom-clear">✕</button>
        <div className="chart-menu-wrap" ref={menuRef}>
          <button type="button" className="btn-sm" onClick={() => setMenu((o) => !o)} title="DOM settings" data-testid="dom-settings">…</button>
          {menu && (
            <div className="chart-menu cdrop right">
              <label><input type="checkbox" checked={s.between} onChange={(e) => setS({ ...s, between: e.target.checked })} />Show prices between the best bid / ask</label>
              <label><input type="checkbox" checked={s.zeroVol} onChange={(e) => setS({ ...s, zeroVol: e.target.checked })} />Show zero trade volume prices</label>
              <label><input type="checkbox" checked={s.volume} onChange={(e) => setS({ ...s, volume: e.target.checked })} />Volume column</label>
              <div className="chart-menu-row">
                <span>Rows</span>
                <select className="cform-sel" value={s.rows} onChange={(e) => setS({ ...s, rows: Number(e.target.value) })} aria-label="Rows">
                  {[11, 15, 21, 31, 41].map((n) => <option key={n} value={n}>{n}</option>)}
                </select>
              </div>
            </div>
          )}
        </div>
        <button type="button" className="btn-sm" onClick={onClose} title="Close" aria-label="Close DOM">✕</button>
      </div>
      <div className="dom-pos" data-testid="dom-position">
        <span>Position <b className={pos ? (pos.qty > 0 ? "up" : "down") : ""}>{pos ? `${pos.qty > 0 ? "+" : ""}${pos.qty}` : "0"}</b></span>
        <span>{pos ? <>Avg <b>{fmt(pos.avg)}</b> · P&amp;L <b className={(pnl ?? 0) >= 0 ? "up" : "down"}>{fmt(pnl)}</b></> : <span className="dim">flat</span>}</span>
      </div>
      <div className="dom-btns">
        <button type="button" className="btn-sm" disabled={!pos} onClick={() => pos && confirmGo(`Flatten ${symbol.short_name} at market`) && onFlatten().then(() => onMsg(`Flattened ${symbol.short_name}`), (e) => onMsg(`Flatten failed: ${e instanceof Error ? e.message : String(e)}`))} data-testid="dom-flatten">Flatten</button>
        <button type="button" className="btn-sm" disabled={!orders.length} onClick={() => { if (window.confirm(`Cancel all ${orders.length} working order(s) on ${symbol.short_name}?`)) orders.forEach((o) => o.id && onCancel(o.id)); }} data-testid="dom-cxl-all">CXL All</button>
        <button type="button" className="btn-sm" disabled={!pos} onClick={() => pos && send({ side: pos.qty > 0 ? "SELL" : "BUY", type: "MARKET", price: null }, `Reverse ${symbol.short_name}: ${pos.qty > 0 ? "SELL" : "BUY"} ${Math.abs(pos.qty) * 2} at market`)} data-testid="dom-reverse" title="Opposite side, twice the position, at market">Reverse</button>
      </div>
      {!d ? (
        <div className="hint">Loading depth…</div>
      ) : !d.ok && rows.length === 0 ? (
        <div className="hint">{d.reason ?? "Depth unavailable."}</div>
      ) : (
        <div
          className="dom-ladder"
          onWheel={(e) => {
            if (center == null) return;
            setPinned(center + (e.deltaY > 0 ? -3 : 3));
          }}
        >
          <table className="dom-table dom-full" data-testid="dom-ladder">
            <thead><tr><th>Buy</th><th>Bid qty</th><th>Price</th><th>Ask qty</th><th>Sell</th>{s.volume && <th>Vol</th>}</tr></thead>
            <tbody>
              {rows.map((k) => {
                const p = k * tick;
                const bq = bidAt.get(k);
                const aq = askAt.get(k);
                const v = vap.get(k);
                const own = ordAt.get(k) ?? [];
                const isLtp = ltp != null && key(ltp) === k;
                const below = mid != null && p < mid;
                const chip = (o: OrderRow) => (
                  <span
                    key={o.key}
                    className={`dom-ord ${o.side === "BUY" ? "b" : "s"}`}
                    draggable
                    onDragStart={(e) => { setDrag(o.id); e.dataTransfer.effectAllowed = "move"; }}
                    onDragEnd={() => setDrag(null)}
                    title={`${o.side} ${o.remaining ?? o.qty} ${o.type === "STOP_LOSS" ? "stop-limit" : o.type.toLowerCase()} — drag to another price to modify`}
                  >
                    {o.remaining ?? o.qty}{o.type !== "LIMIT" && <i>stp</i>}
                    <button type="button" onClick={(e) => { e.stopPropagation(); if (o.id && window.confirm(`Cancel ${o.side} ${o.remaining ?? o.qty} @ ${fmt(p)}?`)) onCancel(o.id); }} title="Cancel">✕</button>
                  </span>
                );
                return (
                  <tr
                    key={k}
                    className={`${isLtp ? "ltp" : ""}${drag ? " droppable" : ""}`}
                    onDragOver={(e) => drag && e.preventDefault()}
                    onDrop={(e) => { e.preventDefault(); dropAt(p); }}
                  >
                    <td className="ord">{own.filter((o) => o.side === "BUY").map(chip)}</td>
                    <td className="bid" onClick={() => clickBid(p)} title={`Buy @ ${fmt(p)}`}>
                      {bq != null && <span className="dom-bar b" style={{ width: `${(bq / maxQ) * 100}%` }} />}
                      <span>{bq != null ? fmtQty(bq) : ""}</span>
                    </td>
                    <td className={`px${below ? " lo" : " hi"}`}>{fmt(p)}</td>
                    <td className="ask" onClick={() => clickAsk(p)} title={`Sell @ ${fmt(p)}`}>
                      {aq != null && <span className="dom-bar a" style={{ width: `${(aq / maxQ) * 100}%` }} />}
                      <span>{aq != null ? fmtQty(aq) : ""}</span>
                    </td>
                    <td className="ord">{own.filter((o) => o.side === "SELL").map(chip)}</td>
                    {s.volume && (
                      <td className="vol">
                        {v != null && <span className="dom-bar v" style={{ width: `${(v / maxV) * 100}%` }} />}
                        <span>{v != null ? fmtQty(v) : ""}</span>
                      </td>
                    )}
                  </tr>
                );
              })}
              <tr className="tot">
                <td />
                <td title="Total resting buy quantity">{d.total_buy != null ? fmtQty(d.total_buy) : "—"}</td>
                <td>Total</td>
                <td title="Total resting sell quantity">{d.total_sell != null ? fmtQty(d.total_sell) : "—"}</td>
                <td />
                {s.volume && <td title="Traded since the panel opened">{fmtQty([...vap.values()].reduce((a, b) => a + b, 0))}</td>}
              </tr>
            </tbody>
          </table>
        </div>
      )}
      <div className="dom-actions">
        <button type="button" className="buy" onClick={() => send({ side: "BUY", type: "MARKET", price: null }, `Buy ${qty} ${symbol.short_name} at market`)} data-testid="dom-buy-mkt">Buy Mkt</button>
        <input
          className="qt-qty"
          type="number"
          min={lot}
          step={lot}
          value={qty}
          onChange={(e) => onQty(Math.max(lot, Math.floor(Number(e.target.value) || lot)))}
          aria-label="Quantity"
          data-testid="dom-qty"
        />
        <button type="button" className="sell" onClick={() => send({ side: "SELL", type: "MARKET", price: null }, `Sell ${qty} ${symbol.short_name} at market`)} data-testid="dom-sell-mkt">Sell Mkt</button>
      </div>
    </section>
  );
}
