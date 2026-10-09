// Option scalper (Fyers-style): the underlying's chart flanked by the chosen
// CE and PE charts, each leg with BUY / SELL at market for N lots. Strikes are
// picked as ATM ± steps and follow the ATM as the spot moves. Every order is
// a real intraday order: a confirm per click unless "1-click" is switched on
// for the session — only then do the hotkeys work too.
import { useEffect, useState, type ReactNode } from "react";
import ChartPanel, { type ChartOrder, type ChartPosition } from "./ChartPanel";
import { useQuote } from "../../hooks/useApi";
import { useLiveQuote } from "../../hooks/useQuotes";
import type { InstrumentHit, OptionChainResponse, OptionLeg, Position } from "../../types";
import { isOptionOf, optionRoot } from "../../lib/options";

type Helpers = {
  positionFor: (sym: string) => ChartPosition | null;
  levelsFor: (sym: string) => (sl: number | null, tp: number | null) => Promise<void>;
  closeFor: (sym: string) => () => Promise<void>;
  orderFor: (sym: string, name: string, qty: number) => (o: ChartOrder) => Promise<string>;
};

function stored<T>(key: string, fallback: T): T {
  try {
    const v = localStorage.getItem(key);
    return v == null ? fallback : (JSON.parse(v) as T);
  } catch {
    return fallback;
  }
}

// Live last price for a leg; polling the quote also keeps it on the socket.
function Ltp({ symbol, fallback }: { symbol: string; fallback: number | null }) {
  const { data } = useQuote(symbol);
  const live = useLiveQuote(symbol);
  const v = live?.last_price ?? (data?.ok ? data.last_price : null) ?? fallback;
  return <span className="scalp-ltp">{v != null ? v.toFixed(2) : "—"}</span>;
}

const STEPS = [-5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5];

// Drag a split line of a grid (layouts, scalper columns): `set` gets the
// pointer's position as a fraction of the grid's width / height.
export function splitDrag(e: React.PointerEvent, axis: "x" | "y", set: (f: number) => void, lo = 0.15, hi = 0.85): void {
  e.preventDefault();
  const r = (e.currentTarget as HTMLElement).parentElement!.getBoundingClientRect();
  const move = (ev: PointerEvent) => {
    const f = axis === "x" ? (ev.clientX - r.left) / r.width : (ev.clientY - r.top) / r.height;
    set(Math.max(lo, Math.min(hi, f)));
  };
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
}

