// Algo Lab — build indicator strategies, backtest them on Fyers candles,
// optimise their parameters, and switch them on as paper or live automations.
//
// One engine runs everywhere (app/algo/engine.py): the backtest, the
// optimiser and the live runner evaluate the same JSON spec, on completed
// bars, filling at the next bar's open. What you backtest is what runs.
// Signals come from the underlying; the instrument decides what is traded —
// the stock/index, its future, or option legs (ATM/ITM/OTM/premium strikes).
//
// Control lives here (frontend-only-control invariant): saved strategies
// start OFF and in paper; LIVE needs a real Fyers account and the typed
// confirm "LIVE".

import { useEffect, useMemo, useRef, useState } from "react";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Legend, Line, LineChart, ReferenceArea, ReferenceLine,
  ResponsiveContainer, Scatter, ScatterChart, Tooltip, XAxis, YAxis, ZAxis,
} from "recharts";
import {
  CandlestickSeries,
  ColorType,
  HistogramSeries,
  LineSeries,
  createChart,
  createSeriesMarkers,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { api } from "../api/client";
import { useSessionState } from "../router";
import { peekQuote, useQuoteTick, watchSymbol } from "../hooks/useQuotes";
import { Toggle } from "../components/common/Toggle";
import { chartHref } from "../lib/chartLink";
import { TcaPanel } from "../components/algo/TcaPanel";

// ---------------------------------------------------------------------------
// types
// ---------------------------------------------------------------------------

type Operand = {
  ind?: string;
  params?: Record<string, number | string>;
  field?: string;
  offset?: number;
  tf?: number;
  mult?: number;
  add?: number;
  value?: number;
  /** Reshape the series: its angle / slope / change … over `xn` bars. */
  xf?: string;
  xn?: number;
  /** A combined value: a <math> b. */
  math?: string;
  a?: Operand;
  b?: Operand;
};
type Cond = { left: Operand; op: string; right?: Operand; right2?: Operand; for_bars?: number; within_bars?: number; negate?: boolean };

const TRANSFORMS: [string, string, string][] = [
  ["", "value", ""],
  ["angle", "angle °", "degrees of the line's rise over N bars, measured against ATR(14): one ATR per bar = 45°, so it means the same on any symbol or zoom"],
  ["slope", "slope / bar", "average change per bar over N bars"],
  ["change", "change", "change over N bars"],
  ["pct_change", "% change", "percent change over N bars"],
  ["highest", "highest", "highest value of the last N bars"],
  ["lowest", "lowest", "lowest value of the last N bars"],
  ["sma", "average", "simple average of the last N bars"],
  ["zscore", "z-score", "standard deviations from its N-bar average"],
  ["abs", "absolute", "the value without its sign"],
  ["rank", "rank 0-100", "where it sits between its lowest (0) and highest (100) of the last N bars — IV rank = VIX on daily with this over ~200 (live keeps about a year of daily candles)"],
  ["percentile", "percentile", "% of the previous N bars that were below it — IV percentile"],
];
const MATH_OPS: [string, string][] = [["-", "−"], ["+", "+"], ["*", "×"], ["/", "÷"], ["pct_diff", "% gap"]];
type Group = { logic: "AND" | "OR"; conditions: (Cond | Group)[] };
type Level = { type: string; value: number; atr_period?: number; activate?: number } | null;
// Partial exit: close qty_pct % of the ENTRY size at this level (rr = R multiples of the stop).
type ScaleStep = { type: string; value: number; atr_period?: number; qty_pct: number };
// delta: with strike "DELTA", the strike nearest that |delta|. expiry: the leg's own expiry (calendars); absent = the instrument's.
// sl_pct / tg_pct: the leg's own stop / target, % of its entry premium (absent = off).
type Leg = { right: "CE" | "PE" | "FUT"; action: "BUY" | "SELL"; strike: string; steps: number; premium: number; lots: number; delta?: number; expiry?: "current" | "next"; sl_pct?: number; tg_pct?: number };
// A rule on the option position's Greeks / premium (per set): entry = all must hold, exit = any.
type Rule = { metric: string; op: string; value: number };
// span: F&O blocks an exchange-style SPAN + exposure estimate; absent = notional / leverage.
type Margin = { model: "span" | "leverage"; scan_pct?: number | null; exposure_pct?: number | null };
type Instrument = {
  type: "equity" | "future" | "option";
  expiry: "current" | "next";
  expiry_kind: "weekly" | "monthly";
  legs_long: Leg[];
  legs_short: Leg[];
  levels_on: "instrument" | "underlying";
  iv: { source: string; value: number };
  on_leg_stop?: "leg" | "cost" | "all";   // when a leg's own stop hits (absent = only that leg exits)
};
type Spec = {
  symbols: string[];
  timeframe: number;
  bars: { type: string; per_day: number };
  direction: "long" | "short" | "both";
  entry_long: Group;
  exit_long: Group | null;
  entry_short: Group;
  exit_short: Group | null;
  instrument: Instrument;
  stop_loss: Level;
  target: Level;
  trailing: Level;
  breakeven: Level;
  scale_out?: ScaleStep[];        // absent / empty = off (the server leaves it out when off)
  margin?: Margin;                // absent = leverage model
  position_rules?: { entry?: Rule[]; exit?: Rule[] };   // absent = none
  mtm: { stop: number | null; target: number | null; trail_start: number | null; trail_gap: number | null; stop_pct?: number | null; target_pct?: number | null };
  daily: { max_loss: number | null; max_profit: number | null };
  session: { start: string | null; end: string | null; square_off: string | null; days?: number[] | null };
  entry_order: { type: string; offset_pct: number; valid_bars: number };
  max_trades_per_day: number;
  cooldown_bars: number;
  max_bars: number | null;
  sizing: { mode: string; value: number };
  portfolio: { capital: number; leverage: number; max_positions: number; compounding: boolean; max_position_pct?: number | null };
  costs: { slippage_pct: number; charges: boolean };
};
type IndicatorDef = { name: string; params: Record<string, number | string>; outputs: string[]; group: string; about?: string };
type Catalog = {
  indicators: IndicatorDef[];
  operators: string[];
  timeframes: number[];
  cond_timeframes: number[];
  sources: string[];
  metrics: string[];
  sizing: string[];
  defaults: Spec;
  max_range_days: number;
};
type BtLeg = { label: string; side: string; qty: number; entry: number; exit: number; reason?: string };
// Option position at entry, per set: delta / gamma in lots, theta / vega / credit in ₹, iv %, dte days.
type EntryGreeks = { delta?: number; gamma?: number; theta?: number; vega?: number; credit?: number; iv?: number; dte?: number };
type BtTrade = {
  symbol: string; instrument: string; side: string; qty: number; lots: number; entry_t: number; entry: number;
  exit_t: number; exit: number; u_entry: number; u_exit: number; reason: string; gross: number; charges: number;
  net: number; bars: number; margin: number; legs: BtLeg[];
  mae_pct: number | null; mfe_pct: number | null; r: number | null; minutes: number; greeks?: EntryGreeks;
};
type Summary = Record<string, number | null>;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Stats = Record<string, any> & {
  equity: [number, number][]; daily: [number, number][]; monthly: Record<string, number>;
  weekday: Record<string, number>; skipped: Record<string, number>;
  in_sample?: Summary; out_of_sample?: Summary; oos_from?: number;
};
type MonteCarlo = { runs: number; trades: number; net_p5: number; net_p50: number; net_p95: number; dd_pct_p50: number; dd_pct_p95: number; prob_loss_pct: number; prob_half_capital_pct: number };
type BtResult = {
  stats: Stats;
  per_symbol: Record<string, { trades: number; win_rate: number; net_pnl: number; profit_factor: number | null; max_drawdown: number; bars: number; buy_hold_pct: number | null }>;
  monte_carlo: MonteCarlo | null;
  by_reason: Record<string, { count: number; net: number }>;
  trades: BtTrade[];
  trades_total: number;
  notes: { symbol: string; note: string }[];
  elapsed_s: number;
  chart: { symbol: string; candles: number[][]; trades: BtTrade[]; overlays?: { label: string; price: boolean; values: (number | null)[] }[] };
  flow_source?: Record<string, number>;
  spec: Spec;
};
type OptRow = { params: Record<string, number>; trades: number; win_rate: number; net_pnl: number; profit_factor: number | null; sharpe: number | null; max_drawdown: number; return_pct: number; expectancy: number; t_stat: number | null; oos: Summary | null };
type Overfitting = {
  note?: string;
  days?: number;
  deflated_sharpe?: { dsr: number | null; n_trials: number; sharpe_per_period: number; benchmark_sharpe: number } | null;
  pbo?: { pbo: number; splits: number; n_configs: number } | null;
} | null;
type HeatCell = { params: Record<string, number>; value: number | null; trades: number };
type OptResult = { combos: number; ranked: OptRow[]; too_few_trades: number; metric: string; notes: { symbol: string; note: string }[]; elapsed_s: number; oos_from: number | null; overfitting?: Overfitting; all?: HeatCell[] };
type WfFold = { is_from: number; oos_from: number; oos_to: number; params: Record<string, number> | null; is: Summary | null; is_value?: number | null; oos: Summary | null };
type WfResult = {
  combos: number; metric: string; windows: number; anchored: boolean; folds: WfFold[]; wfe_pct: number | null; elapsed_s: number;
  oos: { trades: number; win_rate: number; net_pnl: number; profit_factor: number | null; sharpe: number | null; max_drawdown: number; max_drawdown_pct: number; equity: [number, number][] };
};
const isWf = (r: OptResult | WfResult): r is WfResult => (r as WfResult).folds !== undefined;

// One line under the optimiser table: is the winner better than the best of
// N lucky draws (Deflated Sharpe), and how often does the in-sample winner
// fall below the median out of sample (PBO)?
export function overfittingLine(o: Overfitting | undefined): string | null {
  if (!o) return null;
  if (o.note) return `Overfitting check: ${o.note}.`;
  const parts: string[] = [];
  const d = o.deflated_sharpe;
  if (d && d.dsr !== null && d.dsr !== undefined) {
    parts.push(`Deflated Sharpe ${(d.dsr * 100).toFixed(0)}% after ${d.n_trials} trials${d.dsr < 0.95 ? " — the top row may be luck" : ""}`);
  }
  if (o.pbo) {
    parts.push(`PBO ${(o.pbo.pbo * 100).toFixed(0)}%${o.pbo.pbo > 0.5 ? " — the in-sample winner usually loses out of sample" : ""}`);
  }
  return parts.length ? `Overfitting check: ${parts.join(" · ")}.` : null;
}
type Saved = { id: number; name: string; spec: Spec; enabled: boolean; mode: "paper" | "live"; account_id: number | null; closed_trades: number; realized_pnl: number; open_positions: number; version: number; versions: number };
type Version = { version: number; spec: Spec; note: string | null; active: boolean; created_at: string | null; closed_trades: number; realized_pnl: number };
type LiveLeg = { symbol: string; label?: string; act: number; qty: number; entry: number; exit?: number; ltp?: number | null };
type LiveTrade = {
  id: number; strategy_id: number; symbol: string; instrument: string | null; side: string; quantity: number; mode: string; status: string;
  entry_at: string; entry_price: number | null; stop_loss: number | null; target: number | null; trail_stop: number | null;
  exit_at: string | null; exit_price: number | null; exit_reason: string | null; net_pnl: number | null; charges: number | null;
  note: string | null; ltp?: number | null; unrealized?: number | null; legs: LiveLeg[] | null; be_on?: boolean | null; ref?: string;
  version?: number | null;
};
type RunnerStatus = {
  running: boolean; last_tick: number | null; last_sync: { t: number; symbols: number; ok: number; failed: string[] } | null;
  events: { t: number; level: string; msg: string; strategy_id?: number }[]; open: LiveTrade[];
  ticks?: { seen: number; last_exit_check: number | null; watching: number };
};
type Account = { id: number; name: string; broker: string; paper_mode: boolean; enabled: boolean };
type InstInfo = { symbol: string; name: string; fno: boolean; lot: number | null; weekly: boolean; futures: { symbol: string; expiry: number }[]; coverage: { first: number; last: number; rows: number } | null };
type Hit = { symbol: string; short_name: string; segment: string; exchange: string; display: string; lot_size: number };

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

const IST_S = 19800;
const isGroup = (c: Cond | Group): c is Group => (c as Group).conditions !== undefined;
const I = (ind: string, params: Record<string, number | string> = {}, field?: string, tf?: number): Operand => ({ ind, params, field, offset: 0, ...(tf ? { tf } : {}) });
const N = (value: number): Operand => ({ value });
const A = (o: Operand, ago: number): Operand => ({ ...o, offset: ago });
const C = (left: Operand, op: string, right?: Operand): Cond => ({ left, op, right });
const G = (conditions: (Cond | Group)[], logic: "AND" | "OR" = "AND"): Group => ({ logic, conditions });
const L = (right: Leg["right"], action: "BUY" | "SELL", strike = "ATM", steps = 0, lots = 1, premium = 100): Leg => ({ right, action, strike, steps, premium, lots });
// A leg at a target |delta| (0.2 = the 20-delta strike), optionally on its own expiry.
const D = (right: "CE" | "PE", action: "BUY" | "SELL", delta: number, lots = 1, expiry?: Leg["expiry"]): Leg => ({ ...L(right, action, "DELTA", 0, lots), delta, ...(expiry ? { expiry } : {}) });
const clone = <T,>(x: T): T => JSON.parse(JSON.stringify(x)) as T;
const inr = (v: number | null | undefined, dp = 0) =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : `₹${v.toLocaleString("en-IN", { maximumFractionDigits: dp, minimumFractionDigits: dp })}`;
const num = (v: number | null | undefined, dp = 2) => (v === null || v === undefined ? "—" : v.toFixed(dp));
const pnlCls = (v: number | null | undefined) => (v === null || v === undefined || v === 0 ? "" : v > 0 ? "pnl-pos" : "pnl-neg");
const ist = (epoch: number) =>
  new Date(epoch * 1000).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short", year: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
const isoIst = (iso: string | null) => (iso ? ist(Date.parse(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z") / 1000) : "—");
// The trade on the chart, 15-minute candles at its entry: an option trade
// opens underlying | option side by side with BUY / SELL arrows on the
// option leg; an equity trade opens its one chart with the arrows.
const tradeChart = (t: LiveTrade): string => {
  const leg = t.legs?.[0];
  const legOf = (l: LiveLeg) => ({ side: (l.act > 0 ? "BUY" : "SELL") as "BUY" | "SELL", qty: l.qty, price: l.entry, exitPrice: l.exit ?? null, exitTime: t.exit_at });
  if (leg && leg.symbol && leg.symbol !== t.symbol) {
    return chartHref(t.symbol, t.entry_at, null, { iv: "15", pair: { symbol: leg.symbol, trade: legOf(leg) } });
  }
  const own = t.entry_price ? { side: (t.side === "SELL" ? "SELL" : "BUY") as "BUY" | "SELL", qty: t.quantity, price: t.entry_price, exitPrice: t.exit_price, exitTime: t.exit_at } : null;
  return chartHref(t.symbol, t.entry_at, own, { iv: "15" });
};
// Panels the operator drags taller / shorter (native corner grip).
const grow = (height?: number): React.CSSProperties => ({ resize: "vertical", overflow: "auto", minHeight: 60, ...(height ? { height } : {}) });
const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
// The server takes 5–80% out-of-sample (0 = off); round a smaller number up instead of failing the run.
const oosParam = (pct: number) => (pct > 0 ? Math.min(80, Math.max(5, pct)) : undefined);
const draftKey = (sp: Spec, nm: string) => JSON.stringify([nm, sp]);
const daysAgo = (n: number) => new Date(Date.now() - n * 86400000).toISOString().slice(0, 10);
const shortSym = (s: string) => s.replace(/^NSE:/, "").replace(/-EQ$/, "").replace(/-INDEX$/, "");
type SizingPreset = { name: string; hint: string; riskPct: number; equityPct: number; maxPositions: number; capPct: number; dailyLossPct: number; compound: boolean };
export const SIZING_PRESETS: SizingPreset[] = [
  { name: "Conservative", hint: "0.5% risk per trade (or 10% of equity without a stop), 3 open, ≤ 25% per position, stop the day at −1.5%", riskPct: 0.5, equityPct: 10, maxPositions: 3, capPct: 25, dailyLossPct: 1.5, compound: false },
  { name: "Balanced", hint: "1% risk per trade (or 20% of equity), 5 open, ≤ 40% per position, stop the day at −3%", riskPct: 1, equityPct: 20, maxPositions: 5, capPct: 40, dailyLossPct: 3, compound: true },
  { name: "Aggressive", hint: "2% risk per trade (or 35% of equity), 10 open, ≤ 60% per position, stop the day at −5%", riskPct: 2, equityPct: 35, maxPositions: 10, capPct: 60, dailyLossPct: 5, compound: true },
];
const SIZE_SHORT: Record<string, string> = { qty: "fixed shares", lots: "fixed lots", amount: "₹ amount", pct_equity: "% of equity", risk: "₹ risk", risk_pct: "% risk" };
const SIZE_UNIT: Record<string, string> = { qty: "shares", lots: "lots", amount: "₹ per trade", pct_equity: "% of equity per trade", risk: "₹ lost if the stop hits", risk_pct: "% of equity lost if the stop hits" };

/** The whole sizing block in one plain sentence. */
export function sizingSummary(spec: Pick<Spec, "sizing" | "portfolio" | "daily">, lot: number | null): string {
  const { mode, value: v } = spec.sizing;
  const pf = spec.portfolio;
  const each =
    mode === "qty" ? `${v} shares` :
    mode === "lots" ? `${v} lot${v === 1 ? "" : "s"}${lot ? ` (${v * lot} qty)` : ""}` :
    mode === "amount" ? `${inr(v)} of margin` :
    mode === "pct_equity" ? `${v}% of equity (${inr((pf.capital * v) / 100)} to start)` :
    mode === "risk" ? `sized so the stop loses ${inr(v)}` :
    `sized so the stop loses ${v}% of equity (${inr((pf.capital * v) / 100)} to start)`;
  const parts = [
    `Each trade: ${each}.`,
    `Up to ${pf.max_positions} open at once from ${inr(pf.capital)}${pf.leverage !== 1 ? ` at ${pf.leverage}× leverage` : ""}.`,
    pf.max_position_pct ? `No position uses more than ${pf.max_position_pct}% (${inr((pf.capital * pf.max_position_pct) / 100)}).` : "",
    spec.daily.max_loss ? `Trading stops for the day at −${inr(spec.daily.max_loss)}.` : "",
    pf.compounding ? "Profits grow later trade sizes." : "Sizes stay on the starting capital.",
  ];
  return parts.filter(Boolean).join(" ");
}

const tfLabel = (t: number) => (t === 1440 ? "daily" : t >= 60 ? `${t / 60}h` : `${t}m`);
const SECTIONS = ["instrument", "session", "sizing", "portfolio", "mtm", "daily", "costs", "entry_order", "bars"] as const;
const barLabel = (sp: Spec) => (!sp.bars || sp.bars.type === "time" ? tfLabel(sp.timeframe) : `${sp.bars.type} ~${sp.bars.per_day}/day`);
const STRIKES = ["ATM", ...Array.from({ length: 20 }, (_, k) => `ITM${k + 1}`), ...Array.from({ length: 20 }, (_, k) => `OTM${k + 1}`), "PREMIUM", "DELTA"];
const strikeKey = (lg: Leg) => (lg.strike === "ITM" || lg.strike === "OTM" ? `${lg.strike}${Math.max(1, lg.steps)}` : lg.strike);
const SESSION_DEFAULT = { start: "09:20", end: "15:00", square_off: "15:15" } as const;

// Merge a partial spec (template, import, saved) onto the defaults section by section.
function withDefaults(defaults: Spec, part: Partial<Spec>): Spec {
  const s = clone(defaults) as unknown as Record<string, unknown>;
  Object.entries(clone(part)).forEach(([k, v]) => {
    if ((SECTIONS as readonly string[]).includes(k) && v && typeof v === "object") s[k] = { ...(s[k] as object), ...(v as object) };
    else s[k] = v;
  });
  return s as unknown as Spec;
}

// Backtests can run for minutes when history has to be downloaded first —
// the shared client's 30s ceiling would abort them.
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
  keys.slice(0, -1).forEach((k, i) => {
    if (o[k] === undefined || o[k] === null) o[k] = /^\d+$/.test(keys[i + 1]) ? [] : {};   // scale_out.0.value: a list
    o = o[k] as Record<string, unknown>;
  });
  o[keys[keys.length - 1]] = value;
}

function download(name: string, text: string, type = "application/json") {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type }));
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
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
          if (o.mult !== undefined && o.mult !== 1) out.push({ path: `${p}.${side}.mult`, label: `${label} #${i + 1} ${side}: × multiplier`, value: o.mult });
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
  (["stop_loss", "target", "trailing", "breakeven"] as const).forEach((k) => {
    const lv = spec[k];
    if (lv) out.push({ path: `${k}.value`, label: `${k.replace("_", " ")} (${lv.type})`, value: lv.value });
  });
  if (spec.trailing?.activate) out.push({ path: "trailing.activate", label: "trailing activation", value: spec.trailing.activate });
  (spec.scale_out ?? []).forEach((st, i) => {
    out.push({ path: `scale_out.${i}.value`, label: `scale-out ${i + 1} level (${st.type})`, value: st.value });
    out.push({ path: `scale_out.${i}.qty_pct`, label: `scale-out ${i + 1} % closed`, value: st.qty_pct });
  });
  (["stop", "target", "trail_start", "trail_gap"] as const).forEach((k) => {
    const v = spec.mtm[k];
    if (v) out.push({ path: `mtm.${k}`, label: `MTM ${k.replace("_", " ")} ₹`, value: v });
  });
  (["stop_pct", "target_pct"] as const).forEach((k) => {
    const v = spec.mtm[k];
    if (v) out.push({ path: `mtm.${k}`, label: `MTM ${k === "stop_pct" ? "stop" : "target"} % of premium`, value: v });
  });
  (["entry", "exit"] as const).forEach((k) => (spec.position_rules?.[k] ?? []).forEach((r, i) =>
    out.push({ path: `position_rules.${k}.${i}.value`, label: `${k} rule ${i + 1}: ${r.metric} ${r.op}`, value: r.value })));
  if (spec.instrument.type === "option") {
    (["legs_long", "legs_short"] as const).forEach((side) => spec.instrument[side].forEach((lg, i) => {
      if (lg.strike === "ITM" || lg.strike === "OTM") out.push({ path: `instrument.${side}.${i}.steps`, label: `${side} leg ${i + 1} ${lg.right} strikes ${lg.strike}`, value: lg.steps });
      if (lg.strike === "PREMIUM") out.push({ path: `instrument.${side}.${i}.premium`, label: `${side} leg ${i + 1} ${lg.right} target premium`, value: lg.premium });
      if (lg.strike === "DELTA") out.push({ path: `instrument.${side}.${i}.delta`, label: `${side} leg ${i + 1} ${lg.action} ${lg.right} target delta`, value: lg.delta ?? 0.3 });
      if (lg.sl_pct) out.push({ path: `instrument.${side}.${i}.sl_pct`, label: `${side} leg ${i + 1} ${lg.right} stop % of premium`, value: lg.sl_pct });
      if (lg.tg_pct) out.push({ path: `instrument.${side}.${i}.tg_pct`, label: `${side} leg ${i + 1} ${lg.right} target % of premium`, value: lg.tg_pct });
      if (spec.instrument[side].length > 1) out.push({ path: `instrument.${side}.${i}.lots`, label: `${side} leg ${i + 1} ${lg.action} ${lg.right} lots (0 = off: hedged vs unhedged)`, value: lg.lots });
    }));
  }
  out.push({ path: "max_trades_per_day", label: "max trades / day", value: spec.max_trades_per_day });
  out.push({ path: "cooldown_bars", label: "cooldown bars", value: spec.cooldown_bars });
  out.push({ path: "sizing.value", label: `size (${spec.sizing.mode})`, value: spec.sizing.value });
  return out;
}

// Make sure every indicator operand carries a params object (the optimiser
// writes into it).
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

