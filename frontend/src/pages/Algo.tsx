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
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Area, AreaChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
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
import { peekQuote, useQuoteTick } from "../hooks/useQuotes";
import { Toggle } from "../components/common/Toggle";

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
};
type Cond = { left: Operand; op: string; right?: Operand };
type Group = { logic: "AND" | "OR"; conditions: (Cond | Group)[] };
type Level = { type: string; value: number; atr_period?: number; activate?: number } | null;
type Leg = { right: "CE" | "PE"; action: "BUY" | "SELL"; strike: string; steps: number; premium: number; lots: number };
type Instrument = {
  type: "equity" | "future" | "option";
  expiry: "current" | "next";
  expiry_kind: "weekly" | "monthly";
  legs_long: Leg[];
  legs_short: Leg[];
  levels_on: "instrument" | "underlying";
  iv: { source: string; value: number };
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
  mtm: { stop: number | null; target: number | null; trail_start: number | null; trail_gap: number | null };
  daily: { max_loss: number | null; max_profit: number | null };
  session: { start: string | null; end: string | null; square_off: string | null };
  entry_order: { type: string; offset_pct: number; valid_bars: number };
  max_trades_per_day: number;
  cooldown_bars: number;
  max_bars: number | null;
  sizing: { mode: string; value: number };
  portfolio: { capital: number; leverage: number; max_positions: number; compounding: boolean; max_position_pct?: number | null };
  costs: { slippage_pct: number; charges: boolean };
};
type IndicatorDef = { name: string; params: Record<string, number | string>; outputs: string[]; group: string };
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
type BtLeg = { label: string; side: string; qty: number; entry: number; exit: number };
type BtTrade = {
  symbol: string; instrument: string; side: string; qty: number; lots: number; entry_t: number; entry: number;
  exit_t: number; exit: number; u_entry: number; u_exit: number; reason: string; gross: number; charges: number;
  net: number; bars: number; margin: number; legs: BtLeg[];
  mae_pct: number | null; mfe_pct: number | null; r: number | null; minutes: number;
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
type OptResult = { combos: number; ranked: OptRow[]; too_few_trades: number; metric: string; notes: { symbol: string; note: string }[]; elapsed_s: number; oos_from: number | null };
type Saved = { id: number; name: string; spec: Spec; enabled: boolean; mode: "paper" | "live"; account_id: number | null; closed_trades: number; realized_pnl: number; open_positions: number; version: number; versions: number };
type Version = { version: number; spec: Spec; note: string | null; active: boolean; created_at: string | null; closed_trades: number; realized_pnl: number };
type LiveLeg = { symbol: string; label?: string; act: number; qty: number; entry: number; exit?: number; ltp?: number | null };
type LiveTrade = {
  id: number; strategy_id: number; symbol: string; instrument: string | null; side: string; quantity: number; mode: string; status: string;
  entry_at: string; entry_price: number | null; stop_loss: number | null; target: number | null; trail_stop: number | null;
  exit_at: string | null; exit_price: number | null; exit_reason: string | null; net_pnl: number | null; charges: number | null;
  note: string | null; ltp?: number | null; unrealized?: number | null; legs: LiveLeg[] | null; be_on?: boolean | null; ref?: string;
};
type RunnerStatus = { running: boolean; last_tick: number | null; last_sync: { t: number; symbols: number; ok: number; failed: string[] } | null; events: { t: number; level: string; msg: string }[]; open: LiveTrade[] };
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
const L = (right: "CE" | "PE", action: "BUY" | "SELL", strike = "ATM", steps = 0, lots = 1, premium = 100): Leg => ({ right, action, strike, steps, premium, lots });
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
const STRIKES = ["ATM", ...Array.from({ length: 20 }, (_, k) => `ITM${k + 1}`), ...Array.from({ length: 20 }, (_, k) => `OTM${k + 1}`), "PREMIUM"];
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
  for (const k of keys.slice(0, -1)) {
    if (o[k] === undefined || o[k] === null) o[k] = {};
    o = o[k] as Record<string, unknown>;
  }
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
  (["stop", "target", "trail_start", "trail_gap"] as const).forEach((k) => {
    const v = spec.mtm[k];
    if (v) out.push({ path: `mtm.${k}`, label: `MTM ${k.replace("_", " ")} ₹`, value: v });
  });
  if (spec.instrument.type === "option") {
    (["legs_long", "legs_short"] as const).forEach((side) => spec.instrument[side].forEach((lg, i) => {
      if (lg.strike === "ITM" || lg.strike === "OTM") out.push({ path: `instrument.${side}.${i}.steps`, label: `${side} leg ${i + 1} ${lg.right} strikes ${lg.strike}`, value: lg.steps });
      if (lg.strike === "PREMIUM") out.push({ path: `instrument.${side}.${i}.premium`, label: `${side} leg ${i + 1} ${lg.right} target premium`, value: lg.premium });
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
];

const LEG_PRESETS: { name: string; long: Leg[]; short: Leg[] }[] = [
  { name: "Buy ATM CE / PE", long: [L("CE", "BUY")], short: [L("PE", "BUY")] },
  { name: "Buy 1-OTM CE / PE", long: [L("CE", "BUY", "OTM", 1)], short: [L("PE", "BUY", "OTM", 1)] },
  { name: "Sell ATM PE / CE (writer)", long: [L("PE", "SELL")], short: [L("CE", "SELL")] },
  { name: "Bull call / bear put spread", long: [L("CE", "BUY"), L("CE", "SELL", "OTM", 2)], short: [L("PE", "BUY"), L("PE", "SELL", "OTM", 2)] },
  { name: "Short straddle", long: [L("CE", "SELL"), L("PE", "SELL")], short: [L("CE", "SELL"), L("PE", "SELL")] },
  { name: "Short strangle (3 OTM)", long: [L("CE", "SELL", "OTM", 3), L("PE", "SELL", "OTM", 3)], short: [L("CE", "SELL", "OTM", 3), L("PE", "SELL", "OTM", 3)] },
  { name: "Iron fly (hedged straddle)", long: [L("CE", "SELL"), L("PE", "SELL"), L("CE", "BUY", "OTM", 4), L("PE", "BUY", "OTM", 4)], short: [L("CE", "SELL"), L("PE", "SELL"), L("CE", "BUY", "OTM", 4), L("PE", "BUY", "OTM", 4)] },
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

function OperandEditor({ v, onChange, cat, baseTf, allowNumber = true }: { v: Operand; onChange: (o: Operand) => void; cat: Catalog; baseTf: number; allowNumber?: boolean }) {
  const def = v.ind ? cat.indicators.find((d) => d.name === v.ind) : undefined;
  const groups = useMemo(() => Array.from(new Set(cat.indicators.map((d) => d.group))), [cat]);
  const [math, setMath] = useState((v.mult !== undefined && v.mult !== 1) || !!v.add);
  const tfs = cat.cond_timeframes.filter((t) => t === 1440 || (t > baseTf && t % baseTf === 0));
  return (
    <span className="algo-operand">
      <select value={v.ind ?? "#"} onChange={(e) => onChange(e.target.value === "#" ? { value: 0 } : I(e.target.value))}>
        {allowNumber && <option value="#">Number</option>}
        {groups.map((g) => (
          <optgroup key={g} label={g}>
            {cat.indicators.filter((d) => d.group === g).map((d) => <option key={d.name} value={d.name}>{d.name}</option>)}
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
};

function CondEditor({ c, onChange, cat, baseTf }: { c: Cond; onChange: (c: Cond) => void; cat: Catalog; baseTf: number }) {
  const trend = c.op === "rising" || c.op === "falling";
  return (
    <span className="algo-operand" style={{ gap: 8 }}>
      <OperandEditor v={c.left} cat={cat} baseTf={baseTf} allowNumber={false} onChange={(left) => onChange({ ...c, left })} />
      <select value={c.op} onChange={(e) => {
        const op = e.target.value;
        const t = op === "rising" || op === "falling";
        onChange({ ...c, op, right: t ? N(Math.max(1, c.right?.value ?? 1)) : c.right?.ind || c.right?.value !== undefined ? c.right : N(0) });
      }}>
        {cat.operators.map((o) => <option key={o} value={o}>{OP_LABEL[o] ?? o}</option>)}
      </select>
      {trend ? (
        <NumInput width={44} value={c.right?.value ?? 1} onChange={(n) => onChange({ ...c, right: N(Math.max(1, Math.floor(n ?? 1))) })} />
      ) : (
        <OperandEditor v={c.right ?? N(0)} cat={cat} baseTf={baseTf} onChange={(right) => onChange({ ...c, right })} />
      )}
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
            <option value="CE">CE (call)</option><option value="PE">PE (put)</option>
          </select>
          <select value={strikeKey(lg)} title="strikes are counted in the underlying's strike steps" onChange={(e) => {
            const m = e.target.value.match(/^(ITM|OTM)(\d+)$/);
            set(i, m ? { strike: m[1], steps: Number(m[2]) } : { strike: e.target.value, steps: 0 });
          }}>
            {STRIKES.map((k) => <option key={k} value={k}>{k === "PREMIUM" ? "closest to premium ₹" : k.replace(/(\d+)$/, " $1")}</option>)}
          </select>
          {lg.strike === "PREMIUM" && <label className="meta">₹<NumInput width={56} value={lg.premium} onChange={(n) => set(i, { premium: n ?? 100 })} /></label>}
          <label className="meta">lots <NumInput width={40} value={lg.lots} onChange={(n) => set(i, { lots: Math.max(1, Math.floor(n ?? 1)) })} /></label>
          <button type="button" className="btn-sm danger" onClick={() => onChange(legs.filter((_, j) => j !== i))}>✕</button>
        </div>
      ))}
      <div><button type="button" className="btn-sm" onClick={() => onChange([...legs, L("CE", "BUY")])}>+ leg</button></div>
    </div>
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

function tradesCsv(trades: BtTrade[]): string {
  const head = ["symbol", "instrument", "side", "qty", "lots", "entry_time", "entry", "exit_time", "exit", "underlying_entry", "underlying_exit", "reason", "bars", "minutes", "mae_pct", "mfe_pct", "r_multiple", "gross", "charges", "net"];
  const iso = (t: number) => new Date((t + IST_S) * 1000).toISOString().replace("T", " ").slice(0, 16);
  const rows = trades.map((t) => [t.symbol, t.instrument, t.side, t.qty, t.lots, iso(t.entry_t), t.entry, iso(t.exit_t), t.exit, t.u_entry, t.u_exit, t.reason, t.bars, t.minutes, t.mae_pct ?? "", t.mfe_pct ?? "", t.r ?? "", t.gross, t.charges, t.net]);
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
            <thead><tr><th>Instrument</th><th>Signal</th><th>Qty</th><th>Entry (IST)</th><th>Entry</th><th>Exit (IST)</th><th>Exit</th><th>Reason</th><th>Bars</th><th title="max adverse / favourable excursion, % of entry">MAE / MFE %</th><th title="net / initial stop risk">R</th><th>Charges</th><th>Net</th></tr></thead>
            <tbody>{trades.map((t, i) => (
              <tr key={i} title={t.legs.map((l) => `${l.side} ${l.qty} ${l.label}: ${l.entry} → ${l.exit}`).join("\n")}>
                <td>{t.instrument.length > 46 ? `${t.legs.length} legs · ${shortSym(t.symbol)}` : t.instrument.replace(/^NSE:/, "")}</td>
                <td><span className={`badge ${t.side === "BUY" ? "buy" : "sell"}`}>{t.side === "BUY" ? "LONG" : "SHORT"}</span></td>
                <td className="mono">{t.qty}</td><td className="mono">{ist(t.entry_t)}</td><td className="mono">{t.entry.toFixed(2)}</td>
                <td className="mono">{ist(t.exit_t)}</td><td className="mono">{t.exit.toFixed(2)}</td><td>{t.reason}</td>
                <td className="mono">{t.bars}</td><td className="mono">{num(t.mae_pct, 2)} / {num(t.mfe_pct, 2)}</td>
                <td className={`mono ${pnlCls(t.r)}`}>{num(t.r, 2)}</td><td className="mono">{t.charges.toFixed(0)}</td><td className={`mono ${pnlCls(t.net)}`}>{t.net.toFixed(0)}</td></tr>))}
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
  const [oos, setOos] = useState(30);
  const values = (a: { from: number; to: number; step: number }) => {
    const out: number[] = [];
    if (a.step <= 0) return [a.from];
    for (let v = a.from; v <= a.to + 1e-9 && out.length < 60; v += a.step) out.push(Math.round(v * 1e6) / 1e6);
    return out;
  };
  const combos = axes.reduce((n, a) => n * values(a).length, axes.length ? 1 : 0);
  const run = useMutation({
    mutationFn: () => longPost<OptResult>("/api/algo/optimize", {
      spec: tidy(spec), ...range, metric, min_trades: minTrades, oos_pct: oos || undefined,
      grid: axes.map((a) => ({ path: a.path, values: values(a) })),
    }),
  });
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
          <label className="meta" title="the last X% of the range is held out: ranking uses only the earlier part, and each row shows how it did afterwards (walk-forward). 0 = off">hold out last <NumInput width={36} value={oos} onChange={(n) => setOos(Math.max(0, Math.min(80, Math.floor(n ?? 0))))} />% as out-of-sample</label>
          <label className="meta" title="combos with fewer trades are not ranked — a 1-trade 100% win rate means nothing">min trades <NumInput width={52} value={minTrades} onChange={(n) => setMinTrades(Math.max(1, Math.floor(n ?? 1)))} /></label>
          <span className="meta">{combos} combinations (max 400)</span>
          <button type="button" className="primary" disabled={!combos || combos > 400 || run.isPending} onClick={() => run.mutate()}>{run.isPending ? "Optimising…" : "Run optimisation"}</button>
        </div>
      </div>
      {run.error && <div className="pnl-neg">{errMsg(run.error)}</div>}
      {run.data && (
        <>
          <div className="meta" style={{ marginBottom: 6 }}>{run.data.combos} combos in {run.data.elapsed_s}s · {run.data.too_few_trades} skipped for too few trades.{run.data.oos_from ? ` Ranked on the in-sample period only (before ${ist(run.data.oos_from)}); trust rows whose OOS columns hold up.` : " Beware overfitting — hold out an out-of-sample % to check."}</div>
          <div style={{ maxHeight: 480, overflow: "auto" }}>
            <table>
              <thead><tr><th></th>{axes.map((a) => <th key={a.path} title={a.path}>{label(a.path)}</th>)}<th>Trades</th><th>Win %</th><th>PF</th><th>Sharpe</th><th>t</th><th>Max DD</th><th>Net</th>{run.data.oos_from ? <><th title="out-of-sample">OOS trades</th><th title="out-of-sample">OOS PF</th><th title="out-of-sample">OOS net</th></> : null}</tr></thead>
              <tbody>{run.data.ranked.map((r, i) => (
                <tr key={i}>
                  <td><button type="button" className="btn-sm" onClick={() => onApply(r.params)}>apply</button></td>
                  {axes.map((a) => <td key={a.path} className="mono">{r.params[a.path]}</td>)}
                  <td className="mono">{r.trades}</td><td className="mono">{num(r.win_rate, 1)}</td><td className="mono">{num(r.profit_factor)}</td>
                  <td className="mono">{num(r.sharpe)}</td><td className="mono">{num(r.t_stat)}</td><td className="mono">{inr(r.max_drawdown)}</td><td className={`mono ${pnlCls(r.net_pnl)}`}>{inr(r.net_pnl)}</td>
                  {run.data!.oos_from ? <><td className="mono">{r.oos?.trades ?? "—"}</td><td className="mono">{num(r.oos?.profit_factor)}</td>
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

function AlgoLiveLtp({ t }: { t: LiveTrade }) {
  useQuoteTick();
  const ref = t.ref === "u" ? t.symbol : t.legs?.[0]?.symbol ?? t.symbol;
  return <>{num(peekQuote(ref)?.last_price ?? t.ltp)}</>;
}

function Automations({ onEdit }: { onEdit: (s: Saved) => void }) {
  const qc = useQueryClient();
  const strategies = useQuery({ queryKey: ["algo", "strategies"], queryFn: () => api.get<{ strategies: Saved[] }>("/api/algo/strategies"), refetchInterval: 10000 });
  // 1 s: the endpoint reads the tick stream (no Fyers REST per call); the MTM cells tick per frame on top of it.
  const status = useQuery({ queryKey: ["algo", "status"], queryFn: () => api.get<RunnerStatus>("/api/algo/status"), refetchInterval: 1000 });
  const accounts = useQuery({ queryKey: ["algo", "accounts"], queryFn: () => api.get<{ accounts: Account[] }>("/api/broker-accounts") });
  const [sel, setSel] = useState<number | null>(null);
  const trades = useQuery({
    queryKey: ["algo", "trades", sel], refetchInterval: 3000,
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
  const nameOf = (id: number) => strategies.data?.strategies.find((s) => s.id === id)?.name ?? String(id);
  const openShown = (st?.open ?? []).filter((t) => !sel || t.strategy_id === sel);
  useQuoteTick();   // re-render the header total per tick
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
    return `options · ${i.expiry_kind} ${i.expiry} · ${legs.map((l) => `${l.action[0]} ${l.strike === "PREMIUM" ? `₹${l.premium}` : l.strike + (l.steps ? l.steps : "")} ${l.right}`).join(", ")}`;
  };
  return (
    <>
      {err && <div className="widget widget-wide pnl-neg" style={{ marginBottom: 12 }}>{err}</div>}
      <div className="widget widget-wide" style={{ marginBottom: 12 }}>
        <h3>Automations <span className="meta">
          runner {st?.running ? (ago !== null && ago < 30 ? `alive · tick ${ago}s ago` : "idle") : "not running"} ·
          evaluates on each completed candle, exits on live LTP every 1s · every setting lives in Builder & backtest
        </span></h3>
        {(strategies.data?.strategies ?? []).length === 0 ? (
          <div className="empty">No saved strategies yet — build one in the Builder tab and press “Save as new”.</div>
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table>
              <thead><tr><th>On</th><th>Strategy</th><th>Version</th><th>Symbols</th><th>Trades</th><th>TF</th><th>Mode</th><th>Open</th><th>Closed</th><th>Realised</th><th></th></tr></thead>
              <tbody>{strategies.data!.strategies.map((s) => (
                <tr key={s.id} style={sel === s.id ? { background: "var(--bg-row)" } : undefined}>
                  <td><Toggle on={s.enabled} size="sm" onChange={(on: boolean) => put(s.id, { enabled: on })} /></td>
                  <td><a href="#/algo" onClick={(e) => { e.preventDefault(); setSel(sel === s.id ? null : s.id); }}>{s.name}</a>{" "}
                    <button type="button" className="btn-sm" onClick={() => setSel(s.id)} title="this automation's trades and open positions" data-testid={`algo-trades-${s.id}`}>Trades</button></td>
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
                  <td className="meta" style={{ maxWidth: 240, whiteSpace: "normal" }}>{instLabel(s)}</td>
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
        <h3>Open positions {sel ? <span className="meta">— {nameOf(sel)}</span> : null} <span className="meta">· live MTM {inr(liveTotal)}</span></h3>
        {!openShown.length ? <div className="empty">flat</div> : (
          <div style={{ overflowX: "auto" }}>
            <table>
              <thead><tr><th>Strategy</th><th>Instrument</th><th>Signal</th><th>Qty</th><th>Mode</th><th>Ref entry</th><th>Ref LTP</th><th>Stop</th><th>Trail</th><th>Target</th><th>MTM</th><th>Since</th></tr></thead>
              <tbody>{openShown.map((t) => (
                <tr key={t.id} title={(t.legs ?? []).map((l) => `${l.act > 0 ? "BUY" : "SELL"} ${l.qty} ${l.label ?? l.symbol}: ${num(l.entry)} → ${num(l.ltp)}`).join("\n")}>
                  <td>{nameOf(t.strategy_id)}</td><td>{(t.instrument ?? t.symbol).replace(/NSE:/g, "")}</td>
                  <td><span className={`badge ${t.side === "BUY" ? "buy" : "sell"}`}>{t.side === "BUY" ? "LONG" : "SHORT"}</span></td><td className="mono">{t.quantity}</td><td>{t.mode}</td>
                  <td className="mono">{num(t.entry_price)}</td><td className="mono"><AlgoLiveLtp t={t} /></td><td className="mono">{t.be_on ? `BE ${num(t.entry_price)}` : num(t.stop_loss)}</td>
                  <td className="mono">{num(t.trail_stop)}</td><td className="mono">{num(t.target)}</td>
                  <AlgoLiveMtm t={t} /><td className="mono">{isoIst(t.entry_at)}</td></tr>))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(380px, 1fr))", gap: 12 }}>
        <div className="widget">
          <h3>Trades {sel ? <span className="meta">— {nameOf(sel)} <button type="button" className="btn-sm" onClick={() => setSel(null)}>show all</button></span> : <span className="meta">— all strategies</span>}</h3>
          <div className="meta" data-testid="algo-trade-summary">{summary.n} closed · {summary.n ? Math.round((summary.wins / summary.n) * 100) : 0}% winners · net <span className={pnlCls(summary.net)}>{inr(summary.net)}</span> · open MTM <span className={pnlCls(liveTotal)}>{inr(liveTotal)}</span></div>
          <div style={{ maxHeight: 420, overflow: "auto" }}>
            {!trades.data?.trades.length ? <div className="empty">none yet</div> : (
              <table>
                <thead><tr><th>Instrument</th><th>Signal</th><th>Qty</th><th>Status</th><th>Entry</th><th>Exit</th><th>Reason</th><th>Net</th></tr></thead>
                <tbody>{trades.data.trades.map((t) => (
                  <tr key={t.id} title={[t.note ?? "", ...(t.legs ?? []).map((l) => `${l.act > 0 ? "BUY" : "SELL"} ${l.qty} ${l.label ?? l.symbol}: ${num(l.entry)} → ${num(l.exit)}`)].filter(Boolean).join("\n")}>
                    <td>{(t.instrument ?? t.symbol).replace(/NSE:/g, "")}</td><td>{t.side === "BUY" ? "LONG" : "SHORT"}</td><td className="mono">{t.quantity}</td>
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

type RecStatus = { enabled: boolean; config_symbols: string[]; strategy_symbols: string[]; subscribed: string[];
  today: Record<string, { ticks: number; trades: number; buy: number; sell: number; last: number }>;
  days_recorded: Record<string, number>; disk_mb: number };

function RecorderPanel() {
  const qc = useQueryClient();
  const st = useQuery({ queryKey: ["algo", "ticks"], queryFn: () => api.get<RecStatus>("/api/algo/ticks/status"), refetchInterval: 10000 });
  const [syms, setSyms] = useState<string[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const list = syms ?? st.data?.config_symbols ?? [];
  const save = async (enabled: boolean, symbols: string[]) => {
    setErr(null);
    try { await api.put("/api/algo/ticks/config", { enabled, symbols }); setSyms(null); qc.invalidateQueries({ queryKey: ["algo", "ticks"] }); }
    catch (e) { setErr(errMsg(e)); }
  };
  const fmt = (v: number) => Math.round(v).toLocaleString("en-IN");
  const keys = Array.from(new Set([...Object.keys(st.data?.today ?? {}), ...Object.keys(st.data?.days_recorded ?? {})])).sort();
  return (
    <div className="widget widget-wide" style={{ marginBottom: 12 }}>
      <h3>Real tick recorder <span className="meta">every Fyers tick classified buy / sell (Lee-Ready) → real order flow for backtests, live candles and the chart</span></h3>
      {err && <div className="pnl-neg">{err}</div>}
      <div className="algo-operand" style={{ gap: 10 }}>
        <Toggle on={!!st.data?.enabled} onChange={(on: boolean) => save(on, list)} label="recording" />
        <span className="meta">{st.data ? `${st.data.subscribed.length} subscribed · ${st.data.disk_mb} MB on disk` : "…"}</span>
      </div>
      <div className="meta" style={{ margin: "8px 0 4px" }}>Symbols to record ("NIFTY:FUT" = the current NIFTY future, rolls by itself; an index records its future). Symbols of switched-on strategies are added automatically.</div>
      <SymbolPicker value={list} onChange={(v) => setSyms(v)} />
      <div className="algo-operand" style={{ gap: 6, marginTop: 6 }}>
        {["NIFTY:FUT", "BANKNIFTY:FUT", "FINNIFTY:FUT", "MIDCPNIFTY:FUT", "SENSEX:FUT"].filter((x) => !list.includes(x)).map((x) => (
          <button key={x} type="button" className="btn-sm ghost" onClick={() => setSyms([...list, x])}>+ {x.replace(":FUT", " future")}</button>
        ))}
        {syms && <button type="button" className="primary" onClick={() => save(st.data?.enabled ?? true, list)}>Save list</button>}
        {(st.data?.strategy_symbols ?? []).length > 0 && <span className="meta">+ from strategies: {st.data!.strategy_symbols.map(shortSym).join(", ")}</span>}
      </div>
      {keys.length > 0 && (
        <div className="algo-scroll" style={{ marginTop: 8 }}>
          <table>
            <thead><tr><th>Key</th><th>Ticks today</th><th>Trades</th><th>Buy vol</th><th>Sell vol</th><th>Delta</th><th>Buy %</th><th>Last tick</th><th>Days stored</th></tr></thead>
            <tbody>{keys.map((k) => {
              const t = st.data?.today[k];
              return (
                <tr key={k}><td>{k}</td><td className="mono">{t ? fmt(t.ticks) : "—"}</td><td className="mono">{t ? fmt(t.trades) : "—"}</td>
                  <td className="mono up">{t ? fmt(t.buy) : "—"}</td><td className="mono down">{t ? fmt(t.sell) : "—"}</td>
                  <td className={`mono ${t && t.buy >= t.sell ? "pnl-pos" : "pnl-neg"}`}>{t ? fmt(t.buy - t.sell) : "—"}</td>
                  <td className="mono">{t && t.buy + t.sell ? ((t.buy / (t.buy + t.sell)) * 100).toFixed(1) : "—"}</td>
                  <td className="mono">{t?.last ? ist(t.last) : "—"}</td><td className="mono">{st.data?.days_recorded[k] ?? 0}</td></tr>
              );
            })}</tbody>
          </table>
        </div>
      )}
      {keys.length === 0 && <div className="empty">Nothing recorded yet — ticks arrive during market hours (09:15–15:30 IST).</div>}
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
  const run = async () => {
    setRunning(true);
    const out: typeof rows = [];
    for (const o of options.filter((x) => pick.includes(x.key))) {
      try {
        const r = await longPost<BtResult>("/api/algo/backtest", { spec: tidy(withDefaults(cat.defaults, o.spec)), ...range, oos_pct: oos || undefined });
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
        <label className="meta">out-of-sample <NumInput width={36} value={oos} onChange={(n) => setOos(Math.max(0, Math.min(80, Math.floor(n ?? 0))))} />%</label>
        <button type="button" className="primary" disabled={running || !pick.length} onClick={run}>{running ? `Running ${rows.length + 1}/${pick.length}…` : "Run comparison"}</button>
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
  const fileRef = useRef<HTMLInputElement>(null);

  // Restore the draft (or start from the server defaults + the first template).
  useEffect(() => {
    if (!cat.data || spec) return;
    let draft: { spec: Partial<Spec>; name: string; editing: number | null; viewing?: number | null } | null = null;
    try { draft = JSON.parse(localStorage.getItem(DRAFT_KEY) ?? "null"); } catch { /* private mode */ }
    if (draft?.spec) {
      setSpec(withDefaults(cat.data.defaults, draft.spec));
      setName(draft.name);
      setEditing(draft.editing);
      setViewing(draft.viewing ?? null);
    } else {
      setSpec(withDefaults(cat.data.defaults, { symbols: ["NSE:SBIN-EQ"], ...TEMPLATES[0].spec }));
      setName(TEMPLATES[0].name);
    }
  }, [cat.data, spec]);
  useEffect(() => {
    if (!spec) return;
    try { localStorage.setItem(DRAFT_KEY, JSON.stringify({ spec, name, editing, viewing })); } catch { /* ignore */ }
  }, [spec, name, editing, viewing]);

  const first = spec?.symbols[0];
  const info = useQuery({
    queryKey: ["algo", "instrument", first], enabled: !!first, staleTime: 300000,
    queryFn: () => api.get<InstInfo>(`/api/algo/instrument?symbol=${encodeURIComponent(first!)}`),
  });

  const bt = useMutation({
    mutationFn: () => longPost<BtResult>("/api/algo/backtest", { spec: tidy(spec!), ...range, oos_pct: oosPct || undefined }),
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
    onSuccess: (s) => {
      setEditing(s.id);
      setViewing(s.version);
      setMsg(`Saved “${s.name}” as v${s.version} — it is the active version. Switch it on in the Automations tab.`);
      qc.invalidateQueries({ queryKey: ["algo"] });
    },
    onError: (e) => setMsg(errMsg(e)),
  });
  const activate = useMutation({
    mutationFn: (v: number) => api.post<Saved>(`/api/algo/strategies/${editing}/versions/${v}/activate`, {}),
    onSuccess: (s) => { setMsg(`v${s.version} is now the active version.`); qc.invalidateQueries({ queryKey: ["algo"] }); },
    onError: (e) => setMsg(errMsg(e)),
  });

  if (cat.isLoading || (!spec && !cat.error)) return <div className="empty">loading…</div>;
  if (cat.error || !cat.data || !spec) return <div className="empty">Algo API unavailable: {errMsg(cat.error)}</div>;
  const c = cat.data;
  const upd = (patch: Partial<Spec>) => setSpec({ ...spec, ...patch });
  const inst = spec.instrument;
  const updInst = (patch: Partial<Instrument>) => upd({ instrument: { ...inst, ...patch } });
  const showLong = spec.direction !== "short";
  const showShort = spec.direction !== "long";
  const fno = inst.type !== "equity";
  const multi = inst.type === "option" && ((showLong && inst.legs_long.length > 1) || (showShort && inst.legs_short.length > 1));
  const sizingModes = c.sizing.filter((m) => (fno ? m !== "qty" : m !== "lots"));
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
  const levelKinds = inst.type === "option" && inst.levels_on === "instrument" ? ["pct", "points"] : ["pct", "points", "atr"];
  const levelsHint = inst.type === "option"
    ? inst.levels_on === "instrument" ? "measured on the option premium" : "measured on the underlying's price"
    : inst.type === "future" && inst.levels_on === "instrument" ? "measured on the futures price" : "measured on the traded price";

  return (
    <div className="algo">
      <div className="dashboard-head">
        <h1 className="page-title">Algo Lab</h1>
        <button type="button" className="primary" style={{ marginLeft: 12 }} onClick={() => {
          if (!editing && !window.confirm("Start a new algo? The unsaved draft in the builder will be replaced.")) return;
          setSpec(withDefaults(cat.data!.defaults, { symbols: [], entry_long: G([]), entry_short: G([]) }));
          setName("New strategy"); setEditing(null); setViewing(null); bt.reset(); setTab("builder");
          setMsg("New algo — pick symbols, add entry conditions, then backtest and save.");
        }}>＋ New algo</button>
      </div>
      <div className="tabs">
        {(["builder", "optimize", "compare", "automations", "data"] as Tab[]).map((t) => (
          <button type="button" key={t} className={`tab ${tab === t ? "active" : ""}`} onClick={() => setTab(t)}>
            {{ builder: "Builder & backtest", optimize: "Optimise", compare: "Compare", automations: "Automations", data: "Data" }[t]}
          </button>
        ))}
      </div>

      {tab === "automations" && <Automations onEdit={(s) => {
        setName(s.name); setEditing(s.id); setViewing(s.version);
        setSpec(withDefaults(c.defaults, s.spec)); setTab("builder");
      }} />}
      {tab === "data" && <><RecorderPanel /><DataTab /></>}
      {tab === "compare" && <CompareTab cat={c} range={range} draft={spec} />}
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
            <h3>Strategy {editing ? <span className="meta">saved #{editing}{viewing ? ` · v${viewing}` : ""}</span> : <span className="meta">unsaved draft</span>}
              <span style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
                <button type="button" className="btn-sm" onClick={() => download(`${name.replace(/[^\w-]+/g, "_")}.json`, JSON.stringify({ name, spec: tidy(spec) }, null, 2))}>⬇ export</button>
                <button type="button" className="btn-sm" onClick={() => fileRef.current?.click()}>⬆ import</button>
                <input ref={fileRef} type="file" accept="application/json,.json" style={{ display: "none" }} onChange={async (e) => {
                  const f = e.target.files?.[0];
                  e.target.value = "";
                  if (!f) return;
                  try {
                    const j = JSON.parse(await f.text()) as { name?: string; spec?: Partial<Spec> } & Partial<Spec>;
                    setSpec(withDefaults(c.defaults, j.spec ?? j)); setName(j.name ?? f.name.replace(/\.json$/, "")); setEditing(null); setViewing(null);
                    setMsg("Imported — save it to keep it.");
                  } catch (err) { setMsg(`Import failed: ${errMsg(err)}`); }
                }} />
              </span>
            </h3>
            <div className="algo-fields">
              <label className="algo-field"><span className="meta">name</span><input value={name} onChange={(e) => setName(e.target.value)} /></label>
              <label className="algo-field"><span className="meta">template</span>
                <select value="" onChange={(e) => {
                  const t = TEMPLATES[Number(e.target.value)];
                  if (t) { setSpec(withDefaults(c.defaults, { symbols: t.symbols ?? (spec.symbols.length ? spec.symbols : ["NSE:SBIN-EQ"]), ...t.spec })); setName(t.name); setEditing(null); setViewing(null); }
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
                    onClick={() => { setSpec(withDefaults(c.defaults, v.spec)); setViewing(v.version); setMsg(`Loaded v${v.version} into the builder${v.active ? " (active)" : " — not active yet"}.`); }}>
                    v{v.version}{v.active ? " ✓" : ""}
                  </button>
                ))}
                {viewing && viewing !== versions.data.active && (
                  <button type="button" className="btn-sm" disabled={activate.isPending} onClick={() => activate.mutate(viewing)}>make v{viewing} active</button>
                )}
                <span className="meta">✓ = runs in Automations · saving creates the next version</span>
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
                  <span className="meta">preset</span>
                  {LEG_PRESETS.map((p) => (
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
                <div className="algo-operand" style={{ gap: 8, marginTop: 8 }}>
                  <label className="meta" title="backtests only — live trades use real premiums">backtest IV
                    <select value={inst.iv.source} onChange={(e) => updInst({ iv: { ...inst.iv, source: e.target.value } })}>
                      <option value="auto">auto (India VIX for indices, realised vol for stocks)</option>
                      <option value="vix">India VIX</option><option value="hv">20-day realised volatility</option><option value="fixed">fixed %</option>
                    </select>
                    {inst.iv.source === "fixed" && <NumInput width={48} value={inst.iv.value} onChange={(n) => updInst({ iv: { ...inst.iv, value: n ?? 15 } })} />}
                  </label>
                  <span className="meta">Strikes ATM/ITM/OTM are counted in the underlying's strike steps. Multi-leg positions exit together — use MTM ₹ stops.</span>
                </div>
              </>
            )}
          </div>

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
            <div className="algo-operand" style={{ gap: 14, marginTop: 8 }}>
              <span className="meta" style={{ color: "var(--text)" }}>Position MTM:</span>
              <Rupee label="stop" value={spec.mtm.stop} onChange={(v) => upd({ mtm: { ...spec.mtm, stop: v } })} hint="exit when the position's P&L falls to −₹X" />
              <Rupee label="target" value={spec.mtm.target} onChange={(v) => upd({ mtm: { ...spec.mtm, target: v } })} hint="exit when the position's P&L reaches +₹X" />
              <Rupee label="trail after" value={spec.mtm.trail_start} onChange={(v) => upd({ mtm: { ...spec.mtm, trail_start: v } })} hint="once P&L has reached ₹X…" />
              <Rupee label="by" value={spec.mtm.trail_gap} onChange={(v) => upd({ mtm: { ...spec.mtm, trail_gap: v } })} hint="…exit if it gives back ₹Y from its peak" />
            </div>
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
                const off = { start: "from the 09:15 open", end: "until square-off", square_off: "at 15:29" }[k];
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
              <label className="meta" title="intraday margin multiplier: equity MIS ≈ 5, futures / written options ≈ 6–9. Bought options always need the full premium.">leverage × <NumInput width={40} value={spec.portfolio.leverage} onChange={(n) => upd({ portfolio: { ...spec.portfolio, leverage: n ?? 1 } })} /></label>
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
              <label className="meta" title="split the result: the last X% of the range as out-of-sample (0 = off)">out-of-sample <NumInput width={36} value={oosPct} onChange={(n) => setOosPct(Math.max(0, Math.min(80, Math.floor(n ?? 0))))} />%</label>
              <button type="button" className="primary" disabled={bt.isPending} onClick={() => { setMsg(null); bt.mutate(); }}>
                {bt.isPending ? "Backtesting… (downloads missing candles first)" : "▶ Run backtest"}
              </button>
              {editing && <button type="button" disabled={save.isPending} onClick={() => save.mutate(false)}
                title="keeps every earlier version — switch back any time">Save as v{(versions.data?.versions[0]?.version ?? 0) + 1}</button>}
              <button type="button" disabled={save.isPending} onClick={() => save.mutate(true)}>Save as new strategy</button>
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
