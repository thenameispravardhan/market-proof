// ProPanels — the desk tools TradingView / Fyers put next to the chart:
//   DepthPanel     DOM price ladder + 5-level market depth + book imbalance
//   TapePanel      Time & Sales built from the live tick stream
//   FuturesPanel   futures chain with basis and annualised carry
//   StrategyPanel  multi-leg options payoff (max P/L, breakevens)
// All prices come from Fyers (depth / quotes / chain). Nothing here places an
// order — every click prefills the ticket, which keeps its own confirm.

import { useEffect, useMemo, useRef, useState } from "react";
import type { InstrumentHit, OptionChainResponse } from "../../types";
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
        <span className="hint">click a qty cell → limit ticket</span>
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

export interface Leg { type: "CE" | "PE"; strike: number; side: 1 | -1; lots: number; premium: number; lot: number }

/** P&L at expiry for an underlying price. */
export function payoffAt(legs: Leg[], x: number): number {
  return legs.reduce((acc, l) => {
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
  const upSlope = legs.reduce((a, l) => a + (l.type === "CE" ? l.side * l.lots * l.lot : 0), 0);
  const atZero = payoffAt(legs, 0);
  const maxP = upSlope > 0 ? null : Math.max(...ys, atZero);
  const maxL = upSlope < 0 ? null : Math.min(...ys, atZero);
  return { xs, ys, be, maxP, maxL, net: legs.reduce((a, l) => a - l.side * l.premium * l.lots * l.lot, 0) };
}

export function StrategyPanel({ chain }: { chain: OptionChainResponse | undefined }) {
  const [legs, setLegs] = useState<Leg[]>([]);
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
    return { type, strike: s.strike, side, lots, premium: o.ltp ?? 0, lot: o.lot_size || 1 };
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
          <thead><tr><th>Side</th><th>Strike</th><th>Type</th><th>Lots</th><th>Prem</th><th /></tr></thead>
          <tbody>
            {legs.map((l, i) => (
              <tr key={i}>
                <td className={l.side > 0 ? "up" : "down"}>{l.side > 0 ? "B" : "S"}</td>
                <td>{l.strike}</td>
                <td>{l.type}</td>
                <td>{l.lots}×{l.lot}</td>
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
          <div className="hint">Payoff at expiry, spot ±10%. Premiums are current LTPs; costs and slippage not included.</div>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Dock wrappers: two rail buttons, two tabs each (keeps the rail short).
// ---------------------------------------------------------------------------

export type DeskTab = "depth" | "tape" | "futures" | "strategy";

function Tabs({ tabs, tab, onTab }: { tabs: [DeskTab, string][]; tab: DeskTab; onTab: (t: DeskTab) => void }) {
  return (
    <div className="seg">
      {tabs.map(([k, l]) => (
        <button key={k} type="button" className={tab === k ? "on" : ""} onClick={() => onTab(k)} data-testid={`desk-${k}`}>{l}</button>
      ))}
    </div>
  );
}

export function BookPanel(p: { symbol: string | null; tick: number; tab: DeskTab; onTab: (t: DeskTab) => void; onPrice: (price: number, side: "BUY" | "SELL") => void; onMarket: (side: "BUY" | "SELL") => void }) {
  const tab = p.tab === "tape" ? "tape" : "depth";
  return (
    <>
      <Tabs tabs={[["depth", "DOM / Depth"], ["tape", "Time & Sales"]]} tab={tab} onTab={p.onTab} />
      {tab === "depth" ? <DepthPanel symbol={p.symbol} tick={p.tick} onPrice={p.onPrice} onMarket={p.onMarket} /> : <TapePanel symbol={p.symbol} />}
    </>
  );
}

export function FnoPanel(p: { base: InstrumentHit | null; chain: OptionChainResponse | undefined; tab: DeskTab; onTab: (t: DeskTab) => void; onOpen: (h: InstrumentHit) => void }) {
  const tab = p.tab === "strategy" ? "strategy" : "futures";
  return (
    <>
      <Tabs tabs={[["futures", "Futures chain"], ["strategy", "Strategy builder"]]} tab={tab} onTab={p.onTab} />
      {tab === "futures" ? <FuturesPanel base={p.base} onOpen={p.onOpen} /> : <StrategyPanel chain={p.chain} />}
    </>
  );
}