type Template = { name: string; symbols?: string[]; spec: Partial<Spec> };
const ST = (f: "crosses_above" | "crosses_below") => C(I("SUPERTREND", { period: 10, multiplier: 3 }, "direction"), f, N(0));
export const TEMPLATES: Template[] = [
  { name: "Equity · EMA 9/21 crossover", spec: {
    direction: "both", instrument: { type: "equity" } as Instrument,
    entry_long: G([C(I("EMA", { period: 9 }), "crosses_above", I("EMA", { period: 21 }))]),
    entry_short: G([C(I("EMA", { period: 9 }), "crosses_below", I("EMA", { period: 21 }))]),
    stop_loss: { type: "atr", value: 1.5, atr_period: 14 }, target: { type: "rr", value: 2 }, trailing: null,
    sizing: { mode: "pct_equity", value: 25 } } },
  { name: "Equity · VWAP + RSI momentum (60m trend filter)", spec: {
    direction: "both", instrument: { type: "equity" } as Instrument,
    entry_long: G([C(I("PRICE"), "crosses_above", I("VWAP")), C(I("RSI", { period: 14 }), ">", N(55)), C(I("PRICE"), ">", I("EMA", { period: 20 }, undefined, 60))]),
    entry_short: G([C(I("PRICE"), "crosses_below", I("VWAP")), C(I("RSI", { period: 14 }), "<", N(45)), C(I("PRICE"), "<", I("EMA", { period: 20 }, undefined, 60))]),
    stop_loss: { type: "atr", value: 1.2, atr_period: 14 }, target: { type: "rr", value: 1.5 }, breakeven: { type: "pct", value: 0.4 },
    sizing: { mode: "risk_pct", value: 0.5 } } },
  { name: "Equity · Opening range breakout (15m)", spec: {
    direction: "both", timeframe: 5, instrument: { type: "equity" } as Instrument,
    session: { start: "09:30", end: "13:00", square_off: "15:10" },
    entry_long: G([C(I("PRICE"), "crosses_above", I("ORB", { minutes: 15 }, "high"))]),
    entry_short: G([C(I("PRICE"), "crosses_below", I("ORB", { minutes: 15 }, "low"))]),
    max_trades_per_day: 1, stop_loss: { type: "pct", value: 0.6 }, target: { type: "rr", value: 2 } } },
  { name: "Equity · Bollinger mean reversion", spec: {
    direction: "long", instrument: { type: "equity" } as Instrument,
    entry_long: G([C(I("PRICE"), "crosses_above", I("BBANDS", { period: 20, multiplier: 2 }, "lower")), C(I("RSI", { period: 14 }, undefined, 1440), ">", N(40))]),
    exit_long: G([C(I("PRICE"), "crosses_above", I("BBANDS", { period: 20, multiplier: 2 }, "middle"))]),
    stop_loss: { type: "pct", value: 1 }, target: null } },
  { name: "Equity · Gap-up continuation (open > prev high × 1.005)", spec: {
    direction: "long", instrument: { type: "equity" } as Instrument, session: { start: "09:20", end: "10:30", square_off: "15:15" },
    entry_long: G([C(I("DAILY", {}, "day_open"), ">", { ...I("DAILY", {}, "prev_high"), mult: 1.005 }), C(I("PRICE"), ">", A(I("DAILY", {}, "day_high"), 1)), C(I("VOLUME_SMA", { period: 20 }), "rising", N(2))], "AND"),
    max_trades_per_day: 1, stop_loss: { type: "pct", value: 1 }, trailing: { type: "pct", value: 0.8, activate: 0.8 } } },
  { name: "NIFTY options · Supertrend → buy ATM CE / PE", symbols: ["NSE:NIFTY50-INDEX"], spec: {
    direction: "both", timeframe: 5,
    instrument: { type: "option", expiry: "current", expiry_kind: "weekly", legs_long: [L("CE", "BUY")], legs_short: [L("PE", "BUY")], levels_on: "instrument", iv: { source: "auto", value: 15 } },
    entry_long: G([ST("crosses_above")]), entry_short: G([ST("crosses_below")]),
    stop_loss: { type: "pct", value: 30 }, target: { type: "pct", value: 60 }, trailing: { type: "pct", value: 20, activate: 30 },
    sizing: { mode: "lots", value: 1 }, daily: { max_loss: 5000, max_profit: null } } },
  { name: "NIFTY options · 9:20 short straddle (MTM ₹ stop/target)", symbols: ["NSE:NIFTY50-INDEX"], spec: {
    direction: "long", timeframe: 5, session: { start: "09:20", end: "09:25", square_off: "15:15" },
    instrument: { type: "option", expiry: "current", expiry_kind: "weekly", legs_long: [L("CE", "SELL"), L("PE", "SELL")], legs_short: [L("PE", "BUY")], levels_on: "underlying", iv: { source: "auto", value: 15 } },
    entry_long: G([C(I("TIME", {}, "hhmm"), ">=", N(920))]), max_trades_per_day: 1,
    stop_loss: null, target: null, trailing: null, mtm: { stop: 3000, target: 4000, trail_start: 2000, trail_gap: 1000 },
    sizing: { mode: "lots", value: 1 }, portfolio: { capital: 300000, leverage: 8, max_positions: 1, compounding: false } } },
  { name: "NIFTY options · expiry-day OTM strangle sell (DTE < 1)", symbols: ["NSE:NIFTY50-INDEX"], spec: {
    direction: "long", timeframe: 5, session: { start: "09:30", end: "09:35", square_off: "15:15" },
    instrument: { type: "option", expiry: "current", expiry_kind: "weekly", legs_long: [L("CE", "SELL", "OTM", 3), L("PE", "SELL", "OTM", 3)], legs_short: [L("PE", "BUY")], levels_on: "underlying", iv: { source: "auto", value: 15 } },
    entry_long: G([C(I("TIME", {}, "hhmm"), ">=", N(930)), C(I("DTE"), "<", N(1))]), max_trades_per_day: 1,
    stop_loss: null, target: null, mtm: { stop: 2500, target: null, trail_start: 1500, trail_gap: 800 },
    sizing: { mode: "lots", value: 1 }, portfolio: { capital: 300000, leverage: 8, max_positions: 1, compounding: false } } },
  { name: "NIFTY options · 20-delta short strangle, SPAN margin (Tue–Thu)", symbols: ["NSE:NIFTY50-INDEX"], spec: {
    direction: "long", timeframe: 5, session: { start: "09:30", end: "09:35", square_off: "15:15", days: [2, 3, 4] },
    instrument: { type: "option", expiry: "current", expiry_kind: "weekly", legs_long: [D("CE", "SELL", 0.2), D("PE", "SELL", 0.2)], legs_short: [L("PE", "BUY")], levels_on: "underlying", iv: { source: "auto", value: 15 } },
    entry_long: G([C(I("TIME", {}, "hhmm"), ">=", N(930))]), max_trades_per_day: 1,
    stop_loss: null, target: null, mtm: { stop: 4000, target: null, trail_start: 2500, trail_gap: 1200 },
    margin: { model: "span" }, sizing: { mode: "lots", value: 1 }, portfolio: { capital: 300000, leverage: 1, max_positions: 1, compounding: false } } },
  { name: "NIFTY options · 16Δ iron condor (hedged: a fraction of the strangle's margin)", symbols: ["NSE:NIFTY50-INDEX"], spec: {
    direction: "long", timeframe: 5, session: { start: "09:30", end: "09:35", square_off: "15:15" },
    instrument: { type: "option", expiry: "current", expiry_kind: "weekly", legs_long: [D("CE", "SELL", 0.16), D("PE", "SELL", 0.16), D("CE", "BUY", 0.05), D("PE", "BUY", 0.05)], legs_short: [L("PE", "BUY")], levels_on: "underlying", iv: { source: "auto", value: 15 } },
    entry_long: G([C(I("TIME", {}, "hhmm"), ">=", N(930))]), max_trades_per_day: 1,
    stop_loss: null, target: null, mtm: { stop: 3000, target: 2500, trail_start: null, trail_gap: null },
    position_rules: { entry: [{ metric: "abs_delta", op: "<=", value: 0.1 }], exit: [{ metric: "abs_delta", op: ">", value: 0.35 }] },
    margin: { model: "span" }, sizing: { mode: "pct_equity", value: 90 }, portfolio: { capital: 300000, leverage: 1, max_positions: 1, compounding: true } } },
  { name: "NIFTY options · 20Δ strangle, 30% stop per leg → other leg to cost, keep half the credit", symbols: ["NSE:NIFTY50-INDEX"], spec: {
    direction: "long", timeframe: 5, session: { start: "09:30", end: "09:35", square_off: "15:15" },
    instrument: { type: "option", expiry: "current", expiry_kind: "weekly", on_leg_stop: "cost", levels_on: "underlying", iv: { source: "auto", value: 15 },
      legs_long: [{ ...D("CE", "SELL", 0.2), sl_pct: 30 }, { ...D("PE", "SELL", 0.2), sl_pct: 30 }], legs_short: [L("PE", "BUY")] },
    entry_long: G([C(I("TIME", {}, "hhmm"), ">=", N(930)), C(I("VIX"), ">", N(11))]), max_trades_per_day: 1,
    stop_loss: null, target: null, mtm: { stop: null, target: null, trail_start: null, trail_gap: null, target_pct: 50, stop_pct: 100 },
    position_rules: { entry: [{ metric: "credit", op: ">=", value: 1500 }] },
    margin: { model: "span" }, sizing: { mode: "lots", value: 1 }, portfolio: { capital: 300000, leverage: 1, max_positions: 1, compounding: false } } },
  { name: "BANKNIFTY futures · EMA cross + daily trend", symbols: ["NSE:NIFTYBANK-INDEX"], spec: {
    direction: "both", timeframe: 15,
    instrument: { type: "future", expiry: "current", expiry_kind: "monthly", legs_long: [], legs_short: [], levels_on: "instrument", iv: { source: "auto", value: 15 } },
    entry_long: G([C(I("EMA", { period: 9 }), "crosses_above", I("EMA", { period: 21 })), C(I("PRICE"), ">", I("EMA", { period: 20 }, undefined, 1440))]),
    entry_short: G([C(I("EMA", { period: 9 }), "crosses_below", I("EMA", { period: 21 })), C(I("PRICE"), "<", I("EMA", { period: 20 }, undefined, 1440))]),
    stop_loss: { type: "atr", value: 2, atr_period: 14 }, target: { type: "rr", value: 2 }, breakeven: { type: "atr", value: 1, atr_period: 14 },
    sizing: { mode: "lots", value: 1 }, portfolio: { capital: 300000, leverage: 6, max_positions: 1, compounding: true } } },
  { name: "Stock options · CPR breakout → buy 1-ITM", symbols: ["NSE:RELIANCE-EQ", "NSE:HDFCBANK-EQ"], spec: {
    direction: "both", timeframe: 15,
    instrument: { type: "option", expiry: "current", expiry_kind: "monthly", legs_long: [L("CE", "BUY", "ITM", 1)], legs_short: [L("PE", "BUY", "ITM", 1)], levels_on: "underlying", iv: { source: "hv", value: 25 } },
    entry_long: G([C(I("PRICE"), "crosses_above", I("DAILY", {}, "tc")), C(I("PRICE"), ">", I("DAILY", {}, "pivot"))]),
    entry_short: G([C(I("PRICE"), "crosses_below", I("DAILY", {}, "bc")), C(I("PRICE"), "<", I("DAILY", {}, "pivot"))]),
    max_trades_per_day: 1, stop_loss: { type: "pct", value: 0.7 }, target: { type: "rr", value: 2 },
    sizing: { mode: "lots", value: 1 }, portfolio: { capital: 300000, leverage: 1, max_positions: 2, compounding: true } } },
  // ---- trend / breakout ----
  { name: "Equity · Turtle 20/10 Donchian breakout (15m)", spec: {
    direction: "both", timeframe: 15, instrument: { type: "equity" } as Instrument,
    entry_long: G([C(I("PRICE"), "crosses_above", A(I("DONCHIAN", { period: 20 }, "upper"), 1))]),
    exit_long: G([C(I("PRICE"), "crosses_below", A(I("DONCHIAN", { period: 10 }, "lower"), 1))]),
    entry_short: G([C(I("PRICE"), "crosses_below", A(I("DONCHIAN", { period: 20 }, "lower"), 1))]),
    exit_short: G([C(I("PRICE"), "crosses_above", A(I("DONCHIAN", { period: 10 }, "upper"), 1))]),
    stop_loss: { type: "atr", value: 2, atr_period: 14 }, target: null, sizing: { mode: "risk_pct", value: 0.5 } } },
  { name: "Equity · Bollinger squeeze breakout", spec: {
    direction: "both", instrument: { type: "equity" } as Instrument,
    entry_long: G([C(A(I("BBANDS", { period: 20, multiplier: 2 }, "upper"), 1), "<", A(I("KELTNER", { period: 20, multiplier: 1.5 }, "upper"), 1)),
                   C(I("PRICE"), "crosses_above", I("BBANDS", { period: 20, multiplier: 2 }, "upper"))]),
    entry_short: G([C(A(I("BBANDS", { period: 20, multiplier: 2 }, "lower"), 1), ">", A(I("KELTNER", { period: 20, multiplier: 1.5 }, "lower"), 1)),
                    C(I("PRICE"), "crosses_below", I("BBANDS", { period: 20, multiplier: 2 }, "lower"))]),
    stop_loss: { type: "atr", value: 1.5, atr_period: 14 }, target: { type: "rr", value: 2 } } },
  { name: "Equity · Ichimoku TK cross above the cloud (15m)", spec: {
    direction: "both", timeframe: 15, instrument: { type: "equity" } as Instrument,
    entry_long: G([C(I("ICHIMOKU", {}, "tenkan"), "crosses_above", I("ICHIMOKU", {}, "kijun")),
                   C(I("PRICE"), ">", I("ICHIMOKU", {}, "span_a")), C(I("PRICE"), ">", I("ICHIMOKU", {}, "span_b"))]),
    entry_short: G([C(I("ICHIMOKU", {}, "tenkan"), "crosses_below", I("ICHIMOKU", {}, "kijun")),
                    C(I("PRICE"), "<", I("ICHIMOKU", {}, "span_a")), C(I("PRICE"), "<", I("ICHIMOKU", {}, "span_b"))]),
    stop_loss: { type: "atr", value: 2, atr_period: 14 }, trailing: { type: "atr", value: 2.5, atr_period: 14, activate: 0 }, target: null } },
  { name: "Equity · Heikin-Ashi trend + ADX", spec: {
    direction: "both", instrument: { type: "equity" } as Instrument,
    entry_long: G([C(I("HEIKIN_ASHI", {}, "close"), "crosses_above", I("HEIKIN_ASHI", {}, "open")), C(I("ADX", { period: 14 }, "adx"), ">", N(20))]),
    exit_long: G([C(I("HEIKIN_ASHI", {}, "close"), "crosses_below", I("HEIKIN_ASHI", {}, "open"))]),
    entry_short: G([C(I("HEIKIN_ASHI", {}, "close"), "crosses_below", I("HEIKIN_ASHI", {}, "open")), C(I("ADX", { period: 14 }, "adx"), ">", N(20))]),
    exit_short: G([C(I("HEIKIN_ASHI", {}, "close"), "crosses_above", I("HEIKIN_ASHI", {}, "open"))]),
    stop_loss: { type: "atr", value: 1.5, atr_period: 14 }, target: null } },
  { name: "Equity · Linear-regression slope trend + RSI", spec: {
    direction: "both", instrument: { type: "equity" } as Instrument,
    entry_long: G([C(I("LINREG", { period: 20 }, "slope_pct"), "crosses_above", N(0.02)), C(I("RSI", { period: 14 }), ">", N(50))]),
    entry_short: G([C(I("LINREG", { period: 20 }, "slope_pct"), "crosses_below", N(-0.02)), C(I("RSI", { period: 14 }), "<", N(50))]),
    stop_loss: { type: "pct", value: 0.6 }, trailing: { type: "pct", value: 0.6, activate: 0.4 }, target: null } },
  { name: "Equity · PSAR flip with ADX filter", spec: {
    direction: "both", instrument: { type: "equity" } as Instrument,
    entry_long: G([C(I("PRICE"), "crosses_above", I("PSAR")), C(I("ADX", { period: 14 }, "adx"), ">", N(20))]),
    exit_long: G([C(I("PRICE"), "crosses_below", I("PSAR"))]),
    entry_short: G([C(I("PRICE"), "crosses_below", I("PSAR")), C(I("ADX", { period: 14 }, "adx"), ">", N(20))]),
    exit_short: G([C(I("PRICE"), "crosses_above", I("PSAR"))]),
    stop_loss: { type: "atr", value: 2, atr_period: 14 }, target: null } },
  { name: "Equity · NR7 breakout (15m)", spec: {
    direction: "both", timeframe: 15, instrument: { type: "equity" } as Instrument, max_trades_per_day: 1,
    entry_long: G([C(A(I("CANDLE", {}, "nr7"), 1), "==", N(1)), C(I("PRICE"), ">", A(I("PRICE", { source: "high" }), 1))]),
    entry_short: G([C(A(I("CANDLE", {}, "nr7"), 1), "==", N(1)), C(I("PRICE"), "<", A(I("PRICE", { source: "low" }), 1))]),
    stop_loss: { type: "atr", value: 1, atr_period: 14 }, target: { type: "rr", value: 2 } } },
  // ---- mean reversion / gaps ----
  { name: "Equity · Z-score mean reversion (±2σ)", spec: {
    direction: "both", instrument: { type: "equity" } as Instrument, max_bars: 24,
    entry_long: G([C(I("ZSCORE", { period: 20 }), "crosses_above", N(-2))]), exit_long: G([C(I("ZSCORE", { period: 20 }), "crosses_above", N(0))]),
    entry_short: G([C(I("ZSCORE", { period: 20 }), "crosses_below", N(2))]), exit_short: G([C(I("ZSCORE", { period: 20 }), "crosses_below", N(0))]),
    stop_loss: { type: "pct", value: 1 }, target: null } },
  { name: "Equity · Gap fade back to previous close", spec: {
    direction: "both", instrument: { type: "equity" } as Instrument, max_trades_per_day: 1,
    session: { start: "09:20", end: "10:00", square_off: "15:15" },
    entry_short: G([C(I("DAILY", {}, "gap_pct"), ">", N(0.8)), C(I("TIME", {}, "hhmm"), ">=", N(920))]),
    exit_short: G([C(I("PRICE"), "<=", I("DAILY", {}, "prev_close"))]),
    entry_long: G([C(I("DAILY", {}, "gap_pct"), "<", N(-0.8)), C(I("TIME", {}, "hhmm"), ">=", N(920))]),
    exit_long: G([C(I("PRICE"), ">=", I("DAILY", {}, "prev_close"))]),
    stop_loss: { type: "pct", value: 0.8 }, target: null } },
  { name: "Equity · Stoch-RSI pullback in a 1h uptrend (15m)", spec: {
    direction: "long", timeframe: 15, instrument: { type: "equity" } as Instrument,
    entry_long: G([C(I("STOCHRSI", {}, "k"), "crosses_above", N(20)), C(I("PRICE"), ">", I("EMA", { period: 50 }, undefined, 60))]),
    exit_long: G([C(I("STOCHRSI", {}, "k"), "crosses_above", N(80))]),
    stop_loss: { type: "atr", value: 1.5, atr_period: 14 }, target: null, entry_order: { type: "pullback", offset_pct: 0.1, valid_bars: 2 } } },
  // ---- options / futures ----
  { name: "NIFTY options · 9:30 iron condor (MTM)", symbols: ["NSE:NIFTY50-INDEX"], spec: {
    direction: "long", timeframe: 5, session: { start: "09:30", end: "09:35", square_off: "15:15" }, max_trades_per_day: 1,
    instrument: { type: "option", expiry: "current", expiry_kind: "weekly", levels_on: "underlying", iv: { source: "auto", value: 15 },
      legs_long: [L("CE", "SELL", "OTM", 3), L("PE", "SELL", "OTM", 3), L("CE", "BUY", "OTM", 6), L("PE", "BUY", "OTM", 6)], legs_short: [L("PE", "BUY")] },
    entry_long: G([C(I("TIME", {}, "hhmm"), ">=", N(930))]), stop_loss: null, target: null,
    mtm: { stop: 2500, target: 2000, trail_start: null, trail_gap: null },
    sizing: { mode: "lots", value: 1 }, portfolio: { capital: 300000, leverage: 8, max_positions: 1, compounding: false } } },
  { name: "NIFTY options · long straddle on a squeeze release", symbols: ["NSE:NIFTY50-INDEX"], spec: {
    direction: "long", timeframe: 5,
    instrument: { type: "option", expiry: "current", expiry_kind: "weekly", levels_on: "underlying", iv: { source: "auto", value: 15 },
      legs_long: [L("CE", "BUY"), L("PE", "BUY")], legs_short: [L("PE", "BUY")] },
    entry_long: G([C(A(I("BBANDS", { period: 20, multiplier: 2 }, "upper"), 1), "<", A(I("KELTNER", { period: 20, multiplier: 1.5 }, "upper"), 1)),
                   C(I("BBANDS", { period: 20, multiplier: 2 }, "upper"), ">", I("KELTNER", { period: 20, multiplier: 1.5 }, "upper"))]),
    stop_loss: null, target: null, mtm: { stop: 2500, target: 5000, trail_start: 3000, trail_gap: 1500 }, max_trades_per_day: 2,
    sizing: { mode: "lots", value: 1 } } },
  { name: "NIFTY options · credit spreads on Supertrend", symbols: ["NSE:NIFTY50-INDEX"], spec: {
    direction: "both", timeframe: 5,
    instrument: { type: "option", expiry: "current", expiry_kind: "weekly", levels_on: "underlying", iv: { source: "auto", value: 15 },
      legs_long: [L("PE", "SELL"), L("PE", "BUY", "OTM", 2)], legs_short: [L("CE", "SELL"), L("CE", "BUY", "OTM", 2)] },
    entry_long: G([ST("crosses_above")]), entry_short: G([ST("crosses_below")]),
    stop_loss: null, target: null, mtm: { stop: 2000, target: 2500, trail_start: null, trail_gap: null },
    sizing: { mode: "lots", value: 1 }, portfolio: { capital: 300000, leverage: 8, max_positions: 1, compounding: false } } },
  { name: "BANKNIFTY futures · opening range breakout", symbols: ["NSE:NIFTYBANK-INDEX"], spec: {
    direction: "both", timeframe: 5, session: { start: "09:30", end: "12:00", square_off: "15:10" }, max_trades_per_day: 1,
    instrument: { type: "future", expiry: "current", expiry_kind: "monthly", legs_long: [], legs_short: [], levels_on: "instrument", iv: { source: "auto", value: 15 } },
    entry_long: G([C(I("PRICE"), "crosses_above", I("ORB", { minutes: 15 }, "high"))]),
    entry_short: G([C(I("PRICE"), "crosses_below", I("ORB", { minutes: 15 }, "low"))]),
    stop_loss: { type: "pct", value: 0.4 }, target: { type: "rr", value: 2 },
    sizing: { mode: "lots", value: 1 }, portfolio: { capital: 300000, leverage: 6, max_positions: 1, compounding: true } } },
  { name: "EMA 9 × 21 cross at a 30–45° angle (5m)", spec: {
    direction: "both", timeframe: 5, instrument: { type: "equity" } as Instrument,
    entry_long: G([C(I("EMA", { period: 9 }), "crosses_above", I("EMA", { period: 21 })),
      { left: { ...I("EMA", { period: 9 }), xf: "angle", xn: 3 }, op: "between", right: N(30), right2: N(45) }]),
    entry_short: G([C(I("EMA", { period: 9 }), "crosses_below", I("EMA", { period: 21 })),
      { left: { ...I("EMA", { period: 9 }), xf: "angle", xn: 3 }, op: "between", right: N(-45), right2: N(-30) }]),
    stop_loss: { type: "atr", value: 1.5, atr_period: 14 }, target: { type: "rr", value: 2 } } },
  { name: "Spread · EMA 9 more than 0.3% above EMA 21, held 3 bars (15m)", spec: {
    direction: "long", timeframe: 15, instrument: { type: "equity" } as Instrument,
    entry_long: G([{ left: { math: "pct_diff", a: I("EMA", { period: 9 }), b: I("EMA", { period: 21 }) }, op: ">", right: N(0.3), for_bars: 3 },
      { left: I("RSI", { period: 14 }), op: "crosses_above", right: N(50), within_bars: 3 }]),
    exit_long: G([C(I("EMA", { period: 9 }), "crosses_below", I("EMA", { period: 21 }))]),
    stop_loss: { type: "atr", value: 1.5, atr_period: 14 }, target: null } },
  // ---- order flow (delta estimated per minute from 1-minute candles) ----
  { name: "Order flow · delta-confirmed EMA cross on volume candles", spec: {
    direction: "both", bars: { type: "volume", per_day: 60 }, timeframe: 1, instrument: { type: "equity" } as Instrument,
    entry_long: G([C(I("EMA", { period: 9 }), "crosses_above", I("EMA", { period: 21 })), C(I("DELTA", {}, "delta_pct"), ">", N(15))]),
    entry_short: G([C(I("EMA", { period: 9 }), "crosses_below", I("EMA", { period: 21 })), C(I("DELTA", {}, "delta_pct"), "<", N(-15))]),
    stop_loss: { type: "atr", value: 1.5, atr_period: 14 }, target: { type: "rr", value: 2 }, sizing: { mode: "risk_pct", value: 0.5 } } },
  { name: "Order flow · cumulative-delta divergence reversal (5m)", spec: {
    direction: "both", timeframe: 5, instrument: { type: "equity" } as Instrument, max_bars: 12,
    entry_long: G([C(I("CVD_DIVERGENCE", { period: 20 }, "bull"), "==", N(1)), C(I("RSI", { period: 14 }), "<", N(40))]),
    entry_short: G([C(I("CVD_DIVERGENCE", { period: 20 }, "bear"), "==", N(1)), C(I("RSI", { period: 14 }), ">", N(60))]),
    stop_loss: { type: "atr", value: 1, atr_period: 14 }, target: { type: "rr", value: 1.5 } } },
  { name: "Order flow · absorption reversal (5m)", spec: {
    direction: "both", timeframe: 5, instrument: { type: "equity" } as Instrument, max_bars: 12, max_trades_per_day: 3,
    entry_long: G([C(I("FLOW_PRESSURE", { period: 20, smooth: 5, threshold: 2 }, "absorption"), "==", N(1)), C(I("RSI", { period: 14 }), "<", N(45))]),
    entry_short: G([C(I("FLOW_PRESSURE", { period: 20, smooth: 5, threshold: 2 }, "absorption"), "==", N(-1)), C(I("RSI", { period: 14 }), ">", N(55))]),
    stop_loss: { type: "atr", value: 1, atr_period: 14 }, target: { type: "rr", value: 1.5 } } },
  { name: "Order flow · initiative-pressure trend (5m)", spec: {
    direction: "both", timeframe: 5, instrument: { type: "equity" } as Instrument, max_trades_per_day: 2,
    entry_long: G([C(I("FLOW_PRESSURE", { period: 20, smooth: 5, threshold: 2 }, "aggression"), "==", N(1)), C(I("FLOW_PRESSURE", { period: 20, smooth: 5, threshold: 2 }, "pressure"), ">", N(10)), C(I("PRICE"), ">", I("VWAP"))]),
    entry_short: G([C(I("FLOW_PRESSURE", { period: 20, smooth: 5, threshold: 2 }, "aggression"), "==", N(-1)), C(I("FLOW_PRESSURE", { period: 20, smooth: 5, threshold: 2 }, "pressure"), "<", N(-10)), C(I("PRICE"), "<", I("VWAP"))]),
    exit_long: G([C(I("FLOW_PRESSURE", { period: 20, smooth: 5, threshold: 2 }, "pressure"), "<", N(-10))]),
    exit_short: G([C(I("FLOW_PRESSURE", { period: 20, smooth: 5, threshold: 2 }, "pressure"), ">", N(10))]),
    stop_loss: { type: "atr", value: 1.2, atr_period: 14 }, target: { type: "rr", value: 2 } } },
  { name: "Order flow · value-area rejection back to POC (5m)", spec: {
    direction: "both", timeframe: 5, instrument: { type: "equity" } as Instrument, max_trades_per_day: 1,
    entry_short: G([C(I("DAILY", {}, "day_open"), ">", I("VPROFILE", {}, "prev_vah")), C(I("PRICE"), "crosses_below", I("VPROFILE", {}, "prev_vah"))]),
    exit_short: G([C(I("PRICE"), "<=", I("VPROFILE", {}, "prev_poc"))]),
    entry_long: G([C(I("DAILY", {}, "day_open"), "<", I("VPROFILE", {}, "prev_val")), C(I("PRICE"), "crosses_above", I("VPROFILE", {}, "prev_val"))]),
    exit_long: G([C(I("PRICE"), ">=", I("VPROFILE", {}, "prev_poc"))]),
    stop_loss: { type: "pct", value: 0.7 }, target: null } },
  { name: "Order flow · 80% rule (re-enter yesterday's value area)", spec: {
    direction: "both", timeframe: 15, instrument: { type: "equity" } as Instrument, max_trades_per_day: 1,
    entry_long: G([C(I("DAILY", {}, "day_open"), "<", I("VPROFILE", {}, "prev_val")), C(I("PRICE"), ">", I("VPROFILE", {}, "prev_val")), C(A(I("PRICE"), 1), ">", I("VPROFILE", {}, "prev_val"))]),
    exit_long: G([C(I("PRICE"), ">=", I("VPROFILE", {}, "prev_vah"))]),
    entry_short: G([C(I("DAILY", {}, "day_open"), ">", I("VPROFILE", {}, "prev_vah")), C(I("PRICE"), "<", I("VPROFILE", {}, "prev_vah")), C(A(I("PRICE"), 1), "<", I("VPROFILE", {}, "prev_vah"))]),
    exit_short: G([C(I("PRICE"), "<=", I("VPROFILE", {}, "prev_val"))]),
    stop_loss: { type: "pct", value: 0.8 }, target: null } },
  { name: "Order flow · RVOL breakout on turnover candles", spec: {
    direction: "both", bars: { type: "turnover", per_day: 40 }, timeframe: 1, instrument: { type: "equity" } as Instrument,
    entry_long: G([C(I("RVOL", { days: 10 }), ">", N(1.5)), C(I("PRICE"), "crosses_above", A(I("DONCHIAN", { period: 20 }, "upper"), 1)), C(I("DELTA", {}, "delta"), ">", N(0))]),
    entry_short: G([C(I("RVOL", { days: 10 }), ">", N(1.5)), C(I("PRICE"), "crosses_below", A(I("DONCHIAN", { period: 20 }, "lower"), 1)), C(I("DELTA", {}, "delta"), "<", N(0))]),
    stop_loss: { type: "atr", value: 1.5, atr_period: 14 }, trailing: { type: "atr", value: 2, atr_period: 14, activate: 1 }, target: null } },
  { name: "Order flow · absorption at the day's extreme", spec: {
    direction: "both", timeframe: 5, instrument: { type: "equity" } as Instrument, max_trades_per_day: 2,
    entry_long: G([C(I("ZSCORE", { period: 20, source: "volume" }), ">", N(2)), C(I("ZSCORE", { period: 20, source: "range" }), "<", N(0)),
                   C(I("PRICE"), "<=", { ...I("DAILY", {}, "day_low"), mult: 1.002 }), C(I("DELTA", {}, "delta"), ">", N(0))]),
    entry_short: G([C(I("ZSCORE", { period: 20, source: "volume" }), ">", N(2)), C(I("ZSCORE", { period: 20, source: "range" }), "<", N(0)),
                    C(I("PRICE"), ">=", { ...I("DAILY", {}, "day_high"), mult: 0.998 }), C(I("DELTA", {}, "delta"), "<", N(0))]),
    stop_loss: { type: "atr", value: 1, atr_period: 14 }, target: { type: "rr", value: 2 } } },
  { name: "NIFTY options · buy ATM on a delta surge with high RVOL", symbols: ["NSE:NIFTY50-INDEX"], spec: {
    direction: "both", timeframe: 5,
    instrument: { type: "option", expiry: "current", expiry_kind: "weekly", levels_on: "instrument", iv: { source: "auto", value: 15 },
      legs_long: [L("CE", "BUY")], legs_short: [L("PE", "BUY")] },
    entry_long: G([C(I("DELTA", {}, "delta_pct"), ">", N(25)), C(I("RVOL", { days: 10 }), ">", N(1.3)), C(I("PRICE"), ">", I("VWAP"))]),
    entry_short: G([C(I("DELTA", {}, "delta_pct"), "<", N(-25)), C(I("RVOL", { days: 10 }), ">", N(1.3)), C(I("PRICE"), "<", I("VWAP"))]),
    stop_loss: { type: "pct", value: 25 }, target: { type: "pct", value: 50 }, max_trades_per_day: 3, sizing: { mode: "lots", value: 1 } } },
  // ---- trade management ----
  { name: "Equity · EMA trend, scale out ½ at 1R, stop to cost, rest to 3R (Tue–Thu)", spec: {
    direction: "both", instrument: { type: "equity" } as Instrument,
    session: { start: "09:30", end: "14:30", square_off: "15:15", days: [2, 3, 4] },
    entry_long: G([C(I("EMA", { period: 9 }), "crosses_above", I("EMA", { period: 21 })), C(I("PRICE"), ">", I("VWAP"))]),
    entry_short: G([C(I("EMA", { period: 9 }), "crosses_below", I("EMA", { period: 21 })), C(I("PRICE"), "<", I("VWAP"))]),
    stop_loss: { type: "atr", value: 1.5, atr_period: 14 }, target: { type: "rr", value: 3 },
    scale_out: [{ type: "rr", value: 1, qty_pct: 50, atr_period: 14 }], breakeven: { type: "atr", value: 1.5, atr_period: 14 },
    sizing: { mode: "risk_pct", value: 0.5 } } },
];

