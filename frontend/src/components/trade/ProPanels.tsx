// ProPanels — the desk tools TradingView / Fyers put next to the chart:
//   DepthPanel     DOM price ladder + 5-level market depth + book imbalance
//   TapePanel      Time & Sales built from the live tick stream
//   FuturesPanel   futures chain with basis and annualised carry
//   StrategyPanel  multi-leg payoff (max P/L, breakevens), the held position as
//                  a leg, net delta, and "Place legs" (bought legs first)
// All prices come from Fyers (depth / quotes / chain). Apart from the strategy
// builder's own confirmed "Place legs", every click prefills the ticket, which
// keeps its own confirm.

import { useEffect, useMemo, useRef, useState } from "react";
import type { InstrumentHit, OptionChainResponse, Position } from "../../types";
import { usePlaceOrder } from "../../hooks/useApi";
import { useLiveQuote } from "../../hooks/useQuotes";
import { useDepth } from "../../hooks/useDepth";

const fmt = (v: number | null | undefined, d = 2) => (v == null || !Number.isFinite(v) ? "—" : v.toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d }));
const fmtQty = (v: number) => (v >= 1e7 ? `${(v / 1e7).toFixed(2)}Cr` : v >= 1e5 ? `${(v / 1e5).toFixed(2)}L` : v >= 1e3 ? `${(v / 1e3).toFixed(1)}K` : String(Math.round(v)));

// ---------------------------------------------------------------------------
// Depth: DOM ladder + market depth
// ---------------------------------------------------------------------------

interface Depth {
  ok: boolean;
  reason?: string;
  bids?: [number, number, number][];
  asks?: [number, number, number][];
  total_buy?: number;
  total_sell?: number;
  ltp?: number | null;
}

/** Book imbalance in [-1, 1]: + = more resting bids than offers. */
export function imbalance(bid: number, ask: number): number {
  return bid + ask > 0 ? (bid - ask) / (bid + ask) : 0;
}

