// Algo Lab — build indicator strategies, backtest them on Fyers candles,
// optimise their parameters, and switch them on as paper or live automations.
//
// One engine runs everywhere (app/algo/engine.py): the backtest, the
// optimiser and the live runner evaluate the same JSON spec, on completed
// bars, filling at the next bar's open. What you backtest is what runs.
//
// Control lives here (frontend-only-control invariant): saved strategies
// start OFF and in paper; LIVE needs a real Fyers account and the typed
// confirm "LIVE".

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import {
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { api } from "../api/client";
import { Toggle } from "../components/common/Toggle";

// ---------------------------------------------------------------------------
// types
// ---------------------------------------------------------------------------

type Operand = {
  ind?: string;
  params?: Record<string, number | string>;
  field?: string;
  offset?: number;
  value?: number;
};
type Cond = { left: Operand; op: string; right?: Operand };
type Group = { logic: "AND" | "OR"; conditions: (Cond | Group)[] };
type Level = { type: string; value: number; atr_period?: number } | null;
type Spec = {
  symbols: string[];
  timeframe: number;
  direction: "long" | "short" | "both";
  entry_long: Group;
  exit_long: Group | null;
  entry_short: Group;
  exit_short: Group | null;
  stop_loss: Level;
  target: Level;
  trailing: Level;
  session: { start: string; end: string; square_off: string };
  max_trades_per_day: number;
  sizing: { mode: string; value: number };
  capital: number;
  costs: { slippage_pct: number; charges: boolean };
};
type IndicatorDef = { name: string; params: Record<string, number | string>; outputs: string[]; group: string };
type Catalog = {
  indicators: IndicatorDef[];
  operators: string[];
  timeframes: number[];
  sources: string[];
  metrics: string[];
  defaults: Spec;
  max_range_days: number;
};
type BtTrade = {
  symbol: string; side: string; qty: number; entry_t: number; entry: number; exit_t: number;
  exit: number; reason: string; gross: number; charges: number; net: number; bars: number;
};
type Stats = Record<string, number | null> & { equity: [number, number][]; daily: [number, number][] };
type BtResult = {
  stats: Stats;
  per_symbol: Record<string, { trades: number; win_rate: number; net_pnl: number; profit_factor: number | null; max_drawdown: number; bars: number }>;
  by_reason: Record<string, { count: number; net: number }>;
  trades: BtTrade[];
  trades_total: number;
  notes: { symbol: string; note: string }[];
  elapsed_s: number;
  chart: { symbol: string; candles: number[][]; trades: BtTrade[] };
  spec: Spec;
};
type OptRow = { params: Record<string, number>; trades: number; win_rate: number; net_pnl: number; profit_factor: number | null; sharpe: number | null; max_drawdown: number; return_pct: number; expectancy: number };
type OptResult = { combos: number; ranked: OptRow[]; too_few_trades: number; metric: string; notes: { symbol: string; note: string }[]; elapsed_s: number };
type Saved = { id: number; name: string; spec: Spec; enabled: boolean; mode: "paper" | "live"; account_id: number | null; closed_trades: number; realized_pnl: number; open_positions: number };
type LiveTrade = {
  id: number; strategy_id: number; symbol: string; side: string; quantity: number; mode: string; status: string;
  entry_at: string; entry_price: number | null; stop_loss: number | null; target: number | null; trail_stop: number | null;
  exit_at: string | null; exit_price: number | null; exit_reason: string | null; net_pnl: number | null; charges: number | null;
  note: string | null; ltp?: number | null; unrealized?: number | null;
};
type RunnerStatus = { running: boolean; last_tick: number | null; last_sync: { t: number; symbols: number; ok: number; failed: string[] } | null; events: { t: number; level: string; msg: string }[]; open: LiveTrade[] };
type Account = { id: number; name: string; broker: string; paper_mode: boolean; enabled: boolean };

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

const IST_S = 19800;
const isGroup = (c: Cond | Group): c is Group => (c as Group).conditions !== undefined;
const I = (ind: string, params: Record<string, number | string> = {}, field?: string): Operand => ({ ind, params, field, offset: 0 });
const N = (value: number): Operand => ({ value });
const C = (left: Operand, op: string, right?: Operand): Cond => ({ left, op, right });
const G = (conditions: (Cond | Group)[], logic: "AND" | "OR" = "AND"): Group => ({ logic, conditions });
const clone = <T,>(x: T): T => JSON.parse(JSON.stringify(x)) as T;
const inr = (v: number | null | undefined, dp = 0) =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : `₹${v.toLocaleString("en-IN", { maximumFractionDigits: dp, minimumFractionDigits: dp })}`;
const num = (v: number | null | undefined, dp = 2) => (v === null || v === undefined ? "—" : v.toFixed(dp));
const pnlCls = (v: number | null | undefined) => (v === null || v === undefined || v === 0 ? "" : v > 0 ? "pnl-pos" : "pnl-neg");
const ist = (epoch: number) =>
  new Date(epoch * 1000).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short", year: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
const isoIst = (iso: string | null) => (iso ? ist(Date.parse(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z") / 1000) : "—");
const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
const daysAgo = (n: number) => new Date(Date.now() - n * 86400000).toISOString().slice(0, 10);

// Backtests can run for minutes when a year of candles has to be downloaded
// first — the shared client's 30s ceiling would abort them.
async function longPost<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const text = await res.text();
  let parsed: unknown;
  try { parsed = JSON.parse(text); } catch { parsed = undefined; }
  if (!res.ok) {
    const d = (parsed as { detail?: unknown } | undefined)?.detail;
    throw new Error(d ? (typeof d === "string" ? d : JSON.stringify(d)) : text || `HTTP ${res.status}`);
  }
  return parsed as T;
}

function setPath(obj: unknown, path: string, value: unknown) {
  const keys = path.split(".");
  let o = obj as Record<string, unknown>;
  for (const k of keys.slice(0, -1)) {
    if (o[k] === undefined || o[k] === null) o[k] = {};
    o = o[k] as Record<string, unknown>;
  }
  o[keys[keys.length - 1]] = value;
}

// Every number in a spec the optimiser can sweep.
function numericPaths(spec: Spec, cat: Catalog): { path: string; label: string; value: number }[] {
  const out: { path: string; label: string; value: number }[] = [];
  const walk = (g: Group | null, prefix: string, label: string) => {
    g?.conditions.forEach((c, i) => {
      const p = `${prefix}.conditions.${i}`;
      if (isGroup(c)) return walk(c, p, `${label} ▸ group ${i + 1}`);
      (["left", "right"] as const).forEach((side) => {
        const o = c[side];
        if (!o) return;
        if (o.ind) {
          const def = cat.indicators.find((d) => d.name === o.ind);
          Object.entries(def?.params ?? {}).forEach(([k, dv]) => {
            if (k === "source") return;
            out.push({ path: `${p}.${side}.params.${k}`, label: `${label} #${i + 1} ${side}: ${o.ind}.${k}`, value: Number(o.params?.[k] ?? dv) });
          });
        } else if (o.value !== undefined) {
          out.push({ path: `${p}.${side}.value`, label: `${label} #${i + 1} ${side}: number`, value: o.value });
        }
      });
    });
  };
  walk(spec.entry_long, "entry_long", "Long entry");
  walk(spec.exit_long, "exit_long", "Long exit");
  walk(spec.entry_short, "entry_short", "Short entry");
  walk(spec.exit_short, "exit_short", "Short exit");
  (["stop_loss", "target", "trailing"] as const).forEach((k) => {
    const lv = spec[k];
    if (lv) out.push({ path: `${k}.value`, label: `${k.replace("_", " ")} (${lv.type})`, value: lv.value });
  });
  out.push({ path: "max_trades_per_day", label: "max trades / day", value: spec.max_trades_per_day });
  return out;
}

// Make sure every indicator operand carries a params object (the optimiser
// writes into it) and drop the side of a direction that isn't traded.
function tidy(spec: Spec): Spec {
  const s = clone(spec);
  const fix = (g: Group | null) =>
    g?.conditions.forEach((c) => {
      if (isGroup(c)) return fix(c);
      [c.left, c.right].forEach((o) => { if (o && o.ind && !o.params) o.params = {}; });
    });
  [s.entry_long, s.exit_long, s.entry_short, s.exit_short].forEach(fix);
  return s;
}

// ---------------------------------------------------------------------------
// templates — starting points, all editable
// ---------------------------------------------------------------------------

type Template = { name: string; spec: Partial<Spec> };
const TEMPLATES: Template[] = [
  { name: "EMA 9/21 crossover (both sides)", spec: {
    direction: "both",
    entry_long: G([C(I("EMA", { period: 9 }), "crosses_above", I("EMA", { period: 21 }))]),
    entry_short: G([C(I("EMA", { period: 9 }), "crosses_below", I("EMA", { period: 21 }))]),
    stop_loss: { type: "atr", value: 1.5, atr_period: 14 }, target: { type: "rr", value: 2 }, trailing: null } },
  { name: "Supertrend flip", spec: {
    direction: "both",
    entry_long: G([C(I("SUPERTREND", { period: 10, multiplier: 3 }, "direction"), "crosses_above", N(0))]),
    exit_long: G([C(I("SUPERTREND", { period: 10, multiplier: 3 }, "direction"), "crosses_below", N(0))]),
    entry_short: G([C(I("SUPERTREND", { period: 10, multiplier: 3 }, "direction"), "crosses_below", N(0))]),
    exit_short: G([C(I("SUPERTREND", { period: 10, multiplier: 3 }, "direction"), "crosses_above", N(0))]),
    stop_loss: { type: "pct", value: 1 }, target: null, trailing: null } },
  { name: "Opening range breakout (15m)", spec: {
    direction: "both", timeframe: 5,
    session: { start: "09:30", end: "13:00", square_off: "15:10" },
    entry_long: G([C(I("PRICE"), "crosses_above", I("ORB", { minutes: 15 }, "high"))]),
    entry_short: G([C(I("PRICE"), "crosses_below", I("ORB", { minutes: 15 }, "low"))]),
    max_trades_per_day: 1, stop_loss: { type: "pct", value: 0.6 }, target: { type: "rr", value: 2 }, trailing: null } },
  { name: "VWAP + RSI momentum", spec: {
    direction: "both",
    entry_long: G([C(I("PRICE"), "crosses_above", I("VWAP")), C(I("RSI", { period: 14 }), ">", N(55))]),
    entry_short: G([C(I("PRICE"), "crosses_below", I("VWAP")), C(I("RSI", { period: 14 }), "<", N(45))]),
    stop_loss: { type: "atr", value: 1.2, atr_period: 14 }, target: { type: "rr", value: 1.5 }, trailing: null } },
  { name: "RSI reversal 30/70", spec: {
    direction: "both",
    entry_long: G([C(I("RSI", { period: 14 }), "crosses_above", N(30))]),
    exit_long: G([C(I("RSI", { period: 14 }), "crosses_above", N(70))]),
    entry_short: G([C(I("RSI", { period: 14 }), "crosses_below", N(70))]),
    exit_short: G([C(I("RSI", { period: 14 }), "crosses_below", N(30))]),
    stop_loss: { type: "pct", value: 0.8 }, target: null, trailing: null } },
  { name: "Bollinger mean reversion", spec: {
    direction: "long",
    entry_long: G([C(I("PRICE"), "crosses_above", I("BBANDS", { period: 20, multiplier: 2 }, "lower"))]),
    exit_long: G([C(I("PRICE"), "crosses_above", I("BBANDS", { period: 20, multiplier: 2 }, "middle"))]),
    stop_loss: { type: "pct", value: 1 }, target: null, trailing: null } },
  { name: "MACD cross + ADX trend filter", spec: {
    direction: "both",
    entry_long: G([C(I("MACD", {}, "macd"), "crosses_above", I("MACD", {}, "signal")), C(I("ADX", { period: 14 }, "adx"), ">", N(25))]),
    entry_short: G([C(I("MACD", {}, "macd"), "crosses_below", I("MACD", {}, "signal")), C(I("ADX", { period: 14 }, "adx"), ">", N(25))]),
    stop_loss: { type: "atr", value: 2, atr_period: 14 }, target: null, trailing: { type: "atr", value: 2, atr_period: 14 } } },
  { name: "CPR breakout", spec: {
    direction: "both", timeframe: 15,
    entry_long: G([C(I("PRICE"), "crosses_above", I("DAILY", {}, "tc")), C(I("PRICE"), ">", I("DAILY", {}, "pivot"))]),
    entry_short: G([C(I("PRICE"), "crosses_below", I("DAILY", {}, "bc")), C(I("PRICE"), "<", I("DAILY", {}, "pivot"))]),
    max_trades_per_day: 1, stop_loss: { type: "pct", value: 0.7 }, target: { type: "rr", value: 2 }, trailing: null } },
];

const DRAFT_KEY = "algo.draft.v1";

// ---------------------------------------------------------------------------
// builder widgets
// ---------------------------------------------------------------------------

const inp: React.CSSProperties = { width: 64 };

function OperandEditor({ v, onChange, cat, allowNumber = true }: { v: Operand; onChange: (o: Operand) => void; cat: Catalog; allowNumber?: boolean }) {
  const def = v.ind ? cat.indicators.find((d) => d.name === v.ind) : undefined;
  const groups = useMemo(() => Array.from(new Set(cat.indicators.map((d) => d.group))), [cat]);
  return (
    <span style={{ display: "inline-flex", gap: 4, alignItems: "center", flexWrap: "wrap" }}>
      <select
        value={v.ind ?? "#"}
        onChange={(e) => onChange(e.target.value === "#" ? { value: 0 } : I(e.target.value))}
      >
        {allowNumber && <option value="#">Number</option>}
        {groups.map((g) => (
          <optgroup key={g} label={g}>
            {cat.indicators.filter((d) => d.group === g).map((d) => <option key={d.name} value={d.name}>{d.name}</option>)}
          </optgroup>
        ))}
      </select>
      {!def && (
        <input type="number" step="any" style={inp} value={v.value ?? 0}
          onChange={(e) => onChange({ value: Number(e.target.value) })} />
      )}
      {def && def.outputs.length > 1 && (
        <select value={v.field ?? def.outputs[0]} onChange={(e) => onChange({ ...v, field: e.target.value })}>
          {def.outputs.map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
      )}
      {def && Object.entries(def.params).map(([k, dv]) => (
        <label key={k} className="meta" style={{ display: "inline-flex", gap: 2, alignItems: "center" }} title={k}>
          {k.replace("_period", "").replace("multiplier", "mult")}
          {k === "source" ? (
            <select value={String(v.params?.[k] ?? dv)} onChange={(e) => onChange({ ...v, params: { ...v.params, [k]: e.target.value } })}>
              {cat.sources.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          ) : (
            <input type="number" step="any" style={{ width: 52 }} value={Number(v.params?.[k] ?? dv)}
              onChange={(e) => onChange({ ...v, params: { ...v.params, [k]: Number(e.target.value) } })} />
          )}
        </label>
      ))}
      {def && (
        <label className="meta" title="bars ago — 1 = the previous candle's value" style={{ display: "inline-flex", gap: 2, alignItems: "center" }}>
          ago
          <input type="number" min={0} style={{ width: 40 }} value={v.offset ?? 0}
            onChange={(e) => onChange({ ...v, offset: Math.max(0, Number(e.target.value)) })} />
        </label>
      )}
    </span>
  );
}

const OP_LABEL: Record<string, string> = {
  crosses_above: "crosses above", crosses_below: "crosses below", crosses: "crosses",
  rising: "is rising for (bars)", falling: "is falling for (bars)",
};

function CondEditor({ c, onChange, cat }: { c: Cond; onChange: (c: Cond) => void; cat: Catalog }) {
  const trend = c.op === "rising" || c.op === "falling";
  return (
    <span style={{ display: "inline-flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
      <OperandEditor v={c.left} cat={cat} allowNumber={false} onChange={(left) => onChange({ ...c, left })} />
      <select value={c.op} onChange={(e) => {
        const op = e.target.value;
        const t = op === "rising" || op === "falling";
        onChange({ ...c, op, right: t ? N(Math.max(1, c.right?.value ?? 1)) : c.right?.ind || c.right?.value !== undefined ? c.right : N(0) });
      }}>
        {cat.operators.map((o) => <option key={o} value={o}>{OP_LABEL[o] ?? o}</option>)}
      </select>
      {trend ? (
        <input type="number" min={1} style={{ width: 48 }} value={c.right?.value ?? 1}
          onChange={(e) => onChange({ ...c, right: N(Math.max(1, Number(e.target.value))) })} />
      ) : (
        <OperandEditor v={c.right ?? N(0)} cat={cat} onChange={(right) => onChange({ ...c, right })} />
      )}
    </span>
  );
}

function GroupEditor({ g, onChange, cat, depth = 0 }: { g: Group; onChange: (g: Group) => void; cat: Catalog; depth?: number }) {
  const set = (i: number, c: Cond | Group) => onChange({ ...g, conditions: g.conditions.map((x, j) => (j === i ? c : x)) });
  return (
    <div style={{ borderLeft: `2px solid ${depth ? "var(--cyan)" : "var(--accent)"}`, paddingLeft: 8, display: "grid", gap: 6 }}>
      <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
        <select value={g.logic} onChange={(e) => onChange({ ...g, logic: e.target.value as "AND" | "OR" })}>
          <option value="AND">ALL of (AND)</option>
          <option value="OR">ANY of (OR)</option>
        </select>
        <button className="btn-sm" onClick={() => onChange({ ...g, conditions: [...g.conditions, C(I("PRICE"), ">", I("EMA", { period: 20 }))] })}>+ condition</button>
        {depth < 2 && <button className="btn-sm" onClick={() => onChange({ ...g, conditions: [...g.conditions, G([C(I("RSI", { period: 14 }), ">", N(50))], "OR")] })}>+ group</button>}
        {g.conditions.length === 0 && <span className="meta">no conditions — never fires</span>}
      </div>
      {g.conditions.map((c, i) => (
        <div key={i} style={{ display: "flex", gap: 6, alignItems: "flex-start" }}>
          <div style={{ flex: 1 }}>
            {isGroup(c)
              ? <GroupEditor g={c} cat={cat} depth={depth + 1} onChange={(x) => set(i, x)} />
              : <CondEditor c={c} cat={cat} onChange={(x) => set(i, x)} />}
          </div>
          <button className="btn-sm danger" title="remove" onClick={() => onChange({ ...g, conditions: g.conditions.filter((_, j) => j !== i) })}>✕</button>
        </div>
      ))}
    </div>
  );
}

function LevelEditor({ label, v, onChange, allowRR = false }: { label: string; v: Level; onChange: (l: Level) => void; allowRR?: boolean }) {
  return (
    <label className="meta" style={{ display: "inline-flex", gap: 4, alignItems: "center" }}>
      {label}
      <select value={v?.type ?? "none"} onChange={(e) => onChange(e.target.value === "none" ? null : { type: e.target.value, value: v?.value ?? 1, atr_period: v?.atr_period ?? 14 })}>
        <option value="none">none</option>
        <option value="pct">% of price</option>
        <option value="points">points (₹)</option>
        <option value="atr">× ATR</option>
        {allowRR && <option value="rr">× risk (R:R)</option>}
      </select>
      {v && <input type="number" step="any" min={0} style={inp} value={v.value} onChange={(e) => onChange({ ...v, value: Number(e.target.value) })} />}
      {v?.type === "atr" && (
        <>ATR<input type="number" min={1} style={{ width: 44 }} value={v.atr_period ?? 14} onChange={(e) => onChange({ ...v, atr_period: Number(e.target.value) })} /></>
      )}
    </label>
  );
}

function Metric({ label, value, cls, hint }: { label: string; value: string; cls?: string; hint?: string }) {
  return (
    <div style={{ minWidth: 112 }} title={hint}>
      <div className="meta">{label}</div>
      <div className={`mono ${cls ?? ""}`} style={{ fontSize: 16 }}>{value}</div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// result views
// ---------------------------------------------------------------------------

function TradeChart({ chart }: { chart: BtResult["chart"] }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el || chart.candles.length === 0) return;
    const css = getComputedStyle(document.documentElement);
    const color = (n: string, f: string) => css.getPropertyValue(n).trim() || f;
    const c = createChart(el, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: color("--text-dim", "#999"), fontSize: 11, attributionLogo: false },
      grid: { vertLines: { color: color("--border-soft", "#1f1f1f") }, horzLines: { color: color("--border-soft", "#1f1f1f") } },
      timeScale: { timeVisible: true, secondsVisible: false },
      localization: { locale: "en-IN" },
    });
    const up = color("--green", "#00D787");
    const dn = color("--red", "#FF3838");
    const s = c.addSeries(CandlestickSeries, { upColor: up, downColor: dn, wickUpColor: up, wickDownColor: dn, borderVisible: false });
    // lightweight-charts has no timezone: shift by IST so the axis reads IST.
    const times = chart.candles.map((k) => k[0]);
    s.setData(chart.candles.map((k) => ({ time: (k[0] + IST_S) as UTCTimestamp, open: k[1], high: k[2], low: k[3], close: k[4] })));
    const snap = (t: number) => {   // marker times must sit on a candle
      let lo = 0, hi = times.length - 1;
      while (lo < hi) { const m = (lo + hi + 1) >> 1; if (times[m] <= t) lo = m; else hi = m - 1; }
      return times[lo];
    };
    const first = times[0];
    const markers: SeriesMarker<Time>[] = [];
    chart.trades.filter((t) => t.entry_t >= first).forEach((t) => {
      const buy = t.side === "BUY";
      markers.push({ time: (snap(t.entry_t) + IST_S) as UTCTimestamp, position: buy ? "belowBar" : "aboveBar", color: buy ? up : dn, shape: buy ? "arrowUp" : "arrowDown", text: `${t.side} ${t.entry}` });
      markers.push({ time: (snap(t.exit_t) + IST_S) as UTCTimestamp, position: buy ? "aboveBar" : "belowBar", color: t.net >= 0 ? up : dn, shape: "circle", text: `${t.reason} ${t.net >= 0 ? "+" : ""}${t.net.toFixed(0)}` });
    });
    markers.sort((a, b) => (a.time as number) - (b.time as number));
    createSeriesMarkers(s, markers);
    c.timeScale().setVisibleLogicalRange({ from: Math.max(0, times.length - 300), to: times.length + 5 });
    return () => c.remove();
  }, [chart]);
  return <div ref={ref} style={{ height: 380, width: "100%" }} />;
}

function Results({ r }: { r: BtResult }) {
  const s = r.stats;
  const eq = useMemo(() => {
    const pts = s.equity;
    const step = Math.max(1, Math.ceil(pts.length / 800));
    return pts.filter((_, i) => i % step === 0 || i === pts.length - 1).map(([t, v]) => ({ t: ist(t), v }));
  }, [s.equity]);
  const [showAll, setShowAll] = useState(false);
  const trades = useMemo(() => [...r.trades].reverse().slice(0, showAll ? 3000 : 200), [r.trades, showAll]);
  return (
    <>
      {r.notes.length > 0 && (
        <div className="widget widget-wide" style={{ marginBottom: 12, borderColor: "var(--amber)" }}>
          {r.notes.map((n) => <div key={n.symbol} className="meta">⚠ {n.symbol}: {n.note}</div>)}
        </div>
      )}
      <div className="widget widget-wide" style={{ marginBottom: 12 }}>
        <h3>Performance <span className="meta">{r.trades_total} trades · {s.trading_days} days · {r.elapsed_s}s</span></h3>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 18 }}>
          <Metric label="Net P&L" value={inr(s.net_pnl as number)} cls={pnlCls(s.net_pnl as number)} hint="after charges and slippage" />
          <Metric label="Return" value={`${num(s.return_pct as number)}%`} cls={pnlCls(s.return_pct as number)} hint="net P&L / capital" />
          <Metric label="Win rate" value={`${num(s.win_rate as number, 1)}%`} />
          <Metric label="Profit factor" value={num(s.profit_factor)} hint="gross wins / gross losses" />
          <Metric label="Expectancy" value={inr(s.expectancy as number)} hint="average net per trade" />
          <Metric label="Max drawdown" value={`${inr(s.max_drawdown as number)} (${num(s.max_drawdown_pct as number, 1)}%)`} cls="pnl-neg" />
          <Metric label="Sharpe" value={num(s.sharpe)} hint="daily P&L, annualised √252" />
          <Metric label="Charges" value={inr(s.charges as number)} hint="brokerage, STT, exchange, SEBI, GST, stamp" />
          <Metric label="Gross P&L" value={inr(s.gross_pnl as number)} cls={pnlCls(s.gross_pnl as number)} />
          <Metric label="Avg win / loss" value={`${inr(s.avg_win as number)} / ${inr(s.avg_loss as number)}`} />
          <Metric label="Largest win / loss" value={`${inr(s.largest_win as number)} / ${inr(s.largest_loss as number)}`} />
          <Metric label="Streaks W / L" value={`${s.max_consecutive_wins} / ${s.max_consecutive_losses}`} />
          <Metric label="Profitable days" value={`${s.profitable_days} / ${s.trading_days}`} />
          <Metric label="Avg bars held" value={num(s.avg_bars_held as number, 1)} />
        </div>
      </div>
      <div className="widget widget-wide" style={{ marginBottom: 12 }}>
        <h3>Equity curve</h3>
        {eq.length < 2 ? <div className="empty">no trades</div> : (
          <ResponsiveContainer width="100%" height={240}>
            <LineChart data={eq}>
              <CartesianGrid stroke="var(--border-soft)" />
              <XAxis dataKey="t" tick={{ fontSize: 10, fill: "var(--text-dim)" }} minTickGap={60} />
              <YAxis tick={{ fontSize: 10, fill: "var(--text-dim)" }} domain={["auto", "auto"]} width={70} />
              <Tooltip contentStyle={{ background: "var(--bg-panel)", border: "1px solid var(--border)" }} formatter={(v: number) => inr(v)} />
              <Line type="monotone" dataKey="v" stroke="var(--accent)" dot={false} strokeWidth={1.5} isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>
      <div className="widget widget-wide" style={{ marginBottom: 12 }}>
        <h3>{r.chart.symbol} <span className="meta">last {r.chart.candles.length} candles · ▲▼ entries, ● exits</span></h3>
        <TradeChart chart={r.chart} />
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 12, marginBottom: 12 }}>
        <div className="widget">
          <h3>By symbol</h3>
          <table><thead><tr><th>Symbol</th><th>Bars</th><th>Trades</th><th>Win %</th><th>PF</th><th>Net</th></tr></thead>
            <tbody>{Object.entries(r.per_symbol).map(([k, v]) => (
              <tr key={k}><td>{k}</td><td className="mono">{v.bars}</td><td className="mono">{v.trades}</td><td className="mono">{num(v.win_rate, 1)}</td>
                <td className="mono">{num(v.profit_factor)}</td><td className={`mono ${pnlCls(v.net_pnl)}`}>{inr(v.net_pnl)}</td></tr>))}
            </tbody></table>
        </div>
        <div className="widget">
          <h3>By exit reason</h3>
          <table><thead><tr><th>Reason</th><th>Count</th><th>Net</th></tr></thead>
            <tbody>{Object.entries(r.by_reason).map(([k, v]) => (
              <tr key={k}><td>{k}</td><td className="mono">{v.count}</td><td className={`mono ${pnlCls(v.net)}`}>{inr(v.net)}</td></tr>))}
            </tbody></table>
        </div>
      </div>
      <div className="widget widget-wide">
        <h3>Trades <span className="meta">newest first · showing {trades.length} of {r.trades_total}</span>
          {r.trades.length > 200 && <button className="btn-sm" style={{ marginLeft: 8 }} onClick={() => setShowAll(!showAll)}>{showAll ? "fewer" : "show all"}</button>}
        </h3>
        <div style={{ maxHeight: 420, overflow: "auto" }}>
          <table>
            <thead><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Entry (IST)</th><th>Entry</th><th>Exit (IST)</th><th>Exit</th><th>Reason</th><th>Bars</th><th>Charges</th><th>Net</th></tr></thead>
            <tbody>{trades.map((t, i) => (
              <tr key={i}><td>{t.symbol}</td><td><span className={`badge ${t.side === "BUY" ? "buy" : "sell"}`}>{t.side}</span></td>
                <td className="mono">{t.qty}</td><td className="mono">{ist(t.entry_t)}</td><td className="mono">{t.entry.toFixed(2)}</td>
                <td className="mono">{ist(t.exit_t)}</td><td className="mono">{t.exit.toFixed(2)}</td><td>{t.reason}</td>
                <td className="mono">{t.bars}</td><td className="mono">{t.charges.toFixed(0)}</td><td className={`mono ${pnlCls(t.net)}`}>{t.net.toFixed(0)}</td></tr>))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// tabs
// ---------------------------------------------------------------------------

function Optimizer({ spec, cat, range, onApply }: { spec: Spec; cat: Catalog; range: { start: string; end: string }; onApply: (params: Record<string, number>) => void }) {
  const paths = useMemo(() => numericPaths(spec, cat), [spec, cat]);
  const [axes, setAxes] = useState<{ path: string; from: number; to: number; step: number }[]>([]);
  const [metric, setMetric] = useState("net_pnl");
  const [minTrades, setMinTrades] = useState(10);
  const values = (a: { from: number; to: number; step: number }) => {
    const out: number[] = [];
    if (a.step <= 0) return [a.from];
    for (let v = a.from; v <= a.to + 1e-9 && out.length < 60; v += a.step) out.push(Math.round(v * 1e6) / 1e6);
    return out;
  };
  const combos = axes.reduce((n, a) => n * values(a).length, axes.length ? 1 : 0);
  const run = useMutation({
    mutationFn: () => longPost<OptResult>("/api/algo/optimize", {
      spec: tidy(spec), ...range, metric, min_trades: minTrades,
      grid: axes.map((a) => ({ path: a.path, values: values(a) })),
    }),
  });
  const label = (p: string) => paths.find((x) => x.path === p)?.label ?? p;
  return (
    <div className="widget widget-wide">
      <h3>Optimise <span className="meta">grid search over the builder's strategy, {range.start} → {range.end}</span></h3>
      <div style={{ display: "grid", gap: 6, marginBottom: 8 }}>
        {axes.map((a, i) => (
          <div key={i} style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
            <select value={a.path} onChange={(e) => {
              const p = paths.find((x) => x.path === e.target.value);
              setAxes(axes.map((x, j) => (j === i ? { path: e.target.value, from: p?.value ?? 1, to: (p?.value ?? 1) * 2, step: Math.max(1, Math.round((p?.value ?? 1) / 4)) } : x)));
            }}>
              {paths.map((p) => <option key={p.path} value={p.path}>{p.label}</option>)}
            </select>
            {(["from", "to", "step"] as const).map((k) => (
              <label key={k} className="meta">{k} <input type="number" step="any" style={inp} value={a[k]} onChange={(e) => setAxes(axes.map((x, j) => (j === i ? { ...x, [k]: Number(e.target.value) } : x)))} /></label>
            ))}
            <span className="meta">{values(a).length} values</span>
            <button className="btn-sm danger" onClick={() => setAxes(axes.filter((_, j) => j !== i))}>✕</button>
          </div>
        ))}
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <button className="btn-sm" disabled={axes.length >= paths.length} onClick={() => {
            const p = paths.find((x) => !axes.some((a) => a.path === x.path));
            if (!p) return;
            setAxes([...axes, { path: p.path, from: p.value, to: p.value * 2, step: Math.max(1, Math.round(p.value / 4)) }]);
          }}>+ parameter</button>
          <label className="meta">rank by <select value={metric} onChange={(e) => setMetric(e.target.value)}>{cat.metrics.map((m) => <option key={m}>{m}</option>)}</select></label>
          <label className="meta" title="combos with fewer trades are not ranked — a 1-trade 100% win rate means nothing">min trades <input type="number" min={1} style={{ width: 52 }} value={minTrades} onChange={(e) => setMinTrades(Number(e.target.value))} /></label>
          <span className="meta">{combos} combinations (max 400)</span>
          <button className="primary" disabled={!combos || combos > 400 || run.isPending} onClick={() => run.mutate()}>{run.isPending ? "Optimising…" : "Run optimisation"}</button>
        </div>
      </div>
      {run.error && <div className="pnl-neg">{errMsg(run.error)}</div>}
      {run.data && (
        <>
          <div className="meta" style={{ marginBottom: 6 }}>{run.data.combos} combos in {run.data.elapsed_s}s · {run.data.too_few_trades} skipped for too few trades. Beware overfitting — confirm the winner on a later date range.</div>
          <div style={{ maxHeight: 480, overflow: "auto" }}>
            <table>
              <thead><tr><th></th>{axes.map((a) => <th key={a.path} title={a.path}>{label(a.path)}</th>)}<th>Trades</th><th>Win %</th><th>PF</th><th>Sharpe</th><th>Max DD</th><th>Net</th></tr></thead>
              <tbody>{run.data.ranked.map((r, i) => (
                <tr key={i}>
                  <td><button className="btn-sm" onClick={() => onApply(r.params)}>apply</button></td>
                  {axes.map((a) => <td key={a.path} className="mono">{r.params[a.path]}</td>)}
                  <td className="mono">{r.trades}</td><td className="mono">{num(r.win_rate, 1)}</td><td className="mono">{num(r.profit_factor)}</td>
                  <td className="mono">{num(r.sharpe)}</td><td className="mono">{inr(r.max_drawdown)}</td><td className={`mono ${pnlCls(r.net_pnl)}`}>{inr(r.net_pnl)}</td>
                </tr>))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

function Automations({ onEdit }: { onEdit: (s: Saved) => void }) {
  const qc = useQueryClient();
  const strategies = useQuery({ queryKey: ["algo", "strategies"], queryFn: () => api.get<{ strategies: Saved[] }>("/api/algo/strategies"), refetchInterval: 10000 });
  const status = useQuery({ queryKey: ["algo", "status"], queryFn: () => api.get<RunnerStatus>("/api/algo/status"), refetchInterval: 5000 });
  const accounts = useQuery({ queryKey: ["algo", "accounts"], queryFn: () => api.get<{ accounts: Account[] }>("/api/broker-accounts") });
  const [sel, setSel] = useState<number | null>(null);
  const trades = useQuery({
    queryKey: ["algo", "trades", sel], refetchInterval: 10000,
    queryFn: () => api.get<{ trades: LiveTrade[] }>(`/api/algo/trades?limit=300${sel ? `&strategy_id=${sel}` : ""}`),
  });
  const [err, setErr] = useState<string | null>(null);
  const refresh = () => { qc.invalidateQueries({ queryKey: ["algo"] }); };
  const put = async (id: number, body: Record<string, unknown>) => {
    setErr(null);
    try { await api.put(`/api/algo/strategies/${id}`, body); } catch (e) { setErr(errMsg(e)); }
    refresh();
  };
  const live = (accounts.data?.accounts ?? []).filter((a) => a.broker === "fyers" && !a.paper_mode);
  const st = status.data;
  const ago = st?.last_tick ? Math.round(Date.now() / 1000 - st.last_tick) : null;
  return (
    <>
      {err && <div className="widget widget-wide pnl-neg" style={{ marginBottom: 12 }}>{err}</div>}
      <div className="widget widget-wide" style={{ marginBottom: 12 }}>
        <h3>Automations <span className="meta">
          runner {st?.running ? (ago !== null && ago < 30 ? `alive · tick ${ago}s ago` : "idle") : "not running"} ·
          evaluates on each completed candle, exits on live LTP every 5s, squares off at the session's square-off time
        </span></h3>
        {(strategies.data?.strategies ?? []).length === 0 ? (
          <div className="empty">No saved strategies yet — build one in the Builder tab and press “Save as new”.</div>
        ) : (
          <table>
            <thead><tr><th>On</th><th>Strategy</th><th>Symbols</th><th>TF</th><th>Mode</th><th>Open</th><th>Closed</th><th>Realised</th><th></th></tr></thead>
            <tbody>{strategies.data!.strategies.map((s) => (
              <tr key={s.id} style={sel === s.id ? { background: "var(--bg-row)" } : undefined}>
                <td><Toggle on={s.enabled} size="sm" onChange={(on: boolean) => put(s.id, { enabled: on })} /></td>
                <td><a href="#/algo" onClick={(e) => { e.preventDefault(); setSel(sel === s.id ? null : s.id); }}>{s.name}</a></td>
                <td className="mono" style={{ maxWidth: 220, overflow: "hidden", textOverflow: "ellipsis" }} title={s.spec.symbols.join(", ")}>{s.spec.symbols.join(", ")}</td>
                <td className="mono">{s.spec.timeframe}m</td>
                <td>
                  <select value={s.mode} onChange={(e) => {
                    if (e.target.value === "live") {
                      if (!live.length) { setErr("No real Fyers account — connect one on the Accounts page first."); return; }
                      const acc = s.account_id && live.some((a) => a.id === s.account_id) ? s.account_id : live[0].id;
                      const typed = window.prompt(`REAL MONEY: "${s.name}" will place MARKET intraday orders on ${live.find((a) => a.id === acc)?.name}. Type LIVE to confirm.`);
                      if (typed !== "LIVE") return;
                      put(s.id, { mode: "live", account_id: acc, confirm: "LIVE" });
                    } else put(s.id, { mode: "paper" });
                  }}>
                    <option value="paper">paper</option>
                    <option value="live">LIVE</option>
                  </select>
                  {s.mode === "live" && <span className="badge danger" style={{ marginLeft: 4 }}>REAL ₹</span>}
                </td>
                <td className="mono">{s.open_positions}</td>
                <td className="mono">{s.closed_trades}</td>
                <td className={`mono ${pnlCls(s.realized_pnl)}`}>{inr(s.realized_pnl)}</td>
                <td style={{ whiteSpace: "nowrap" }}>
                  <button className="btn-sm" onClick={() => onEdit(s)}>edit</button>{" "}
                  <button className="btn-sm" disabled={!s.open_positions} onClick={async () => {
                    if (!window.confirm(`Square off every open position of "${s.name}" now?`)) return;
                    try { const r = await api.post<{ closed: number; failed: string[] }>(`/api/algo/strategies/${s.id}/squareoff`, {}); setErr(r.failed.length ? `could not close: ${r.failed.join(", ")}` : null); } catch (e) { setErr(errMsg(e)); }
                    refresh();
                  }}>square off</button>{" "}
                  <button className="btn-sm danger" onClick={async () => {
                    if (!window.confirm(`Delete "${s.name}" and its trade history?`)) return;
                    try { await api.delete(`/api/algo/strategies/${s.id}`); } catch (e) { setErr(errMsg(e)); }
                    refresh();
                  }}>delete</button>
                </td>
              </tr>))}
            </tbody>
          </table>
        )}
      </div>
      <div className="widget widget-wide" style={{ marginBottom: 12 }}>
        <h3>Open positions</h3>
        {!st?.open.length ? <div className="empty">flat</div> : (
          <table>
            <thead><tr><th>Strategy</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Mode</th><th>Entry</th><th>LTP</th><th>Stop</th><th>Trail</th><th>Target</th><th>Unrealised</th><th>Since</th></tr></thead>
            <tbody>{st.open.map((t) => (
              <tr key={t.id}><td>{strategies.data?.strategies.find((s) => s.id === t.strategy_id)?.name ?? t.strategy_id}</td><td>{t.symbol}</td>
                <td><span className={`badge ${t.side === "BUY" ? "buy" : "sell"}`}>{t.side}</span></td><td className="mono">{t.quantity}</td><td>{t.mode}</td>
                <td className="mono">{num(t.entry_price)}</td><td className="mono">{num(t.ltp)}</td><td className="mono">{num(t.stop_loss)}</td>
                <td className="mono">{num(t.trail_stop)}</td><td className="mono">{num(t.target)}</td>
                <td className={`mono ${pnlCls(t.unrealized)}`}>{inr(t.unrealized)}</td><td className="mono">{isoIst(t.entry_at)}</td></tr>))}
            </tbody>
          </table>
        )}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(380px, 1fr))", gap: 12 }}>
        <div className="widget">
          <h3>Trades {sel ? <span className="meta">— {strategies.data?.strategies.find((s) => s.id === sel)?.name} (click the name again for all)</span> : <span className="meta">— all strategies</span>}</h3>
          <div style={{ maxHeight: 420, overflow: "auto" }}>
            {!trades.data?.trades.length ? <div className="empty">none yet</div> : (
              <table>
                <thead><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Status</th><th>Entry</th><th>Exit</th><th>Reason</th><th>Net</th></tr></thead>
                <tbody>{trades.data.trades.map((t) => (
                  <tr key={t.id} title={t.note ?? ""}><td>{t.symbol}</td><td>{t.side}</td><td className="mono">{t.quantity}</td>
                    <td>{t.status}{t.mode === "live" ? " ₹" : ""}</td>
                    <td className="mono">{num(t.entry_price)} <span className="meta">{isoIst(t.entry_at)}</span></td>
                    <td className="mono">{num(t.exit_price)} <span className="meta">{t.exit_at ? isoIst(t.exit_at) : ""}</span></td>
                    <td>{t.exit_reason ?? (t.status === "rejected" ? t.note?.slice(0, 40) : "")}</td>
                    <td className={`mono ${pnlCls(t.net_pnl)}`}>{inr(t.net_pnl)}</td></tr>))}
                </tbody>
              </table>
            )}
          </div>
        </div>
        <div className="widget">
          <h3>Runner log</h3>
          <div style={{ maxHeight: 420, overflow: "auto", fontSize: 12 }}>
            {!st?.events.length ? <div className="empty">quiet</div> : st.events.map((e, i) => (
              <div key={i} className="mono" style={{ color: e.level === "error" ? "var(--red)" : e.level === "entry" ? "var(--green)" : e.level === "exit" ? "var(--accent)" : "var(--text-dim)" }}>
                {ist(e.t)} {e.msg}
              </div>))}
          </div>
        </div>
      </div>
    </>
  );
}

function DataTab() {
  const [symbols, setSymbols] = useState("NSE:SBIN-EQ, NSE:RELIANCE-EQ, NSE:NIFTY50-INDEX");
  const [days, setDays] = useState(365);
  const dl = useMutation({
    mutationFn: () => longPost<{ results: { symbol: string; note: string | null; coverage: { first: number; last: number; rows: number } | null }[] }>(
      "/api/algo/data/download", { symbols: symbols.split(/[\s,]+/).filter(Boolean), days }),
  });
  const sync = useMutation({ mutationFn: () => longPost<{ symbols: number; ok: number; failed: string[] }>("/api/algo/sync", {}) });
  return (
    <div className="widget widget-wide">
      <h3>Market data <span className="meta">1-minute candles from Fyers, stored locally; every timeframe is built from these</span></h3>
      <p className="meta" style={{ marginTop: 0 }}>
        Backtests download whatever they're missing on their own. Every trading day after 15:45 IST the runner also downloads that day's candles for every
        symbol any saved strategy uses. Use this to pre-load history in bulk (≈4 Fyers calls per symbol-year).
      </p>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginBottom: 8 }}>
        <input style={{ width: 460, maxWidth: "100%" }} value={symbols} onChange={(e) => setSymbols(e.target.value)} placeholder="NSE:SBIN-EQ, RELIANCE, NSE:NIFTYBANK-INDEX" />
        <label className="meta">days <input type="number" min={1} max={1098} style={inp} value={days} onChange={(e) => setDays(Number(e.target.value))} /></label>
        <button className="primary" disabled={dl.isPending} onClick={() => dl.mutate()}>{dl.isPending ? "Downloading…" : "Download"}</button>
        <button disabled={sync.isPending} onClick={() => sync.mutate()} title="the last 5 days for every strategy symbol">{sync.isPending ? "Syncing…" : "Sync strategy symbols now"}</button>
      </div>
      {(dl.error || sync.error) && <div className="pnl-neg">{errMsg(dl.error ?? sync.error)}</div>}
      {sync.data && <div className="meta">synced {sync.data.ok}/{sync.data.symbols} symbols {sync.data.failed.join("; ")}</div>}
      {dl.data && (
        <table>
          <thead><tr><th>Symbol</th><th>From</th><th>To</th><th>1-min candles</th><th>Note</th></tr></thead>
          <tbody>{dl.data.results.map((r) => (
            <tr key={r.symbol}><td>{r.symbol}</td><td className="mono">{r.coverage ? ist(r.coverage.first) : "—"}</td>
              <td className="mono">{r.coverage ? ist(r.coverage.last) : "—"}</td><td className="mono">{r.coverage?.rows.toLocaleString("en-IN") ?? "—"}</td>
              <td className="meta">{r.note ?? "ok"}</td></tr>))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// page
// ---------------------------------------------------------------------------

type Tab = "builder" | "optimize" | "automations" | "data";

export default function Algo() {
  const cat = useQuery({ queryKey: ["algo", "catalog"], queryFn: () => api.get<Catalog>("/api/algo/indicators"), staleTime: Infinity });
  const qc = useQueryClient();
  const [tab, setTab] = useState<Tab>("builder");
  const [spec, setSpecRaw] = useState<Spec | null>(null);
  const [name, setName] = useState("My strategy");
  const [editing, setEditing] = useState<number | null>(null);
  const [range, setRange] = useState({ start: daysAgo(180), end: daysAgo(0) });
  const [msg, setMsg] = useState<string | null>(null);

  // Restore the draft (or start from the server defaults + the first template).
  useEffect(() => {
    if (!cat.data || spec) return;
    let draft: { spec: Spec; name: string; editing: number | null } | null = null;
    try { draft = JSON.parse(localStorage.getItem(DRAFT_KEY) ?? "null"); } catch { /* private mode */ }
    if (draft?.spec) {
      setSpecRaw({ ...clone(cat.data.defaults), ...draft.spec });
      setName(draft.name);
      setEditing(draft.editing);
    } else {
      setSpecRaw({ ...clone(cat.data.defaults), symbols: ["NSE:SBIN-EQ"], ...clone(TEMPLATES[0].spec) } as Spec);
    }
  }, [cat.data, spec]);
  const setSpec = setSpecRaw;
  useEffect(() => {
    if (!spec) return;
    try { localStorage.setItem(DRAFT_KEY, JSON.stringify({ spec, name, editing })); } catch { /* ignore */ }
  }, [spec, name, editing]);
  const [symText, setSymText] = useState<string | null>(null);

  const bt = useMutation({
    mutationFn: () => longPost<BtResult>("/api/algo/backtest", { spec: tidy(spec!), ...range }),
  });
  const save = useMutation({
    mutationFn: async (asNew: boolean) => {
      const body = { name, spec: tidy(spec!) };
      if (!asNew && editing) return api.put<Saved>(`/api/algo/strategies/${editing}`, body);
      return api.post<Saved>("/api/algo/strategies", body);
    },
    onSuccess: (s) => {
      setEditing(s.id);
      setMsg(`Saved “${s.name}” — switch it on in the Automations tab.`);
      qc.invalidateQueries({ queryKey: ["algo", "strategies"] });
    },
    onError: (e) => setMsg(errMsg(e)),
  });

  if (cat.isLoading || !spec) return <div className="empty">loading…</div>;
  if (cat.error || !cat.data) return <div className="empty">Algo API unavailable: {errMsg(cat.error)}</div>;
  const c = cat.data;
  const upd = (patch: Partial<Spec>) => setSpec({ ...spec, ...patch });
  const showLong = spec.direction !== "short";
  const showShort = spec.direction !== "long";
  const exitBlock = (key: "exit_long" | "exit_short", label: string) => {
    const g = spec[key];
    return (
      <div>
        <div className="meta" style={{ marginBottom: 4 }}>
          {label}{" "}
          {g ? <button className="btn-sm" onClick={() => upd({ [key]: null } as Partial<Spec>)}>remove</button>
            : <button className="btn-sm" onClick={() => upd({ [key]: G([]) } as Partial<Spec>)}>+ add exit conditions</button>}
          {!g && <span> — exits by stop / target / trailing / square-off only</span>}
        </div>
        {g && <GroupEditor g={g} cat={c} onChange={(x) => upd({ [key]: x } as Partial<Spec>)} />}
      </div>
    );
  };

  return (
    <div className="algo">
      <div className="dashboard-head">
        <h1 className="page-title">Algo Lab</h1>
      </div>
      <div className="tabs">
        {(["builder", "optimize", "automations", "data"] as Tab[]).map((t) => (
          <button key={t} className={`tab ${tab === t ? "active" : ""}`} onClick={() => setTab(t)}>
            {{ builder: "Builder & backtest", optimize: "Optimise", automations: "Automations", data: "Data" }[t]}
          </button>
        ))}
      </div>

      {tab === "automations" && <Automations onEdit={(s) => {
        setName(s.name); setEditing(s.id); setSymText(null);
        setSpec({ ...clone(c.defaults), ...clone(s.spec) }); setTab("builder");
      }} />}
      {tab === "data" && <DataTab />}
      {tab === "optimize" && (
        <Optimizer spec={spec} cat={c} range={range} onApply={(params) => {
          const s = clone(spec);
          Object.entries(params).forEach(([p, v]) => setPath(s, p, v));
          setSpec(s);
          setMsg("Applied — run the backtest on a different date range to check it holds up.");
          setTab("builder");
        }} />
      )}

      {tab === "builder" && (
        <>
          <div className="widget widget-wide" style={{ marginBottom: 12 }}>
            <h3>Strategy {editing ? <span className="meta">editing saved #{editing}</span> : <span className="meta">unsaved draft</span>}</h3>
            <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center", marginBottom: 10 }}>
              <label className="meta">name <input value={name} onChange={(e) => setName(e.target.value)} style={{ width: 200 }} /></label>
              <label className="meta">template{" "}
                <select value="" onChange={(e) => {
                  const t = TEMPLATES[Number(e.target.value)];
                  if (t) { setSpec({ ...clone(c.defaults), symbols: spec.symbols, ...clone(t.spec) } as Spec); setName(t.name); setEditing(null); }
                }}>
                  <option value="">— load a template —</option>
                  {TEMPLATES.map((t, i) => <option key={t.name} value={i}>{t.name}</option>)}
                </select>
              </label>
              <label className="meta">symbols{" "}
                <input style={{ width: 300 }} value={symText ?? spec.symbols.join(", ")}
                  onChange={(e) => setSymText(e.target.value)}
                  onBlur={() => { if (symText !== null) { upd({ symbols: symText.split(/[\s,]+/).filter(Boolean).map((x) => x.toUpperCase()) }); setSymText(null); } }}
                  placeholder="NSE:SBIN-EQ, RELIANCE, NSE:NIFTYBANK-INDEX" />
              </label>
              <label className="meta">timeframe{" "}
                <select value={spec.timeframe} onChange={(e) => upd({ timeframe: Number(e.target.value) })}>
                  {c.timeframes.map((t) => <option key={t} value={t}>{t >= 60 ? `${t / 60}h` : `${t}m`}</option>)}
                </select>
              </label>
              <label className="meta">direction{" "}
                <select value={spec.direction} onChange={(e) => upd({ direction: e.target.value as Spec["direction"] })}>
                  <option value="long">long only</option><option value="short">short only</option><option value="both">long + short</option>
                </select>
              </label>
            </div>
            <div style={{ display: "grid", gap: 12 }}>
              {showLong && (
                <div>
                  <div className="meta" style={{ marginBottom: 4, color: "var(--green)" }}>BUY when</div>
                  <GroupEditor g={spec.entry_long} cat={c} onChange={(g) => upd({ entry_long: g })} />
                </div>
              )}
              {showLong && exitBlock("exit_long", "Exit long when")}
              {showShort && (
                <div>
                  <div className="meta" style={{ marginBottom: 4, color: "var(--red)" }}>SELL SHORT when</div>
                  <GroupEditor g={spec.entry_short} cat={c} onChange={(g) => upd({ entry_short: g })} />
                </div>
              )}
              {showShort && exitBlock("exit_short", "Cover short when")}
            </div>
          </div>

          <div className="widget widget-wide" style={{ marginBottom: 12 }}>
            <h3>Risk, sizing & session</h3>
            <div style={{ display: "flex", gap: 14, flexWrap: "wrap", alignItems: "center" }}>
              <LevelEditor label="stop loss" v={spec.stop_loss} onChange={(l) => upd({ stop_loss: l })} />
              <LevelEditor label="target" v={spec.target} allowRR onChange={(l) => upd({ target: l })} />
              <LevelEditor label="trailing stop" v={spec.trailing} onChange={(l) => upd({ trailing: l })} />
            </div>
            <div style={{ display: "flex", gap: 14, flexWrap: "wrap", alignItems: "center", marginTop: 8 }}>
              <label className="meta">size{" "}
                <select value={spec.sizing.mode} onChange={(e) => upd({ sizing: { ...spec.sizing, mode: e.target.value } })}>
                  <option value="qty">fixed shares</option><option value="amount">₹ per trade</option><option value="risk">₹ risk per trade (needs stop)</option>
                </select>
                <input type="number" min={1} style={{ width: 90 }} value={spec.sizing.value} onChange={(e) => upd({ sizing: { ...spec.sizing, value: Number(e.target.value) } })} />
              </label>
              <label className="meta">max trades / day / symbol <input type="number" min={1} style={{ width: 48 }} value={spec.max_trades_per_day} onChange={(e) => upd({ max_trades_per_day: Number(e.target.value) })} /></label>
              {(["start", "end", "square_off"] as const).map((k) => (
                <label key={k} className="meta">{k === "start" ? "entries from" : k === "end" ? "entries until" : "square off"}{" "}
                  <input type="time" value={spec.session[k]} min="09:15" max="15:29" onChange={(e) => upd({ session: { ...spec.session, [k]: e.target.value } })} />
                </label>
              ))}
            </div>
            <div style={{ display: "flex", gap: 14, flexWrap: "wrap", alignItems: "center", marginTop: 8 }}>
              <label className="meta">capital ₹ <input type="number" min={1} style={{ width: 100 }} value={spec.capital} onChange={(e) => upd({ capital: Number(e.target.value) })} /></label>
              <label className="meta">slippage % <input type="number" step="0.01" min={0} style={inp} value={spec.costs.slippage_pct} onChange={(e) => upd({ costs: { ...spec.costs, slippage_pct: Number(e.target.value) } })} /></label>
              <label className="meta"><input type="checkbox" checked={spec.costs.charges} onChange={(e) => upd({ costs: { ...spec.costs, charges: e.target.checked } })} /> Indian intraday charges (brokerage, STT, GST, stamp…)</label>
            </div>
          </div>

          <div className="widget widget-wide" style={{ marginBottom: 12 }}>
            <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
              <label className="meta">from <input type="date" value={range.start} onChange={(e) => setRange({ ...range, start: e.target.value })} /></label>
              <label className="meta">to <input type="date" value={range.end} onChange={(e) => setRange({ ...range, end: e.target.value })} /></label>
              {[30, 90, 180, 365, 730].map((d) => (
                <button key={d} className="btn-sm" onClick={() => setRange({ start: daysAgo(d), end: daysAgo(0) })}>{d < 365 ? `${d}d` : `${d / 365}y`}</button>
              ))}
              <button className="primary" disabled={bt.isPending} onClick={() => { setMsg(null); bt.mutate(); }}>
                {bt.isPending ? "Backtesting… (downloads missing candles first)" : "▶ Run backtest"}
              </button>
              <button disabled={save.isPending} onClick={() => save.mutate(true)}>Save as new</button>
              {editing && <button disabled={save.isPending} onClick={() => save.mutate(false)}>Update saved #{editing}</button>}
            </div>
            {msg && <div className="meta" style={{ marginTop: 6 }}>{msg}</div>}
            {bt.error && <div className="pnl-neg" style={{ marginTop: 6 }}>{errMsg(bt.error)}</div>}
          </div>
          {bt.data && <Results r={bt.data} />}
        </>
      )}
    </div>
  );
}