const LEG_PRESETS: { name: string; long: Leg[]; short: Leg[] }[] = [
  { name: "Buy ATM CE / PE", long: [L("CE", "BUY")], short: [L("PE", "BUY")] },
  { name: "Buy 1-OTM CE / PE", long: [L("CE", "BUY", "OTM", 1)], short: [L("PE", "BUY", "OTM", 1)] },
  { name: "Sell ATM PE / CE (writer)", long: [L("PE", "SELL")], short: [L("CE", "SELL")] },
  { name: "Bull call / bear put spread", long: [L("CE", "BUY"), L("CE", "SELL", "OTM", 2)], short: [L("PE", "BUY"), L("PE", "SELL", "OTM", 2)] },
  { name: "Short straddle", long: [L("CE", "SELL"), L("PE", "SELL")], short: [L("CE", "SELL"), L("PE", "SELL")] },
  { name: "Short strangle (3 OTM)", long: [L("CE", "SELL", "OTM", 3), L("PE", "SELL", "OTM", 3)], short: [L("CE", "SELL", "OTM", 3), L("PE", "SELL", "OTM", 3)] },
  { name: "Iron fly (hedged straddle)", long: [L("CE", "SELL"), L("PE", "SELL"), L("CE", "BUY", "OTM", 4), L("PE", "BUY", "OTM", 4)], short: [L("CE", "SELL"), L("PE", "SELL"), L("CE", "BUY", "OTM", 4), L("PE", "BUY", "OTM", 4)] },
  { name: "Short strangle (20Δ)", long: [D("CE", "SELL", 0.2), D("PE", "SELL", 0.2)], short: [D("CE", "SELL", 0.2), D("PE", "SELL", 0.2)] },
  { name: "Bull put / bear call credit spread", long: [L("PE", "SELL", "OTM", 1), L("PE", "BUY", "OTM", 5)], short: [L("CE", "SELL", "OTM", 1), L("CE", "BUY", "OTM", 5)] },
  { name: "Long straddle / strangle", long: [L("CE", "BUY"), L("PE", "BUY")], short: [L("CE", "BUY", "OTM", 3), L("PE", "BUY", "OTM", 3)] },
  { name: "Iron condor (16Δ short, 5Δ wings)", long: [D("CE", "SELL", 0.16), D("PE", "SELL", 0.16), D("CE", "BUY", 0.05), D("PE", "BUY", 0.05)], short: [D("CE", "SELL", 0.16), D("PE", "SELL", 0.16), D("CE", "BUY", 0.05), D("PE", "BUY", 0.05)] },
  { name: "Ratio spread 1×2", long: [L("CE", "BUY"), L("CE", "SELL", "OTM", 4, 2)], short: [L("PE", "BUY"), L("PE", "SELL", "OTM", 4, 2)] },
  { name: "Butterfly", long: [L("CE", "BUY", "ITM", 4), L("CE", "SELL", "ATM", 0, 2), L("CE", "BUY", "OTM", 4)], short: [L("PE", "BUY", "ITM", 4), L("PE", "SELL", "ATM", 0, 2), L("PE", "BUY", "OTM", 4)] },
  { name: "Calendar (sell this expiry, buy next)", long: [L("CE", "SELL"), { ...L("CE", "BUY"), expiry: "next" }], short: [L("PE", "SELL"), { ...L("PE", "BUY"), expiry: "next" }] },
  { name: "Jade lizard", long: [L("PE", "SELL", "OTM", 2), L("CE", "SELL", "OTM", 2), L("CE", "BUY", "OTM", 5)], short: [L("CE", "SELL", "OTM", 2), L("PE", "SELL", "OTM", 2), L("PE", "BUY", "OTM", 5)] },
  { name: "Iron condor (hedged strangle)", long: [L("CE", "SELL", "OTM", 2), L("PE", "SELL", "OTM", 2), L("CE", "BUY", "OTM", 4), L("PE", "BUY", "OTM", 4)], short: [L("CE", "SELL", "OTM", 2), L("PE", "SELL", "OTM", 2), L("CE", "BUY", "OTM", 4), L("PE", "BUY", "OTM", 4)] },
  // Hedged futures: the future carries the view, the option caps the loss / pays for itself.
  { name: "Protective put / call (FUT + option)", long: [L("FUT", "BUY"), L("PE", "BUY")], short: [L("FUT", "SELL"), L("CE", "BUY")] },
  { name: "Covered call / put (FUT − option)", long: [L("FUT", "BUY"), L("CE", "SELL", "OTM", 2)], short: [L("FUT", "SELL"), L("PE", "SELL", "OTM", 2)] },
  { name: "Collar (FUT + put − call)", long: [L("FUT", "BUY"), L("PE", "BUY", "OTM", 1), L("CE", "SELL", "OTM", 2)], short: [L("FUT", "SELL"), L("CE", "BUY", "OTM", 1), L("PE", "SELL", "OTM", 2)] },
];

const DRAFT_KEY = "algo.draft.v2";

// ---------------------------------------------------------------------------
// inputs
// ---------------------------------------------------------------------------

// A number box that lets you TYPE decimals ("0.", "1.05") — a controlled
// <input type=number> fights partial input in some browsers. Keeps the raw
// text locally and reports only complete numbers.
function NumInput({ value, onChange, width = 64, allowEmpty = false, signed = false, title, placeholder }: {
  value: number | null | undefined; onChange: (v: number | null) => void; width?: number;
  allowEmpty?: boolean; signed?: boolean; title?: string; placeholder?: string;
}) {
  const shown = value === null || value === undefined ? "" : String(value);
  const [text, setText] = useState(shown);
  const committed = useRef(value);
  useEffect(() => {
    if (value !== committed.current) { committed.current = value; setText(shown); }
  }, [value, shown]);
  const re = signed ? /^-?\d*\.?\d*$/ : /^\d*\.?\d*$/;
  return (
    <input type="text" inputMode="decimal" style={{ width }} value={text} title={title} placeholder={placeholder}
      onChange={(e) => {
        const t = e.target.value.trim();
        if (!re.test(t)) return;
        setText(t);
        if (t === "") { if (allowEmpty) { committed.current = null; onChange(null); } return; }
        if (t === "-" || t === "." || t.endsWith(".")) return;
        const n = Number(t);
        if (!Number.isNaN(n)) { committed.current = n; onChange(n); }
      }}
      onBlur={() => { if (text === "" && !allowEmpty) setText(shown); else if (text.endsWith(".")) setText(text.slice(0, -1)); }} />
  );
}

const QUICK = [
  { s: "NSE:NIFTY50-INDEX", l: "NIFTY" }, { s: "NSE:NIFTYBANK-INDEX", l: "BANKNIFTY" }, { s: "NSE:FINNIFTY-INDEX", l: "FINNIFTY" },
  { s: "NSE:MIDCPNIFTY-INDEX", l: "MIDCPNIFTY" }, { s: "BSE:SENSEX-INDEX", l: "SENSEX" },
];