export default function Scalper({
  base,
  underlyings,
  onBase,
  chain,
  expiry,
  onExpiry,
  atmStrike,
  positions,
  helpers,
  instant,
  onInstant,
  settings,
  showPlus,
}: {
  base: InstrumentHit | null;
  underlyings: InstrumentHit[];
  onBase: (h: InstrumentHit) => void;
  chain: OptionChainResponse | undefined;
  expiry: string | null;
  onExpiry: (ts: string | null) => void;
  atmStrike: number | null;
  positions: Position[] | undefined;
  helpers: Helpers;
  /** Trading setting: no confirm per order (also arms the hotkeys). */
  instant: boolean;
  onInstant: (v: boolean) => void;
  /** The settings menu, shown at the end of the shared top bar. */
  settings: ReactNode;
  showPlus: boolean;
}) {
  // Offsets from ATM: + is OTM (a higher CE strike, a lower PE strike).
  const [ceOff, setCeOff] = useState(() => stored("scalp:ceOff", 0));
  const [peOff, setPeOff] = useState(() => stored("scalp:peOff", 0));
  const [lots, setLots] = useState(() => Math.max(1, stored("scalp:lots", 1)));
  const oneClick = instant;
  // One shared toolbar + drawing strip, driven by the clicked (active) chart.
  const [top, setTop] = useState<HTMLDivElement | null>(null);
  const [left, setLeft] = useState<HTMLDivElement | null>(null);
  const [activeCol, setActiveCol] = useState(1);
  // Column split positions (fractions of the width), draggable.
  const [cols, setCols] = useState<[number, number]>(() => stored("scalp:cols", [1 / 3, 2 / 3]));
  useEffect(() => { try { localStorage.setItem("scalp:cols", JSON.stringify(cols)); } catch { /* best-effort */ } }, [cols]);
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => {
    try {
      localStorage.setItem("scalp:ceOff", JSON.stringify(ceOff));
      localStorage.setItem("scalp:peOff", JSON.stringify(peOff));
      localStorage.setItem("scalp:lots", JSON.stringify(lots));
    } catch { /* best-effort */ }
  }, [ceOff, peOff, lots]);

  const strikes = chain?.strikes ?? [];
  const atmIdx = strikes.findIndex((s) => s.strike === atmStrike);
  const at = (i: number) => strikes[Math.max(0, Math.min(strikes.length - 1, i))];
  const ceRow = atmIdx >= 0 ? at(atmIdx + ceOff) : undefined;
  const peRow = atmIdx >= 0 ? at(atmIdx - peOff) : undefined;
  const ce = ceRow?.ce ?? null;
  const pe = peRow?.pe ?? null;
  const name = base ? optionRoot(base) : "";
  const legName = (row: typeof ceRow, t: "CE" | "PE") => (row ? `${name} ${row.strike} ${t}` : t);

  const trade = async (leg: OptionLeg | null, label: string, side: "BUY" | "SELL") => {
    if (!leg) return;
    const qty = lots * leg.lot_size;
    if (!oneClick && !window.confirm(`${side} ${qty} ${label} at market?\nReal order · intraday`)) return;
    setMsg(`${side} ${qty} ${label}…`);
    try {
      setMsg(await helpers.orderFor(leg.symbol, label, qty)({ side, type: "MARKET", price: null }));
    } catch (e) {
      setMsg(`✕ ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  // This underlying's open option positions (e.g. NSE:NIFTY…22400CE). Keyed
  // on the option root: the index's own name (NIFTY50, NIFTYBANK) never
  // prefixes its options, so "exit all" found nothing to close.
  const root = base ? optionRoot(base) : null;
  const open = (positions ?? []).filter(
    (p) => base && root && p.quantity !== 0 && isOptionOf(p.symbol, base.exchange, root),
  );
  const exitAll = async () => {
    if (open.length === 0) return setMsg("No open positions on this underlying.");
    if (!oneClick && !window.confirm(`Exit ${open.length} position(s) on ${name} at market?`)) return;
    setMsg(`Exiting ${open.length}…`);
    const res = await Promise.allSettled(open.map((p) => helpers.closeFor(p.symbol)()));
    const bad = res.filter((r) => r.status === "rejected").length;
    setMsg(bad ? `✕ ${bad} of ${open.length} exits failed — check Positions` : `Exited ${open.length} position(s)`);
  };

  // Hotkeys, only while 1-click is on: Shift+↑ buy CE · Shift+↓ buy PE · Shift+X exit all.
  useEffect(() => {
    if (!oneClick) return;
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (!e.shiftKey || (t && /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName))) return;
      if (e.key === "ArrowUp") { e.preventDefault(); void trade(ce, legName(ceRow, "CE"), "BUY"); }
      else if (e.key === "ArrowDown") { e.preventDefault(); void trade(pe, legName(peRow, "PE"), "BUY"); }
      else if (e.key === "X" || e.key === "x") { e.preventDefault(); void exitAll(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  if (!base) {
    return <section className="trade-card tv-empty"><div className="empty">Open an index or F&amp;O stock first — the scalper trades its options.</div></section>;
  }

  const stepLabel = (o: number) => (o === 0 ? "ATM" : o > 0 ? `OTM ${o}` : `ITM ${-o}`);
  const leg = (row: typeof ceRow, l: OptionLeg | null, t: "CE" | "PE", off: number, setOff: (n: number) => void) => (
    <div className={`scalp-leg ${t.toLowerCase()}`}>
      <select value={off} onChange={(e) => setOff(Number(e.target.value))} aria-label={`${t} strike`} title={`${t} strike, relative to ATM`}>
        {STEPS.map((o) => {
          const r = atmIdx >= 0 ? at(atmIdx + (t === "CE" ? o : -o)) : undefined;
          return <option key={o} value={o}>{stepLabel(o)}{r ? ` · ${r.strike}` : ""}</option>;
        })}
      </select>
      <span className="name" title={legName(row, t)}>{t}</span>
      {l ? <Ltp symbol={l.symbol} fallback={l.ltp} /> : <span className="scalp-ltp">—</span>}
      <button type="button" className="scalp-btn buy" disabled={!l} onClick={() => void trade(l, legName(row, t), "BUY")} data-testid={`scalp-buy-${t}`}>BUY</button>
      <button type="button" className="scalp-btn sell" disabled={!l} onClick={() => void trade(l, legName(row, t), "SELL")} data-testid={`scalp-sell-${t}`}>SELL</button>
    </div>
  );
  const chart = (sym: string, label: string, col: number, qty?: number) => (
    <ChartPanel
      key={sym}
      symbol={sym}
      shortName={label}
      chrome={activeCol === col}
      toolbarSlot={activeCol === col ? top : undefined}
      toolsSlot={activeCol === col ? left : undefined}
      instant={instant}
      showPlus={showPlus}
      position={helpers.positionFor(sym)}
      onLevels={helpers.levelsFor(sym)}
      onClosePosition={helpers.closeFor(sym)}
      onChartOrder={qty ? helpers.orderFor(sym, label, qty) : undefined}
      orderQty={qty}
    />
  );
  const lotSize = ce?.lot_size ?? pe?.lot_size ?? 1;

  return (
    <div className="scalp" data-testid="scalper">
      <div className="scalp-bar">
        <b className="scalp-title">⚡ Scalper</b>
        <select value={base.symbol} onChange={(e) => { const h = underlyings.find((u) => u.symbol === e.target.value); if (h) onBase(h); }} aria-label="Underlying">
          {underlyings.map((u) => <option key={u.symbol} value={u.symbol}>{u.short_name}</option>)}
        </select>
        {(chain?.expiries?.length ?? 0) > 0 && (
          <select value={expiry ?? chain?.selected_expiry ?? ""} onChange={(e) => onExpiry(e.target.value || null)} aria-label="Expiry">
            {chain!.expiries.map((x) => <option key={x.ts} value={x.ts}>{x.label}</option>)}
          </select>
        )}
        <span className="scalp-spot">spot <b>{chain?.spot != null ? chain.spot.toFixed(2) : "—"}</b></span>
        <label className="scalp-lots">
          lots
          <button type="button" onClick={() => setLots((n) => Math.max(1, n - 1))} aria-label="Fewer lots">−</button>
          <input type="number" min={1} value={lots} onChange={(e) => setLots(Math.max(1, Math.floor(Number(e.target.value) || 1)))} />
          <button type="button" onClick={() => setLots((n) => n + 1)} aria-label="More lots">+</button>
          <span>× {lotSize} = <b>{lots * lotSize}</b></span>
        </label>
        <label className={`scalp-oneclick${oneClick ? " on" : ""}`} title="Place orders without the confirm; enables Shift+↑ buy CE · Shift+↓ buy PE · Shift+X exit all">
          <input
            type="checkbox"
            checked={oneClick}
            onChange={(e) => onInstant(e.target.checked)}
          />
          1-click{oneClick && " · Shift+↑ CE · Shift+↓ PE · Shift+X exit"}
        </label>
        <button type="button" className="scalp-btn exit" onClick={() => void exitAll()} disabled={open.length === 0} title="Close every open option position on this underlying at market">
          Exit all{open.length ? ` (${open.length})` : ""}
        </button>
        {msg && <span className="scalp-msg" title={msg}>{msg}</span>}
      </div>
      {strikes.length === 0 ? (
        <section className="trade-card tv-empty"><div className="empty">{chain?.reason ?? (chain ? "No options listed for this underlying." : "Loading the option chain…")}</div></section>
      ) : (
        <div className="tv-layout">
          <div className="tv-topbar"><div className="tv-topslot" ref={setTop} />{settings}</div>
          <div className="tv-chartrow">
            <div className="tv-leftbar" ref={setLeft} />
            <div className="scalp-grid" style={{ gridTemplateColumns: `${cols[0]}fr ${cols[1] - cols[0]}fr ${1 - cols[1]}fr` }}>
              {[0, 1, 2].map((i) => (
                <div key={i} className={`scalp-col${activeCol === i ? " active" : ""}`} onMouseDownCapture={() => setActiveCol(i)}>
                  {i === 0 && leg(ceRow, ce, "CE", ceOff, setCeOff)}
                  {i === 0 && (ce ? chart(ce.symbol, legName(ceRow, "CE"), 0, lots * ce.lot_size) : <section className="trade-card tv-empty" />)}
                  {i === 1 && <div className="scalp-leg base"><span className="name">{name}</span><span className="scalp-ltp">{chain?.spot != null ? chain.spot.toFixed(2) : "—"}</span><span className="hint">underlying</span></div>}
                  {i === 1 && chart(base.symbol, name, 1)}
                  {i === 2 && leg(peRow, pe, "PE", peOff, setPeOff)}
                  {i === 2 && (pe ? chart(pe.symbol, legName(peRow, "PE"), 2, lots * pe.lot_size) : <section className="trade-card tv-empty" />)}
                </div>
              ))}
              {[0, 1].map((k) => (
                <div
                  key={k}
                  className="split-x"
                  style={{ left: `calc(${cols[k] * 100}% - 3px)` }}
                  title="Drag to resize · double-click to reset"
                  onPointerDown={(e) => splitDrag(e, "x", (f) => setCols((c) => (k === 0 ? [f, c[1]] : [c[0], f])), k === 0 ? 0.1 : cols[0] + 0.1, k === 0 ? cols[1] - 0.1 : 0.9)}
                  onDoubleClick={() => setCols([1 / 3, 2 / 3])}
                />
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