export function DepthPanel({ symbol, tick, onPrice, onMarket }: {
  symbol: string | null;
  tick: number;
  onPrice: (price: number, side: "BUY" | "SELL") => void;
  onMarket: (side: "BUY" | "SELL") => void;
}) {
  const d = useDepth(symbol);   // pushed book, shared with the DOM panel
  const [view, setView] = useState<"ladder" | "depth">("ladder");
  const live = useLiveQuote(symbol);


  if (!symbol) return <div className="hint">Open a symbol to see its book.</div>;
  if (!d) return <div className="hint">Loading depth…</div>;
  if (!d.ok) return <div className="hint">{d.reason ?? "Depth unavailable."}</div>;
  const bids = d?.bids ?? [];
  const asks = d?.asks ?? [];
  const tb = d?.total_buy ?? 0;
  const ts = d?.total_sell ?? 0;
  const top5b = bids.reduce((a, r) => a + r[1], 0);
  const top5a = asks.reduce((a, r) => a + r[1], 0);
  const imbTot = imbalance(tb, ts);
  const imb5 = imbalance(top5b, top5a);
  const ltp = live?.last_price ?? d?.ltp ?? null;
  const bestBid = bids[0]?.[0] ?? live?.bid ?? null;
  const bestAsk = asks[0]?.[0] ?? live?.ask ?? null;
  const t = tick > 0 ? tick : 0.05;
  const key = (p: number) => Math.round(p / t);
  const bidAt = new Map(bids.map((r) => [key(r[0]), r[1]]));
  const askAt = new Map(asks.map((r) => [key(r[0]), r[1]]));
  const maxQ = Math.max(1, ...bids.map((r) => r[1]), ...asks.map((r) => r[1]));
  // Ladder auto-centres on the mid every refresh — no recenter button needed.
  const mid = bestBid != null && bestAsk != null ? (bestBid + bestAsk) / 2 : ltp;
  const rows: number[] = [];
  if (mid != null) {
    const c = key(mid);
    for (let k = c + 10; k >= c - 10; k--) rows.push(k);
  }
  const bar = (q: number, cls: string) => <span className={`dom-bar ${cls}`} style={{ width: `${(q / maxQ) * 100}%` }} />;

  return (
    <div className="dom">
      <div className="dom-stats">
        <span>Spread <b>{bestBid != null && bestAsk != null ? fmt(bestAsk - bestBid) : "—"}</b></span>
        <span title="(bid − ask) / (bid + ask) of the top 5 levels">Top-5 imb <b className={imb5 >= 0 ? "up" : "down"}>{(imb5 * 100).toFixed(0)}%</b></span>
        <span title="Total resting buy vs sell quantity across the whole book">Book <b className={imbTot >= 0 ? "up" : "down"}>{(imbTot * 100).toFixed(0)}%</b></span>
      </div>
      <div className="dom-imb" title={`buy ${fmtQty(tb)} · sell ${fmtQty(ts)}`}>
        <span className="b" style={{ width: `${tb + ts > 0 ? (tb / (tb + ts)) * 100 : 50}%` }} />
      </div>
      <div className="seg">
        <button type="button" className={view === "ladder" ? "on" : ""} onClick={() => setView("ladder")}>DOM ladder</button>
        <button type="button" className={view === "depth" ? "on" : ""} onClick={() => setView("depth")}>Market depth</button>
      </div>
      {view === "ladder" ? (
        <table className="dom-table" data-testid="dom-ladder">
          <thead><tr><th>Bid qty</th><th>Price</th><th>Ask qty</th></tr></thead>
          <tbody>
            {rows.map((k) => {
              const p = k * t;
              const bq = bidAt.get(k);
              const aq = askAt.get(k);
              const isLtp = ltp != null && key(ltp) === k;
              const below = mid != null && p < mid;
              return (
                <tr key={k} className={isLtp ? "ltp" : ""}>
                  <td className="bid" onClick={() => onPrice(p, "BUY")} title={`Buy limit @ ${fmt(p)}`}>{bq != null && bar(bq, "b")}<span>{bq != null ? fmtQty(bq) : ""}</span></td>
                  <td className={`px${below ? " lo" : " hi"}`}>{fmt(p)}</td>
                  <td className="ask" onClick={() => onPrice(p, "SELL")} title={`Sell limit @ ${fmt(p)}`}>{aq != null && bar(aq, "a")}<span>{aq != null ? fmtQty(aq) : ""}</span></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : (
        <table className="dom-table depth">
          <thead><tr><th>Orders</th><th>Bid qty</th><th>Bid</th><th>Ask</th><th>Ask qty</th><th>Orders</th></tr></thead>
          <tbody>
            {Array.from({ length: 5 }, (_, i) => (
              <tr key={i}>
                <td>{bids[i] ? bids[i][2] : ""}</td>
                <td className="bid" onClick={() => bids[i] && onPrice(bids[i][0], "BUY")}>{bids[i] && bar(bids[i][1], "b")}<span>{bids[i] ? fmtQty(bids[i][1]) : ""}</span></td>
                <td className="up">{bids[i] ? fmt(bids[i][0]) : ""}</td>
                <td className="down">{asks[i] ? fmt(asks[i][0]) : ""}</td>
                <td className="ask" onClick={() => asks[i] && onPrice(asks[i][0], "SELL")}>{asks[i] && bar(asks[i][1], "a")}<span>{asks[i] ? fmtQty(asks[i][1]) : ""}</span></td>
                <td>{asks[i] ? asks[i][2] : ""}</td>
              </tr>
            ))}
            <tr className="tot"><td /><td>{fmtQty(tb)}</td><td colSpan={2}>Total</td><td>{fmtQty(ts)}</td><td /></tr>
          </tbody>
        </table>
      )}
      <div className="dom-actions">
        <button type="button" className="buy" onClick={() => onMarket("BUY")}>Buy Mkt</button>
        <button type="button" className="sell" onClick={() => onMarket("SELL")}>Sell Mkt</button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Time & Sales (tape)
// ---------------------------------------------------------------------------

interface Print { t: number; price: number; qty: number; side: "B" | "S" | "" }

/** Side by quote rule: at/above the ask = buyer lifted, at/below the bid = seller hit. */
export function tapeSide(price: number, bid: number | null, ask: number | null): "B" | "S" | "" {
  if (ask != null && price >= ask) return "B";
  if (bid != null && price <= bid) return "S";
  return "";
}

export function TapePanel({ symbol }: { symbol: string | null }) {
  const q = useLiveQuote(symbol);
  const [prints, setPrints] = useState<Print[]>([]);
  const lastVol = useRef<number | null>(null);
  useEffect(() => {
    setPrints([]);
    lastVol.current = null;
  }, [symbol]);
  useEffect(() => {
    if (!q || q.last_price == null || q.volume == null) return;
    const prev = lastVol.current;
    lastVol.current = q.volume;
    if (prev == null || q.volume <= prev) return;
    // ponytail: ticks are coalesced per frame, so one row can be several trades; real prints need the Fyers trade feed
    const p: Print = { t: Date.now(), price: q.last_price, qty: q.volume - prev, side: tapeSide(q.last_price, q.bid, q.ask) };
    setPrints((xs) => [p, ...xs].slice(0, 300));
  }, [q]);
  const { buy, sell, big } = useMemo(() => {
    let b = 0;
    let s = 0;
    for (const p of prints) {
      if (p.side === "B") b += p.qty;
      else if (p.side === "S") s += p.qty;
    }
    const sorted = prints.map((p) => p.qty).sort((a, c) => a - c);
    const med = sorted.length ? sorted[Math.floor(sorted.length / 2)] : 0;
    return { buy: b, sell: s, big: med * 5 };
  }, [prints]);
  if (!symbol) return <div className="hint">Open a symbol to watch its tape.</div>;
  return (
    <div className="tape">
      <div className="dom-stats">
        <span>Buy <b className="up">{fmtQty(buy)}</b></span>
        <span>Sell <b className="down">{fmtQty(sell)}</b></span>
        <span title="Buy − sell volume since the panel opened">Δ <b className={buy - sell >= 0 ? "up" : "down"}>{fmtQty(Math.abs(buy - sell))}</b></span>
      </div>
      {prints.length === 0 ? (
        <div className="hint">Waiting for trades — needs the live Fyers feed during market hours.</div>
      ) : (
        <table className="dom-table tape-table" data-testid="tape">
          <thead><tr><th>Time</th><th>Price</th><th>Qty</th></tr></thead>
          <tbody>
            {prints.map((p, i) => (
              <tr key={`${p.t}-${i}`} className={`${p.side === "B" ? "up" : p.side === "S" ? "down" : ""}${big > 0 && p.qty >= big ? " big" : ""}`}>
                <td>{new Date(p.t).toLocaleTimeString("en-IN", { hour12: false })}</td>
                <td>{fmt(p.price)}</td>
                <td>{fmtQty(p.qty)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Futures chain
// ---------------------------------------------------------------------------

interface FutRow { symbol: string; expiry: number }

/** Annualised cost of carry (%) of a future over spot. */
export function carryPct(fut: number, spot: number, expiryTs: number, now = Date.now() / 1000): number | null {
  const days = (expiryTs - now) / 86400;
  if (!(spot > 0) || days <= 0) return null;
  return ((fut - spot) / spot) * (365 / days) * 100;
}

export function FuturesPanel({ base, onOpen }: { base: InstrumentHit | null; onOpen: (h: InstrumentHit) => void }) {
  const [info, setInfo] = useState<{ name: string; lot: number | null; futures: FutRow[] } | null>(null);
  const [quotes, setQuotes] = useState<Record<string, { ltp: number; change: number | null; change_pct: number | null }>>({});
  const spot = useLiveQuote(base?.symbol)?.last_price ?? null;
  const [spotRest, setSpotRest] = useState<number | null>(null);
  useEffect(() => {
    setInfo(null);
    if (!base) return;
    void fetch(`/api/algo/instrument?symbol=${encodeURIComponent(base.symbol)}`)
      .then((r) => r.json())
      .then((j) => setInfo({ name: j.name, lot: j.lot, futures: Array.isArray(j.futures) ? j.futures : [] }))
      .catch(() => setInfo({ name: base.short_name, lot: null, futures: [] }));
  }, [base?.symbol]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!info?.futures.length || !base) return;
    const syms = [base.symbol, ...info.futures.map((f) => f.symbol)].join(",");
    const load = () =>
      void fetch(`/api/market/quotes?symbols=${encodeURIComponent(syms)}`)
        .then((r) => r.json())
        .then((j) => {
          const qs = j?.quotes ?? {};
          setQuotes(qs);
          setSpotRest(qs[base.symbol.toUpperCase()]?.ltp ?? null);
        })
        .catch(() => undefined);
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, [info, base]);
  if (!base) return <div className="hint">Open an index or F&O stock.</div>;
  if (!info) return <div className="hint">Loading…</div>;
  if (!info.futures.length) return <div className="hint">No listed futures for {base.short_name}.</div>;
  const s = spot ?? spotRest;
  return (
    <table className="dom-table fut-table" data-testid="futures-chain">
      <thead><tr><th>Contract</th><th>LTP</th><th>Chg%</th><th>Basis</th><th title="annualised cost of carry">Carry</th></tr></thead>
      <tbody>
        {info.futures.map((f) => {
          const q = quotes[f.symbol.toUpperCase()];
          const basis = q && s ? q.ltp - s : null;
          const carry = q && s ? carryPct(q.ltp, s, f.expiry) : null;
          const exp = new Date(f.expiry * 1000);
          return (
            <tr key={f.symbol} className="click" onClick={() => onOpen({
              symbol: f.symbol,
              short_name: info.name,
              exchange: f.symbol.split(":")[0],
              segment: "FO",
              instrument_type: "FUT",
              lot_size: info.lot ?? 0,   // 0 = unknown: the ticket blocks it
              tick_size: 0.05,
              expiry: exp.toISOString().slice(0, 10),
              strike: null,
              underlying: info.name,
              display: `${info.name} ${exp.toLocaleDateString("en-IN", { day: "2-digit", month: "short" })} FUT`,
            })}>
              <td>{exp.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "2-digit" })}</td>
              <td>{fmt(q?.ltp)}</td>
              <td className={(q?.change_pct ?? 0) >= 0 ? "up" : "down"}>{q?.change_pct != null ? `${q.change_pct.toFixed(2)}%` : "—"}</td>
              <td className={(basis ?? 0) >= 0 ? "up" : "down"}>{fmt(basis)}</td>
              <td>{carry != null ? `${carry.toFixed(1)}%` : "—"}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

// ---------------------------------------------------------------------------
// Strategy builder (options payoff)
// ---------------------------------------------------------------------------

/** FUT = any linear leg (future or the cash stock): `premium` is its entry price.
 *  `held` legs are open positions (never re-placed); `mark` is the live LTP. */
export interface Leg { type: "CE" | "PE" | "FUT"; strike: number; side: 1 | -1; lots: number; premium: number; lot: number; symbol?: string; held?: boolean; mark?: number | null }

/** P&L at expiry for an underlying price. */
export function payoffAt(legs: Leg[], x: number): number {
  return legs.reduce((acc, l) => {
    if (l.type === "FUT") return acc + l.side * (x - l.premium) * l.lots * l.lot;
    const intrinsic = l.type === "CE" ? Math.max(0, x - l.strike) : Math.max(0, l.strike - x);
    return acc + l.side * (intrinsic - l.premium) * l.lots * l.lot;
  }, 0);
}

/** Breakevens, max profit / loss (null = unlimited) over [lo, hi]. */
export function payoffStats(legs: Leg[], lo: number, hi: number, n = 400) {
  const xs = Array.from({ length: n + 1 }, (_, i) => lo + ((hi - lo) * i) / n);
  const ys = xs.map((x) => payoffAt(legs, x));
  const be: number[] = [];
  for (let i = 1; i < xs.length; i++) {
    if ((ys[i - 1] < 0 && ys[i] >= 0) || (ys[i - 1] >= 0 && ys[i] < 0)) {
      be.push(xs[i - 1] + ((xs[i] - xs[i - 1]) * (0 - ys[i - 1])) / (ys[i] - ys[i - 1]));
    }
  }
  // Net call exposure above the top strike makes a side unlimited; the put
  // side is bounded by the underlying at 0, so it is evaluated there.
  const upSlope = legs.reduce((a, l) => a + (l.type !== "PE" ? l.side * l.lots * l.lot : 0), 0);
  const atZero = payoffAt(legs, 0);
  const maxP = upSlope > 0 ? null : Math.max(...ys, atZero);
  const maxL = upSlope < 0 ? null : Math.min(...ys, atZero);
  return { xs, ys, be, maxP, maxL, net: legs.reduce((a, l) => a - (l.type === "FUT" ? 0 : l.side * l.premium * l.lots * l.lot), 0) };
}

const RISK_FREE = 0.065;   // same as app/algo/fno.py
/** Standard normal CDF (Abramowitz–Stegun 7.1.26, |error| < 1.5e-7). */
function ncdf(x: number): number {
  const t = 1 / (1 + (0.3275911 * Math.abs(x)) / Math.SQRT2);
  const y = 1 - ((((1.061405429 * t - 1.453152027) * t + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp((-x * x) / 2);
  return x >= 0 ? (1 + y) / 2 : (1 - y) / 2;
}
function d1(s: number, k: number, t: number, iv: number) { return (Math.log(s / k) + (RISK_FREE + (iv * iv) / 2) * t) / (iv * Math.sqrt(t)); }
function bs(s: number, k: number, t: number, iv: number, type: "CE" | "PE") {
  const a = d1(s, k, t, iv), b = a - iv * Math.sqrt(t), df = Math.exp(-RISK_FREE * t);
  return type === "CE" ? s * ncdf(a) - k * df * ncdf(b) : k * df * ncdf(-b) - s * ncdf(-a);
}
/** Black-Scholes delta of one option at the IV its price implies (bisection). */
export function optionDelta(s: number, k: number, t: number, price: number, type: "CE" | "PE"): number {
  let lo = 0.01, hi = 3;
  for (let i = 0; i < 50; i++) { const m = (lo + hi) / 2; if (bs(s, k, t, m, type) > price) hi = m; else lo = m; }
  const n = ncdf(d1(s, k, t, (lo + hi) / 2));
  return type === "CE" ? n : n - 1;
}
/** Net delta in underlying units: the position's ₹ P&L per 1-point move of the underlying. */
export function netDelta(legs: Leg[], spot: number, tYears: number): number {
  return legs.reduce((a, l) => a + l.side * l.lots * l.lot * (l.type === "FUT" ? 1 : optionDelta(spot, l.strike, tYears, l.mark ?? l.premium, l.type)), 0);
}

export function StrategyPanel({ chain, base, positions, accountId }: { chain: OptionChainResponse | undefined; base?: InstrumentHit | null; positions?: Position[]; accountId?: number | null }) {
  const [legs, setLegs] = useState<Leg[]>([]);
  const [fut, setFut] = useState<{ lot: number | null; futures: FutRow[] }>({ lot: null, futures: [] });
  useEffect(() => {
    if (!base) return;
    void fetch(`/api/algo/instrument?symbol=${encodeURIComponent(base.symbol)}`).then((r) => r.json())
      .then((j) => setFut({ lot: j.lot ?? null, futures: Array.isArray(j.futures) ? j.futures : [] }))
      .catch(() => setFut({ lot: null, futures: [] }));
  }, [base?.symbol]); // eslint-disable-line react-hooks/exhaustive-deps
  const place = usePlaceOrder();
  const [msg, setMsg] = useState<string | null>(null);
  const strikes = chain?.strikes ?? [];
  const spot = chain?.spot ?? null;
  const atmIdx = useMemo(() => {
    if (!strikes.length || spot == null) return Math.floor(strikes.length / 2);
    let best = 0;
    strikes.forEach((s, i) => { if (Math.abs(s.strike - spot) < Math.abs(strikes[best].strike - spot)) best = i; });
    return best;
  }, [strikes, spot]);
  const [pick, setPick] = useState({ type: "CE" as "CE" | "PE", side: 1 as 1 | -1, idx: -1, lots: 1 });
  const legAt = (i: number, type: "CE" | "PE", side: 1 | -1, lots = 1): Leg | null => {
    const s = strikes[Math.max(0, Math.min(strikes.length - 1, i))];
    const o = s?.[type === "CE" ? "ce" : "pe"];
    if (!s || !o) return null;
    return { type, strike: s.strike, side, lots, premium: o.ltp ?? 0, lot: o.lot_size || 1, symbol: o.symbol, mark: o.ltp };
  };
  // Open positions on this underlying (its options, futures, or the stock) as held legs.
  const bare = (x: string) => x.toUpperCase().replace(/^NSE:|-EQ$/g, "");
  const held: Leg[] = (positions ?? []).filter((p) => p.quantity !== 0).flatMap((p): Leg[] => {
    const sym = p.symbol.toUpperCase();
    const side = (p.quantity > 0 ? 1 : -1) as 1 | -1;
    const q = Math.abs(p.quantity);
    for (const s of strikes) {
      for (const t of ["CE", "PE"] as const) {
        const o = s[t === "CE" ? "ce" : "pe"];
        if (o?.symbol.toUpperCase() === sym) return [{ type: t, strike: s.strike, side, lots: q, lot: 1, premium: p.average_price, symbol: p.symbol, held: true, mark: o.ltp ?? p.last_price }];
      }
    }
    const linear = (base != null && bare(sym) === bare(base.symbol)) || fut.futures.some((f) => f.symbol.toUpperCase() === sym);
    return linear ? [{ type: "FUT", strike: p.average_price, side, lots: q, lot: 1, premium: p.average_price, symbol: p.symbol, held: true }] : [];
  });
  const expTs = Number(chain?.selected_expiry ?? chain?.expiries[0]?.ts);
  // ponytail: years to the listed expiry timestamp, no session-close / holiday adjustment.
  const tYears = Number.isFinite(expTs) ? Math.max((expTs - Date.now() / 1000) / (365 * 86400), 1 / (365 * 24)) : null;
  const placeLegs = async () => {
    const todo = legs.filter((l) => !l.held).sort((a, b) => b.side - a.side);   // bought legs first (margin)
    if (!todo.length || accountId == null) return;
    const chainLot = (sym?: string) => strikes.flatMap((s) => [s.ce, s.pe]).find((o) => o?.symbol === sym)?.lot_size ?? null;
    if (todo.some((l) => !l.symbol || !chainLot(l.symbol))) { setMsg("Lot size unknown for a leg (F&O master not loaded). Nothing placed."); return; }
    const lines = todo.map((l) => `${l.side > 0 ? "BUY" : "SELL"} ${l.lots * l.lot} ${l.symbol}`);
    if (!window.confirm(`${accountId === -1 ? "PAPER" : "REAL MONEY (Fyers)"}: place ${todo.length} intraday market orders, bought legs first?\n\n${lines.join("\n")}\n\nIf a leg fails, the rest are NOT sent and legs already sent stay open.`)) return;
    const done: string[] = [];
    for (const [i, l] of todo.entries()) {
      try {
        const r = await place.mutateAsync({ account_id: accountId, symbol: l.symbol!, side: l.side > 0 ? "BUY" : "SELL", quantity: l.lots * l.lot,
          order_type: "MARKET", product_type: "INTRADAY", operator: "ui_strategy_builder" });
        if (r.ok === false || r.blocked || r.status === "REJECTED" || r.status === "REJECTED_RISK") throw new Error(r.error || r.risk_message || r.status);
        done.push(`${lines[i]} (${r.status})`);
      } catch (e) {
        setMsg(`Stopped at ${lines[i]}: ${(e as Error).message}. ${done.length ? `Already sent: ${done.join("; ")}. Close them from Positions if the structure is now one-sided.` : "Nothing was sent."}`);
        return;
      }
    }
    setMsg(`Sent: ${done.join("; ")}`);
  };
  const preset = (name: string) => {
    const a = atmIdx;
    const L: Record<string, [number, "CE" | "PE", 1 | -1][]> = {
      "Long straddle": [[a, "CE", 1], [a, "PE", 1]],
      "Short straddle": [[a, "CE", -1], [a, "PE", -1]],
      "Long strangle": [[a + 2, "CE", 1], [a - 2, "PE", 1]],
      "Bull call spread": [[a, "CE", 1], [a + 2, "CE", -1]],
      "Bear put spread": [[a, "PE", 1], [a - 2, "PE", -1]],
      "Iron condor": [[a + 2, "CE", -1], [a + 4, "CE", 1], [a - 2, "PE", -1], [a - 4, "PE", 1]],
    };
    setLegs(L[name].map(([i, t, s]) => legAt(i, t, s)).filter((x): x is Leg => x != null));
  };
  if (!strikes.length) return <div className="hint">Open an index or F&O stock with an option chain (the chain loads live prices from Fyers).</div>;
  const lo = (spot ?? strikes[atmIdx].strike) * 0.9;
  const hi = (spot ?? strikes[atmIdx].strike) * 1.1;
  const st = legs.length ? payoffStats(legs, lo, hi) : null;
  const W = 300;
  const H = 120;
  let path = "";
  let zeroY = H / 2;
  if (st) {
    const yMax = Math.max(1, ...st.ys.map(Math.abs));
    const y = (v: number) => H / 2 - (v / yMax) * (H / 2 - 4);
    zeroY = y(0);
    path = st.xs.map((x, i) => `${i ? "L" : "M"}${(((x - lo) / (hi - lo)) * W).toFixed(1)},${y(st.ys[i]).toFixed(1)}`).join(" ");
  }
  const money = (v: number | null) => (v == null ? "Unlimited" : `₹${Math.round(v).toLocaleString("en-IN")}`);
  return (
    <div className="strat">
      <div className="strat-presets">
        {["Long straddle", "Short straddle", "Long strangle", "Bull call spread", "Bear put spread", "Iron condor"].map((p) => (
          <button key={p} type="button" className="btn-sm" onClick={() => preset(p)}>{p}</button>
        ))}
      </div>
      {held.length > 0 && (
        <div className="strat-presets">
          <button type="button" className="btn-sm" title="Add your open position(s) on this underlying as legs, then add the hedge"
            onClick={() => setLegs([...legs.filter((l) => !l.held), ...held])}>+ My position ({held.length})</button>
        </div>
      )}
      <div className="strat-add">
        <select value={pick.side} onChange={(e) => setPick({ ...pick, side: Number(e.target.value) as 1 | -1 })} aria-label="Side"><option value={1}>Buy</option><option value={-1}>Sell</option></select>
        <select value={pick.idx < 0 ? atmIdx : pick.idx} onChange={(e) => setPick({ ...pick, idx: Number(e.target.value) })} aria-label="Strike">
          {strikes.map((s, i) => <option key={s.strike} value={i}>{s.strike}{i === atmIdx ? " (ATM)" : ""}</option>)}
        </select>
        <select value={pick.type} onChange={(e) => setPick({ ...pick, type: e.target.value as "CE" | "PE" })} aria-label="Type"><option>CE</option><option>PE</option></select>
        <input type="number" min={1} value={pick.lots} onChange={(e) => setPick({ ...pick, lots: Math.max(1, Number(e.target.value) || 1) })} aria-label="Lots" />
        <button type="button" className="btn-sm" onClick={() => { const l = legAt(pick.idx < 0 ? atmIdx : pick.idx, pick.type, pick.side, pick.lots); if (l) setLegs([...legs, l]); }}>+ Leg</button>
      </div>
      {legs.length > 0 && (
        <table className="dom-table strat-legs">
          <thead><tr><th>Side</th><th>Strike</th><th>Type</th><th>Qty</th><th>Prem</th><th /></tr></thead>
          <tbody>
            {legs.map((l, i) => (
              <tr key={i}>
                <td className={l.side > 0 ? "up" : "down"}>{l.side > 0 ? "B" : "S"}{l.held ? " · held" : ""}</td>
                <td>{l.type === "FUT" ? "—" : l.strike}</td>
                <td title={l.symbol}>{l.type === "FUT" ? (l.symbol?.toUpperCase().endsWith("FUT") ? "FUT" : "Stock") : l.type}</td>
                <td>{l.lots * l.lot}</td>
                <td>{fmt(l.premium)}</td>
                <td><button type="button" className="chart-menu-x" onClick={() => setLegs(legs.filter((_, j) => j !== i))} aria-label="Remove leg">✕</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {st && (
        <>
          <svg viewBox={`0 0 ${W} ${H}`} className="strat-chart" role="img" aria-label="Payoff at expiry">
            <line x1={0} x2={W} y1={zeroY} y2={zeroY} className="zero" />
            {spot != null && <line x1={((spot - lo) / (hi - lo)) * W} x2={((spot - lo) / (hi - lo)) * W} y1={0} y2={H} className="spot" />}
            <path d={path} className="pnl" />
          </svg>
          <div className="dom-stats">
            <span>Max profit <b className="up">{money(st.maxP)}</b></span>
            <span>Max loss <b className="down">{money(st.maxL)}</b></span>
          </div>
          <div className="dom-stats">
            <span>{st.net >= 0 ? "Net credit" : "Net debit"} <b>₹{Math.abs(Math.round(st.net)).toLocaleString("en-IN")}</b></span>
            <span>Breakeven <b>{st.be.length ? st.be.map((b) => b.toFixed(0)).join(" / ") : "—"}</b></span>
          </div>
          {spot != null && tYears != null && (() => {
            const d = netDelta(legs, spot, tYears);
            const lot = fut.lot ?? strikes[atmIdx]?.ce?.lot_size ?? null;
            const n = lot ? Math.round(-d / lot) : 0;
            return (
              <div className="dom-stats" title="Black-Scholes delta at the IV each option's price implies: ₹ gained per 1-point rise of the underlying">
                <span>Net delta <b>{Math.round(d).toLocaleString("en-IN")}</b></span>
                <span>Neutralise <b>{!lot ? "—" : n === 0 ? `under ½ FUT lot (${lot})` : `${n > 0 ? "buy" : "sell"} ${Math.abs(n)} FUT lot${Math.abs(n) > 1 ? "s" : ""}`}</b></span>
              </div>
            );
          })()}
          <div className="hint">Payoff at expiry, spot ±10%. New legs at current LTPs, held legs at their average price. Costs and slippage not included.</div>
          {accountId != null && legs.some((l) => !l.held) && (
            <button type="button" className="btn-sm" disabled={place.isPending} onClick={() => void placeLegs()}>
              {place.isPending ? "placing…" : `Place ${legs.filter((l) => !l.held).length} new leg(s) · ${accountId === -1 ? "paper" : "Fyers"}`}
            </button>
          )}
          {msg && <div className="hint">{msg}</div>}
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Risk desk: size a trade from a rupee risk and an ATR stop, and see what the market can move today.
// ---------------------------------------------------------------------------

/** Position size from a rupee risk and an ATR-based stop, the R-multiple targets, and the 1σ move left in the
 *  session from realised volatility (random walk: σ_daily × √(minutes left / 375)). Pure, for the tests. */
export function riskPlan(p: { ltp: number; atr: number; vol: number; risk: number; atrMult: number; lot: number; tick: number; long: boolean; minsLeft: number }) {
  const tick = p.tick > 0 ? p.tick : 0.05;
  const snap = (x: number) => Math.round(x / tick) * tick;
  const stopDist = Math.max(tick, snap(p.atr * p.atrMult));
  const step = Math.max(1, p.lot);
  const qty = Math.max(0, Math.floor(p.risk / stopDist / step) * step);
  const sgn = p.long ? 1 : -1;
  const move = p.ltp * p.vol * Math.sqrt(Math.max(0, p.minsLeft) / (252 * 375));
  return {
    stopDist, qty, stop: snap(p.ltp - sgn * stopDist), targets: [1, 2, 3].map((r) => snap(p.ltp + sgn * r * stopDist)),
    notional: qty * p.ltp, maxLoss: qty * stopDist, move, moveInStops: move / stopDist,
  };
}

/** ATR(14) and annualised 20-day realised volatility from daily candles [t, o, h, l, c, v]. */
export function dailyRisk(candles: number[][]): { atr: number; vol: number } | null {
  const k = candles.filter((r) => r.length >= 5 && r[4] > 0);
  if (k.length < 21) return null;
  const tr = k.slice(1).map((r, i) => Math.max(r[2] - r[3], Math.abs(r[2] - k[i][4]), Math.abs(r[3] - k[i][4])));
  const atr = tr.slice(-14).reduce((a, b) => a + b, 0) / 14;
  const rets = k.slice(-21).slice(1).map((r, i) => Math.log(r[4] / k.slice(-21)[i][4]));
  const m = rets.reduce((a, b) => a + b, 0) / rets.length;
  const vol = Math.sqrt(rets.reduce((a, b) => a + (b - m) ** 2, 0) / (rets.length - 1)) * Math.sqrt(252);
  return { atr, vol };
}

const minsLeftIST = (now = Date.now()) => {
  const m = Math.floor(now / 60000 + 330) % 1440;            // IST minute of the day
  return m >= 555 && m < 930 ? 930 - m : 375;                 // in session: to 15:30; otherwise a whole session
};

export function RiskPanel({ symbol, tick, lot }: { symbol: string | null; tick: number; lot: number }) {
  const live = useLiveQuote(symbol);
  const [d, setD] = useState<{ atr: number; vol: number } | null | string>(null);
  const [risk, setRisk] = useState(() => { try { return Number(localStorage.getItem("risk:inr")) || 1000; } catch { return 1000; } });
  const [mult, setMult] = useState(0.5);
  const [long, setLong] = useState(true);
  useEffect(() => {
    setD(null);
    if (!symbol) return;
    let stop = false;
    const now = Math.floor(Date.now() / 1000);
    void fetch(`/api/market/history?symbol=${encodeURIComponent(symbol)}&resolution=D&from=${now - 90 * 86400}&to=${now}`)
      .then((r) => r.json())
      .then((j: { ok?: boolean; candles?: number[][]; reason?: string }) => { if (!stop) setD(j.ok ? dailyRisk(j.candles ?? []) ?? "under 21 daily candles" : j.reason ?? "no daily history"); })
      .catch((e) => !stop && setD(String(e)));
    return () => { stop = true; };
  }, [symbol]);
  if (!symbol) return <div className="hint">Open a symbol to size a trade.</div>;
  if (d === null) return <div className="hint">Loading daily candles…</div>;
  if (typeof d === "string") return <div className="hint">{d}</div>;
  const ltp = live?.last_price;
  if (!ltp) return <div className="hint">Waiting for a live price…</div>;
  const p = riskPlan({ ltp, atr: d.atr, vol: d.vol, risk, atrMult: mult, lot, tick, long, minsLeft: minsLeftIST() });
  const f = (x: number) => x.toFixed(tick < 1 ? 2 : 0);
  const inr = (x: number) => `₹${Math.round(x).toLocaleString("en-IN")}`;
  return (
    <div className="risk-desk" data-testid="risk-desk">
      <div className="dom-stats">
        <span title="Average true range of the last 14 days">ATR 14d <b>{f(d.atr)}</b></span>
        <span title="Annualised standard deviation of the last 20 daily log returns">Vol 20d <b>{(d.vol * 100).toFixed(1)}%</b></span>
        <span title="1σ move left in this session (or a whole session outside market hours), from the realised volatility">1σ left <b>±{f(p.move)}</b> ({p.moveInStops.toFixed(1)} stops)</span>
      </div>
      <div className="risk-inputs">
        <label>Risk ₹ <input type="number" min={1} step={100} value={risk} aria-label="Risk in rupees"
          onChange={(e) => { const v = Math.max(1, Number(e.target.value) || 0); setRisk(v); try { localStorage.setItem("risk:inr", String(v)); } catch { /* storage off */ } }} /></label>
        <label>Stop <select value={mult} onChange={(e) => setMult(Number(e.target.value))} aria-label="Stop as a multiple of ATR">
          {[0.25, 0.5, 0.75, 1, 1.5, 2].map((m) => <option key={m} value={m}>{m} × ATR</option>)}</select></label>
        <span className="seg">
          <button type="button" className={long ? "on" : ""} onClick={() => setLong(true)}>Long</button>
          <button type="button" className={!long ? "on" : ""} onClick={() => setLong(false)}>Short</button>
        </span>
      </div>
      <table className="risk-table"><tbody>
        <tr><td>Quantity</td><td className="mono"><b>{p.qty}</b>{lot > 1 ? ` (${p.qty / lot} lot${p.qty / lot === 1 ? "" : "s"})` : ""}</td></tr>
        <tr><td>Stop</td><td className="mono down">{f(p.stop)} <span className="text-dim">({f(p.stopDist)} away)</span></td></tr>
        {p.targets.map((t, i) => <tr key={i}><td>Target {i + 1}R</td><td className="mono up">{f(t)}</td></tr>)}
        <tr><td>Notional</td><td className="mono">{inr(p.notional)}</td></tr>
        <tr><td title="Loss if the stop fills exactly (before charges and slippage)">Loss at stop</td><td className="mono">{inr(p.maxLoss)}</td></tr>
      </tbody></table>
      {p.qty === 0 && <div className="hint">₹{risk} doesn't cover one {lot > 1 ? "lot" : "share"} at this stop — risk more or use a tighter stop.</div>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Dock wrappers: two rail buttons, two tabs each (keeps the rail short).
// ---------------------------------------------------------------------------

export type DeskTab = "depth" | "tape" | "risk" | "futures" | "strategy";

function Tabs({ tabs, tab, onTab }: { tabs: [DeskTab, string][]; tab: DeskTab; onTab: (t: DeskTab) => void }) {
  return (
    <div className="seg">
      {tabs.map(([k, l]) => (
        <button key={k} type="button" className={tab === k ? "on" : ""} onClick={() => onTab(k)} data-testid={`desk-${k}`}>{l}</button>
      ))}
    </div>
  );
}

export function BookPanel(p: { symbol: string | null; tick: number; lot?: number; tab: DeskTab; onTab: (t: DeskTab) => void; onPrice: (price: number, side: "BUY" | "SELL") => void; onMarket: (side: "BUY" | "SELL") => void }) {
  const tab = p.tab === "tape" || p.tab === "risk" ? p.tab : "depth";
  return (
    <>
      <Tabs tabs={[["depth", "DOM / Depth"], ["tape", "Time & Sales"], ["risk", "Risk"]]} tab={tab} onTab={p.onTab} />
      {tab === "depth" ? <DepthPanel symbol={p.symbol} tick={p.tick} onPrice={p.onPrice} onMarket={p.onMarket} />
        : tab === "tape" ? <TapePanel symbol={p.symbol} /> : <RiskPanel symbol={p.symbol} tick={p.tick} lot={p.lot || 1} />}
    </>
  );
}

export function FnoPanel(p: { base: InstrumentHit | null; chain: OptionChainResponse | undefined; tab: DeskTab; onTab: (t: DeskTab) => void; onOpen: (h: InstrumentHit) => void; positions?: Position[]; accountId?: number | null }) {
  const tab = p.tab === "strategy" ? "strategy" : "futures";
  return (
    <>
      <Tabs tabs={[["futures", "Futures chain"], ["strategy", "Strategy builder"]]} tab={tab} onTab={p.onTab} />
      {tab === "futures" ? <FuturesPanel base={p.base} onOpen={p.onOpen} /> : <StrategyPanel chain={p.chain} base={p.base} positions={p.positions} accountId={p.accountId} />}
    </>
  );
}