function SymbolPicker({ value, onChange }: { value: string[]; onChange: (v: string[]) => void }) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Hit[]>([]);
  const [open, setOpen] = useState(false);
  const [hi, setHi] = useState(0);
  useEffect(() => {
    if (!open) return;
    const h = setTimeout(async () => {
      try {
        const r = await api.get<{ hits: Hit[] }>(`/api/search/symbols?q=${encodeURIComponent(q)}&segment=EQ,INDEX&limit=15`);
        setHits(r.hits.filter((x) => !value.includes(x.symbol)));
        setHi(0);
      } catch { setHits([]); }
    }, 120);
    return () => clearTimeout(h);
  }, [q, open, value]);
  const add = (...syms: string[]) => {
    const next = [...value];
    syms.map((s) => s.trim().toUpperCase()).filter(Boolean).forEach((s) => { if (!next.includes(s)) next.push(s); });
    onChange(next);
    setQ("");
  };
  return (
    <div className="algo-symbols">
      {value.map((s) => (
        <span className="algo-chip" key={s} title={s}>
          {shortSym(s)}
          <button type="button" aria-label={`remove ${s}`} onClick={() => onChange(value.filter((x) => x !== s))}>×</button>
        </span>
      ))}
      <span style={{ position: "relative" }}>
        <input value={q} placeholder={value.length ? "add symbol…" : "search a stock or index…"} style={{ width: 190 }}
          onFocus={() => setOpen(true)} onBlur={() => setTimeout(() => setOpen(false), 150)}
          onChange={(e) => { setQ(e.target.value); setOpen(true); }}
          onPaste={(e) => {
            const t = e.clipboardData.getData("text");
            if (/[,\s]/.test(t.trim())) { e.preventDefault(); add(...t.split(/[\s,]+/)); }
          }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") { e.preventDefault(); setHi(Math.min(hi + 1, hits.length - 1)); }
            else if (e.key === "ArrowUp") { e.preventDefault(); setHi(Math.max(hi - 1, 0)); }
            else if (e.key === "Enter") { e.preventDefault(); if (hits[hi]) add(hits[hi].symbol); else if (q.includes(":")) add(q); }
            else if (e.key === "Backspace" && !q && value.length) onChange(value.slice(0, -1));
            else if (e.key === "Escape") setOpen(false);
          }} />
        {open && hits.length > 0 && (
          <div className="algo-dropdown" role="listbox">
            {hits.map((h, i) => (
              <div key={h.symbol} role="option" aria-selected={i === hi} className={i === hi ? "active" : ""}
                onMouseEnter={() => setHi(i)} onMouseDown={(e) => { e.preventDefault(); add(h.symbol); }}>
                <b>{h.short_name}</b> <span className="meta">{h.symbol}{h.segment === "INDEX" ? " · index" : ""}</span>
              </div>
            ))}
          </div>
        )}
      </span>
      {QUICK.filter((x) => !value.includes(x.s)).map((x) => (
        <button key={x.s} type="button" className="btn-sm ghost" onClick={() => add(x.s)}>+ {x.l}</button>
      ))}
      {value.length > 1 && <button type="button" className="btn-sm ghost" onClick={() => onChange([])}>clear</button>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// builder widgets
// ---------------------------------------------------------------------------

function TransformEditor({ v, onChange }: { v: Operand; onChange: (o: Operand) => void }) {
  const t = TRANSFORMS.find((x) => x[0] === (v.xf ?? "")) ?? TRANSFORMS[0];
  return (
    <label className="meta" title={t[2] || "use the value itself, or reshape it: its angle, slope, change, highest … over N bars"}>
      as
      <select value={v.xf ?? ""} data-testid="operand-xf" onChange={(e) => {
        const n = { ...v };
        if (e.target.value) { n.xf = e.target.value; n.xn = v.xn ?? 3; } else { delete n.xf; delete n.xn; }
        onChange(n);
      }}>
        {TRANSFORMS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
      </select>
      {v.xf && v.xf !== "abs" && <>over <NumInput width={36} value={v.xn ?? 3} onChange={(n) => onChange({ ...v, xn: Math.max(1, Math.floor(n ?? 1)) })} /> bars</>}
    </label>
  );
}

function OperandEditor({ v, onChange, cat, baseTf, allowNumber = true, depth = 0 }: { v: Operand; onChange: (o: Operand) => void; cat: Catalog; baseTf: number; allowNumber?: boolean; depth?: number }) {
  const def = v.ind ? cat.indicators.find((d) => d.name === v.ind) : undefined;
  const groups = useMemo(() => Array.from(new Set(cat.indicators.map((d) => d.group))), [cat]);
  const [math, setMath] = useState((v.mult !== undefined && v.mult !== 1) || !!v.add);
  const tfs = cat.cond_timeframes.filter((t) => t === 1440 || (t > baseTf && t % baseTf === 0));
  if (v.math) {
    return (
      <span className="algo-operand algo-combined" data-testid="operand-math">
        (<OperandEditor v={v.a ?? I("EMA", { period: 9 })} cat={cat} baseTf={baseTf} depth={depth + 1} onChange={(a) => onChange({ ...v, a })} />
        <select value={v.math} onChange={(e) => onChange({ ...v, math: e.target.value })} title="% gap = how far the first is from the second, in % of the second">
          {MATH_OPS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
        </select>
        <OperandEditor v={v.b ?? I("EMA", { period: 21 })} cat={cat} baseTf={baseTf} depth={depth + 1} onChange={(b) => onChange({ ...v, b })} />)
        <TransformEditor v={v} onChange={onChange} />
        <button type="button" className="btn-sm ghost" title="back to a single value" onClick={() => onChange(v.a ?? I("PRICE"))}>✕</button>
      </span>
    );
  }
  return (
    <span className="algo-operand">
      <select value={v.ind ?? "#"} title={def?.about || undefined} onChange={(e) => onChange(e.target.value === "#" ? { value: 0 }
        : e.target.value === "@math" ? { math: "-", a: v.ind ? { ...v } : I("EMA", { period: 9 }), b: I("EMA", { period: 21 }) } : I(e.target.value))}>
        {allowNumber && <option value="#">Number</option>}
        {depth < 2 && <option value="@math">ƒ Combine two values (A − B, A ÷ B, % gap…)</option>}
        {groups.map((g) => (
          <optgroup key={g} label={g}>
            {cat.indicators.filter((d) => d.group === g).map((d) => <option key={d.name} value={d.name} title={d.about}>{d.name}</option>)}
          </optgroup>
        ))}
      </select>
      {!def && <NumInput value={v.value ?? 0} signed width={80} onChange={(n) => onChange({ value: n ?? 0 })} />}
      {def && def.outputs.length > 1 && (
        <select value={v.field ?? def.outputs[0]} onChange={(e) => onChange({ ...v, field: e.target.value })}>
          {def.outputs.map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
      )}
      {def && Object.entries(def.params).map(([k, dv]) => (
        <label key={k} className="meta" title={k}>
          {k.replace("_period", "").replace("multiplier", "mult")}
          {k === "source" ? (
            <select value={String(v.params?.[k] ?? dv)} onChange={(e) => onChange({ ...v, params: { ...v.params, [k]: e.target.value } })}>
              {cat.sources.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          ) : (
            <NumInput width={52} value={Number(v.params?.[k] ?? dv)} onChange={(n) => onChange({ ...v, params: { ...v.params, [k]: n ?? Number(dv) } })} />
          )}
        </label>
      ))}
      {def && tfs.length > 0 && (
        <label className="meta" title="evaluate this indicator on a higher timeframe (only its completed candles are used)">
          on
          <select value={v.tf ?? 0} onChange={(e) => { const tf = Number(e.target.value); const n = { ...v }; if (tf) n.tf = tf; else delete n.tf; onChange(n); }}>
            <option value={0}>base candle</option>
            {tfs.map((t) => <option key={t} value={t}>{tfLabel(t)}</option>)}
          </select>
        </label>
      )}
      {def && (
        <label className="meta" title="bars ago — 1 = the previous candle's value">
          ago <NumInput width={36} value={v.offset ?? 0} onChange={(n) => onChange({ ...v, offset: Math.max(0, Math.floor(n ?? 0)) })} />
        </label>
      )}
      {def && <TransformEditor v={v} onChange={onChange} />}
      {def && (math ? (
        <>
          <label className="meta" title="multiply, e.g. 1.02 = 2% above">× <NumInput width={52} value={v.mult ?? 1} onChange={(n) => onChange({ ...v, mult: n ?? 1 })} /></label>
          <label className="meta" title="add (can be negative)">+ <NumInput width={52} signed value={v.add ?? 0} onChange={(n) => onChange({ ...v, add: n ?? 0 })} /></label>
        </>
      ) : <button type="button" className="btn-sm ghost" title="scale or offset this value (× 1.02, + 20 …)" onClick={() => setMath(true)}>ƒ</button>)}
    </span>
  );
}

const OP_LABEL: Record<string, string> = {
  crosses_above: "crosses above", crosses_below: "crosses below", crosses: "crosses",
  rising: "is rising for (bars)", falling: "is falling for (bars)",
  between: "is between", outside: "is outside",
};

function CondEditor({ c, onChange, cat, baseTf }: { c: Cond; onChange: (c: Cond) => void; cat: Catalog; baseTf: number }) {
  const trend = c.op === "rising" || c.op === "falling";
  const range = c.op === "between" || c.op === "outside";
  const [more, setMore] = useState(!!(c.for_bars || c.within_bars || c.negate));
  const setMod = (k: "for_bars" | "within_bars", n: number | null) => {
    const x = { ...c };
    if (n && n > 1) x[k] = Math.floor(n); else delete x[k];
    onChange(x);
  };
  return (
    <span className="algo-operand" style={{ gap: 8 }}>
      {c.negate && <b className="meta" title="the condition must be false">NOT</b>}
      <OperandEditor v={c.left} cat={cat} baseTf={baseTf} allowNumber={false} onChange={(left) => onChange({ ...c, left })} />
      <select value={c.op} onChange={(e) => {
        const op = e.target.value;
        const t = op === "rising" || op === "falling";
        const x: Cond = { ...c, op, right: t ? N(Math.max(1, c.right?.value ?? 1)) : c.right?.ind || c.right?.value !== undefined ? c.right : N(0) };
        if (op === "between" || op === "outside") x.right2 = c.right2 ?? N(0); else delete x.right2;   // the high bound is part of the rule
        onChange(x);
      }}>
        {cat.operators.map((o) => <option key={o} value={o}>{OP_LABEL[o] ?? o}</option>)}
      </select>
      {trend ? (
        <NumInput width={44} value={c.right?.value ?? 1} onChange={(n) => onChange({ ...c, right: N(Math.max(1, Math.floor(n ?? 1))) })} />
      ) : (
        <OperandEditor v={c.right ?? N(0)} cat={cat} baseTf={baseTf} onChange={(right) => onChange({ ...c, right })} />
      )}
      {range && <>
        <span className="meta">and</span>
        <OperandEditor v={c.right2 ?? N(0)} cat={cat} baseTf={baseTf} onChange={(right2) => onChange({ ...c, right2 })} />
      </>}
      {more ? (
        <span className="algo-operand" data-testid="cond-modifiers">
          <label className="meta" title="true only when it has held this many bars in a row (2+)">held <NumInput width={36} value={c.for_bars ?? 0} onChange={(n) => setMod("for_bars", n)} /> bars</label>
          <label className="meta" title="true if it happened at least once in the last N bars (2+), e.g. a crossover 3 bars ago still counts">within last <NumInput width={36} value={c.within_bars ?? 0} onChange={(n) => setMod("within_bars", n)} /> bars</label>
          <label className="meta" title="the condition must be FALSE"><input type="checkbox" checked={!!c.negate} onChange={(e) => { const x = { ...c }; if (e.target.checked) x.negate = true; else delete x.negate; onChange(x); }} /> NOT</label>
        </span>
      ) : <button type="button" className="btn-sm ghost" title="held for N bars, happened within the last N bars, NOT" onClick={() => setMore(true)} data-testid="cond-more">⋯</button>}
    </span>
  );
}

function GroupEditor({ g, onChange, cat, baseTf, depth = 0 }: { g: Group; onChange: (g: Group) => void; cat: Catalog; baseTf: number; depth?: number }) {
  const set = (i: number, c: Cond | Group) => onChange({ ...g, conditions: g.conditions.map((x, j) => (j === i ? c : x)) });
  return (
    <div style={{ borderLeft: `2px solid ${depth ? "var(--cyan)" : "var(--accent)"}`, paddingLeft: 8, display: "grid", gap: 6 }}>
      <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
        <select value={g.logic} onChange={(e) => onChange({ ...g, logic: e.target.value as "AND" | "OR" })}>
          <option value="AND">ALL of (AND)</option>
          <option value="OR">ANY of (OR)</option>
        </select>
        <button type="button" className="btn-sm" onClick={() => onChange({ ...g, conditions: [...g.conditions, C(I("PRICE"), ">", I("EMA", { period: 20 }))] })}>+ condition</button>
        {depth < 2 && <button type="button" className="btn-sm" onClick={() => onChange({ ...g, conditions: [...g.conditions, G([C(I("RSI", { period: 14 }), ">", N(50))], "OR")] })}>+ group</button>}
        {g.conditions.length === 0 && <span className="meta">no conditions — never fires</span>}
      </div>
      {g.conditions.map((c, i) => (
        <div key={i} style={{ display: "flex", gap: 6, alignItems: "flex-start" }}>
          <div style={{ flex: 1 }}>
            {isGroup(c)
              ? <GroupEditor g={c} cat={cat} baseTf={baseTf} depth={depth + 1} onChange={(x) => set(i, x)} />
              : <CondEditor c={c} cat={cat} baseTf={baseTf} onChange={(x) => set(i, x)} />}
          </div>
          <button type="button" className="btn-sm danger" title="remove" onClick={() => onChange({ ...g, conditions: g.conditions.filter((_, j) => j !== i) })}>✕</button>
        </div>
      ))}
    </div>
  );
}

function LevelEditor({ label, v, onChange, kinds, activate = false, hint }: { label: string; v: Level; onChange: (l: Level) => void; kinds: string[]; activate?: boolean; hint?: string }) {
  const KIND_LABEL: Record<string, string> = { pct: "% of price", points: "points", atr: "× ATR", rr: "× risk (R:R)" };
  return (
    <label className="meta" title={hint}>
      {label}
      <select value={v?.type ?? "none"} onChange={(e) => onChange(e.target.value === "none" ? null : { type: e.target.value, value: v?.value ?? 1, atr_period: v?.atr_period ?? 14, ...(activate ? { activate: v?.activate ?? 0 } : {}) })}>
        <option value="none">off</option>
        {kinds.map((k) => <option key={k} value={k}>{KIND_LABEL[k]}</option>)}
      </select>
      {v && <NumInput value={v.value} onChange={(n) => onChange({ ...v, value: n ?? 0 })} />}
      {v?.type === "atr" && <>ATR <NumInput width={40} value={v.atr_period ?? 14} onChange={(n) => onChange({ ...v, atr_period: Math.max(1, Math.floor(n ?? 14)) })} /></>}
      {v && activate && <>after <NumInput width={52} value={v.activate ?? 0} title="start trailing only once this far in profit (same unit); 0 = at once" onChange={(n) => onChange({ ...v, activate: n ?? 0 })} /> profit</>}
    </label>
  );
}

const SCALE_KIND: Record<string, string> = { rr: "× risk (R)", pct: "% of price", points: "points", atr: "× ATR" };

/** Partial exits: close part of the position at each level; the rest keeps its stop / trail / target. */
function ScaleOutEditor({ steps, onChange, hasStop }: { steps: ScaleStep[]; onChange: (s: ScaleStep[]) => void; hasStop: boolean }) {
  const set = (i: number, p: Partial<ScaleStep>) => onChange(steps.map((x, j) => (j === i ? { ...x, ...p } : x)));
  const total = steps.reduce((a, s) => a + (s.qty_pct || 0), 0);
  return (
    <div className="algo-operand" style={{ gap: 10, marginTop: 8 }} data-testid="scale-out">
      <span className="meta" style={{ color: "var(--text)" }}
        title="Partial exits, measured like the target. Each step closes that % of the size you entered with, rounded down to whole shares / lots (a step under one lot is skipped). The rest keeps its stop, trailing and target. Each partial is its own trade row (SCALE_OUT_1, _2 …), in backtests and live.">
        Scale out:</span>
      {steps.map((s, i) => (
        <span key={i} className="algo-operand">
          <label className="meta">close <NumInput width={40} value={s.qty_pct} onChange={(n) => set(i, { qty_pct: n ?? 0 })} />% at</label>
          <NumInput width={48} value={s.value} onChange={(n) => set(i, { value: n ?? 0 })} />
          <select value={s.type} onChange={(e) => set(i, { type: e.target.value })}>
            {Object.entries(SCALE_KIND).map(([k, l]) => <option key={k} value={k} disabled={k === "rr" && !hasStop}>{l}</option>)}
          </select>
          {s.type === "atr" && <label className="meta">ATR <NumInput width={40} value={s.atr_period ?? 14} onChange={(n) => set(i, { atr_period: Math.max(1, Math.floor(n ?? 14)) })} /></label>}
          <button type="button" className="btn-sm danger" aria-label={`remove scale-out step ${i + 1}`} onClick={() => onChange(steps.filter((_, j) => j !== i))}>✕</button>
        </span>
      ))}
      {steps.length < 5 && (
        <button type="button" className="btn-sm" onClick={() => onChange([...steps, { type: hasStop ? "rr" : "pct", value: steps.length + 1, qty_pct: 50, atr_period: 14 }])}>+ step</button>
      )}
      {!steps.length && <span className="meta">off — the whole position exits together</span>}
      {total > 100 && <span className="meta pnl-neg">steps close {total}% — at most 100%</span>}
      {steps.some((s) => s.type === "rr") && !hasStop && <span className="meta pnl-neg">R steps need a stop loss</span>}
    </div>
  );
}

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"];

/** Entry days. null = every day; never empty (the last ticked day can't be unticked). */
export function nextDays(cur: number[] | null | undefined, day: number, on: boolean): number[] | null {
  const base = cur && cur.length ? cur : [1, 2, 3, 4, 5];
  const next = on ? Array.from(new Set([...base, day])).sort((a, b) => a - b) : base.filter((d) => d !== day);
  if (!next.length) return base;
  return [1, 2, 3, 4, 5].every((d) => next.includes(d)) && next.length === 5 ? null : next;
}

function Rupee({ label, value, onChange, hint }: { label: string; value: number | null; onChange: (v: number | null) => void; hint?: string }) {
  return (
    <label className="meta" title={hint}>{label} ₹<NumInput width={76} allowEmpty placeholder="off" value={value} onChange={onChange} /></label>
  );
}

function LegsEditor({ legs, onChange, title }: { legs: Leg[]; onChange: (l: Leg[]) => void; title: string }) {
  const set = (i: number, p: Partial<Leg>) => onChange(legs.map((x, j) => (j === i ? { ...x, ...p } : x)));
  return (
    <div style={{ display: "grid", gap: 4 }}>
      <div className="meta">{title}</div>
      {legs.map((lg, i) => (
        <div key={i} className="algo-operand">
          <select value={lg.action} onChange={(e) => set(i, { action: e.target.value as Leg["action"] })}>
            <option value="BUY">BUY</option><option value="SELL">SELL</option>
          </select>
          <select value={lg.right} onChange={(e) => set(i, { right: e.target.value as Leg["right"] })}>
            <option value="CE">CE (call)</option><option value="PE">PE (put)</option><option value="FUT">FUT (future)</option>
          </select>
          {lg.right !== "FUT" && <select value={strikeKey(lg)} title="strikes are counted in the underlying's strike steps" onChange={(e) => {
            const m = e.target.value.match(/^(ITM|OTM)(\d+)$/);
            set(i, m ? { strike: m[1], steps: Number(m[2]) } : { strike: e.target.value, steps: 0, ...(e.target.value === "DELTA" ? { delta: lg.delta ?? 0.3 } : {}) });
          }}>
            {STRIKES.map((k) => <option key={k} value={k}>{k === "PREMIUM" ? "closest to premium ₹" : k === "DELTA" ? "closest to delta Δ" : k.replace(/(\d+)$/, " $1")}</option>)}
          </select>}
          {lg.right !== "FUT" && lg.strike === "PREMIUM" && <label className="meta">₹<NumInput width={56} value={lg.premium} onChange={(n) => set(i, { premium: n ?? 100 })} /></label>}
          {lg.right !== "FUT" && lg.strike === "DELTA" && <label className="meta" title="|delta| of the strike to pick: 0.5 ≈ ATM, 0.2 = a typical short-strangle strike, 0.05 = a far wing">Δ<NumInput width={44} value={lg.delta ?? 0.3} onChange={(n) => set(i, { delta: Math.min(0.99, Math.max(0.01, n ?? 0.3)) })} /></label>}
          {lg.right !== "FUT" && <select value={lg.expiry ?? ""} title="this leg's own expiry — sell this week, buy next week = a calendar" data-testid="leg-expiry" onChange={(e) => {
            const { expiry: _old, ...rest } = lg;
            onChange(legs.map((x, j) => (j === i ? (e.target.value ? { ...rest, expiry: e.target.value as Leg["expiry"] } : rest) : x)));
          }}>
            <option value="">strategy expiry</option><option value="current">current expiry</option><option value="next">next expiry</option>
          </select>}
          <label className="meta" title="0 = leg off (the optimiser can sweep a hedge on / off)">lots <NumInput width={40} value={lg.lots} onChange={(n) => set(i, { lots: Math.max(0, Math.floor(n ?? 1)) })} /></label>
          {lg.right !== "FUT" && (["sl_pct", "tg_pct"] as const).map((k) => (
            <label key={k} className="meta" data-testid={`leg-${k}`}
              title={k === "sl_pct"
                ? `this leg's own stop: exit it when its premium ${lg.action === "SELL" ? "rises" : "falls"} this % from entry (the other legs stay unless “when a leg's stop hits” says otherwise)`
                : `this leg's own target: exit it when its premium ${lg.action === "SELL" ? "falls" : "rises"} this % from entry`}>
              {k === "sl_pct" ? "SL" : "TG"}<NumInput width={40} allowEmpty placeholder="off" value={lg[k] ?? null} onChange={(n) => {
                const { [k]: _old, ...rest } = lg;
                onChange(legs.map((x, j) => (j === i ? (n && n > 0 ? { ...rest, [k]: n } : rest) : x)));
              }} />%
            </label>
          ))}
          <button type="button" className="btn-sm danger" onClick={() => onChange(legs.filter((_, j) => j !== i))}>✕</button>
        </div>
      ))}
      <div><button type="button" className="btn-sm" onClick={() => onChange([...legs, L("CE", "BUY")])}>+ leg</button></div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// payoff & funds required (POST /api/algo/payoff)
// ---------------------------------------------------------------------------

type PayLeg = {
  label: string; kind: string; side: string; strike: number | null; expiry: number | null; qty: number; dte: number | null;
  price: number; delta: number; pos_delta: number; theta: number; vega: number; gamma: number;
  margin_alone: number; margin_if_bought: number | null; margin_if_sold: number | null;
  prob_itm_pct?: number | null; lambda?: number | null; rho?: number; vanna?: number; charm?: number; vomma?: number;
  sl_px?: number | null; tg_px?: number | null;
};
export type Payoff = {
  symbol: string; spot: number; spot_source: string; iv_pct: number; iv_source: string; lot: number | null; side: string;
  dte: number | null; first_expiry: number | null; legs: PayLeg[]; curve: { spot: number; expiry: number; now: number; mid: number }[];
  breakevens: number[]; max_profit: number | null; max_loss: number | null; pop_pct: number; expected_move: number | null;
  greeks: { delta: number; gamma: number; theta: number; vega: number; rho?: number; vanna?: number; charm?: number; vomma?: number; speed?: number };
  premium: { net: number; kind: string };
  position?: Record<string, number | null> | null;
  entry_rules?: { rule: string; metric: string; value: number | null; ok: boolean }[];
  margin: { model: string; used: number; span: { span: number; exposure: number; premium: number; total: number; scan_pct: number | null; exposure_pct: number | null }; leverage: number;
    leverage_x: number; naked: number; hedge_benefit: number; sets_affordable: number | null; capital: number };
  return_on_margin_pct: number | null;
};

const lakh = (v: number) => (Math.abs(v) >= 100000 ? `₹${(v / 100000).toFixed(2)} L` : inr(v));

/** Where the funds go: SPAN | exposure | premium paid, each as a share of the total. */
function FundsBar({ m }: { m: Payoff["margin"] }) {
  const parts: [string, number, string, string][] = [
    ["SPAN (worst scenario loss)", m.span.span, "var(--red)", "the biggest loss over ±price and ±volatility moves — what the exchange holds against a writer"],
    ["exposure", m.span.exposure, "var(--amber)", "a flat % of contract value on every short option / future, on top of SPAN"],
    ["premium paid", m.span.premium, "var(--accent)", "bought options cost their premium up front — the most a buyer can lose"],
  ];
  const tot = parts.reduce((a, p) => a + p[1], 0);
  if (!tot) return null;
  return (
    <div data-testid="funds-bar">
      <div style={{ display: "flex", height: 14, borderRadius: 4, overflow: "hidden", gap: 2, background: "var(--border-soft)" }}>
        {parts.filter((p) => p[1] > 0).map(([k, v, col, hint]) => (
          <div key={k} title={`${k}: ${inr(v)} — ${hint}`} style={{ width: `${(v / tot) * 100}%`, background: col, minWidth: 3 }} />
        ))}
      </div>
      <div className="meta" style={{ display: "flex", gap: 12, flexWrap: "wrap", marginTop: 4 }}>
        {parts.filter((p) => p[1] > 0).map(([k, v, col]) => (
          <span key={k}><span style={{ display: "inline-block", width: 8, height: 8, borderRadius: 2, background: col, marginRight: 4 }} />{k} {lakh(v)}</span>
        ))}
      </div>
    </div>
  );
}

function PayoffPanel({ spec, onSpan }: { spec: Spec; onSpan: () => void }) {
  const both = spec.direction === "both";
  const [side, setSide] = useState<"BUY" | "SELL">(spec.direction === "short" ? "SELL" : "BUY");
  const [spot, setSpot] = useState<number | null>(null);
  const [iv, setIv] = useState<number | null>(null);
  const [show, setShow] = useState({ expiry: true, now: true, mid: false });
  const sideNow = spec.direction === "long" ? "BUY" : spec.direction === "short" ? "SELL" : side;
  const body = JSON.stringify({ spec: { symbols: spec.symbols, instrument: spec.instrument, portfolio: spec.portfolio, ...(spec.margin ? { margin: spec.margin } : {}),
    ...(spec.position_rules ? { position_rules: spec.position_rules } : {}) },
    side: sideNow, ...(spot ? { spot } : {}), ...(iv ? { iv_pct: iv } : {}) });
  const [key, setKey] = useState(body);
  useEffect(() => { const h = setTimeout(() => setKey(body), 350); return () => clearTimeout(h); }, [body]);
  const q = useQuery({
    queryKey: ["algo", "payoff", key], enabled: spec.symbols.length > 0, placeholderData: keepPreviousData, staleTime: 60000,
    queryFn: () => api.post<Payoff>("/api/algo/payoff", JSON.parse(key)),
  });
  const p = q.data;
  const writes = (spec.direction !== "short" ? spec.instrument.legs_long : []).concat(spec.direction !== "long" ? spec.instrument.legs_short : [])
    .some((lg) => lg.lots && lg.action === "SELL") || spec.instrument.type === "future";
  const fmtK = (v: number) => (Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(Math.abs(v) >= 10000 ? 0 : 1)}k` : v.toFixed(0));
  return (
    <div className="widget widget-wide" style={{ marginBottom: 12 }} data-testid="payoff-panel">
      <h3>Payoff & funds required
        {p && <span className="meta">{shortSym(p.symbol)} · spot {p.spot.toLocaleString("en-IN")} ({p.spot_source}) · IV {num(p.iv_pct, 1)}% ({p.iv_source}){p.dte !== null ? ` · ${num(p.dte, 1)} days to expiry` : ""} · 1 set{p.lot ? `, lot ${p.lot}` : ""}</span>}
      </h3>
      <div className="algo-operand" style={{ gap: 12 }}>
        {both && (
          <div className="seg">
            {(["BUY", "SELL"] as const).map((s_) => (
              <button type="button" key={s_} className={side === s_ ? "on" : ""} onClick={() => setSide(s_)}>{s_ === "BUY" ? "LONG-signal legs" : "SHORT-signal legs"}</button>
            ))}
          </div>
        )}
        <label className="meta" title="what-if: price the position at another spot">spot <NumInput width={80} allowEmpty placeholder="last close" value={spot} onChange={setSpot} /></label>
        <label className="meta" title="what-if: implied volatility %; empty = the backtest's IV source">IV % <NumInput width={48} allowEmpty placeholder="auto" value={iv} onChange={setIv} /></label>
        {(["expiry", "now", "mid"] as const).map((k) => (
          <label key={k} className="meta"><input type="checkbox" checked={show[k]} onChange={(e) => setShow({ ...show, [k]: e.target.checked })} />
            {{ expiry: "at expiry", now: "today (T+0)", mid: "halfway" }[k]}</label>
        ))}
        {q.isFetching && <span className="meta">updating…</span>}
      </div>
      {q.error && <div className="meta pnl-neg" style={{ marginTop: 6 }}>{errMsg(q.error)}</div>}
      {p && (
        <>
          {writes && p.margin.model !== "span" && p.margin.leverage > 1.5 * p.margin.span.total && (
            <div className="meta" style={{ marginTop: 8, color: "var(--amber)" }} data-testid="margin-warning">
              ⚠ At leverage ×{p.margin.leverage_x}, writing blocks {lakh(p.margin.leverage)} per set — the contract's value ÷ leverage. Brokers block SPAN + exposure,
              about {lakh(p.margin.span.total)} here, so backtests size this far too small (or skip it for “no capital”).{" "}
              <button type="button" className="btn-sm" onClick={onSpan}>use the SPAN estimate</button>
            </div>
          )}
          <div style={{ display: "flex", flexWrap: "wrap", gap: 18, marginTop: 10 }}>
            <Metric label={`Funds required${p.margin.model === "span" ? " (SPAN est.)" : ` (×${p.margin.leverage_x})`}`} value={lakh(p.margin.used)}
              hint="what one set blocks under this strategy's margin model — the backtest sizes with this number" />
            <Metric label={p.premium.kind === "credit" ? "Premium received" : "Premium paid"} value={inr(Math.abs(p.premium.net))} cls={p.premium.kind === "credit" ? "pnl-pos" : p.premium.kind === "debit" ? "pnl-neg" : ""}
              hint="net option premium at entry: credit = you collect it (a writer's maximum gain), debit = you pay it" />
            <Metric label="Max profit" value={p.max_profit === null ? "unlimited" : inr(p.max_profit)} cls="pnl-pos" hint="at the first expiry, over any price" />
            <Metric label="Max loss" value={p.max_loss === null ? "unlimited" : inr(p.max_loss)} cls="pnl-neg" hint="at the first expiry; 'unlimited' = a naked short call side (or short future) with no cap" />
            <Metric label="Breakeven" value={p.breakevens.length ? p.breakevens.map((b) => b.toFixed(0)).join(" / ") : "—"} hint="underlying prices where the expiry P&L crosses zero" />
            <Metric label="Chance of profit" value={`${num(p.pop_pct, 1)}%`} hint="probability the expiry P&L is above zero, lognormal at this IV — selling wins often, but small; buying wins rarely, but big" />
            <Metric label="Return on funds" value={p.return_on_margin_pct === null ? "—" : `${num(p.return_on_margin_pct, 1)}%`} hint="max profit / funds required" />
            <Metric label="Sets from capital" value={p.margin.sets_affordable === null ? "—" : String(p.margin.sets_affordable)} cls={p.margin.sets_affordable === 0 ? "pnl-neg" : ""}
              hint={`how many sets ${inr(p.margin.capital)} of capital can hold at once`} />
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 18, marginTop: 8 }} data-testid="payoff-greeks">
            <Metric label="Δ delta" value={num(p.greeks.delta, 1)} hint="₹ gained per 1-point rise of the underlying (position)" />
            <Metric label="Γ gamma" value={num(p.greeks.gamma, 4)} hint="how fast delta changes per point. Negative = a writer: moves hurt more the further they go" />
            <Metric label="Θ theta / day" value={inr(p.greeks.theta)} cls={pnlCls(p.greeks.theta)} hint="₹ the position earns (+) or bleeds (−) per day from time decay alone" />
            <Metric label="ν vega / vol pt" value={inr(p.greeks.vega)} cls={pnlCls(p.greeks.vega)} hint="₹ per 1-point rise in implied volatility" />
            {p.expected_move !== null && <Metric label="Expected move ±1σ" value={`±${p.expected_move.toFixed(0)}`} hint="one standard deviation of the underlying to the first expiry at this IV (shaded on the chart)" />}
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 18, marginTop: 8 }} data-testid="payoff-greeks-more">
            <Metric label="ρ rho / 1% rate" value={inr(p.greeks.rho ?? 0)} hint="₹ per 1% rise in interest rates" />
            <Metric label="vanna" value={num(p.greeks.vanna ?? 0, 3)} hint="how much delta (₹ per point) changes for a 1-point rise in IV — why a short strangle's delta drifts when volatility jumps" />
            <Metric label="charm / day" value={num(p.greeks.charm ?? 0, 3)} hint="how much delta (₹ per point) changes per day that passes, with the price unchanged — delta decay into expiry" />
            <Metric label="vomma" value={inr(p.greeks.vomma ?? 0)} hint="how much vega (₹) changes for a 1-point rise in IV" />
            <Metric label="speed" value={(p.greeks.speed ?? 0).toExponential(2)} hint="how much gamma changes per point — how fast the gamma risk grows as price moves" />
            {p.position && <Metric label="per set: delta / gamma (lots)" value={`${num(p.position.delta, 2)} / ${num(p.position.gamma, 3)}`}
              hint="the units the Greeks rules use: delta in lots of the underlying (1 = one lot of the future), gamma = delta change for a 1% move" />}
          </div>
          {(p.entry_rules ?? []).length > 0 && (
            <div className="meta" style={{ marginTop: 6 }} data-testid="payoff-entry-rules">
              Entry rules at this spot: {p.entry_rules!.map((r) => (
                <span key={r.rule} className={r.ok ? "pnl-pos" : "pnl-neg"} style={{ marginRight: 10 }}>
                  {r.ok ? "✓" : "✗"} {r.rule} <span className="meta">(now {r.value === null ? "n/a" : num(r.value, 3)})</span></span>))}
            </div>
          )}
          <ResponsiveContainer width="100%" height={260}>
            <LineChart data={p.curve} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
              <CartesianGrid stroke="var(--border-soft)" />
              <XAxis dataKey="spot" type="number" domain={["dataMin", "dataMax"]} tick={{ fontSize: 10, fill: "var(--text-dim)" }} tickFormatter={(v: number) => v.toFixed(0)} />
              <YAxis tick={{ fontSize: 10, fill: "var(--text-dim)" }} width={60} tickFormatter={fmtK} />
              <Tooltip contentStyle={{ background: "var(--bg-panel)", border: "1px solid var(--border)" }} labelFormatter={(v: number) => `underlying ${Number(v).toFixed(0)}`} formatter={(v: number) => inr(v)} />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              {p.expected_move !== null && <ReferenceArea x1={p.spot - p.expected_move} x2={p.spot + p.expected_move} fill="var(--accent)" fillOpacity={0.06} />}
              <ReferenceLine y={0} stroke="var(--text-faint)" />
              <ReferenceLine x={p.spot} stroke="var(--text-dim)" strokeDasharray="3 3" label={{ value: "spot", fontSize: 10, fill: "var(--text-dim)", position: "insideTopLeft" }} />
              {p.breakevens.map((b) => <ReferenceLine key={b} x={b} stroke="var(--amber)" strokeDasharray="2 4" />)}
              {show.expiry && <Line type="linear" dataKey="expiry" name="at expiry" stroke="var(--accent)" strokeWidth={2} dot={false} isAnimationActive={false} />}
              {show.now && <Line type="monotone" dataKey="now" name="today (T+0)" stroke="var(--cyan)" strokeWidth={1.5} strokeDasharray="5 3" dot={false} isAnimationActive={false} />}
              {show.mid && <Line type="monotone" dataKey="mid" name="halfway to expiry" stroke="var(--text-dim)" strokeWidth={1.5} strokeDasharray="2 3" dot={false} isAnimationActive={false} />}
            </LineChart>
          </ResponsiveContainer>
          <div className="algo-quad" style={{ marginTop: 8, marginBottom: 0 }}>
            <div>
              <div className="meta" style={{ color: "var(--text)", marginBottom: 4 }}>Where the funds go <span className="meta">(SPAN + exposure estimate)</span></div>
              <FundsBar m={p.margin} />
              <div className="algo-scroll" style={{ marginTop: 6 }}><table><tbody>
                <tr><td>SPAN + exposure estimate</td><td className="mono">{inr(p.margin.span.total)}</td></tr>
                <tr><td>Notional ÷ leverage ×{p.margin.leverage_x}</td><td className="mono">{inr(p.margin.leverage)}</td></tr>
                <tr><td title="every leg margined on its own, no offsets">Each leg on its own</td><td className="mono">{inr(p.margin.naked)}</td></tr>
                <tr><td title="what the legs offsetting each other saves — bought wings cap a writer's worst loss">Hedge benefit</td><td className="mono pnl-pos">{inr(p.margin.hedge_benefit)}</td></tr>
              </tbody></table></div>
            </div>
            <div className="meta" style={{ lineHeight: 1.5 }}>
              <b style={{ color: "var(--text)" }}>Why selling needs more money than buying.</b> A buyer can lose only the premium, so the premium is all that is blocked.
              A writer collects the premium but the loss has no floor, so the exchange holds SPAN — the worst loss if the underlying jumps
              {" "}±{num(p.margin.span.scan_pct ?? 6, 1)}% or IV moves 4 points —
              plus {num(p.margin.span.exposure_pct ?? 2, 1)}% exposure on the contract value (and +2% on expiry day for index shorts). Buying a far wing caps that worst loss, which is why a hedged
              spread or iron condor needs a fraction of a naked strangle's margin. These are estimates of the exchange method; your broker's calculator is final.
            </div>
          </div>
          <div className="algo-scroll" style={{ marginTop: 8 }}>
            <table data-testid="payoff-legs">
              <thead><tr><th>Leg</th><th>Side</th><th>Qty</th><th>Price</th><th title="per unit">Δ</th><th title="₹ per point, whole leg">pos Δ</th><th title="₹ per day">θ / day</th><th title="₹ per vol point">vega</th>
                <th title="risk-neutral chance it expires in the money">P(ITM)</th><th title="leverage: % the premium moves for a 1% move of the underlying">λ</th>
                <th title="this leg's own stop / target, as premium prices">SL / TG</th>
                <th title="this contract bought on its own: its premium">if bought</th><th title="this contract written on its own: SPAN + exposure">if sold</th></tr></thead>
              <tbody>{p.legs.map((lg, i) => (
                <tr key={i}><td>{lg.label}{lg.dte !== null ? <span className="meta"> · {num(lg.dte, 1)}d</span> : null}</td>
                  <td><span className={`badge ${lg.side === "BUY" ? "buy" : "sell"}`}>{lg.side}</span></td>
                  <td className="mono">{lg.qty}</td><td className="mono">{lg.price.toFixed(2)}</td><td className="mono">{lg.delta.toFixed(2)}</td>
                  <td className="mono">{lg.pos_delta.toFixed(1)}</td><td className={`mono ${pnlCls(lg.theta)}`}>{lg.theta.toFixed(0)}</td><td className="mono">{lg.vega.toFixed(0)}</td>
                  <td className="mono">{lg.prob_itm_pct == null ? "—" : `${lg.prob_itm_pct.toFixed(0)}%`}</td><td className="mono">{lg.lambda == null ? "—" : lg.lambda.toFixed(1)}</td>
                  <td className="mono">{lg.sl_px == null && lg.tg_px == null ? "—" : `${lg.sl_px == null ? "—" : lg.sl_px.toFixed(1)} / ${lg.tg_px == null ? "—" : lg.tg_px.toFixed(1)}`}</td>
                  <td className="mono">{lg.margin_if_bought === null ? "—" : inr(lg.margin_if_bought)}</td>
                  <td className="mono">{lg.margin_if_sold === null ? inr(lg.margin_alone) : inr(lg.margin_if_sold)}</td></tr>))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// trade analytics — distribution, timing, excursions, capital in use
// ---------------------------------------------------------------------------

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Net P&L of the trades in `bins` equal-width buckets. */
export function pnlHistogram(nets: number[], bins = 20): { mid: number; label: string; count: number }[] {
  if (!nets.length) return [];
  const lo = Math.min(...nets), hi = Math.max(...nets);
  if (lo === hi) return [{ mid: lo, label: lo.toFixed(0), count: nets.length }];
  const w = (hi - lo) / bins;
  const out = Array.from({ length: bins }, (_, k) => ({ mid: lo + w * (k + 0.5), label: (lo + w * (k + 0.5)).toFixed(0), count: 0 }));
  nets.forEach((v) => { out[Math.min(bins - 1, Math.floor((v - lo) / w))].count += 1; });
  return out;
}

/** Margin blocked over time: each exit row frees its part, each position takes its whole margin at entry. */
export function capitalInUse(trades: Pick<BtTrade, "entry_t" | "exit_t" | "margin">[]): { t: number; used: number }[] {
  const ev: [number, number][] = [];
  trades.forEach((x) => { ev.push([x.entry_t, x.margin]); ev.push([x.exit_t, -x.margin]); });
  ev.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  let used = 0;
  const out: { t: number; used: number }[] = [];
  ev.forEach(([t, d]) => {
    used = Math.max(0, used + d);
    if (out.length && out[out.length - 1].t === t) out[out.length - 1].used = used;
    else out.push({ t, used });
  });
  return out;
}

function TradeAnalytics({ r }: { r: BtResult }) {
  const s = r.stats;
  const capital = Number(r.spec?.portfolio?.capital ?? 0);
  const hist = useMemo(() => pnlHistogram(r.trades.map((t) => t.net)), [r.trades]);
  const hours = useMemo(() => {
    const by: Record<number, { hour: string; net: number; n: number }> = {};
    r.trades.forEach((t) => {
      const h = Math.floor(((t.entry_t + IST_S) % 86400) / 3600);
      by[h] = by[h] ?? { hour: `${String(h).padStart(2, "0")}:00`, net: 0, n: 0 };
      by[h].net += t.net; by[h].n += 1;
    });
    return Object.keys(by).map(Number).sort((a, b) => a - b).map((h) => ({ ...by[h], net: Math.round(by[h].net) }));
  }, [r.trades]);
  const exc = useMemo(() => {
    const pts = r.trades.filter((t) => t.mae_pct !== null && t.mfe_pct !== null).slice(-1500);
    return { win: pts.filter((t) => t.net > 0).map((t) => ({ mae: t.mae_pct, mfe: t.mfe_pct, net: t.net })),
      loss: pts.filter((t) => t.net <= 0).map((t) => ({ mae: t.mae_pct, mfe: t.mfe_pct, net: t.net })) };
  }, [r.trades]);
  const cap = useMemo(() => {
    const pts = capitalInUse(r.trades);
    const step = Math.max(1, Math.ceil(pts.length / 800));
    const peak = pts.reduce((a, p) => Math.max(a, p.used), 0);
    const avg = r.trades.length ? r.trades.reduce((a, t) => a + t.margin, 0) / r.trades.length : 0;
    return { pts: pts.filter((_, i) => i % step === 0 || i === pts.length - 1).map((p) => ({ t: ist(p.t), used: Math.round(p.used) })), peak, avg };
  }, [r.trades]);
  const years = useMemo(() => {
    const m = s.monthly ?? {};
    const ys = Array.from(new Set(Object.keys(m).map((k) => k.slice(0, 4)))).sort();
    const maxAbs = Math.max(1, ...Object.values(m).map((v) => Math.abs(v as number)));
    return { ys, m: m as Record<string, number>, maxAbs };
  }, [s.monthly]);
  if (!r.trades.length) return null;
  const tip = { contentStyle: { background: "var(--bg-panel)", border: "1px solid var(--border)" } };
  const heat = (v: number | undefined) => (v === undefined ? "transparent"
    : `color-mix(in srgb, ${v >= 0 ? "var(--green)" : "var(--red)"} ${Math.round(12 + (Math.abs(v) / years.maxAbs) * 58)}%, transparent)`);
  return (
    <>
      <div className="widget widget-wide" style={{ marginBottom: 12 }} data-testid="capital-in-use">
        <h3>Capital in use <span className="meta">margin blocked by open positions · the money the strategy actually needs</span></h3>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 18 }}>
          <Metric label="Peak in use" value={`${lakh(cap.peak)}${capital ? ` (${num((cap.peak / capital) * 100, 0)}%)` : ""}`} hint="the most margin blocked at one moment — size the account for this, not the average" />
          <Metric label="Avg per position" value={lakh(cap.avg)} hint="average margin a position blocked at entry" />
          <Metric label="Return on peak capital" value={cap.peak ? `${num((s.net_pnl / cap.peak) * 100, 1)}%` : "—"} cls={pnlCls(s.net_pnl)} hint="net P&L / peak margin in use — how hard the money that was actually needed worked" />
        </div>
        {cap.pts.length > 1 && (
          <ResponsiveContainer width="100%" height={120}>
            <AreaChart data={cap.pts}>
              <XAxis dataKey="t" tick={{ fontSize: 10, fill: "var(--text-dim)" }} minTickGap={80} />
              <YAxis tick={{ fontSize: 10, fill: "var(--text-dim)" }} width={70} tickFormatter={(v: number) => lakh(v)} />
              <Tooltip {...tip} formatter={(v: number) => inr(v)} />
              {capital > 0 && <ReferenceLine y={capital} stroke="var(--text-faint)" strokeDasharray="4 3" label={{ value: "capital", fontSize: 10, fill: "var(--text-dim)", position: "insideTopRight" }} />}
              <Area type="stepAfter" dataKey="used" name="margin in use" stroke="var(--accent)" fill="var(--accent)" fillOpacity={0.15} isAnimationActive={false} />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>
      <div className="algo-quad">
        <div className="widget" data-testid="pnl-histogram">
          <h3>P&L distribution <span className="meta">trades per net-P&L bucket</span></h3>
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={hist} barCategoryGap={2}>
              <CartesianGrid stroke="var(--border-soft)" vertical={false} />
              <XAxis dataKey="label" tick={{ fontSize: 10, fill: "var(--text-dim)" }} minTickGap={24} />
              <YAxis tick={{ fontSize: 10, fill: "var(--text-dim)" }} width={36} allowDecimals={false} />
              <Tooltip {...tip} labelFormatter={(v: string) => `≈ ₹${v}`} formatter={(v: number) => [v, "trades"]} />
              <ReferenceLine x={hist.reduce((a, h) => (Math.abs(h.mid) < Math.abs(a.mid) ? h : a), hist[0])?.label} stroke="var(--text-faint)" />
              <Bar dataKey="count" radius={[4, 4, 0, 0]} isAnimationActive={false}>
                {hist.map((h, i) => <Cell key={i} fill={h.mid >= 0 ? "var(--green)" : "var(--red)"} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
          <div className="meta">A seller's shape: many small wins and a few large losses on the left. A buyer's: the reverse.</div>
        </div>
        <div className="widget" data-testid="pnl-by-hour">
          <h3>P&L by entry hour <span className="meta">IST</span></h3>
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={hours}>
              <CartesianGrid stroke="var(--border-soft)" vertical={false} />
              <XAxis dataKey="hour" tick={{ fontSize: 10, fill: "var(--text-dim)" }} />
              <YAxis tick={{ fontSize: 10, fill: "var(--text-dim)" }} width={60} tickFormatter={(v: number) => lakh(v)} />
              <Tooltip {...tip} formatter={(v: number, _n: string, it: { payload?: { n: number } }) => [`${inr(v)} · ${it.payload?.n ?? 0} trades`, "net"]} />
              <ReferenceLine y={0} stroke="var(--text-faint)" />
              <Bar dataKey="net" radius={[4, 4, 0, 0]} isAnimationActive={false}>
                {hours.map((h, i) => <Cell key={i} fill={h.net >= 0 ? "var(--green)" : "var(--red)"} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
        <div className="widget" data-testid="mae-mfe">
          <h3>Excursions <span className="meta">how far each trade went against (MAE) / for (MFE) you, % of entry</span></h3>
          <ResponsiveContainer width="100%" height={200}>
            <ScatterChart margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
              <CartesianGrid stroke="var(--border-soft)" />
              <XAxis dataKey="mae" type="number" name="MAE %" tick={{ fontSize: 10, fill: "var(--text-dim)" }} tickFormatter={(v: number) => `${v.toFixed(1)}%`} />
              <YAxis dataKey="mfe" type="number" name="MFE %" tick={{ fontSize: 10, fill: "var(--text-dim)" }} width={44} tickFormatter={(v: number) => `${v.toFixed(1)}%`} />
              <ZAxis range={[28, 28]} />
              <Tooltip {...tip} formatter={(v: number, n: string) => [n === "net" ? inr(v) : `${v.toFixed(2)}%`, n]} />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              <Scatter name="winners" data={exc.win} fill="var(--green)" fillOpacity={0.7} isAnimationActive={false} />
              <Scatter name="losers" data={exc.loss} fill="var(--red)" fillOpacity={0.7} isAnimationActive={false} />
            </ScatterChart>
          </ResponsiveContainer>
          <div className="meta">Winners that first went far against you = a stop tighter than that would have cut them. Losers with a big MFE = profit given back: try a trail or target.</div>
        </div>
      </div>
      <div className="widget widget-wide" style={{ marginBottom: 12 }} data-testid="month-heatmap">
        <h3>Monthly returns <span className="meta">net ₹ · darker = bigger</span></h3>
        <div className="algo-scroll"><table>
          <thead><tr><th></th>{MONTHS.map((m) => <th key={m}>{m}</th>)}<th>Year</th></tr></thead>
          <tbody>{years.ys.map((y) => {
            const tot = MONTHS.reduce((a, _, k) => a + (years.m[`${y}-${String(k + 1).padStart(2, "0")}`] ?? 0), 0);
            return (
              <tr key={y}><td className="mono">{y}</td>
                {MONTHS.map((_, k) => {
                  const v = years.m[`${y}-${String(k + 1).padStart(2, "0")}`];
                  return <td key={k} className="mono" title={v === undefined ? "no trades" : inr(v)} style={{ background: heat(v), textAlign: "right", fontSize: 11 }}>
                    {v === undefined ? "" : Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(1)}k` : v.toFixed(0)}</td>;
                })}
                <td className={`mono ${pnlCls(tot)}`} style={{ textAlign: "right" }}>{inr(tot)}</td></tr>
            );
          })}</tbody>
        </table></div>
      </div>
    </>
  );
}

// Position rules: what each metric means (per set = the legs at their configured lots).
export const RULE_METRICS: Record<string, [string, string]> = {
  delta: ["delta (lots)", "net delta per set in lots of the underlying: 1 = like holding one lot of the future, − = bearish"],
  abs_delta: ["|delta| (lots)", "how far the position is from delta-neutral, per set — the usual adjustment / exit trigger"],
  gamma: ["gamma", "how much delta (lots) changes for a 1% move of the underlying, per set. Negative = a seller: moves hurt faster"],
  theta: ["theta ₹/day", "₹ the position earns (+) or loses (−) per day from time decay, per set"],
  vega: ["vega ₹/vol pt", "₹ per 1-point rise in implied volatility, per set"],
  credit: ["credit ₹", "net premium per set: received (+) or paid (−). At entry = what the legs collect"],
  pnl_pct: ["P&L % of premium", "P&L as % of the premium at entry: 50 = half the credit kept, −100 = lost as much as the credit"],
  dte: ["days to expiry", "calendar days to the nearest open leg's expiry"],
  iv: ["IV %", "average implied volatility of the open legs"],
};

function RulesEditor({ rules, onChange, kind }: { rules: Rule[]; onChange: (r: Rule[]) => void; kind: "entry" | "exit" }) {
  const set = (i: number, p: Partial<Rule>) => onChange(rules.map((x, j) => (j === i ? { ...x, ...p } : x)));
  return (
    <div style={{ display: "grid", gap: 4 }} data-testid={`rules-${kind}`}>
      <div className="meta">{kind === "entry" ? "Enter only if ALL of these hold (checked on the legs about to be opened)" : "Exit when ANY of these holds (checked on each candle's close)"}</div>
      {rules.map((r, i) => (
        <div key={i} className="algo-operand">
          <select value={r.metric} title={RULE_METRICS[r.metric]?.[1]} onChange={(e) => set(i, { metric: e.target.value })}>
            {Object.entries(RULE_METRICS).map(([k, [lab, hint]]) => <option key={k} value={k} title={hint}>{lab}</option>)}
          </select>
          <select value={r.op} onChange={(e) => set(i, { op: e.target.value })}>
            {[">", "<", ">=", "<="].map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
          <NumInput width={64} signed value={r.value} onChange={(n) => set(i, { value: n ?? 0 })} />
          <button type="button" className="btn-sm danger" onClick={() => onChange(rules.filter((_, j) => j !== i))}>✕</button>
        </div>
      ))}
      <div><button type="button" className="btn-sm" onClick={() => onChange([...rules, kind === "entry"
        ? { metric: "abs_delta", op: "<=", value: 0.1 } : { metric: "abs_delta", op: ">", value: 0.35 }])}>+ {kind} rule</button></div>
    </div>
  );
}

/** A remembered on / off switch for this browser (falls back to `initial` when storage is unavailable). */
function useFlag(key: string, initial: boolean): [boolean, (v: boolean) => void] {
  const [v, setV] = useState(() => {
    try { const x = localStorage.getItem(key); return x === null ? initial : x === "1"; } catch { return initial; }
  });
  return [v, (nv: boolean) => { setV(nv); try { localStorage.setItem(key, nv ? "1" : "0"); } catch { /* private mode */ } }];
}

/** Text an AI (or a file) gave us -> {name?, spec}. Accepts a ```json block, text around the JSON,
 *  {name, spec} or a bare spec. Throws with a readable reason. */
export function parseStrategyJson(text: string): { name?: string; spec: Partial<Spec> } {
  let t = text.trim();
  const fence = t.match(/```(?:json|jsonc)?\s*([\s\S]*?)```/i);
  if (fence) t = fence[1].trim();
  const a = t.indexOf("{"), b = t.lastIndexOf("}");
  if (a < 0 || b <= a) throw new Error("no JSON object found — paste the {\"name\": …, \"spec\": {…}} the AI wrote");
  let obj: unknown;
  try { obj = JSON.parse(t.slice(a, b + 1)); } catch (e) { throw new Error(`not valid JSON: ${errMsg(e)}`); }
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) throw new Error("expected a JSON object");
  const o = obj as { name?: unknown; spec?: unknown };
  if (o.spec && typeof o.spec === "object") return { name: typeof o.name === "string" ? o.name : undefined, spec: o.spec as Partial<Spec> };
  return { name: typeof o.name === "string" ? o.name : undefined, spec: obj as Partial<Spec> };
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
      markers.push({ time: (snap(t.entry_t) + IST_S) as UTCTimestamp, position: buy ? "belowBar" : "aboveBar", color: buy ? up : dn, shape: buy ? "arrowUp" : "arrowDown", text: t.legs.length > 1 ? t.side : `${t.side} ${t.entry}` });
      markers.push({ time: (snap(t.exit_t) + IST_S) as UTCTimestamp, position: buy ? "aboveBar" : "belowBar", color: t.net >= 0 ? up : dn, shape: "circle", text: `${t.reason} ${t.net >= 0 ? "+" : ""}${t.net.toFixed(0)}` });
    });
    markers.sort((a, b) => (a.time as number) - (b.time as number));
    createSeriesMarkers(s, markers);
    // The strategy's own indicators — what it actually sees.
    const PAL = ["#FFB74D", "#42A5F5", "#AB47BC", "#26C6DA", "#EC407A", "#9CCC65", "#FFD54F", "#8D6E63"];
    let extraPane = chart.candles.some((k) => k[5]) ? (chart.candles.some((k) => k[6] !== null && k[6] !== undefined) ? 3 : 2) : 1;
    (chart.overlays ?? []).forEach((ov, k) => {
      const pane = ov.price ? 0 : extraPane++;
      const ls = c.addSeries(LineSeries, { color: PAL[k % PAL.length], lineWidth: 1, priceLineVisible: false, lastValueVisible: false, title: ov.label }, pane);
      ls.setData(ov.values.map((v, i) => (v === null ? { time: (chart.candles[i][0] + IST_S) as UTCTimestamp } : { time: (chart.candles[i][0] + IST_S) as UTCTimestamp, value: v })));
      if (pane > 0) c.panes()[pane]?.setHeight(70);
    });
    // Volume (pane 1) coloured by the candle's delta when it carries order flow,
    // else by its direction; cumulative delta for the session (pane 2).
    if (chart.candles.some((k) => k[5])) {
      const flow = chart.candles.some((k) => k[6] !== null && k[6] !== undefined);
      const vol = c.addSeries(HistogramSeries, { priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false }, 1);
      vol.setData(chart.candles.map((k) => ({
        time: (k[0] + IST_S) as UTCTimestamp, value: k[5],
        color: (flow ? (k[6] ?? 0) >= 0 : k[4] >= k[1]) ? "rgba(0,215,135,0.55)" : "rgba(255,56,56,0.55)",
      })));
      if (flow) {
        const cvd = c.addSeries(LineSeries, { color: color("--cyan", "#00B4D8"), lineWidth: 1, priceLineVisible: false, title: "CVD" }, 2);
        let run = 0, day = -1;
        cvd.setData(chart.candles.map((k) => {
          const dd = Math.floor((k[0] + IST_S) / 86400);
          if (dd !== day) { run = 0; day = dd; }
          run += k[6] ?? 0;
          return { time: (k[0] + IST_S) as UTCTimestamp, value: run };
        }));
      }
      const panes = c.panes();
      panes[0]?.setHeight(260);
      panes[1]?.setHeight(70);
      panes[2]?.setHeight(70);
    }
    c.timeScale().setVisibleLogicalRange({ from: Math.max(0, times.length - 300), to: times.length + 5 });
    return () => c.remove();
  }, [chart]);
  const extra = (chart.overlays ?? []).filter((o) => !o.price).length;
  return <div ref={ref} style={{ height: (chart.candles.some((k) => k[5]) ? 440 : 380) + extra * 75, width: "100%" }} />;
}

const legLine = (l: BtLeg) => `${l.side} ${l.qty} ${l.label}: ${l.entry} → ${l.exit}${l.reason ? ` (${l.reason})` : ""}`;

export function tradesCsv(trades: BtTrade[]): string {
  const head = ["symbol", "instrument", "side", "qty", "lots", "entry_time", "entry", "exit_time", "exit", "underlying_entry", "underlying_exit", "reason", "bars", "minutes", "mae_pct", "mfe_pct", "r_multiple", "gross", "charges", "net",
    "entry_iv_pct", "entry_delta_lots", "entry_theta", "entry_vega", "entry_credit", "legs"];
  const iso = (t: number) => new Date((t + IST_S) * 1000).toISOString().replace("T", " ").slice(0, 16);
  const rows = trades.map((t) => [t.symbol, t.instrument, t.side, t.qty, t.lots, iso(t.entry_t), t.entry, iso(t.exit_t), t.exit, t.u_entry, t.u_exit, t.reason, t.bars, t.minutes, t.mae_pct ?? "", t.mfe_pct ?? "", t.r ?? "", t.gross, t.charges, t.net,
    t.greeks?.iv ?? "", t.greeks?.delta ?? "", t.greeks?.theta ?? "", t.greeks?.vega ?? "", t.greeks?.credit ?? "", t.legs.map(legLine).join(" | ")]);
  return [head, ...rows].map((r) => r.map((x) => `"${String(x).replace(/"/g, '""')}"`).join(",")).join("\n");
}

function Results({ r }: { r: BtResult }) {
  const s = r.stats;
  const eq = useMemo(() => {
    const pts = s.equity;
    const step = Math.max(1, Math.ceil(pts.length / 800));
    return pts.filter((_, i) => i % step === 0 || i === pts.length - 1).map(([t, v]) => ({ t: ist(t), v }));
  }, [s.equity]);
  const dd = useMemo(() => {   // underwater curve: % below the running peak
    let peak = Number(r.spec?.portfolio?.capital ?? 0);
    return eq.map((p) => { peak = Math.max(peak, p.v); return { t: p.t, dd: peak > 0 ? -((peak - p.v) / peak) * 100 : 0 }; });
  }, [eq, r.spec]);
  const [showAll, setShowAll] = useState(false);
  const trades = useMemo(() => [...r.trades].reverse().slice(0, showAll ? 3000 : 200), [r.trades, showAll]);
  const multiLeg = r.trades.some((t) => t.legs.length > 1);
  const withGreeks = r.trades.some((t) => t.greeks);
  const skipped = Object.entries(s.skipped ?? {}).filter(([, v]) => v > 0);
  return (
    <>
      {r.flow_source && Object.keys(r.flow_source).length > 0 && (
        <div className="meta" style={{ marginBottom: 8 }}>
          Order flow: {Object.entries(r.flow_source).map(([k, v]) => `${shortSym(k)} ${v}% real ticks`).join(" · ")} — the rest is estimated per minute (BVC). Coverage grows as the recorder runs.
        </div>
      )}
      {r.notes.length > 0 && (
        <div className="widget widget-wide" style={{ marginBottom: 12, borderColor: "var(--amber)" }}>
          {r.notes.map((n) => <div key={n.symbol} className="meta">⚠ {n.symbol}: {n.note}</div>)}
        </div>
      )}
      <div className="widget widget-wide" style={{ marginBottom: 12 }}>
        <h3>Performance <span className="meta">{r.trades_total} trades · {s.trading_days} days · {r.elapsed_s}s</span>
          <button type="button" className="btn-sm" style={{ marginLeft: "auto" }} onClick={() => download("backtest_trades.csv", tradesCsv(r.trades), "text/csv")}>⬇ trades CSV</button>
        </h3>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 18 }}>
          <Metric label="Net P&L" value={inr(s.net_pnl)} cls={pnlCls(s.net_pnl)} hint="after charges and slippage" />
          <Metric label="Return" value={`${num(s.return_pct)}%`} cls={pnlCls(s.return_pct)} hint="net P&L / starting capital" />
          <Metric label="CAGR" value={s.cagr_pct === null ? "—" : `${num(s.cagr_pct)}%`} hint="annualised (needs 30+ days)" />
          <Metric label="Win rate" value={`${num(s.win_rate, 1)}%`} />
          <Metric label="Profit factor" value={num(s.profit_factor)} hint="gross wins / gross losses" />
          <Metric label="Expectancy" value={inr(s.expectancy)} hint="average net per trade" />
          <Metric label="Max drawdown" value={`${inr(s.max_drawdown)} (${num(s.max_drawdown_pct, 1)}%)`} cls="pnl-neg" />
          <Metric label="Sharpe" value={num(s.sharpe)} hint="daily P&L, annualised √252" />
          <Metric label="Sortino" value={num(s.sortino)} hint="like Sharpe, but only downside days count as risk" />
          <Metric label="Calmar" value={num(s.calmar)} hint="CAGR / max drawdown %" />
          <Metric label="Charges" value={inr(s.charges)} hint="brokerage, STT, exchange, SEBI, GST, stamp — by segment" />
          <Metric label="Gross P&L" value={inr(s.gross_pnl)} cls={pnlCls(s.gross_pnl)} />
          <Metric label="Avg win / loss" value={`${inr(s.avg_win)} / ${inr(s.avg_loss)}`} />
          <Metric label="Median win / loss" value={`${inr(s.median_win)} / ${inr(s.median_loss)}`} />
          <Metric label="Largest win / loss" value={`${inr(s.largest_win)} / ${inr(s.largest_loss)}`} />
          <Metric label="Best / worst day" value={`${inr(s.best_day)} / ${inr(s.worst_day)}`} />
          <Metric label="Streaks W / L" value={`${s.max_consecutive_wins} / ${s.max_consecutive_losses}`} />
          <Metric label="Profitable days" value={`${s.profitable_days} / ${s.trading_days}`} />
          <Metric label="Avg bars held" value={num(s.avg_bars_held, 1)} />
        </div>
        <div className="meta" style={{ marginTop: 8, color: "var(--text)" }}>Quant</div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 18 }}>
          <Metric label="t-stat (mean trade)" value={num(s.t_stat)} cls={s.t_stat !== null && Math.abs(s.t_stat) >= 2 ? (s.t_stat > 0 ? "pnl-pos" : "pnl-neg") : ""}
            hint="mean trade / standard error. |t| ≥ 2 ≈ unlikely to be luck; below that the edge is not proven" />
          <Metric label="Payoff ratio" value={num(s.payoff_ratio)} hint="average win / average loss" />
          <Metric label="Kelly %" value={s.kelly_pct === null ? "—" : `${num(s.kelly_pct, 1)}%`} cls={pnlCls(s.kelly_pct)} hint="W − (1−W)/payoff. ≤ 0 = no edge. Trade a fraction (¼–½) of it at most" />
          <Metric label="Avg R" value={num(s.avg_r, 2)} cls={pnlCls(s.avg_r)} hint="net P&L per trade in units of the initial stop risk" />
          <Metric label="Recovery factor" value={num(s.recovery_factor)} hint="net P&L / max drawdown" />
          <Metric label="Ulcer index" value={num(s.ulcer_index)} hint="RMS of % drawdown along the equity curve — depth AND duration of pain" />
          <Metric label="Avg MAE / MFE" value={`${num(s.avg_mae_pct, 2)}% / ${num(s.avg_mfe_pct, 2)}%`} hint="how far trades went against / for you before exit — tune stops and targets with it" />
          <Metric label="Exposure" value={`${num(s.exposure_pct, 1)}%`} hint="share of candles with a position open" />
          <Metric label="Avg hold" value={`${num(s.avg_minutes_held, 0)} min`} />
          <Metric label="Buy & hold" value={s.buy_hold_pct === null || s.buy_hold_pct === undefined ? "—" : `${num(s.buy_hold_pct)}%`} cls={pnlCls(s.buy_hold_pct)} hint="the underlying's own move over the same window (average across symbols) — the benchmark" />
        </div>
        <div className="meta" style={{ marginTop: 8, color: "var(--text)" }}>Risk & confidence</div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 18 }} data-testid="algo-risk-metrics">
          <Metric label="PSR" value={s.psr_pct == null ? "—" : `${num(s.psr_pct, 1)}%`} cls={s.psr_pct == null ? "" : s.psr_pct >= 95 ? "pnl-pos" : s.psr_pct < 50 ? "pnl-neg" : ""}
            hint="Probabilistic Sharpe: the chance the true Sharpe is above 0, given how many days you have and their skew / fat tails. ≥ 95% is the usual bar" />
          <Metric label="SQN" value={num(s.sqn)} cls={s.sqn == null ? "" : s.sqn >= 2 ? "pnl-pos" : s.sqn < 1 ? "pnl-neg" : ""} hint="System Quality Number: √n × mean / sd of trade P&L (n ≤ 100). 1.6–2 average, 2–3 good, > 3 excellent" />
          <Metric label="VaR 95% (1 day)" value={s.var95 == null ? "—" : inr(s.var95)} hint="a day worse than this happened on 5% of days (historical, needs 20+ days)" />
          <Metric label="CVaR 95%" value={s.cvar95 == null ? "—" : inr(s.cvar95)} cls={s.cvar95 == null ? "" : "pnl-neg"} hint="expected shortfall: the average of those worst 5% of days — what a bad day really costs" />
          <Metric label="Tail ratio" value={num(s.tail_ratio)} cls={s.tail_ratio == null ? "" : s.tail_ratio >= 1 ? "pnl-pos" : "pnl-neg"} hint="best 5% day / worst 5% day. > 1 = the right tail is fatter (wins bigger than losses at the extremes)" />
          <Metric label="Skew / ex-kurtosis" value={`${num(s.skew)} / ${num(s.excess_kurtosis)}`} hint="daily P&L shape. Negative skew = rare big losing days; kurtosis > 0 = fatter tails than a normal curve" />
        </div>
        {skipped.length > 0 && (
          <div className="meta" style={{ marginTop: 6 }}>
            Signals not taken: {skipped.map(([k, v]) => `${v} × ${k.replace(/_/g, " ")}`).join(" · ")}
            {skipped.some(([k]) => k === "no_capital") && " — raise capital or leverage, or size smaller"}
          </div>
        )}
      </div>
      {(s.in_sample || r.monte_carlo) && (
        <div className="algo-quad">
          {s.in_sample && s.out_of_sample && (
            <div className="widget">
              <h3>In-sample vs out-of-sample <span className="meta">split at {ist(s.oos_from ?? 0)}</span></h3>
              <div className="algo-scroll"><table>
                <thead><tr><th></th><th>In-sample</th><th>Out-of-sample</th></tr></thead>
                <tbody>{([["trades", "Trades", 0], ["win_rate", "Win %", 1], ["net_pnl", "Net ₹", 0], ["profit_factor", "Profit factor", 2],
                  ["sharpe", "Sharpe", 2], ["expectancy", "Expectancy ₹", 0], ["max_drawdown_pct", "Max DD %", 1], ["t_stat", "t-stat", 2]] as [string, string, number][]).map(([k, lab, dp]) => (
                  <tr key={k}><td>{lab}</td><td className="mono">{num(s.in_sample![k], dp)}</td>
                    <td className={`mono ${k === "net_pnl" ? pnlCls(s.out_of_sample![k]) : ""}`}>{num(s.out_of_sample![k], dp)}</td></tr>))}
                </tbody></table></div>
              <div className="meta">Judge the strategy on the right-hand column — it never influenced the rules.</div>
            </div>
          )}
          {r.monte_carlo && (
            <div className="widget">
              <h3>Monte Carlo <span className="meta">{r.monte_carlo.runs} resampled paths of {r.monte_carlo.trades} trades</span></h3>
              <div className="algo-scroll"><table>
                <tbody>
                  <tr><td>Net P&L — bad case (5%)</td><td className={`mono ${pnlCls(r.monte_carlo.net_p5)}`}>{inr(r.monte_carlo.net_p5)}</td></tr>
                  <tr><td>Net P&L — median</td><td className={`mono ${pnlCls(r.monte_carlo.net_p50)}`}>{inr(r.monte_carlo.net_p50)}</td></tr>
                  <tr><td>Net P&L — good case (95%)</td><td className={`mono ${pnlCls(r.monte_carlo.net_p95)}`}>{inr(r.monte_carlo.net_p95)}</td></tr>
                  <tr><td>Max drawdown — median</td><td className="mono">{num(r.monte_carlo.dd_pct_p50, 1)}%</td></tr>
                  <tr><td>Max drawdown — bad case (95%)</td><td className="mono pnl-neg">{num(r.monte_carlo.dd_pct_p95, 1)}%</td></tr>
                  <tr><td>Chance of ending at a loss</td><td className="mono">{num(r.monte_carlo.prob_loss_pct, 1)}%</td></tr>
                  <tr><td>Chance equity halves at some point</td><td className="mono">{num(r.monte_carlo.prob_half_capital_pct, 1)}%</td></tr>
                </tbody></table></div>
              <div className="meta">The backtest is one ordering of these trades. Size capital for the 95% drawdown, not the one history happened to give.</div>
            </div>
          )}
        </div>
      )}
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
        {dd.length > 1 && (
          <ResponsiveContainer width="100%" height={110}>
            <AreaChart data={dd}>
              <XAxis dataKey="t" hide />
              <YAxis tick={{ fontSize: 10, fill: "var(--text-dim)" }} width={70} tickFormatter={(v: number) => `${v.toFixed(0)}%`} />
              <Tooltip contentStyle={{ background: "var(--bg-panel)", border: "1px solid var(--border)" }} formatter={(v: number) => `${v.toFixed(2)}%`} />
              <Area type="stepAfter" dataKey="dd" stroke="var(--red)" fill="var(--red-bg)" isAnimationActive={false} name="drawdown" />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>
      <TradeAnalytics r={r} />
      <div className="widget widget-wide" style={{ marginBottom: 12 }}>
        <h3>{r.chart.symbol} <span className="meta">last {r.chart.candles.length} {barLabel(r.spec)} candles of the underlying · ▲▼ entries, ● exits{r.chart.candles.some((k) => k[6] !== null && k[6] !== undefined) ? " · volume coloured by delta, cumulative delta below" : ""}</span></h3>
        <TradeChart chart={r.chart} />
      </div>
      <div className="algo-quad">
        <div className="widget">
          <h3>By symbol</h3>
          <div className="algo-scroll"><table><thead><tr><th>Symbol</th><th>Bars</th><th>Trades</th><th>Win %</th><th>PF</th><th>Net</th><th title="the underlying's own move">B&amp;H</th></tr></thead>
            <tbody>{Object.entries(r.per_symbol).map(([k, v]) => (
              <tr key={k}><td>{shortSym(k)}</td><td className="mono">{v.bars}</td><td className="mono">{v.trades}</td><td className="mono">{num(v.win_rate, 1)}</td>
                <td className="mono">{num(v.profit_factor)}</td><td className={`mono ${pnlCls(v.net_pnl)}`}>{inr(v.net_pnl)}</td>
                <td className={`mono ${pnlCls(v.buy_hold_pct)}`}>{v.buy_hold_pct === null ? "—" : `${num(v.buy_hold_pct, 1)}%`}</td></tr>))}
            </tbody></table></div>
        </div>
        <div className="widget">
          <h3>By exit reason</h3>
          <div className="algo-scroll"><table><thead><tr><th>Reason</th><th>Count</th><th>Net</th></tr></thead>
            <tbody>{Object.entries(r.by_reason).map(([k, v]) => (
              <tr key={k}><td>{k}</td><td className="mono">{v.count}</td><td className={`mono ${pnlCls(v.net)}`}>{inr(v.net)}</td></tr>))}
            </tbody></table></div>
        </div>
        <div className="widget">
          <h3>By month</h3>
          <div className="algo-scroll"><table><thead><tr><th>Month</th><th>Net</th></tr></thead>
            <tbody>{Object.entries(s.monthly ?? {}).map(([k, v]) => (
              <tr key={k}><td>{k}</td><td className={`mono ${pnlCls(v)}`}>{inr(v)}</td></tr>))}
            </tbody></table></div>
        </div>
        <div className="widget">
          <h3>By weekday</h3>
          <div className="algo-scroll"><table><thead><tr><th>Day</th><th>Net</th></tr></thead>
            <tbody>{Object.entries(s.weekday ?? {}).map(([k, v]) => (
              <tr key={k}><td>{k}</td><td className={`mono ${pnlCls(v)}`}>{inr(v)}</td></tr>))}
            </tbody></table></div>
        </div>
      </div>
      <div className="widget widget-wide">
        <h3>Trades <span className="meta">newest first · showing {trades.length} of {r.trades_total}{multiLeg ? " · entry/exit = underlying for multi-leg; hover a row for its legs" : ""}</span>
          {r.trades.length > 200 && <button type="button" className="btn-sm" style={{ marginLeft: 8 }} onClick={() => setShowAll(!showAll)}>{showAll ? "fewer" : "show all"}</button>}
        </h3>
        <div style={{ maxHeight: 420, overflow: "auto" }}>
          <table>
            <thead><tr><th>Instrument</th><th>Signal</th><th>Qty</th><th>Entry (IST)</th><th>Entry</th><th>Exit (IST)</th><th>Exit</th><th>Reason</th><th>Bars</th><th title="max adverse / favourable excursion, % of entry">MAE / MFE %</th><th title="net / initial stop risk">R</th>
              {withGreeks && <><th title="IV the legs were priced at, at entry">IV%</th><th title="net delta per set at entry, in lots">Δ</th><th title="₹ per day per set at entry">Θ/day</th><th title="net premium per set at entry: + received, − paid">credit</th></>}
              <th>Charges</th><th>Net</th></tr></thead>
            <tbody>{trades.map((t, i) => (
              <tr key={i} title={t.legs.map(legLine).join("\n")}>
                <td>{t.instrument.length > 46 ? `${t.legs.length} legs · ${shortSym(t.symbol)}` : t.instrument.replace(/^NSE:/, "")}</td>
                <td><span className={`badge ${t.side === "BUY" ? "buy" : "sell"}`}>{t.side === "BUY" ? "LONG" : "SHORT"}</span></td>
                <td className="mono">{t.qty}</td><td className="mono">{ist(t.entry_t)}</td><td className="mono">{t.entry.toFixed(2)}</td>
                <td className="mono">{ist(t.exit_t)}</td><td className="mono">{t.exit.toFixed(2)}</td><td>{t.reason}</td>
                <td className="mono">{t.bars}</td><td className="mono">{num(t.mae_pct, 2)} / {num(t.mfe_pct, 2)}</td>
                <td className={`mono ${pnlCls(t.r)}`}>{num(t.r, 2)}</td>
                {withGreeks && <><td className="mono">{num(t.greeks?.iv, 1)}</td><td className="mono">{num(t.greeks?.delta, 2)}</td>
                  <td className={`mono ${pnlCls(t.greeks?.theta)}`}>{num(t.greeks?.theta, 0)}</td><td className="mono">{num(t.greeks?.credit, 0)}</td></>}
                <td className="mono">{t.charges.toFixed(0)}</td><td className={`mono ${pnlCls(t.net)}`}>{t.net.toFixed(0)}</td></tr>))}
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

const istDay = (e: number) =>
  new Date(e * 1000).toLocaleDateString("en-IN", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short", year: "2-digit" });

/** Two swept parameters as a grid coloured by the metric: a broad plateau is a
 *  real edge; one bright cell among dark neighbours is a fitted fluke. */
function Heatmap({ cells, metric, minTrades, label, onApply }: {
  cells: HeatCell[]; metric: string; minTrades: number; label: (p: string) => string; onApply: (p: Record<string, number>) => void;
}) {
  const [ya, xa] = Object.keys(cells[0].params);
  const uniq = (k: string) => Array.from(new Set(cells.map((c) => c.params[k]))).sort((a, b) => a - b);
  const xs = uniq(xa);
  const ys = uniq(ya);
  const at = new Map(cells.map((c) => [`${c.params[ya]}|${c.params[xa]}`, c]));
  const ranked = (c?: HeatCell) => !!c && c.value !== null && c.trades >= minTrades;
  const vals = cells.filter(ranked).map((c) => c.value as number);
  const lo = vals.length ? Math.min(...vals) : 0;
  const hi = vals.length ? Math.max(...vals) : 0;
  const span = Math.max(Math.abs(lo), Math.abs(hi)) || 1;
  // diverging around 0 when the metric went negative somewhere, else shaded low -> high
  const bg = (v: number) => (lo < 0
    ? v >= 0 ? `rgba(0,215,135,${0.12 + 0.6 * (v / span)})` : `rgba(255,56,56,${0.12 + 0.6 * (-v / span)})`
    : `rgba(0,215,135,${0.08 + 0.64 * ((v - lo) / (hi - lo || 1))})`);
  return (
    <div className="algo-scroll" style={{ marginBottom: 10 }} data-testid="opt-heatmap">
      <div className="meta" style={{ marginBottom: 4 }}>{metric} by {label(ya)} (rows) × {label(xa)} (columns) — look for a broad bright area, not one bright cell. Click a cell to apply it; · = too few trades.</div>
      <table>
        <thead><tr><th></th>{xs.map((x) => <th key={x} className="mono">{x}</th>)}</tr></thead>
        <tbody>{ys.map((y) => (
          <tr key={y}><th className="mono">{y}</th>{xs.map((x) => {
            const c = at.get(`${y}|${x}`);
            const v = c && ranked(c) ? (c.value as number) : null;
            return (
              <td key={x} className="mono" style={{ textAlign: "center", background: v === null ? undefined : bg(v), cursor: v === null ? undefined : "pointer" }}
                title={c ? `${c.trades} trades` : ""} onClick={() => { if (c && v !== null) onApply(c.params); }}>
                {v === null ? "·" : num(v, Math.abs(v) >= 100 ? 0 : 2)}
              </td>
            );
          })}</tr>))}
        </tbody>
      </table>
    </div>
  );
}

/** Rolling walk-forward: each window traded with the winner of the in-sample window before it. */
function WalkForward({ r, label, onApply }: { r: WfResult; label: (p: string) => string; onApply: (p: Record<string, number>) => void }) {
  const keys = Object.keys(r.folds.find((f) => f.params)?.params ?? {});
  const eq = useMemo(() => r.oos.equity.map(([t, v]) => ({ t: ist(t), v })), [r]);
  return (
    <div data-testid="walk-forward">
      <div className="meta" style={{ marginBottom: 6 }}>
        {r.combos} combos × {r.windows} {r.anchored ? "anchored" : "rolling"} windows in {r.elapsed_s}s. Each window was traded with the combo that ranked best on {r.metric} in
        the in-sample period before it — the stitched windows never influenced their own parameters.
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 18, marginBottom: 8 }}>
        <Metric label="Out-of-sample net" value={inr(r.oos.net_pnl)} cls={pnlCls(r.oos.net_pnl)} hint="every window's unseen trades, stitched" />
        <Metric label="OOS trades" value={String(r.oos.trades)} />
        <Metric label="OOS win rate" value={`${num(r.oos.win_rate, 1)}%`} />
        <Metric label="OOS profit factor" value={num(r.oos.profit_factor)} />
        <Metric label="OOS Sharpe" value={num(r.oos.sharpe)} />
        <Metric label="OOS max DD" value={`${inr(r.oos.max_drawdown)} (${num(r.oos.max_drawdown_pct, 1)}%)`} cls="pnl-neg" />
        <Metric label="WF efficiency" value={r.wfe_pct === null ? "—" : `${num(r.wfe_pct, 0)}%`} cls={r.wfe_pct === null ? "" : r.wfe_pct >= 50 ? "pnl-pos" : "pnl-neg"}
          hint="out-of-sample P&L per day ÷ in-sample P&L per day. Around 50%+ is usually called robust; near 0 or negative = the optimiser fitted noise" />
      </div>
      {eq.length > 1 && (
        <ResponsiveContainer width="100%" height={200}>
          <LineChart data={eq}>
            <CartesianGrid stroke="var(--border-soft)" />
            <XAxis dataKey="t" tick={{ fontSize: 10, fill: "var(--text-dim)" }} minTickGap={60} />
            <YAxis tick={{ fontSize: 10, fill: "var(--text-dim)" }} domain={["auto", "auto"]} width={70} />
            <Tooltip contentStyle={{ background: "var(--bg-panel)", border: "1px solid var(--border)" }} formatter={(v: number) => inr(v)} />
            <Line type="monotone" dataKey="v" stroke="var(--accent)" dot={false} strokeWidth={1.5} isAnimationActive={false} name="out-of-sample equity" />
          </LineChart>
        </ResponsiveContainer>
      )}
      <div className="algo-scroll">
        <table>
          <thead><tr><th>#</th><th>Ranked on</th><th>Traded unseen</th>{keys.map((k) => <th key={k} title={k}>{label(k)}</th>)}<th>IS {r.metric}</th><th>OOS trades</th><th>OOS PF</th><th>OOS net</th><th></th></tr></thead>
          <tbody>{r.folds.map((f, i) => (
            <tr key={i}>
              <td>{i + 1}</td>
              <td className="mono">{istDay(f.is_from)} → {istDay(f.oos_from)}</td>
              <td className="mono">{istDay(f.oos_from)} → {istDay(f.oos_to - 1)}</td>
              {f.params ? (
                <>
                  {keys.map((k) => <td key={k} className="mono">{f.params![k]}</td>)}
                  <td className="mono">{num(f.is_value)}</td><td className="mono">{f.oos?.trades ?? "—"}</td><td className="mono">{num(f.oos?.profit_factor)}</td>
                  <td className={`mono ${pnlCls(f.oos?.net_pnl)}`}>{inr(f.oos?.net_pnl)}</td>
                  <td><button type="button" className="btn-sm" onClick={() => onApply(f.params!)}>apply</button></td>
                </>
              ) : <td colSpan={keys.length + 5} className="meta">no combo had enough in-sample trades — not traded</td>}
            </tr>))}
          </tbody>
        </table>
      </div>
      <div className="meta">Parameters that jump from window to window are a warning sign; stable ones that keep earning unseen are the real thing. The last window's winner is what you would trade next.</div>
    </div>
  );
}

function Optimizer({ spec, cat, range, onApply }: { spec: Spec; cat: Catalog; range: { start: string; end: string }; onApply: (params: Record<string, number>) => void }) {
  const paths = useMemo(() => numericPaths(spec, cat), [spec, cat]);
  const [axes, setAxes] = useState<{ path: string; from: number; to: number; step: number }[]>([]);
  const [metric, setMetric] = useState("net_pnl");
  const [minTrades, setMinTrades] = useState(10);
  const [oos, setOos] = useState(30);
  // "wf" = rolling walk-forward: re-rank on each in-sample window, trade the next window unseen.
  const [mode, setMode] = useState<"rank" | "wf">("rank");
  const [wf, setWf] = useState({ windows: 4, is_mult: 3, anchored: false });
  const values = (a: { from: number; to: number; step: number }) => {
    const out: number[] = [];
    if (a.step <= 0) return [a.from];
    for (let v = a.from; v <= a.to + 1e-9 && out.length < 60; v += a.step) out.push(Math.round(v * 1e6) / 1e6);
    return out;
  };
  const combos = axes.reduce((n, a) => n * values(a).length, axes.length ? 1 : 0);
  // The builder can change under the grid (a condition removed, a stop switched off) — those axes can't run.
  const missing = axes.filter((a) => !paths.some((p) => p.path === a.path)).length;
  const run = useMutation({
    mutationFn: () => longPost<OptResult | WfResult>("/api/algo/optimize", {
      spec: tidy(spec), ...range, metric, min_trades: minTrades,
      // clamped here, not per keystroke (typing "10" passes through "1")
      ...(mode === "wf" ? { walk_forward: { ...wf, windows: Math.max(2, Math.min(12, wf.windows)), is_mult: Math.max(0.5, Math.min(10, wf.is_mult)) } }
        : { oos_pct: oosParam(oos) }),
      grid: axes.map((a) => ({ path: a.path, values: values(a) })),
    }),
  });
  const opt = run.data && !isWf(run.data) ? run.data : null;
  const wfr = run.data && isWf(run.data) ? run.data : null;
  const heat = opt?.all && opt.all.length && Object.keys(opt.all[0].params).length === 2 ? opt.all : null;
  const label = (p: string) => paths.find((x) => x.path === p)?.label ?? p;
  const axisFor = (p: { path: string; value: number }) => ({ path: p.path, from: p.value, to: p.value ? p.value * 2 : 10, step: p.value >= 4 ? Math.round(p.value / 4) : p.value ? p.value / 4 : 1 });
  return (
    <div className="widget widget-wide">
      <h3>Optimise <span className="meta">grid search over the builder's strategy, {range.start} → {range.end}</span></h3>
      <div style={{ display: "grid", gap: 6, marginBottom: 8 }}>
        {axes.map((a, i) => (
          <div key={i} className="algo-operand">
            <select value={a.path} onChange={(e) => {
              const p = paths.find((x) => x.path === e.target.value);
              if (p) setAxes(axes.map((x, j) => (j === i ? axisFor(p) : x)));
            }}>
              {!paths.some((p) => p.path === a.path) && <option value={a.path}>{a.path} — no longer in the strategy</option>}
              {paths.filter((p) => p.path === a.path || !axes.some((x) => x.path === p.path)).map((p) => <option key={p.path} value={p.path}>{p.label}</option>)}
            </select>
            {(["from", "to", "step"] as const).map((k) => (
              <label key={k} className="meta">{k} <NumInput value={a[k]} onChange={(n) => setAxes(axes.map((x, j) => (j === i ? { ...x, [k]: n ?? 0 } : x)))} /></label>
            ))}
            <span className="meta">{values(a).length} values</span>
            <button type="button" className="btn-sm danger" onClick={() => setAxes(axes.filter((_, j) => j !== i))}>✕</button>
          </div>
        ))}
        <div className="algo-operand" style={{ gap: 10 }}>
          <button type="button" className="btn-sm" disabled={axes.length >= paths.length} onClick={() => {
            const p = paths.find((x) => !axes.some((a) => a.path === x.path));
            if (p) setAxes([...axes, axisFor(p)]);
          }}>+ parameter</button>
          <label className="meta">rank by <select value={metric} onChange={(e) => setMetric(e.target.value)}>{cat.metrics.map((m) => <option key={m}>{m}</option>)}</select></label>
          <label className="meta" title="one split: rank on the earlier part, check the held-out end. Walk-forward: cut the range into windows, re-rank on each in-sample window and trade the next one unseen — the stitched unseen windows are what re-optimising would really have earned">check
            <select value={mode} onChange={(e) => setMode(e.target.value as "rank" | "wf")}>
              <option value="rank">one out-of-sample split</option><option value="wf">rolling walk-forward</option>
            </select>
          </label>
          {mode === "rank" ? (
            <label className="meta" title="the last X% of the range is held out: ranking uses only the earlier part, and each row shows how it did afterwards. 5–80, 0 = off">hold out last <NumInput width={36} value={oos} onChange={(n) => setOos(Math.max(0, Math.min(80, Math.floor(n ?? 0))))} />% as out-of-sample</label>
          ) : (
            <>
              <label className="meta" title="out-of-sample windows (2–12), each traded with the winner of the in-sample window before it">windows <NumInput width={36} value={wf.windows} onChange={(n) => setWf({ ...wf, windows: Math.floor(n ?? 4) })} /></label>
              <label className="meta" title="in-sample length in out-of-sample windows (0.5–10): 3 = rank on 3 windows of history, trade the 4th">in-sample × <NumInput width={36} value={wf.is_mult} onChange={(n) => setWf({ ...wf, is_mult: n ?? 3 })} /></label>
              <label className="meta" title="anchored: every in-sample window starts at the beginning of the range (grows); rolling: a fixed-length window that slides"><input type="checkbox" checked={wf.anchored} onChange={(e) => setWf({ ...wf, anchored: e.target.checked })} /> anchored</label>
            </>
          )}
          <label className="meta" title="combos with fewer trades are not ranked — a 1-trade 100% win rate means nothing">min trades <NumInput width={52} value={minTrades} onChange={(n) => setMinTrades(Math.max(1, Math.floor(n ?? 1)))} /></label>
          <span className="meta">{combos} combinations (max 400)</span>
          <button type="button" className="primary" disabled={!combos || combos > 400 || missing > 0 || run.isPending} onClick={() => run.mutate()}>{run.isPending ? "Optimising…" : "Run optimisation"}</button>
        </div>
        {!axes.length && <div className="meta">Add the parameters to sweep with “+ parameter”, then run. Apply a row to send its values back to the builder.</div>}
        {missing > 0 && <div className="meta pnl-neg">{missing} parameter{missing > 1 ? "s are" : " is"} no longer in the builder's strategy — remove {missing > 1 ? "them" : "it"} (✕) to run.</div>}
      </div>
      {run.error && <div className="pnl-neg">{errMsg(run.error)}</div>}
      {wfr && <WalkForward r={wfr} label={label} onApply={onApply} />}
      {opt && (
        <>
          {overfittingLine(opt.overfitting) && (
            <div className="meta" style={{ marginBottom: 6 }} data-testid="overfitting-line">{overfittingLine(opt.overfitting)}</div>
          )}
          <div className="meta" style={{ marginBottom: 6 }}>{opt.combos} combos in {opt.elapsed_s}s · {opt.too_few_trades} skipped for too few trades.{opt.oos_from ? ` Ranked on the in-sample period only (before ${ist(opt.oos_from)}); trust rows whose OOS columns hold up.` : " Beware overfitting — hold out an out-of-sample % or run a walk-forward to check."}</div>
          {heat && <Heatmap cells={heat} metric={opt.metric} minTrades={minTrades} label={label} onApply={onApply} />}
          <div style={{ maxHeight: 480, overflow: "auto" }}>
            <table>
              <thead><tr><th></th>{axes.map((a) => <th key={a.path} title={a.path}>{label(a.path)}</th>)}<th>Trades</th><th>Win %</th><th>PF</th><th>Sharpe</th><th>t</th><th>Max DD</th><th>Net</th>{opt.oos_from ? <><th title="out-of-sample">OOS trades</th><th title="out-of-sample">OOS PF</th><th title="out-of-sample">OOS net</th></> : null}</tr></thead>
              <tbody>{opt.ranked.map((r, i) => (
                <tr key={i}>
                  <td><button type="button" className="btn-sm" onClick={() => onApply(r.params)}>apply</button></td>
                  {axes.map((a) => <td key={a.path} className="mono">{r.params[a.path]}</td>)}
                  <td className="mono">{r.trades}</td><td className="mono">{num(r.win_rate, 1)}</td><td className="mono">{num(r.profit_factor)}</td>
                  <td className="mono">{num(r.sharpe)}</td><td className="mono">{num(r.t_stat)}</td><td className="mono">{inr(r.max_drawdown)}</td><td className={`mono ${pnlCls(r.net_pnl)}`}>{inr(r.net_pnl)}</td>
                  {opt.oos_from ? <><td className="mono">{r.oos?.trades ?? "—"}</td><td className="mono">{num(r.oos?.profit_factor)}</td>
                    <td className={`mono ${pnlCls(r.oos?.net_pnl)}`}>{inr(r.oos?.net_pnl)}</td></> : null}
                </tr>))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

/** Live MTM of one open algo trade from the tick store: Σ act × (ltp − entry) × qty
 *  over legs (closed legs at their exit). Falls back to the server's figure. */
function liveMtm(t: LiveTrade): number | null {
  const legs = t.legs ?? [];
  if (!legs.length) return t.unrealized ?? null;
  let total = 0;
  for (const lg of legs) {
    const px = lg.exit ?? peekQuote(lg.symbol)?.last_price ?? lg.ltp;
    if (px == null) return t.unrealized ?? null;
    total += lg.act * (px - lg.entry) * lg.qty;
  }
  return total;
}

function AlgoLiveMtm({ t }: { t: LiveTrade }) {
  useQuoteTick();
  const v = liveMtm(t);
  return <td className={`mono ${pnlCls(v)}`} data-testid={`algo-mtm-${t.id}`}>{inr(v)}</td>;
}

// The reference price straight off the websocket, flashing green / red on each up / down tick.
function AlgoLiveLtp({ t }: { t: LiveTrade }) {
  useQuoteTick();
  const ref = t.ref === "u" ? t.symbol : t.legs?.[0]?.symbol ?? t.symbol;
  const px = peekQuote(ref)?.last_price ?? t.ltp ?? null;
  const last = useRef<number | null>(null);
  const [dir, setDir] = useState<"up" | "down" | null>(null);
  useEffect(() => {
    if (px == null) return;
    const prev = last.current;
    last.current = px;
    if (prev == null || prev === px) return;
    setDir(px > prev ? "up" : "down");
    const h = setTimeout(() => setDir(null), 350);
    return () => clearTimeout(h);
  }, [px]);
  return <span data-testid={`algo-ltp-${t.id}`} style={dir ? { color: dir === "up" ? "var(--green)" : "var(--red)", fontWeight: 600 } : undefined}>{num(px)}</span>;
}

// Keep every symbol an open automation position holds streaming to this
// browser, so its LTP / MTM cells move on each websocket tick.
function useWatchOpen(open: LiveTrade[]) {
  const key = [...new Set(open.flatMap((t) => [t.symbol, ...(t.legs ?? []).map((l) => l.symbol)]))].sort().join(",");
  useEffect(() => {
    const releases = key ? key.split(",").map((s) => watchSymbol(s)) : [];
    return () => releases.forEach((r) => r());
  }, [key]);
}

function Automations({ onEdit }: { onEdit: (s: Saved) => void }) {
  const qc = useQueryClient();
  const strategies = useQuery({ queryKey: ["algo", "strategies"], queryFn: () => api.get<{ strategies: Saved[] }>("/api/algo/strategies"), refetchInterval: 10000 });
  // 1 s: the endpoint reads the tick stream (no Fyers REST per call); the MTM cells tick per frame on top of it.
  const status = useQuery({ queryKey: ["algo", "status"], queryFn: () => api.get<RunnerStatus>("/api/algo/status"), refetchInterval: 1000 });
  const accounts = useQuery({ queryKey: ["algo", "accounts"], queryFn: () => api.get<{ accounts: Account[] }>("/api/broker-accounts") });
  // Strategies whose trades, positions and log lines are shown; empty = all of them.
  const [sel, setSel] = useState<number[]>([]);
  const selKey = [...sel].sort((a, b) => a - b).join(",");
  const toggleSel = (id: number) => setSel((cur) => cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]);
  const shown = (id: number | undefined) => !sel.length || (id != null && sel.includes(id));
  const trades = useQuery({
    queryKey: ["algo", "trades", selKey], refetchInterval: 3000,
    queryFn: () => api.get<{ trades: LiveTrade[] }>(`/api/algo/trades?limit=300${selKey ? `&strategy_ids=${selKey}` : ""}`),
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
  const nameOf = (id: number) => strategies.data?.strategies.find((s) => s.id === id)?.name ?? String(id);
  const openShown = (st?.open ?? []).filter((t) => shown(t.strategy_id));
  useWatchOpen(st?.open ?? []);
  useQuoteTick();   // re-render the header total per tick
  const selNames = sel.map(nameOf).join(", ");
  const events = (st?.events ?? []).filter((e) => shown(e.strategy_id));
  const tk = st?.ticks;
  const tickAgo = tk?.last_exit_check ? Math.max(0, Math.round(Date.now() / 1000 - tk.last_exit_check)) : null;
  const liveTotal = openShown.reduce((a, t) => a + (liveMtm(t) ?? 0), 0);
  const closed = (trades.data?.trades ?? []).filter((t) => t.status === "closed");
  const summary = {
    n: closed.length,
    wins: closed.filter((t) => (t.net_pnl ?? 0) > 0).length,
    net: closed.reduce((a, t) => a + (t.net_pnl ?? 0), 0),
  };
  const instLabel = (s: Saved) => {
    const i = s.spec.instrument;
    if (!i || i.type === "equity") return "equity";
    if (i.type === "future") return `futures · ${i.expiry}`;
    const legs = (s.spec.direction === "short" ? i.legs_short : i.legs_long) ?? [];
    return `options · ${i.expiry_kind} ${i.expiry} · ${legs.filter((l) => l.lots).map((l) => l.right === "FUT" ? `${l.action[0]} FUT` : `${l.action[0]} ${l.strike === "PREMIUM" ? `₹${l.premium}` : l.strike + (l.steps ? l.steps : "")} ${l.right}`).join(", ")}`;
  };
  return (
    <>
      {err && <div className="widget widget-wide pnl-neg" style={{ marginBottom: 12 }}>{err}</div>}
      <div className="widget widget-wide" style={{ marginBottom: 12 }}>
        <h3>Automations <span className="meta" data-testid="algo-runner-state" title={
          "Entries on each completed candle; stops / targets on every websocket tick, with a 1 s REST sweep as backstop."
          + (tk ? ` ${tk.seen.toLocaleString()} ticks · ${tk.watching} symbol(s) watched${tickAgo !== null ? ` · last check ${tickAgo}s ago` : ""}.` : "")}>
          {(([word, color]) => <><span style={{ color }}>●</span> {word}</>)(
            !st?.running ? ["stopped", "var(--red)"] : ago !== null && ago < 30 ? ["live", "var(--green)"] : ["idle", "var(--amber)"])}
        </span></h3>
        {(strategies.data?.strategies ?? []).length === 0 ? (
          <div className="empty">No saved strategies yet — build one in the Builder & backtest tab and press “Save as new strategy”.</div>
        ) : (
          <div style={grow()} data-testid="algo-auto-panel">
            <table>
              <thead><tr><th title="tick strategies to show only their trades, positions and log">Show</th><th>On</th><th>Strategy</th><th>Version</th><th>Symbols</th><th>Trades</th><th>TF</th><th>Mode</th><th>Open</th><th>Closed</th><th>Realised</th><th></th></tr></thead>
              <tbody>{strategies.data!.strategies.map((s) => (
                <tr key={s.id} style={sel.includes(s.id) ? { background: "var(--bg-row)" } : undefined}>
                  <td><input type="checkbox" checked={sel.includes(s.id)} onChange={() => toggleSel(s.id)}
                    title="show this strategy's trades (tick several to compare them)" data-testid={`algo-sel-${s.id}`} /></td>
                  <td><Toggle on={s.enabled} size="sm" onChange={(on: boolean) => {
                    // Switching on a LIVE automation starts real orders — confirm it every time, not just when the mode was picked.
                    if (on && s.mode === "live" && !window.confirm(`REAL MONEY: switch on "${s.name}"? It will place real orders on the next signal.`)) return;
                    put(s.id, { enabled: on });
                  }} /></td>
                  <td style={{ whiteSpace: "normal", minWidth: 140, maxWidth: 220, overflowWrap: "anywhere" }}><a href="#/algo" onClick={(e) => { e.preventDefault(); toggleSel(s.id); }}>{s.name}</a>{" "}
                    <button type="button" className="btn-sm" onClick={() => setSel([s.id])} title="only this automation's trades and open positions" data-testid={`algo-trades-${s.id}`}>Trades</button></td>
                  <td>
                    <select value={s.version} title="the version that runs — switch back and forth any time" onChange={async (e) => {
                      setErr(null);
                      try { await api.post(`/api/algo/strategies/${s.id}/versions/${e.target.value}/activate`, {}); } catch (er) { setErr(errMsg(er)); }
                      refresh();
                    }}>
                      {Array.from({ length: s.versions || 1 }, (_, k) => (s.versions || 1) - k).map((v) => <option key={v} value={v}>v{v}{v === s.version ? " ✓" : ""}</option>)}
                    </select>
                  </td>
                  <td className="mono" style={{ maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis" }} title={s.spec.symbols.join(", ")}>{s.spec.symbols.map(shortSym).join(", ")}</td>
                  <td className="meta" style={{ minWidth: 180, maxWidth: 260, whiteSpace: "normal" }}>{instLabel(s)}</td>
                  <td className="mono">{barLabel(s.spec)}</td>
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
                    <button type="button" className="btn-sm" onClick={() => onEdit(s)}>edit</button>{" "}
                    <button type="button" className="btn-sm" disabled={!s.open_positions} onClick={async () => {
                      if (!window.confirm(`Square off every open position of "${s.name}" now?`)) return;
                      try { const r = await api.post<{ closed: number; failed: string[] }>(`/api/algo/strategies/${s.id}/squareoff`, {}); setErr(r.failed.length ? `could not close: ${r.failed.join(", ")}` : null); } catch (e) { setErr(errMsg(e)); }
                      refresh();
                    }}>square off</button>{" "}
                    <button type="button" className="btn-sm danger" onClick={async () => {
                      if (!window.confirm(`Delete "${s.name}" and its trade history?`)) return;
                      try { await api.delete(`/api/algo/strategies/${s.id}`); } catch (e) { setErr(errMsg(e)); }
                      refresh();
                    }}>delete</button>
                  </td>
                </tr>))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      <div className="widget widget-wide" style={{ marginBottom: 12 }}>
        <h3>Open positions {sel.length ? <span className="meta">— {selNames}</span> : null} <span className="meta">· live MTM {inr(liveTotal)}</span></h3>
        {!openShown.length ? <div className="empty">flat</div> : (
          <div style={grow()} data-testid="algo-open-panel">
            <table>
              <thead><tr><th>Strategy</th><th>Instrument</th><th>Signal</th><th>Qty</th><th>Mode</th><th>Ref entry</th><th>Ref LTP</th><th>Stop</th><th>Trail</th><th>Target</th><th>MTM</th><th>Since</th></tr></thead>
              <tbody>{openShown.map((t) => (
                <tr key={t.id} title={(t.legs ?? []).map((l) => `${l.act > 0 ? "BUY" : "SELL"} ${l.qty} ${l.label ?? l.symbol}: ${num(l.entry)} → ${num(l.ltp)}`).join("\n")}>
                  <td>{nameOf(t.strategy_id)}</td><td><a href={tradeChart(t)} target="_blank" rel="noopener" className="sym-link" title="Open the chart at this trade (15m)">{(t.instrument ?? t.symbol).replace(/NSE:/g, "")}</a></td>
                  <td><span className={`badge ${t.side === "BUY" ? "buy" : "sell"}`}>{t.side === "BUY" ? "LONG" : "SHORT"}</span></td><td className="mono">{t.quantity}</td><td>{t.mode}</td>
                  <td className="mono">{num(t.entry_price)}</td><td className="mono"><AlgoLiveLtp t={t} /></td><td className="mono">{t.be_on ? `BE ${num(t.entry_price)}` : num(t.stop_loss)}</td>
                  <td className="mono">{num(t.trail_stop)}</td><td className="mono">{num(t.target)}</td>
                  <AlgoLiveMtm t={t} /><td className="mono">{isoIst(t.entry_at)}</td></tr>))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      <div className="widget widget-wide" style={{ marginBottom: 12 }}>
          <h3>Trades {sel.length ? <span className="meta" data-testid="algo-trades-filter">— {selNames} <button type="button" className="btn-sm" onClick={() => setSel([])}>show all</button></span> : <span className="meta">— all strategies · tick strategies above to show only theirs</span>}</h3>
          <div className="meta" data-testid="algo-trade-summary">{summary.n} closed · {summary.n ? Math.round((summary.wins / summary.n) * 100) : 0}% winners · net <span className={pnlCls(summary.net)}>{inr(summary.net)}</span> · open MTM <span className={pnlCls(liveTotal)}>{inr(liveTotal)}</span></div>
          <div style={grow(420)} data-testid="algo-trades-panel">
            {!trades.data?.trades.length ? <div className="empty">none yet</div> : (
              <table>
                <thead><tr><th>Strategy</th><th>Instrument</th><th>Signal</th><th>Qty</th><th>Status</th><th>Entry</th><th>Exit</th><th>Reason</th><th>Net</th></tr></thead>
                <tbody>{trades.data.trades.map((t) => (
                  <tr key={t.id} title={[t.note ?? "", ...(t.legs ?? []).map((l) => `${l.act > 0 ? "BUY" : "SELL"} ${l.qty} ${l.label ?? l.symbol}: ${num(l.entry)} → ${num(l.exit)}`)].filter(Boolean).join("\n")}>
                    <td style={{ whiteSpace: "normal", maxWidth: 180, overflowWrap: "anywhere" }} data-testid={`algo-trade-strategy-${t.id}`}>{nameOf(t.strategy_id)}{t.version ? <span className="meta"> v{t.version}</span> : null}</td>
                    <td><a href={tradeChart(t)} target="_blank" rel="noopener" className="sym-link" title="Open the chart at this trade (15m)">{(t.instrument ?? t.symbol).replace(/NSE:/g, "")}</a></td><td>{t.side === "BUY" ? "LONG" : "SHORT"}</td><td className="mono">{t.quantity}</td>
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
      <TcaPanel />
      <div className="widget widget-wide">
          <h3>Runner log {sel.length ? <span className="meta">— {selNames}</span> : null}</h3>
          <div style={{ ...grow(320), fontSize: 12 }} data-testid="algo-log-panel">
            {!events.length ? <div className="empty">quiet</div> : events.map((e, i) => (
              <div key={i} className="mono" style={{ color: e.level === "error" ? "var(--red)" : e.level === "entry" ? "var(--green)" : e.level === "exit" ? "var(--accent)" : "var(--text-dim)" }}>
                {ist(e.t)} {e.msg}
              </div>))}
          </div>
      </div>
    </>
  );
}

type Universe = {
  enabled: boolean; lists: string[]; stocks: boolean; index_futures: boolean; stock_futures: boolean;
  next_futures: "always" | "roll" | "off"; index_options: boolean; index_strikes: number; index_next_strikes: number;
  stock_options: boolean; stock_strikes: number; stock_next_expiry: "roll" | "off"; roll_days: number;
};
type FlowCfg = { enabled: boolean; symbols: string[]; retention_days: number; raw_days: number; budget: number; min_free_gb: number; universe: Universe };
type PlanSummary = { at?: number; used?: number; budget?: number; classes?: Record<string, number>; wanted?: Record<string, number>;
  dropped?: Record<string, number>; no_price?: string[]; lists?: Record<string, { label: string; n: number; source: string }> };
type FlowStatus = {
  enabled: boolean; config: FlowCfg; config_symbols: string[]; strategy_symbols: string[];
  collector: {
    alive: boolean; state?: string; heartbeat?: number; fps?: number; cpu_pct?: number; lag_p50?: number; lag_p95?: number;
    budget?: { configured: number; active: number; note: string | null }; plan?: PlanSummary; errors?: string[];
    disk?: { free_gb: number | null; min_free_gb: number; note: string | null; paused: boolean };
    stream?: { connected: boolean; reason: string | null; subscribed: number; record_only: number; limit_error: string | null; rejected_count: number } | null;
    today?: { keys: number; trades: number; buy: number; sell: number; unk: number };
    supervisor?: { running: boolean; restarts: number; last_exit: { at: number; rc: number } | null } | null;
  };
  days: { count: number; first: string | null; last: string | null; retention: number };
  disk_mb: number; lists: Record<string, string>; keys_today: number;
  top: [string, number, number, number, number, number, number][];
};

const CLASS_LABEL: Record<string, string> = {
  focus: "hand-picked", index: "indices", index_fut: "index futures", index_opt: "index options", stock_fut: "stock futures",
  stocks: "stocks", index_opt2: "index options (next expiry)", fut_next: "next-month futures", stock_opt: "stock options",
  stock_opt2: "stock options (next expiry)", sticky: "earlier strikes",
};
const ymdLabel = (d: string | null) => (d ? `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6)}` : "—");

function RecorderPanel() {
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const st = useQuery({ queryKey: ["algo", "ticks", q], queryFn: () => api.get<FlowStatus>(`/api/algo/ticks/status?top=50${q ? `&q=${encodeURIComponent(q)}` : ""}`), refetchInterval: 10000 });
  const [syms, setSyms] = useState<string[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const cfg = st.data?.config;
  const list = syms ?? cfg?.symbols ?? [];
  const save = async (patch: Partial<Omit<FlowCfg, "universe">> & { universe?: Partial<Universe> }) => {
    setErr(null);
    try {
      await api.put("/api/algo/ticks/config", patch);
      setSyms(null);
      qc.invalidateQueries({ queryKey: ["algo", "ticks"] });
    } catch (e) { setErr(errMsg(e)); }
  };
  const fmt = (v: number) => Math.round(v).toLocaleString("en-IN");
  const col = st.data?.collector;
  const plan = col?.plan;
  const uni = cfg?.universe;
  const numIn = (value: number, onSave: (n: number) => void, min: number, max: number, testId: string, w = 60) => (
    <input type="number" min={min} max={max} defaultValue={value} key={`${testId}-${value}`} style={{ width: w }} data-testid={testId}
      onBlur={(e) => { const n = Math.round(Number(e.target.value)); if (Number.isFinite(n) && n !== value && n >= min && n <= max) onSave(n); }}
      onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }} />
  );
  const state = !cfg?.enabled ? ["off", "var(--text-dim)"] : col?.alive ? ["recording", "var(--green)"] : ["starting / not running", "var(--amber)"];
  return (
    <div className="widget widget-wide" style={{ marginBottom: 12 }} data-testid="flow-collector">
      <h3>Order-flow collector <span className="meta" title="Its own process and Fyers socket: every frame classified buy / sell (Lee-Ready) for the footprint, CVD and the order-flow strategies">
        <span style={{ color: state[1] }}>●</span> {state[0]}</span></h3>
      {err && <div className="pnl-neg">{err}</div>}
      <div className="algo-operand" style={{ gap: 12 }}>
        <Toggle on={!!cfg?.enabled} onChange={(on: boolean) => save({ enabled: on })} label="recording" />
        {col?.alive && (
          <span className="meta" data-testid="flow-health">
            {fmt(col.stream?.subscribed ?? 0)} symbols on its socket · {fmt(col.fps ?? 0)} frames/s · CPU {col.cpu_pct ?? 0}% · frames {col.lag_p95 ?? 0}s late (p95)
            {col.stream && !col.stream.connected ? ` · socket down: ${col.stream.reason ?? "connecting"}` : ""}
          </span>
        )}
      </div>
      {col?.budget?.note && <div className="meta" style={{ color: "var(--amber)" }}>Load: {col.budget.note}</div>}
      {col?.disk?.note && <div className="meta" style={{ color: col.disk.paused ? "var(--red)" : "var(--amber)" }} data-testid="flow-disk-note">Disk: {col.disk.note}</div>}
      {col?.stream?.limit_error && <div className="meta" style={{ color: "var(--red)" }}>Fyers refused the symbol count: {col.stream.limit_error} — the collector shrinks its budget to fit (Standard plan: 100 symbols, Prime: 5,000).</div>}
      {(col?.errors ?? []).length > 0 && <div className="meta" title={(col?.errors ?? []).join("\n")}>last error: {col!.errors![col!.errors!.length - 1]}</div>}
      <div className="algo-operand" style={{ gap: 14, marginTop: 8 }}>
        <label className="meta" title="Symbols on the collector's socket. Fyers allows 5,000 per user on Prime (100 on Standard), and the app's own price feed needs a few hundred of them">
          Budget {cfg && numIn(cfg.budget, (n) => save({ budget: n }), 50, 5000, "flow-budget", 70)} symbols
        </label>
        <label className="meta" title="A sliding window: when a new day is stored, the oldest beyond this many collected days is deleted">
          Keep {cfg && numIn(cfg.retention_days, (n) => save({ retention_days: n }), 1, 365, "flow-retention")} days
        </label>
        <label className="meta" title="Every frame of the hand-picked symbols (for re-classifying later)">
          raw ticks {cfg && numIn(cfg.raw_days, (n) => save({ raw_days: n }), 0, 30, "flow-raw")} days
        </label>
        <label className="meta" title="The disk guard: below this much free space the oldest days go early (the newest 5 stay); below half of it recording pauses until space is back">
          keep {cfg && numIn(cfg.min_free_gb ?? 5, (n) => save({ min_free_gb: n }), 1, 1000, "flow-min-free")} GB free
        </label>
        <span className="meta" data-testid="flow-days">
          stored: {st.data?.days.count ?? 0} of {st.data?.days.retention ?? 90} days ({ymdLabel(st.data?.days.first ?? null)} → {ymdLabel(st.data?.days.last ?? null)}) · {fmt(st.data?.disk_mb ?? 0)} MB
          {col?.disk?.free_gb != null ? ` · ${col.disk.free_gb} GB free` : ""}
        </span>
      </div>
      {uni && (
        <div className="flow-universe" data-testid="flow-universe">
          <div className="algo-operand" style={{ gap: 10 }}>
            <label className="meta"><input type="checkbox" checked={uni.enabled} onChange={(e) => save({ universe: { enabled: e.target.checked } })} /> universe</label>
            {Object.entries(st.data?.lists ?? {}).map(([k, label]) => (
              <label key={k} className="meta" title={plan?.lists?.[k] ? `${plan.lists[k].n} stocks · from ${plan.lists[k].source}` : undefined}>
                <input type="checkbox" checked={uni.lists.includes(k)} disabled={!uni.enabled} data-testid={`flow-list-${k}`}
                  onChange={(e) => save({ universe: { lists: e.target.checked ? [...uni.lists, k] : uni.lists.filter((x) => x !== k) } })} /> {label}
              </label>
            ))}
            <label className="meta"><input type="checkbox" checked={uni.stocks} disabled={!uni.enabled} onChange={(e) => save({ universe: { stocks: e.target.checked } })} /> the stocks (cash)</label>
          </div>
          <div className="algo-operand" style={{ gap: 10 }}>
            <span className="meta">Futures:</span>
            <label className="meta"><input type="checkbox" checked={uni.index_futures} disabled={!uni.enabled} onChange={(e) => save({ universe: { index_futures: e.target.checked } })} /> index</label>
            <label className="meta"><input type="checkbox" checked={uni.stock_futures} disabled={!uni.enabled} onChange={(e) => save({ universe: { stock_futures: e.target.checked } })} /> stock</label>
            <label className="meta" title="Next month: always, or only in the roll window before the current contract expires">next month
              <select value={uni.next_futures} disabled={!uni.enabled} onChange={(e) => save({ universe: { next_futures: e.target.value as Universe["next_futures"] } })}>
                <option value="always">always</option><option value="roll">in the roll window</option><option value="off">off</option>
              </select>
            </label>
            <label className="meta">roll window {numIn(uni.roll_days, (n) => save({ universe: { roll_days: n } }), 0, 50, "flow-roll", 50)} days</label>
          </div>
          <div className="algo-operand" style={{ gap: 10 }}>
            <span className="meta">Options:</span>
            <label className="meta"><input type="checkbox" checked={uni.index_options} disabled={!uni.enabled} onChange={(e) => save({ universe: { index_options: e.target.checked } })} /> index ATM ±</label>
            {numIn(uni.index_strikes, (n) => save({ universe: { index_strikes: n } }), 0, 50, "flow-index-strikes", 50)}
            <label className="meta">next expiry ± {numIn(uni.index_next_strikes, (n) => save({ universe: { index_next_strikes: n } }), 0, 50, "flow-index-next", 50)}</label>
            <label className="meta" title="Each stock gets the widest band that fits the budget, up to this many strikes each side; the largest companies get more when room is left">
              <input type="checkbox" checked={uni.stock_options} disabled={!uni.enabled} onChange={(e) => save({ universe: { stock_options: e.target.checked } })} /> stock ATM ± up to</label>
            {numIn(uni.stock_strikes, (n) => save({ universe: { stock_strikes: n } }), 0, 50, "flow-stock-strikes", 50)}
            <label className="meta">next expiry
              <select value={uni.stock_next_expiry} disabled={!uni.enabled} onChange={(e) => save({ universe: { stock_next_expiry: e.target.value as Universe["stock_next_expiry"] } })}>
                <option value="roll">in the roll window</option><option value="off">off</option>
              </select>
            </label>
          </div>
        </div>
      )}
      {plan?.classes && (
        <div className="meta" style={{ margin: "6px 0" }} data-testid="flow-plan">
          Recording {fmt(plan.used ?? 0)} of a {fmt(plan.budget ?? 0)} budget: {Object.entries(plan.classes).filter(([, n]) => n).map(([k, n]) => `${CLASS_LABEL[k] ?? k} ${fmt(n)}`).join(" · ")}
          {Object.keys(plan.dropped ?? {}).length > 0 && <> · <span style={{ color: "var(--amber)" }}>over budget: {Object.entries(plan.dropped!).map(([k, n]) => `${CLASS_LABEL[k] ?? k} ${fmt(n)}`).join(" · ")}</span></>}
          {(plan.no_price ?? []).length > 0 && <> · waiting for a price: {plan.no_price!.length}</>}
          {plan.at ? <> · planned {ist(plan.at)}</> : null}
        </div>
      )}
      <div className="meta" style={{ margin: "8px 0 4px" }}>Hand-picked (always recorded first, with their raw ticks; "NIFTY:FUT" = the current NIFTY future, rolling by itself). Switched-on strategies' symbols are added automatically.</div>
      <SymbolPicker value={list} onChange={(v) => setSyms(v)} />
      <div className="algo-operand" style={{ gap: 6, marginTop: 6 }}>
        {["NIFTY:FUT", "BANKNIFTY:FUT", "FINNIFTY:FUT", "MIDCPNIFTY:FUT", "SENSEX:FUT"].filter((x) => !list.includes(x)).map((x) => (
          <button key={x} type="button" className="btn-sm ghost" onClick={() => setSyms([...list, x])}>+ {x.replace(":FUT", " future")}</button>
        ))}
        {syms && <button type="button" className="primary" onClick={() => save({ symbols: list })}>Save list</button>}
        {(st.data?.strategy_symbols ?? []).length > 0 && <span className="meta">+ from strategies: {st.data!.strategy_symbols.map(shortSym).join(", ")}</span>}
      </div>
      <div className="algo-operand" style={{ gap: 8, marginTop: 10 }}>
        <span className="meta">Today: {fmt(st.data?.keys_today ?? 0)} symbols traded{col?.today ? ` · ${fmt(col.today.trades)} prints classified` : ""}</span>
        <input type="search" placeholder="find a symbol…" value={q} onChange={(e) => setQ(e.target.value)} style={{ width: 180 }} data-testid="flow-search" />
      </div>
      {(st.data?.top ?? []).length > 0 ? (
        <div className="algo-scroll" style={{ marginTop: 6 }}>
          <table data-testid="flow-top">
            <thead><tr><th>Key</th><th>Frames</th><th>Prints</th><th>Buy vol</th><th>Sell vol</th><th>Delta</th><th>Buy %</th><th title="Volume with no aggressor: a feed gap or the auction">No side</th><th>Last</th></tr></thead>
            <tbody>{st.data!.top.map(([k, ticks, trades, buy, sell, unk, last]) => (
              <tr key={k}><td>{k}</td><td className="mono">{fmt(ticks)}</td><td className="mono">{fmt(trades)}</td>
                <td className="mono up">{fmt(buy)}</td><td className="mono down">{fmt(sell)}</td>
                <td className={`mono ${buy >= sell ? "pnl-pos" : "pnl-neg"}`}>{fmt(buy - sell)}</td>
                <td className="mono">{buy + sell ? ((buy / (buy + sell)) * 100).toFixed(1) : "—"}</td>
                <td className="mono">{unk ? fmt(unk) : "—"}</td>
                <td className="mono">{last ? ist(last) : "—"}</td></tr>
            ))}</tbody>
          </table>
        </div>
      ) : <div className="empty">Nothing recorded yet today — frames arrive during market hours (09:15–15:30 IST).</div>}
    </div>
  );
}

function CompareTab({ cat, range, draft }: { cat: Catalog; range: { start: string; end: string }; draft: Spec }) {
  const strategies = useQuery({ queryKey: ["algo", "strategies"], queryFn: () => api.get<{ strategies: Saved[] }>("/api/algo/strategies") });
  const [pick, setPick] = useState<string[]>(["draft"]);
  const [oos, setOos] = useState(0);
  const [rows, setRows] = useState<{ name: string; stats: Stats | null; error?: string }[]>([]);
  const [running, setRunning] = useState(false);
  const options = [{ key: "draft", name: "Builder draft", spec: draft },
    ...(strategies.data?.strategies ?? []).map((s) => ({ key: String(s.id), name: `${s.name} (v${s.version})`, spec: s.spec }))];
  const chosen = options.filter((x) => pick.includes(x.key));
  const run = async () => {
    setRunning(true);
    setRows([]);
    const out: typeof rows = [];
    for (const o of chosen) {
      try {
        const r = await longPost<BtResult>("/api/algo/backtest", { spec: tidy(withDefaults(cat.defaults, o.spec)), ...range, oos_pct: oosParam(oos) });
        out.push({ name: o.name, stats: r.stats });
      } catch (e) { out.push({ name: o.name, stats: null, error: errMsg(e) }); }
      setRows([...out]);
    }
    setRunning(false);
  };
  const PAL = ["#FF8C00", "#42A5F5", "#9CCC65", "#AB47BC", "#EC407A", "#26C6DA"];
  const curve = useMemo(() => {   // equity curves merged on time, carried forward
    const ok = rows.filter((r) => r.stats);
    const times = Array.from(new Set(ok.flatMap((r) => r.stats!.equity.map((p) => p[0])))).sort((a, b) => a - b);
    const step = Math.max(1, Math.ceil(times.length / 600));
    const last: number[] = ok.map((r) => Number(r.stats!.equity[0]?.[1] ?? 0));
    const idx = ok.map(() => 0);
    const out: Record<string, number | string>[] = [];
    times.forEach((t, n) => {
      ok.forEach((r, k) => { const e = r.stats!.equity; while (idx[k] < e.length && e[idx[k]][0] <= t) { last[k] = e[idx[k]][1]; idx[k]++; } });
      if (n % step === 0 || n === times.length - 1) out.push({ t: ist(t), ...Object.fromEntries(ok.map((r, k) => [r.name, last[k]])) });
    });
    return out;
  }, [rows]);
  const METRICS: [string, string, number][] = [["trades", "Trades", 0], ["win_rate", "Win %", 1], ["net_pnl", "Net ₹", 0], ["profit_factor", "Profit factor", 2],
    ["sharpe", "Sharpe", 2], ["t_stat", "t-stat", 2], ["max_drawdown_pct", "Max DD %", 1], ["expectancy", "Expectancy ₹", 0], ["kelly_pct", "Kelly %", 1],
    ["exposure_pct", "Exposure %", 1], ["buy_hold_pct", "Buy & hold %", 2]];
  return (
    <div className="widget widget-wide">
      <h3>Compare <span className="meta">run several strategies (or the builder draft) on {range.start} → {range.end} and line them up</span></h3>
      <div className="algo-operand" style={{ gap: 10 }}>
        {options.map((o) => (
          <label key={o.key} className="meta"><input type="checkbox" checked={pick.includes(o.key)} onChange={(e) => setPick(e.target.checked ? [...pick, o.key] : pick.filter((x) => x !== o.key))} /> {o.name}</label>
        ))}
      </div>
      <div className="algo-operand" style={{ gap: 10, marginTop: 8 }}>
        <label className="meta" title="the last X% of the range as out-of-sample (5–80, 0 = off)">out-of-sample <NumInput width={36} value={oos} onChange={(n) => setOos(Math.max(0, Math.min(80, Math.floor(n ?? 0))))} />%</label>
        <button type="button" className="primary" disabled={running || !chosen.length} onClick={run}>{running ? `Running ${rows.length + 1}/${chosen.length}…` : "Run comparison"}</button>
      </div>
      {rows.length > 0 && (
        <>
          <div className="algo-scroll" style={{ marginTop: 10 }}>
            <table>
              <thead><tr><th></th>{rows.map((r) => <th key={r.name}>{r.name}</th>)}</tr></thead>
              <tbody>
                {METRICS.map(([k, lab, dp]) => (
                  <tr key={k}><td>{lab}</td>{rows.map((r) => <td key={r.name} className={`mono ${k === "net_pnl" ? pnlCls(r.stats?.[k]) : ""}`}>{r.error ? "—" : num(r.stats?.[k], dp)}</td>)}</tr>
                ))}
                {oos > 0 && <tr><td>OOS net ₹ / PF</td>{rows.map((r) => <td key={r.name} className="mono">{r.stats?.out_of_sample ? `${num(r.stats.out_of_sample.net_pnl, 0)} / ${num(r.stats.out_of_sample.profit_factor)}` : "—"}</td>)}</tr>}
                {rows.some((r) => r.error) && <tr><td>Error</td>{rows.map((r) => <td key={r.name} className="meta pnl-neg" style={{ whiteSpace: "normal" }}>{r.error ?? ""}</td>)}</tr>}
              </tbody>
            </table>
          </div>
          {curve.length > 1 && (
            <ResponsiveContainer width="100%" height={260}>
              <LineChart data={curve}>
                <CartesianGrid stroke="var(--border-soft)" />
                <XAxis dataKey="t" tick={{ fontSize: 10, fill: "var(--text-dim)" }} minTickGap={60} />
                <YAxis tick={{ fontSize: 10, fill: "var(--text-dim)" }} domain={["auto", "auto"]} width={70} />
                <Tooltip contentStyle={{ background: "var(--bg-panel)", border: "1px solid var(--border)" }} formatter={(v: number) => inr(v)} />
                {rows.filter((r) => r.stats).map((r, k) => <Line key={r.name} type="monotone" dataKey={r.name} stroke={PAL[k % PAL.length]} dot={false} strokeWidth={1.5} isAnimationActive={false} />)}
              </LineChart>
            </ResponsiveContainer>
          )}
        </>
      )}
    </div>
  );
}

function DataTab() {
  const [symbols, setSymbols] = useState<string[]>(["NSE:NIFTY50-INDEX", "NSE:SBIN-EQ"]);
  const [days, setDays] = useState(365);
  const dl = useMutation({
    mutationFn: () => longPost<{ results: { symbol: string; note: string | null; coverage: { first: number; last: number; rows: number } | null }[] }>(
      "/api/algo/data/download", { symbols, days }),
  });
  const sync = useMutation({ mutationFn: () => longPost<{ symbols: number; ok: number; failed: string[] }>("/api/algo/sync", {}) });
  return (
    <div className="widget widget-wide">
      <h3>Market data <span className="meta">1-minute candles from Fyers, stored locally; every timeframe is built from these</span></h3>
      <p className="meta" style={{ marginTop: 0 }}>
        Backtests download whatever they're missing on their own. Every trading day after 15:45 IST the runner also downloads that day's candles for every
        symbol any saved strategy uses (plus India VIX for option strategies). Use this to pre-load history in bulk (≈4 Fyers calls per symbol-year).
      </p>
      <SymbolPicker value={symbols} onChange={setSymbols} />
      <div className="algo-operand" style={{ gap: 10, margin: "8px 0" }}>
        <label className="meta">days <NumInput value={days} onChange={(n) => setDays(Math.max(1, Math.min(1098, Math.floor(n ?? 1))))} /></label>
        <button type="button" className="primary" disabled={dl.isPending || !symbols.length} onClick={() => dl.mutate()}>{dl.isPending ? "Downloading…" : "Download"}</button>
        <button type="button" disabled={sync.isPending} onClick={() => sync.mutate()} title="the last 5 days for every strategy symbol">{sync.isPending ? "Syncing…" : "Sync strategy symbols now"}</button>
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

type Tab = "builder" | "optimize" | "compare" | "automations" | "data";

export default function Algo() {
  const cat = useQuery({ queryKey: ["algo", "catalog"], queryFn: () => api.get<Catalog>("/api/algo/indicators"), staleTime: Infinity });
  const qc = useQueryClient();
  const [tab, setTab] = useSessionState<Tab>("algo:tab", "builder");
  const [spec, setSpec] = useState<Spec | null>(null);
  const [name, setName] = useState("My strategy");
  const [editing, setEditing] = useState<number | null>(null);
  const [viewing, setViewing] = useState<number | null>(null);   // version loaded into the builder
  const [range, setRange] = useState({ start: daysAgo(180), end: daysAgo(0) });
  const [msg, setMsg] = useState<string | null>(null);
  const [oosPct, setOosPct] = useState(0);
  // What the builder held when it was last loaded or saved — anything else is unsaved work.
  const [baseline, setBaseline] = useState<string | null>(null);
  const [btKey, setBtKey] = useState<string | null>(null);   // the inputs the shown backtest ran with
  const fileRef = useRef<HTMLInputElement>(null);
  const [presetsOpen, setPresetsOpen] = useFlag("algo.presets.open", false);
  const [paste, setPaste] = useState<{ text: string; error: string | null; parsed: { name?: string; spec: Partial<Spec> } | null } | null>(null);

  // Restore the draft (or start from the server defaults + the first template).
  useEffect(() => {
    if (!cat.data || spec) return;
    let draft: { spec: Partial<Spec>; name: string; editing: number | null; viewing?: number | null; baseline?: string | null } | null = null;
    try { draft = JSON.parse(localStorage.getItem(DRAFT_KEY) ?? "null"); } catch { /* private mode */ }
    if (draft?.spec) {
      setSpec(withDefaults(cat.data.defaults, draft.spec));
      setName(draft.name);
      setEditing(draft.editing);
      setViewing(draft.viewing ?? null);
      setBaseline(draft.baseline ?? null);
    } else {
      const s = withDefaults(cat.data.defaults, { symbols: ["NSE:SBIN-EQ"], ...TEMPLATES[0].spec });
      setSpec(s);
      setName(TEMPLATES[0].name);
      setBaseline(draftKey(s, TEMPLATES[0].name));
    }
  }, [cat.data, spec]);
  useEffect(() => {
    if (!spec) return;
    try { localStorage.setItem(DRAFT_KEY, JSON.stringify({ spec, name, editing, viewing, baseline })); } catch { /* ignore */ }
  }, [spec, name, editing, viewing, baseline]);
  const strategies = useQuery({ queryKey: ["algo", "strategies"], queryFn: () => api.get<{ strategies: Saved[] }>("/api/algo/strategies") });
  const current = editing ? strategies.data?.strategies.find((x) => x.id === editing) : undefined;

  const first = spec?.symbols[0];
  const info = useQuery({
    queryKey: ["algo", "instrument", first], enabled: !!first, staleTime: 300000,
    queryFn: () => api.get<InstInfo>(`/api/algo/instrument?symbol=${encodeURIComponent(first!)}`),
  });

  const bt = useMutation({
    onMutate: () => setBtKey(JSON.stringify({ spec, range, oosPct })),
    mutationFn: () => longPost<BtResult>("/api/algo/backtest", { spec: tidy(spec!), ...range, oos_pct: oosParam(oosPct) }),
  });
  const versions = useQuery({
    queryKey: ["algo", "versions", editing], enabled: !!editing,
    queryFn: () => api.get<{ active: number; versions: Version[] }>(`/api/algo/strategies/${editing}/versions`),
  });
  const save = useMutation({
    mutationFn: async (asNew: boolean) => {
      const body = { name, spec: tidy(spec!) };
      if (!asNew && editing) return api.put<Saved>(`/api/algo/strategies/${editing}`, body);
      return api.post<Saved>("/api/algo/strategies", body);
    },
    onSuccess: (s, asNew) => {
      const unchanged = !asNew && s.version === versions.data?.active;
      setEditing(s.id);
      setViewing(s.version);
      setBaseline(draftKey(spec!, name));
      setMsg(unchanged
        ? `No rule changes since v${s.version} — nothing new to save.`
        : `Saved “${s.name}” as v${s.version} — it is the active version.${s.enabled ? " It is switched on, so it trades from the next candle." : " Switch it on in the Automations tab."}`);
      qc.invalidateQueries({ queryKey: ["algo"] });
    },
    onError: (e, asNew) => {
      // Deleted from Automations while it was open here: keep the work, save it as a new strategy instead.
      if (!asNew && /not found/.test(errMsg(e))) {
        setEditing(null); setViewing(null);
        setMsg("This strategy was deleted — press “Save as new strategy” to keep the builder's copy.");
      } else setMsg(errMsg(e));
    },
  });
  const activate = useMutation({
    mutationFn: (v: number) => api.post<Saved>(`/api/algo/strategies/${editing}/versions/${v}/activate`, {}),
    onSuccess: (s) => { setMsg(`v${s.version} is now the active version.`); qc.invalidateQueries({ queryKey: ["algo"] }); },
    onError: (e) => setMsg(errMsg(e)),
  });

  if (cat.isLoading || (!spec && !cat.error)) return <div className="empty">loading…</div>;
  if (cat.error || !cat.data || !spec) return <div className="empty">Algo API unavailable: {errMsg(cat.error)}</div>;
  const c = cat.data;
  const dirty = baseline !== draftKey(spec, name);
  const stale = !!bt.data && btKey !== JSON.stringify({ spec, range, oosPct });
  const okToReplace = () => !dirty || window.confirm("The builder has unsaved changes. Replace them?");
  // Check an imported strategy with the server (the same checks a save runs) and load the
  // normalized result. Throws the server's reason when it isn't valid (strict); false = the
  // operator kept their unsaved builder instead.
  const importStrategy = async (p: { name?: string; spec: Partial<Spec> }, fallbackName: string, strict = false): Promise<boolean> => {
    let checked: Spec | null = null;
    try {
      checked = (await api.post<{ ok: boolean; spec: Spec }>("/api/algo/validate", { spec: p.spec })).spec;
    } catch (e) {
      if (strict) throw e;
      setMsg(`Imported, but it needs fixing before it can run: ${errMsg(e)}`);
    }
    if (!okToReplace()) return false;
    load(withDefaults(c.defaults, checked ?? p.spec), p.name ?? fallbackName, null, null, false);
    bt.reset();
    if (checked) setMsg(`Imported “${p.name ?? fallbackName}” — checked, ready to backtest. Save it to keep it.`);
    return true;
  };
  // Put a strategy into the builder; `clean` = it matches what is saved (or is a fresh template).
  const load = (next: Spec, nm: string, ed: number | null, vw: number | null, clean = true) => {
    setSpec(next); setName(nm); setEditing(ed); setViewing(vw); setBaseline(clean ? draftKey(next, nm) : null);
  };
  const saveVersion = () => {
    if (current?.enabled && !window.confirm(`“${current.name}” is switched on${current.mode === "live" ? " in LIVE (real money)" : ""}. Saving makes the new version trade from the next candle. Save?`)) return;
    save.mutate(false);
  };
  const upd = (patch: Partial<Spec>) => setSpec({ ...spec, ...patch });
  const inst = spec.instrument;
  const updInst = (patch: Partial<Instrument>) => upd({ instrument: { ...inst, ...patch } });
  const showLong = spec.direction !== "short";
  const showShort = spec.direction !== "long";
  const fno = inst.type !== "equity";
  const multi = inst.type === "option" && ((showLong && inst.legs_long.filter((l) => l.lots).length > 1) || (showShort && inst.legs_short.filter((l) => l.lots).length > 1));
  const sizingModes = c.sizing.filter((m) => (fno ? m !== "qty" : m !== "lots"));
  const opt = inst.type === "option";
  const legStops = opt && [...(showLong ? inst.legs_long : []), ...(showShort ? inst.legs_short : [])].some((l) => l.sl_pct || l.tg_pct);
  const rules = spec.position_rules ?? {};
  const setRules = (k: "entry" | "exit", r: Rule[]) => {
    const next = { ...rules, [k]: r };
    if (!next.entry?.length) delete next.entry;
    if (!next.exit?.length) delete next.exit;
    upd({ position_rules: next.entry || next.exit ? next : undefined });
  };
  const setMtmPct = (k: "stop_pct" | "target_pct", v: number | null) => {
    const { [k]: _old, ...rest } = spec.mtm;
    upd({ mtm: v && v > 0 ? { ...rest, [k]: v } : rest });
  };
  const SIZE_LABEL: Record<string, string> = {
    qty: "fixed shares", lots: "fixed lots", amount: "₹ capital per trade", pct_equity: "% of equity per trade",
    risk: "₹ risk per trade (needs stop)", risk_pct: "% of equity at risk (needs stop)",
  };
  const applyPreset = (pr: SizingPreset) => {
    const risky = !!spec.stop_loss;
    upd({
      sizing: { mode: risky ? "risk_pct" : "pct_equity", value: risky ? pr.riskPct : pr.equityPct },
      portfolio: { ...spec.portfolio, max_positions: pr.maxPositions, max_position_pct: pr.capPct, compounding: pr.compound },
      daily: { ...spec.daily, max_loss: Math.round((spec.portfolio.capital * pr.dailyLossPct) / 100) },
    });
  };
  const exitBlock = (key: "exit_long" | "exit_short", label: string) => {
    const g = spec[key];
    return (
      <div>
        <div className="meta" style={{ marginBottom: 4 }}>
          {label}{" "}
          {g ? <button type="button" className="btn-sm" onClick={() => upd({ [key]: null } as Partial<Spec>)}>remove</button>
            : <button type="button" className="btn-sm" onClick={() => upd({ [key]: G([]) } as Partial<Spec>)}>+ add exit conditions</button>}
          {!g && <span> — exits by stop / target / trailing / MTM / square-off only</span>}
        </div>
        {g && <GroupEditor g={g} cat={c} baseTf={spec.timeframe} onChange={(x) => upd({ [key]: x } as Partial<Spec>)} />}
      </div>
    );
  };
  // ATR on an option premium = the underlying's ATR × the option's delta (engine.premium_atr).
  const levelKinds = ["pct", "points", "atr"];
  const levelsHint = inst.type === "option"
    ? inst.levels_on === "instrument" ? "measured on the option premium" : "measured on the underlying's price"
    : inst.type === "future" && inst.levels_on === "instrument" ? "measured on the futures price" : "measured on the traded price";

  return (
    <div className="algo">
      <div className="tabs algo-tabs">
        {(["builder", "optimize", "compare", "automations", "data"] as Tab[]).map((t) => (
          <button type="button" key={t} className={`tab ${tab === t ? "active" : ""}`} onClick={() => setTab(t)}>
            {{ builder: "Builder & backtest", optimize: "Optimise", compare: "Compare", automations: "Automations", data: "Data" }[t]}
          </button>
        ))}
        <button type="button" className="primary" style={{ marginLeft: 12 }} onClick={() => {
          if (!okToReplace()) return;
          load(withDefaults(c.defaults, { symbols: [], entry_long: G([]), entry_short: G([]) }), "New strategy", null, null);
          bt.reset(); setTab("builder");
          setMsg("New algo — pick symbols, add entry conditions, then backtest and save.");
        }}>＋ New algo</button>
        <button type="button" style={{ marginLeft: 8 }} data-testid="algo-guide"
          title="everything the builder and backtest can do, with the strategy JSON and worked examples — give it to an AI, describe your idea, then paste its JSON here (📋 paste JSON)"
          onClick={async () => {
            try {
              const res = await fetch("/api/algo/guide");
              if (!res.ok) throw new Error(`HTTP ${res.status}`);
              download("algo-lab-guide.txt", await res.text(), "text/plain");
            } catch (e) { setMsg(`Guide download failed: ${errMsg(e)}`); }
          }}>⬇ Algo guide (for AI)</button>
      </div>

      {tab === "automations" && <Automations onEdit={(s) => {
        if (!okToReplace()) return;
        load(withDefaults(c.defaults, s.spec), s.name, s.id, s.version);
        bt.reset(); setMsg(null); setTab("builder");
      }} />}
      {tab === "data" && <><RecorderPanel /><DataTab /></>}
      {/* Optimise and Compare stay mounted so their results survive a trip to the builder and back. */}
      <div hidden={tab !== "compare"}><CompareTab cat={c} range={range} draft={spec} /></div>
      <div hidden={tab !== "optimize"}>
        <Optimizer spec={spec} cat={c} range={range} onApply={(params) => {
          const s = clone(spec);
          Object.entries(params).forEach(([p, v]) => setPath(s, p, v));
          setSpec(s);
          setMsg("Optimiser values applied — run the backtest on a different date range to check they hold up, then save.");
          setTab("builder");
        }} />
      </div>

      {tab === "builder" && (
        <>
          <div className="widget widget-wide" style={{ marginBottom: 12 }}>
            <h3>Strategy {editing ? <span className="meta">saved #{editing}{viewing ? ` · v${viewing}` : ""}{dirty ? " · unsaved changes" : ""}</span> : <span className="meta">not saved yet</span>}
              <span style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
                <button type="button" className="btn-sm" onClick={() => download(`${name.replace(/[^\w-]+/g, "_")}.json`, JSON.stringify({ name, spec: tidy(spec) }, null, 2))}>⬇ export</button>
                <button type="button" className="btn-sm" onClick={() => fileRef.current?.click()}>⬆ import</button>
                <button type="button" className="btn-sm" data-testid="paste-json" title="paste the strategy JSON an AI wrote from the Algo guide"
                  onClick={() => setPaste(paste ? null : { text: "", error: null, parsed: null })}>📋 paste JSON</button>
                <input ref={fileRef} type="file" accept="application/json,.json" style={{ display: "none" }} onChange={async (e) => {
                  const f = e.target.files?.[0];
                  e.target.value = "";
                  if (!f) return;
                  try {
                    await importStrategy(parseStrategyJson(await f.text()), f.name.replace(/\.json$/, ""));
                  } catch (err) { setMsg(`Import failed: ${errMsg(err)}`); }
                }} />
              </span>
            </h3>
            {paste && (
              <div className="algo-paste" data-testid="paste-panel" style={{ display: "grid", gap: 6, marginBottom: 10 }}>
                <span className="meta">Paste the JSON an AI wrote from the <b>Algo guide</b> (⬇ beside ＋ New algo) — a ```json block or text around it is fine.</span>
                <textarea rows={8} value={paste.text} placeholder='{"name": "…", "spec": {"symbols": ["NSE:NIFTY50-INDEX"], …}}'
                  style={{ fontFamily: "var(--mono, monospace)", fontSize: 12, width: "100%" }}
                  onChange={(e) => setPaste({ text: e.target.value, error: null, parsed: null })} />
                <div className="algo-operand" style={{ gap: 6 }}>
                  <button type="button" className="primary" disabled={!paste.text.trim()} onClick={async () => {
                    let parsed;
                    try { parsed = parseStrategyJson(paste.text); } catch (e) { setPaste({ ...paste, error: errMsg(e), parsed: null }); return; }
                    try { if (await importStrategy(parsed, "Imported strategy", true)) setPaste(null); }
                    catch (e) { setPaste({ ...paste, error: errMsg(e), parsed }); }
                  }}>Import</button>
                  {paste.error && paste.parsed && <button type="button" className="btn-sm" title="load it as it is and fix it in the builder"
                    onClick={() => { const p = paste.parsed!; if (!okToReplace()) return; load(withDefaults(c.defaults, p.spec), p.name ?? "Imported strategy", null, null, false); bt.reset(); setPaste(null); setMsg("Loaded as written — fix what the check flagged, then backtest."); }}>load anyway</button>}
                  <button type="button" className="btn-sm ghost" onClick={() => setPaste(null)}>cancel</button>
                </div>
                {paste.error && <div className="pnl-neg meta" data-testid="paste-error">✗ {paste.error}{paste.parsed ? " — paste this message back to the AI and ask it to fix the JSON." : ""}</div>}
              </div>
            )}
            <div className="algo-fields">
              <label className="algo-field"><span className="meta">name</span><input value={name} onChange={(e) => setName(e.target.value)} /></label>
              <label className="algo-field"><span className="meta">template</span>
                <select value="" onChange={(e) => {
                  const t = TEMPLATES[Number(e.target.value)];
                  if (!t || !okToReplace()) return;
                  load(withDefaults(c.defaults, { symbols: t.symbols ?? (spec.symbols.length ? spec.symbols : ["NSE:SBIN-EQ"]), ...t.spec }), t.name, null, null);
                  bt.reset(); setMsg(`Loaded the “${t.name}” template — adjust it, backtest, then save.`);
                }}>
                  <option value="">— load a template —</option>
                  {TEMPLATES.map((t, i) => <option key={t.name} value={i}>{t.name}</option>)}
                </select>
              </label>
              <label className="algo-field" title="time candles close every N minutes; volume / turnover candles close once that much has traded (busy markets get more candles, quiet ones fewer)"><span className="meta">candles</span>
                <span className="algo-operand" style={{ flexWrap: "nowrap" }}>
                  <select value={spec.bars?.type ?? "time"} onChange={(e) => upd({ bars: { ...(spec.bars ?? { per_day: 50 }), type: e.target.value }, timeframe: e.target.value === "time" ? (spec.timeframe === 1 ? 5 : spec.timeframe) : 1 })}>
                    <option value="time">time</option><option value="volume">volume</option><option value="turnover">turnover ₹</option>
                  </select>
                  {(spec.bars?.type ?? "time") === "time" ? (
                    <select value={spec.timeframe} onChange={(e) => upd({ timeframe: Number(e.target.value) })}>
                      {c.timeframes.map((t) => <option key={t} value={t}>{tfLabel(t)}</option>)}
                    </select>
                  ) : (
                    <label className="meta" title="the size is set so a typical session (median of the 20 before the start) has about this many candles">~<NumInput width={44} value={spec.bars.per_day} onChange={(n) => upd({ bars: { ...spec.bars, per_day: Math.max(5, Math.min(1000, Math.floor(n ?? 50))) } })} />/day</label>
                  )}
                </span>
              </label>
              <label className="algo-field"><span className="meta">direction</span>
                <select value={spec.direction} onChange={(e) => upd({ direction: e.target.value as Spec["direction"] })}>
                  <option value="long">long / bullish only</option><option value="short">short / bearish only</option><option value="both">both</option>
                </select>
              </label>
            </div>
            <div className="algo-field"><span className="meta">symbols — signals are computed on these (stocks or indices)</span>
              <SymbolPicker value={spec.symbols} onChange={(symbols) => upd({ symbols })} />
            </div>
            {editing && versions.data && (
              <div className="algo-operand" style={{ gap: 6 }}>
                <span className="meta">versions</span>
                {versions.data.versions.map((v) => (
                  <button key={v.version} type="button" className={`btn-sm ${v.version === viewing ? "primary" : "ghost"}`}
                    title={`${v.note ?? ""} saved ${isoIst(v.created_at)} · ${v.closed_trades} live/paper trades · ${inr(v.realized_pnl)}${v.active ? " · ACTIVE" : ""}`}
                    onClick={() => {
                      if (v.version === viewing && !dirty) return;
                      if (!okToReplace()) return;
                      load(withDefaults(c.defaults, v.spec), name, editing, v.version);
                      bt.reset(); setMsg(`Loaded v${v.version} into the builder${v.active ? " (active)" : " — not active yet"}.`);
                    }}>
                    v{v.version}{v.active ? " ✓" : ""}
                  </button>
                ))}
                {viewing && viewing !== versions.data.active && (
                  <button type="button" className="btn-sm" disabled={activate.isPending} onClick={() => activate.mutate(viewing)}>make v{viewing} active</button>
                )}
                <span className="meta">✓ = the version Automations runs · saving changes creates the next version and makes it active</span>
              </div>
            )}
          </div>

          <div className="widget widget-wide" style={{ marginBottom: 12 }}>
            <h3>What to trade
              {first && info.data && <span className="meta">
                {shortSym(first)}: {info.data.fno ? `F&O · lot ${info.data.lot}${info.data.weekly ? " · weekly expiries" : " · monthly expiries"}` : "no F&O contracts (equity only)"}
              </span>}
            </h3>
            <div className="algo-operand" style={{ gap: 12 }}>
              {(["equity", "future", "option"] as const).map((t) => (
                <label key={t} className="meta" style={{ cursor: "pointer" }}>
                  <input type="radio" name="inst" checked={inst.type === t} onChange={() => upd({
                    instrument: { ...inst, type: t },
                    sizing: { ...spec.sizing, mode: t === "equity" ? (spec.sizing.mode === "lots" ? "qty" : spec.sizing.mode) : (spec.sizing.mode === "qty" ? "lots" : spec.sizing.mode) },
                  })} />
                  {{ equity: "Equity / index (cash)", future: "Futures", option: "Options" }[t]}
                </label>
              ))}
              {fno && (
                <label className="meta">expiry
                  <select value={inst.expiry} onChange={(e) => updInst({ expiry: e.target.value as Instrument["expiry"] })}>
                    <option value="current">current</option><option value="next">next</option>
                  </select>
                  {inst.type === "option" && (
                    <select value={inst.expiry_kind} onChange={(e) => updInst({ expiry_kind: e.target.value as Instrument["expiry_kind"] })}>
                      <option value="weekly">weekly (NIFTY, SENSEX)</option><option value="monthly">monthly</option>
                    </select>
                  )}
                </label>
              )}
              {fno && (
                <label className="meta" title="where stop / target / trailing / breakeven are measured">levels on
                  <select value={inst.levels_on} onChange={(e) => updInst({ levels_on: e.target.value as Instrument["levels_on"] })}>
                    <option value="instrument" disabled={multi}>{inst.type === "option" ? "the option premium" : "the futures price"}</option>
                    <option value="underlying">the underlying (spot)</option>
                  </select>
                </label>
              )}
            </div>
            {inst.type === "option" && (
              <>
                <div className="algo-operand" style={{ gap: 8, marginTop: 8 }}>
                  <button type="button" className="btn-sm ghost" data-testid="presets-toggle" aria-expanded={presetsOpen}
                    title={presetsOpen ? "hide the ready-made leg setups" : "show ready-made leg setups (straddles, condors, spreads …)"}
                    onClick={() => setPresetsOpen(!presetsOpen)}>{presetsOpen ? "▾" : "▸"} presets ({LEG_PRESETS.length})</button>
                  {presetsOpen && LEG_PRESETS.map((p) => (
                    <button key={p.name} type="button" className="btn-sm ghost" onClick={() => updInst({
                      legs_long: clone(p.long), legs_short: clone(p.short),
                      levels_on: p.long.length > 1 ? "underlying" : inst.levels_on,
                    })}>{p.name}</button>
                  ))}
                </div>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(380px, 1fr))", gap: 12, marginTop: 8 }}>
                  {showLong && <LegsEditor title="On a LONG / bullish signal" legs={inst.legs_long} onChange={(legs_long) => updInst({ legs_long })} />}
                  {showShort && <LegsEditor title="On a SHORT / bearish signal" legs={inst.legs_short} onChange={(legs_short) => updInst({ legs_short })} />}
                </div>
                {legStops && (
                  <div className="algo-operand" style={{ gap: 8, marginTop: 8 }}>
                    <label className="meta" title="what the other legs do when one leg's own stop (SL %) hits; a leg's target only ever closes that leg" data-testid="on-leg-stop">when a leg's stop hits
                      <select value={inst.on_leg_stop ?? "leg"} onChange={(e) => {
                        const { on_leg_stop: _old, ...rest } = inst;
                        upd({ instrument: e.target.value === "leg" ? rest : { ...rest, on_leg_stop: e.target.value as Instrument["on_leg_stop"] } });
                      }}>
                        <option value="leg">only that leg exits</option>
                        <option value="cost">move the other legs' stops to cost</option>
                        <option value="all">exit every leg</option>
                      </select>
                    </label>
                    {(spec.scale_out ?? []).length > 0 && <span className="meta pnl-neg">per-leg stops can't be combined with scale-out — remove one</span>}
                  </div>
                )}
                <div className="algo-operand" style={{ gap: 8, marginTop: 8 }}>
                  <label className="meta" title="backtests only — live trades use real premiums">backtest IV
                    <select value={inst.iv.source} onChange={(e) => updInst({ iv: { ...inst.iv, source: e.target.value } })}>
                      <option value="auto">auto (India VIX for indices, realised vol for stocks)</option>
                      <option value="vix">India VIX</option><option value="hv">20-day realised volatility</option><option value="fixed">fixed %</option>
                    </select>
                    {inst.iv.source === "fixed" && <NumInput width={48} value={inst.iv.value} onChange={(n) => updInst({ iv: { ...inst.iv, value: n ?? 15 } })} />}
                  </label>
                  <span className="meta">Strikes ATM/ITM/OTM are counted in the underlying's strike steps; Δ strikes pick the nearest delta. Multi-leg positions exit together — use MTM ₹ stops.</span>
                </div>
              </>
            )}
            {fno && (
              <div className="algo-operand" style={{ gap: 10, marginTop: 8 }} data-testid="margin-model">
                <label className="meta" title="how much capital a futures / written-option position blocks — decides how many sets the backtest and the runner can afford">margin
                  <select value={spec.margin?.model ?? "leverage"} onChange={(e) => upd({ margin: e.target.value === "span" ? { model: "span", scan_pct: null, exposure_pct: null } : undefined })}>
                    <option value="leverage">contract value ÷ leverage ×{spec.portfolio.leverage}</option>
                    <option value="span">SPAN + exposure (exchange-style estimate)</option>
                  </select>
                </label>
                {spec.margin?.model === "span" && (
                  <>
                    <label className="meta" title="worst-case price move SPAN scans, % of the underlying (empty = 6% index, 13% stock)">price scan <NumInput width={44} allowEmpty placeholder="auto" value={spec.margin.scan_pct ?? null} onChange={(n) => upd({ margin: { ...spec.margin!, scan_pct: n } })} />%</label>
                    <label className="meta" title="exposure margin on short options and futures, % of contract value (empty = 2% index, 3.5% stock)">exposure <NumInput width={44} allowEmpty placeholder="auto" value={spec.margin.exposure_pct ?? null} onChange={(n) => upd({ margin: { ...spec.margin!, exposure_pct: n } })} />%</label>
                    <span className="meta">bought options still need their full premium; hedged spreads get the offset</span>
                  </>
                )}
              </div>
            )}
          </div>
          {fno && spec.symbols.length > 0 && <PayoffPanel spec={spec} onSpan={() => upd({ margin: { model: "span", scan_pct: null, exposure_pct: null } })} />}

          <div className="widget widget-wide" style={{ marginBottom: 12 }}>
            <h3>Entry & exit conditions <span className="meta">evaluated on each completed {barLabel(spec)} candle of the underlying</span></h3>
            <div style={{ display: "grid", gap: 12 }}>
              {showLong && (
                <div>
                  <div className="meta" style={{ marginBottom: 4, color: "var(--green)" }}>LONG / bullish signal when</div>
                  <GroupEditor g={spec.entry_long} cat={c} baseTf={spec.timeframe} onChange={(g) => upd({ entry_long: g })} />
                </div>
              )}
              {showLong && exitBlock("exit_long", "Exit long when")}
              {showShort && (
                <div>
                  <div className="meta" style={{ marginBottom: 4, color: "var(--red)" }}>SHORT / bearish signal when</div>
                  <GroupEditor g={spec.entry_short} cat={c} baseTf={spec.timeframe} onChange={(g) => upd({ entry_short: g })} />
                </div>
              )}
              {showShort && exitBlock("exit_short", "Exit short when")}
            </div>
          </div>

          <div className="widget widget-wide" style={{ marginBottom: 12 }}>
            <h3>Stop loss, target & risk <span className="meta">price levels {levelsHint}</span></h3>
            {multi && inst.levels_on === "instrument" && <div className="meta pnl-neg">Multi-leg: put levels on the underlying or use MTM ₹ stops.</div>}
            <div className="algo-operand" style={{ gap: 14 }}>
              <LevelEditor label="stop loss" v={spec.stop_loss} kinds={levelKinds} onChange={(l) => upd({ stop_loss: l })} />
              <LevelEditor label="target" v={spec.target} kinds={[...levelKinds, "rr"]} onChange={(l) => upd({ target: l })} />
              <LevelEditor label="trailing stop" v={spec.trailing} kinds={levelKinds} activate onChange={(l) => upd({ trailing: l })}
                hint="follows the best price by this gap; 'after' delays it until that much profit" />
              <LevelEditor label="move stop to cost after" v={spec.breakeven} kinds={levelKinds} onChange={(l) => upd({ breakeven: l })}
                hint="once the trade is this far in profit, the stop moves to the entry price" />
            </div>
            <ScaleOutEditor steps={spec.scale_out ?? []} hasStop={!!spec.stop_loss} onChange={(scale_out) => upd({ scale_out })} />
            <div className="algo-operand" style={{ gap: 14, marginTop: 8 }}>
              <span className="meta" style={{ color: "var(--text)" }}>Position MTM:</span>
              <Rupee label="stop" value={spec.mtm.stop} onChange={(v) => upd({ mtm: { ...spec.mtm, stop: v } })} hint="exit when the position's P&L falls to −₹X" />
              <Rupee label="target" value={spec.mtm.target} onChange={(v) => upd({ mtm: { ...spec.mtm, target: v } })} hint="exit when the position's P&L reaches +₹X" />
              <Rupee label="trail after" value={spec.mtm.trail_start} onChange={(v) => upd({ mtm: { ...spec.mtm, trail_start: v } })} hint="once P&L has reached ₹X…" />
              <Rupee label="by" value={spec.mtm.trail_gap} onChange={(v) => upd({ mtm: { ...spec.mtm, trail_gap: v } })} hint="…exit if it gives back ₹Y from its peak" />
              {opt && (
                <>
                  <label className="meta" data-testid="mtm-stop-pct" title="exit when the loss reaches this % of the net premium at entry (100 = lost as much as the credit). With a ₹ stop too, the tighter applies">stop <NumInput width={44} allowEmpty placeholder="off" value={spec.mtm.stop_pct ?? null} onChange={(v) => setMtmPct("stop_pct", v)} />% of premium</label>
                  <label className="meta" data-testid="mtm-target-pct" title="exit when the profit reaches this % of the net premium at entry (50 = half the credit kept)">target <NumInput width={44} allowEmpty placeholder="off" value={spec.mtm.target_pct ?? null} onChange={(v) => setMtmPct("target_pct", v)} />% of premium</label>
                </>
              )}
            </div>
            {opt && (
              <div style={{ marginTop: 10 }}>
                <div className="meta" style={{ color: "var(--text)", marginBottom: 4 }}>Option Greeks & premium rules <span className="meta">— per set; live uses implied volatility from real prices</span></div>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(340px, 1fr))", gap: 12 }}>
                  <RulesEditor kind="entry" rules={rules.entry ?? []} onChange={(r) => setRules("entry", r)} />
                  <RulesEditor kind="exit" rules={rules.exit ?? []} onChange={(r) => setRules("exit", r)} />
                </div>
              </div>
            )}
            <div className="algo-operand" style={{ gap: 14, marginTop: 8 }}>
              <span className="meta" style={{ color: "var(--text)" }}>Per day:</span>
              <Rupee label="max loss" value={spec.daily.max_loss} onChange={(v) => upd({ daily: { ...spec.daily, max_loss: v } })} hint="realised + open P&L for the day; hit it and everything is flattened, no more entries today" />
              <Rupee label="max profit" value={spec.daily.max_profit} onChange={(v) => upd({ daily: { ...spec.daily, max_profit: v } })} hint="lock the day in once this much is made" />
              <label className="meta">max trades / symbol <NumInput width={44} value={spec.max_trades_per_day} onChange={(n) => upd({ max_trades_per_day: Math.max(1, Math.floor(n ?? 1)) })} /></label>
              <label className="meta" title="after an exit, wait this many candles before re-entering the same symbol">re-entry cooldown <NumInput width={44} value={spec.cooldown_bars} onChange={(n) => upd({ cooldown_bars: Math.max(0, Math.floor(n ?? 0)) })} /> bars</label>
              <label className="meta" title="exit a position that has been open this many candles">time stop <NumInput width={44} allowEmpty placeholder="off" value={spec.max_bars} onChange={(n) => upd({ max_bars: n === null ? null : Math.max(1, Math.floor(n)) })} /> bars</label>
            </div>
            <div className="algo-operand" style={{ gap: 14, marginTop: 8 }}>
              {(["start", "end", "square_off"] as const).map((k) => {
                const on = spec.session[k] !== null && spec.session[k] !== undefined;
                const off = { start: "from the 09:15 open", end: "until square-off", square_off: "carry forward overnight" }[k];
                return (
                  <label key={k} className="meta" title={on ? "" : `off — ${off}`}>
                    <input type="checkbox" checked={on} onChange={(e) => upd({ session: { ...spec.session, [k]: e.target.checked ? SESSION_DEFAULT[k] : null } })} />
                    {k === "start" ? "entries from" : k === "end" ? "entries until" : "square off"}
                    {on
                      ? <input type="time" value={spec.session[k] ?? ""} min="09:15" max="15:29" onChange={(e) => upd({ session: { ...spec.session, [k]: e.target.value || null } })} />
                      : <span style={{ color: "var(--text-faint)" }}>off ({off})</span>}
                  </label>
                );
              })}
              <span className="meta" title="new entries only on the ticked weekdays; open positions are still managed and squared off every day" data-testid="algo-days">
                entry days{" "}
                {WEEKDAYS.map((d, k) => {
                  const on = !spec.session.days || !spec.session.days.length || spec.session.days.includes(k + 1);
                  return (
                    <label key={d} style={{ marginRight: 4 }}>
                      <input type="checkbox" checked={on} onChange={(e) => upd({ session: { ...spec.session, days: nextDays(spec.session.days, k + 1, e.target.checked) } })} />{d}
                    </label>
                  );
                })}
              </span>
            </div>
            <div className="algo-operand" style={{ gap: 10, marginTop: 8 }}>
              <label className="meta" title="market: fill at the next candle's open. pullback: wait for a better price (limit-style). breakout: wait for price to run further in the trade's direction (stop-style).">entry order
                <select value={spec.entry_order.type} onChange={(e) => upd({ entry_order: { ...spec.entry_order, type: e.target.value } })}>
                  <option value="market">at market</option><option value="pullback">on a pullback</option><option value="breakout">on a breakout</option>
                </select>
              </label>
              {spec.entry_order.type !== "market" && (
                <>
                  <label className="meta"><NumInput width={48} value={spec.entry_order.offset_pct} onChange={(n) => upd({ entry_order: { ...spec.entry_order, offset_pct: n ?? 0 } })} />% {spec.entry_order.type === "pullback" ? "better than" : "beyond"} the signal close</label>
                  <label className="meta">cancel after <NumInput width={40} value={spec.entry_order.valid_bars} onChange={(n) => upd({ entry_order: { ...spec.entry_order, valid_bars: Math.max(1, Math.floor(n ?? 1)) } })} /> bars</label>
                </>
              )}
            </div>
          </div>

          <div className="widget widget-wide" style={{ marginBottom: 12 }}>
            <h3>Sizing & portfolio <span className="meta">one capital pool shared by every symbol in the strategy</span></h3>
            <div className="algo-operand" style={{ gap: 6 }}>
              <span className="meta">quick setup</span>
              {SIZING_PRESETS.map((pr) => (
                <button type="button" key={pr.name} className="btn-sm" title={pr.hint} onClick={() => applyPreset(pr)}>{pr.name}</button>
              ))}
            </div>
            <div className="algo-operand" style={{ gap: 6, marginTop: 10 }}>
              <span className="meta">each trade is</span>
              <div className="seg">
                {sizingModes.map((m) => (
                  <button type="button" key={m} className={spec.sizing.mode === m ? "on" : ""}
                    disabled={(m === "risk" || m === "risk_pct") && !spec.stop_loss}
                    title={(m === "risk" || m === "risk_pct") && !spec.stop_loss ? "add a stop loss first — risk sizing divides by the stop distance" : SIZE_LABEL[m]}
                    onClick={() => upd({ sizing: { ...spec.sizing, mode: m } })}>{SIZE_SHORT[m]}</button>
                ))}
              </div>
              <label className="meta"><NumInput width={80} value={spec.sizing.value} onChange={(n) => upd({ sizing: { ...spec.sizing, value: n ?? 1 } })} /> {SIZE_UNIT[spec.sizing.mode]}</label>
              {fno && info.data?.lot ? <span className="meta">× {info.data.lot} per lot</span> : null}
            </div>
            <div className="algo-operand" style={{ gap: 14, marginTop: 10 }}>
              <label className="meta">capital ₹ <NumInput width={100} value={spec.portfolio.capital} onChange={(n) => upd({ portfolio: { ...spec.portfolio, capital: n ?? 100000 } })} /></label>
              <label className="meta">max open <NumInput width={40} value={spec.portfolio.max_positions} onChange={(n) => upd({ portfolio: { ...spec.portfolio, max_positions: Math.max(1, Math.floor(n ?? 1)) } })} /> positions</label>
              <label className="meta" title="no single position may block more than this share of equity (empty = no cap)">max <NumInput width={40} allowEmpty placeholder="off" value={spec.portfolio.max_position_pct ?? null} onChange={(n) => upd({ portfolio: { ...spec.portfolio, max_position_pct: n } })} />% per position</label>
              <label className="meta" title="intraday margin multiplier: equity MIS ≈ 5, futures / written options ≈ 6–9 — or pick the SPAN + exposure margin under What to trade. Bought options always need the full premium.">leverage × <NumInput width={40} value={spec.portfolio.leverage} onChange={(n) => upd({ portfolio: { ...spec.portfolio, leverage: n ?? 1 } })} /></label>
              <label className="meta"><input type="checkbox" checked={spec.portfolio.compounding} onChange={(e) => upd({ portfolio: { ...spec.portfolio, compounding: e.target.checked } })} /> compound profits</label>
            </div>
            <div className="algo-operand" style={{ gap: 14, marginTop: 8 }}>
              <label className="meta">slippage % <NumInput width={52} value={spec.costs.slippage_pct} onChange={(n) => upd({ costs: { ...spec.costs, slippage_pct: n ?? 0 } })} /></label>
              <label className="meta"><input type="checkbox" checked={spec.costs.charges} onChange={(e) => upd({ costs: { ...spec.costs, charges: e.target.checked } })} /> Indian charges (brokerage, STT, exchange, GST, stamp)</label>
            </div>
            <div className="hint" style={{ marginTop: 8 }} data-testid="sizing-summary">{sizingSummary(spec, fno ? info.data?.lot ?? null : null)}</div>
          </div>

          <div className="widget widget-wide" style={{ marginBottom: 12 }}>
            <div className="algo-operand" style={{ gap: 10 }}>
              <label className="meta">from <input type="date" value={range.start} onChange={(e) => setRange({ ...range, start: e.target.value })} /></label>
              <label className="meta">to <input type="date" value={range.end} onChange={(e) => setRange({ ...range, end: e.target.value })} /></label>
              {[30, 90, 180, 365, 730].map((d) => (
                <button type="button" key={d} className="btn-sm" onClick={() => setRange({ start: daysAgo(d), end: daysAgo(0) })}>{d < 365 ? `${d}d` : `${d / 365}y`}</button>
              ))}
              <label className="meta" title="split the result: the last X% of the range as out-of-sample (5–80, 0 = off)">out-of-sample <NumInput width={36} value={oosPct} onChange={(n) => setOosPct(Math.max(0, Math.min(80, Math.floor(n ?? 0))))} />%</label>
              <button type="button" className="primary" disabled={bt.isPending} onClick={() => { setMsg(null); bt.mutate(); }}>
                {bt.isPending ? "Backtesting… (downloads missing candles first)" : "▶ Run backtest"}
              </button>
              {editing && <button type="button" disabled={save.isPending || !dirty} onClick={saveVersion}
                title={dirty ? "keeps every earlier version — switch back any time" : "no changes to save"}>Save as v{(versions.data?.versions[0]?.version ?? 0) + 1}</button>}
              <button type="button" disabled={save.isPending} onClick={() => save.mutate(true)}>Save as new strategy</button>
            </div>
            {msg && <div className="meta" style={{ marginTop: 6 }}>{msg}</div>}
            {bt.error && <div className="pnl-neg" style={{ marginTop: 6 }}>{errMsg(bt.error)}</div>}
            {stale && !bt.isPending && <div className="meta" style={{ marginTop: 6, color: "var(--amber)" }} data-testid="algo-stale">
              The settings changed since the results below were run — press ▶ Run backtest to refresh them.</div>}
          </div>
          {bt.data && <Results r={bt.data} />}
        </>
      )}
    </div>
  );
}
