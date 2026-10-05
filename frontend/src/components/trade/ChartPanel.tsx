// ChartPanel — TradingView-style interactive chart for the Trade page.
//
// Candles come from GET /api/market/history (Fyers-only, like every other
// price in the app — no public-feed fallback), and the last bar is kept
// live from the `/ws` quote stream. Built on lightweight-charts v5,
// TradingView's own open-source chart engine.
//
// What lives where:
//   chartData.ts         intervals (5s … 12M), aggregation, Renko / Kagi / P&F …
//   indicatorCatalog.ts  built-in indicators as data; instances with settings
//   drawings.ts          ~85 drawing tools on one shape engine
//   customSeries.ts      volume candles, HLC / high-low bars, HLC area, Kagi, P&F
//   alerts.ts            alert model + per-tick evaluation
//   *Dialog*.tsx         settings, indicators, symbol search, go to, alerts …
//   DrawingToolbar.tsx   the left strip, favorites bar, floating drawing bar
//   ChartWidgets.tsx     data window, object tree, alerts manager (dock panels)
//   chartSync.ts         crosshair / time / interval / drawing sync between charts
//
// This file owns the engine: series, panes, data loading and paging,
// live ticks, replay, mouse + keyboard handling, and the chart chrome
// (top toolbar, drawing strip, legend, date-range bar), which renders
// inline or into the shared slots of a multi-chart layout.
//
// Times: Fyers candles are epoch seconds. lightweight-charts renders
// times as UTC, so every timestamp is shifted by +05:30 before it is
// handed to the chart — the time axis reads exchange time natively and
// other display time zones are applied by the formatters. The same shift
// is undone when paging older history from the API.

import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import {
  AreaSeries,
  BarSeries,
  BaselineSeries,
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  PriceScaleMode,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type Logical,
  type LogicalRange,
  type MouseEventParams,
  type SeriesType,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { api } from "../../api/client";
import { getTheme, THEME_EVENT, toggleTheme } from "../../lib/theme";
import { useLiveQuote } from "../../hooks/useQuotes";
import { heikinAshi, heikinAshiBar, type OhlcvCandle } from "../../lib/indicators";
import type { HistoryResponse, InstrumentHit, SearchResponse } from "../../types";
import {
  aggregate,
  autoBox,
  fetchPlan,
  formatClock,
  formatDate,
  intervalCount,
  intervalGroup,
  intervalLabel,
  intervalLongLabel,
  intervalSeconds,
  isDerivative,
  isIntraday,
  kagi,
  lineBreak,
  liveBucket,
  mergeOlder,
  monthName,
  normalizeInterval,
  parseInterval,
  pointFigure,
  rangeBars,
  renko,
  TIMEZONES,
  wallClock,
  zoneOffsetLabel,
  IST_OFFSET,
  type Bar,
} from "./chartData";
import {
  INDICATOR_BY_TYPE,
  argsLabel,
  instanceDefaults,
  instanceTitle,
  migrateActive,
  newInstance,
  newUid,
  sanitizeInstance,
  visibleOnInterval,
  type IndicatorInstance,
  type Mark,
  type PlotKind,
} from "./indicatorCatalog";
import { symbolInputs, volumeProfileRows } from "./indicatorMore";
import { FillPrimitive, paintOiProfile, paintProfile, type FillBand, type OiStrike } from "./indicatorRender";
import {
  DrawingsPrimitive,
  TOOL_BY_ID,
  anchorBox,
  drawingValueAt,
  finalizeDrawing,
  hitHandle,
  hitTest,
  newDrawingId,
  normalizeDrawing,
  pointsNeeded,
  snapAngle,
  styleOf,
  visibleNow,
  type Drawing,
  type DrawingData,
  type DrawingDeps,
  type DrawingPoint,
  type DrawingStyle,
  type Pending,
  type ToolGroupId,
} from "./drawings";
import { ShapeSeries, type ShapeMode } from "./customSeries";
import { beep, evaluate, migrateAlert, newAlert, type AlertItem } from "./alerts";
import { chartFocus, drawingClipboard, indicatorClipboard, NO_SYNC, publishSync, pushLog, subscribeSync, type SyncFlags } from "./chartSync";
import { LIGHT, brickInputs, loadSettings, saveSettings, type BrickKind, type ChartSettings } from "./chartSettings";
import ChartSettingsDialog, { type TradingFlags } from "./ChartSettingsDialog";
import { IndicatorPicker, IndicatorSettings, SaveTemplateDialog, templateScope, type IndicatorTemplate, type StrategyItem } from "./IndicatorDialogs";
import {
  AlertDialog,
  CommandPalette,
  DrawingSettingsDialog,
  GoToDialog,
  ShortcutsDialog,
  SymbolSearchDialog,
  type AlertTarget,
  type Command,
} from "./ChartDialogs";
import { DrawingFloatBar, FavoritesBar, LeftToolbar, type CursorMode, type Magnet } from "./DrawingToolbar";
import { AlertsPanel, DataWindow, ObjectTree, type DataRow, type GroupAction, type TreeDrop, type TreeItem } from "./ChartWidgets";
import { AboutIndicatorDialog, InsightsDialog, ManagePanesDialog, WhatsNewDialog } from "./ChartInfoDialogs";
import { CHART_SETTINGS_EVENT } from "./UserSettings";

const IST = IST_OFFSET;

interface Candle extends OhlcvCandle {
  time: UTCTimestamp;
  flag?: number;
}

export type ChartKind =
  | "bars" | "candles" | "hollow" | "volcandles" | "hlcbars" | "line" | "linemarkers" | "step" | "area"
  | "hlcarea" | "baseline" | "columns" | "highlow" | "heikin" | "renko" | "linebreak" | "kagi" | "pnf" | "range";

export const CHART_KINDS: { id: ChartKind; label: string; icon: string }[] = [
  { id: "bars", label: "Bars", icon: "┤" },
  { id: "candles", label: "Candles", icon: "▮" },
  { id: "hollow", label: "Hollow candles", icon: "▯" },
  { id: "volcandles", label: "Volume candles", icon: "▬" },
  { id: "hlcbars", label: "HLC bars", icon: "├" },
  { id: "line", label: "Line", icon: "∕" },
  { id: "linemarkers", label: "Line with markers", icon: "⋰" },
  { id: "step", label: "Step line", icon: "⌐" },
  { id: "area", label: "Area", icon: "◿" },
  { id: "hlcarea", label: "HLC area", icon: "◬" },
  { id: "baseline", label: "Baseline", icon: "≖" },
  { id: "columns", label: "Columns", icon: "▥" },
  { id: "highlow", label: "High-low", icon: "┃" },
  { id: "heikin", label: "Heikin Ashi", icon: "Ⓗ" },
  { id: "renko", label: "Renko", icon: "▞" },
  { id: "linebreak", label: "Line break", icon: "⊏" },
  { id: "kagi", label: "Kagi", icon: "⌇" },
  { id: "pnf", label: "Point & figure", icon: "✕" },
  { id: "range", label: "Range", icon: "⇳" },
];

const BRICK_KINDS = new Set<ChartKind>(["renko", "linebreak", "kagi", "pnf", "range"]);
const CUSTOM_MODE: Partial<Record<ChartKind, ShapeMode>> = {
  volcandles: "volcandles",
  hlcbars: "hlc",
  highlow: "highlow",
  hlcarea: "hlcarea",
  kagi: "kagi",
  pnf: "pnf",
};
const LINE_KINDS = new Set<ChartKind>(["line", "linemarkers", "step", "area", "baseline"]);

const INTERVAL_SECTIONS: { title: string; keys: string[] }[] = [
  { title: "Seconds", keys: ["5S", "10S", "15S", "30S", "45S"] },
  { title: "Minutes", keys: ["1", "2", "3", "5", "10", "15", "20", "30", "45", "75"] },
  { title: "Hours", keys: ["60", "120", "180", "240"] },
  { title: "Days", keys: ["D", "1W", "1M", "3M", "6M", "12M"] },
];
const DEFAULT_FAV_INTERVALS = ["1", "5", "15", "60", "240", "D"];
const DEFAULT_FAV_KINDS: ChartKind[] = ["candles", "volcandles"];

interface RangeDef {
  id: string;
  title: string;
  interval: string;
  sessions?: number;
  days?: number;
  ytd?: boolean;
  all?: boolean;
}

const RANGES: RangeDef[] = [
  { id: "1D", title: "1 day in 1 minute intervals", interval: "1", sessions: 1 },
  { id: "5D", title: "5 days in 5 minute intervals", interval: "5", sessions: 5 },
  { id: "1M", title: "1 month in 30 minute intervals", interval: "30", days: 30 },
  { id: "3M", title: "3 months in 1 hour intervals", interval: "60", days: 91 },
  { id: "6M", title: "6 months in 2 hour intervals", interval: "120", days: 182 },
  { id: "YTD", title: "Year to date in 1 day intervals", interval: "D", ytd: true },
  { id: "1Y", title: "1 year in 1 day intervals", interval: "D", days: 365 },
  { id: "5Y", title: "5 years in 1 week intervals", interval: "1W", days: 1826 },
  { id: "10Y", title: "10 years in 1 month intervals", interval: "1M", days: 3652 },
  { id: "All", title: "All data in 1 month intervals", interval: "1M", all: true },
];

/** Actions the chart asks its host (the Trade page) to perform. */
export type HostAction =
  | "panel:chain" | "panel:details" | "panel:tree" | "panel:data" | "panel:alerts" | "panel:watch" | "panel:flow"
  | "panel:depth" | "panel:tape" | "panel:futures" | "panel:strategy"
  | "bottom:positions" | "bottom:orders" | "bottom:basket" | "bottom:smart"
  | "scalper" | "layouts" | "save" | "maximize" | "watch:add" | "privacy" | "logout" | "usersettings"
  /** Quick trade with one-click OFF: open the order window on that side. */
  | "ticket:buy" | "ticket:sell";

interface ToolMenuItem {
  id: string;
  label: string;
  icon: string;
  host?: HostAction;
  badge?: string;
  /** Label when pinned to the top bar. */
  short?: string;
  /** A Fyers account page (funds, MTF, eDIS …) opened in a new tab. */
  href?: string;
}

const TOOLS_MENU: ToolMenuItem[] = [
  { id: "replay", label: "Bar Replay", icon: "⏪", short: "Replay" },
  { id: "flow", label: "Order Flow", icon: "Δ", host: "panel:flow", badge: "NEW", short: "Order Flow" },
  { id: "chain", label: "Option Chain", icon: "⊞", host: "panel:chain", short: "Option Chain" },
  { id: "scalper", label: "Option Scalper", icon: "⚡", host: "scalper", short: "Scalper" },
  { id: "positions", label: "Manage Positions & Orders", icon: "⇅", host: "bottom:positions", short: "Positions" },
  { id: "basket", label: "Basket Orders", icon: "🧺", host: "bottom:basket" },
  { id: "strategy", label: "Strategy Builder", icon: "⚖", host: "panel:strategy" },
  { id: "depth", label: "Market Depth (DOM)", icon: "≣", host: "panel:depth" },
  { id: "tape", label: "Time & Sales", icon: "⌚", host: "panel:tape" },
  { id: "futures", label: "Futures Chain", icon: "⧗", host: "panel:futures" },
  { id: "popout", label: "Popout Chart", icon: "⧉" },
  { id: "saved", label: "View Saved Charts", icon: "🗂", host: "layouts" },
  { id: "refresh", label: "Refresh Chart", icon: "↻" },
  { id: "theme", label: "Change Theme (dark / light)", icon: "◐" },
  { id: "privacy", label: "Privacy (mask P&L)", icon: "🙈", host: "privacy" },
  { id: "settings", label: "User Settings", icon: "⚙" },
  { id: "smartbook", label: "Smart Orderbook", icon: "📑", host: "bottom:smart" },
  { id: "shortcuts", label: "Keyboard Shortcuts", icon: "⌨" },
  // Broker account services live on Fyers' own site: these open it.
  { id: "funds", label: "Add Funds", icon: "₹", href: "https://trade.fyers.in/" },
  { id: "mtf", label: "Pay later (MTF)", icon: "⏳", href: "https://fyers.in/" },
  { id: "edis", label: "Holding Authorization (eDIS)", icon: "🔐", href: "https://trade.fyers.in/" },
  { id: "fia", label: "FIA", icon: "✦", href: "https://fyers.in/" },
  { id: "refer", label: "Refer & Earn", icon: "🎁", href: "https://fyers.in/" },
  { id: "prime", label: "FYERS Prime", icon: "★", href: "https://fyers.in/" },
];

type ScaleMode = "normal" | "log" | "percent" | "indexed";
type DrawMode = string | null; // a tool id, "alert", "ticket", "zoom", "image", or null
type MenuId = "interval" | "kind" | "templates" | "alerts" | "compare" | "tools" | "snapshot" | "scale" | "tz" | "products" | null;
type StratTrade = { side: string; entry_t: number; exit_t: number; entry: number; exit: number; net: number; reason: string; instrument: string };
type StratRun = { name: string; trades: StratTrade[]; stats: Record<string, number | null>; error?: string; running?: boolean };

/** An order placed from the chart's right-click menu. */
export interface ChartOrder {
  side: "BUY" | "SELL";
  /** STOP_LOSS = stop-limit: `price` is the trigger, `limit` the limit price. */
  type: "MARKET" | "LIMIT" | "SL-M" | "STOP_LOSS";
  price: number | null;
  limit?: number | null;
}

/** An open position on the charted symbol and its managed exits. */
export interface ChartPosition {
  qty: number; // signed: + long, - short
  avg: number;
  sl: number | null;
  tp: number | null;
}

/** A broker-state line drawn on the chart (position avg / pending order). */
export interface BrokerLine {
  price: number;
  title: string;
  kind: "position-long" | "position-short" | "order";
}

const COMPARE_COLORS = ["#42A5F5", "#AB47BC", "#26C6DA", "#FFCA28"];

// Hard ceiling on how many candles we keep — beyond this the scroll-back
// pagination stops asking for more.
const MAX_CANDLES = 20000;

const PREFS_KEY = "chart:prefs";

/** Where a compared symbol plots: on the main scale in %, on its own price
 *  scale (the opposite axis), or in a pane of its own. */
type CompareMode = "percent" | "scale" | "pane";
const COMPARE_MODES: { v: CompareMode; l: string }[] = [
  { v: "percent", l: "Same % scale" },
  { v: "scale", l: "New price scale" },
  { v: "pane", l: "New pane" },
];

interface CompareItem {
  symbol: string;
  name: string;
  color: string;
  hidden?: boolean;
  mode?: CompareMode;
}

interface ChartEvent {
  time: number; // chart time
  kind: "D" | "S" | "E" | "B";
  text: string;
}

interface ThemeColors {
  up: string;
  down: string;
  volUp: string;
  volDown: string;
  accent: string;
  text: string;
  grid: string;
  border: string;
  draw: string;
  crosshair: string;
  bg: string;
}

function escapeHtml(t: string): string {
  return t.replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch] ?? ch);
}

/** #RGB / #RRGGBB → rgba() at the given alpha; `fallback` otherwise. */
function withAlpha(hex: string, a: number, fallback: string): string {
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return fallback;
  let h = m[1];
  if (h.length === 3) h = h.split("").map((c) => c + c).join("");
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${a})`;
}

/** Read the active theme's palette so the chart matches the app skin. */
function readThemeColors(): ThemeColors {
  const g = (name: string, fb: string): string => {
    try {
      const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v || fb;
    } catch {
      return fb;
    }
  };
  const up = g("--green", "#26A69A");
  const down = g("--red", "#EF5350");
  const accent = g("--accent", "#FF8C00");
  return {
    up,
    down,
    volUp: withAlpha(up, 0.35, "rgba(38,166,154,0.35)"),
    volDown: withAlpha(down, 0.35, "rgba(239,83,80,0.35)"),
    accent,
    text: g("--text-dim", "#999999"),
    grid: g("--border-soft", "#1F1F1F"),
    border: g("--border", "#2A2A2A"),
    draw: g("--amber", "#FFD700"),
    crosshair: g("--text-faint", "#666666"),
    bg: g("--bg-panel", "#111111"),
  };
}

function fmtNum(v: number, digits = 2): string {
  return v.toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

/** Compact volume in Indian units (K / L / Cr). */
function fmtVol(v: number): string {
  const a = Math.abs(v);
  if (a >= 1e7) return (v / 1e7).toFixed(2) + "Cr";
  if (a >= 1e5) return (v / 1e5).toFixed(2) + "L";
  if (a >= 1e3) return (v / 1e3).toFixed(1) + "K";
  return String(Math.round(v));
}

function loadJson<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return fallback;
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

function saveJson(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* storage full / disabled — persistence is best-effort */
  }
}

/** Run an engine call that a stub / older engine may not support. */
function safe<T>(f: () => T, fallback?: T): T | undefined {
  try {
    return f();
  } catch {
    return fallback;
  }
}

/** Exchange (IST) midnight at or before epoch `ts`. */
function dayStart(ts: number): number {
  return Math.floor((ts + IST) / 86400) * 86400 - IST;
}

function classifyEvent(headline: string): ChartEvent["kind"] | null {
  const s = headline.toLowerCase();
  if (s.includes("dividend")) return "D";
  if (/\bsplit\b|sub-division|subdivision/.test(s)) return "S";
  if (s.includes("bonus")) return "B";
  if (/financial results?|quarterly results?|\bresults\b|earnings/.test(s)) return "E";
  return null;
}

interface ChartPrefs {
  interval?: string;
  resolution?: string;
  chartKind?: ChartKind;
  indicators?: unknown[];
  active?: Record<string, boolean>;
  volumeOn?: boolean;
  magnet?: Magnet | boolean;
  cursor?: CursorMode;
  scaleMode?: ScaleMode;
  autoScale?: boolean;
  invert?: boolean;
  scalePriceOnly?: boolean;
  favIntervals?: string[];
  customIntervals?: string[];
  favKinds?: ChartKind[];
  favTools?: string[];
  favIndicators?: string[];
  lastTool?: Partial<Record<ToolGroupId, string>>;
  stay?: boolean;
  lockAll?: boolean;
  hide?: { drawings: boolean; indicators: boolean; positions: boolean };
  syncDrawings?: boolean;
  legendCollapsed?: boolean;
  showFavBar?: boolean;
  toolsCollapsed?: boolean;
  pinnedTools?: string[];
  mainHidden?: boolean;
}

/** Fetch + normalise one page of candles (sorted, deduped, IST-shifted). */
async function fetchHistory(
  symbol: string,
  resolution: string,
  fromTs: number,
  toTs: number,
): Promise<{ candles: Candle[]; reason: string | null }> {
  try {
    const qs = new URLSearchParams({
      symbol,
      resolution,
      from: String(Math.max(0, Math.floor(fromTs))),
      to: String(Math.floor(toTs)),
    });
    // Futures / options: ask for open interest too (the OI indicators read it).
    if (isDerivative(symbol)) qs.set("oi", "1");
    const r = await api.get<HistoryResponse>(`/api/market/history?${qs.toString()}`);
    if (!r.ok) return { candles: [], reason: r.reason ?? "chart data unavailable" };
    const rows = Array.isArray(r.candles) ? r.candles : [];
    const out: Candle[] = [];
    for (const row of rows) {
      if (!Array.isArray(row) || row.length < 5) continue;
      const [ts, o, h, l, c, v, oi] = row as number[];
      const nums = [ts, o, h, l, c];
      if (!nums.every((x) => typeof x === "number" && Number.isFinite(x))) continue;
      out.push({
        time: (Math.floor(ts) + IST) as UTCTimestamp,
        open: o,
        high: h,
        low: l,
        close: c,
        volume: typeof v === "number" && Number.isFinite(v) ? v : 0,
        ...(typeof oi === "number" && Number.isFinite(oi) ? { oi } : {}),
      });
    }
    out.sort((a, b) => a.time - b.time);
    const dedup: Candle[] = [];
    for (const c of out) {
      if (dedup.length > 0 && dedup[dedup.length - 1].time === c.time) dedup[dedup.length - 1] = c;
      else dedup.push(c);
    }
    return { candles: dedup, reason: null };
  } catch (e) {
    return { candles: [], reason: (e as Error).message || "history request failed" };
  }
}

interface ChartStatus {
  kind: "loading" | "ready" | "empty" | "error";
  message?: string;
}

/** Live clock for the date-range bar (its own 1s timer). */
function Clock({ tz, hour12, onClick }: { tz: string; hour12: boolean; onClick: () => void }) {
  const [now, setNow] = useState(() => Math.floor(Date.now() / 1000) + IST);
  useEffect(() => {
    const id = setInterval(() => setNow(Math.floor(Date.now() / 1000) + IST), 1000);
    return () => clearInterval(id);
  }, []);
  return (
    <button type="button" className="crange-btn" onClick={onClick} title="Time zone" data-testid="chart-clock">
      {formatClock(wallClock(now, tz), hour12, true)} {zoneOffsetLabel(tz)}
    </button>
  );
}

/** "Demonstration" cursor: a fading laser trail over the chart. */
function LaserCanvas({ host }: { host: HTMLElement | null }) {
  const ref = useRef<HTMLCanvasElement | null>(null);
  useEffect(() => {
    const cv = ref.current;
    if (!cv || !host) return;
    const pts: { x: number; y: number; t: number }[] = [];
    const move = (e: MouseEvent) => {
      const r = host.getBoundingClientRect();
      pts.push({ x: e.clientX - r.left, y: e.clientY - r.top, t: performance.now() });
    };
    host.addEventListener("mousemove", move);
    let raf = 0;
    const frame = () => {
      raf = requestAnimationFrame(frame);
      const ctx = cv.getContext?.("2d");
      if (!ctx) return;
      if (cv.width !== host.clientWidth || cv.height !== host.clientHeight) {
        cv.width = host.clientWidth;
        cv.height = host.clientHeight;
      }
      ctx.clearRect(0, 0, cv.width, cv.height);
      const now = performance.now();
      while (pts.length && now - pts[0].t > 700) pts.shift();
      for (let i = 1; i < pts.length; i++) {
        const a = 1 - (now - pts[i].t) / 700;
        ctx.strokeStyle = `rgba(255,40,40,${a})`;
        ctx.lineWidth = 2 + 4 * a;
        ctx.lineCap = "round";
        ctx.beginPath();
        ctx.moveTo(pts[i - 1].x, pts[i - 1].y);
        ctx.lineTo(pts[i].x, pts[i].y);
        ctx.stroke();
      }
    };
    raf = requestAnimationFrame(frame);
    return () => {
      cancelAnimationFrame(raf);
      host.removeEventListener("mousemove", move);
    };
  }, [host]);
  return <canvas ref={ref} className="chart-laser" />;
}

let panelSeq = 0;

export interface ChartPanelProps {
  symbol: string;
  shortName: string;
  /** The instrument (exchange, description) when known. */
  instrument?: InstrumentHit | null;
  /** Open position on this symbol: drawn as a live-P&L line with
   *  draggable stop-loss / target (TradingView-style). */
  position?: ChartPosition | null;
  /** Save new exits (null clears one). Rejects on failure — the chart
   *  then puts the line back where it was. */
  onLevels?: (sl: number | null, tp: number | null) => Promise<void>;
  /** Close the position at market. */
  onClosePosition?: () => Promise<void>;
  /** Place an order from the chart; resolves to the message to show.
   *  Absent = the chart offers no orders. */
  onChartOrder?: (o: ChartOrder) => Promise<string>;
  /** Quantity those orders use (the ticket's). */
  orderQty?: number;
  /** Change the ticket quantity from the chart's quick-trade row. */
  onOrderQty?: (n: number) => void;
  /** Shared top-toolbar / drawing-strip / date-range containers (multi-chart
   *  layouts): the active chart renders its controls there instead. */
  toolbarSlot?: HTMLElement | null;
  toolsSlot?: HTMLElement | null;
  rangeSlot?: HTMLElement | null;
  /** Dock panels the active chart renders its widgets into. */
  widgetSlots?: { data?: HTMLElement | null; tree?: HTMLElement | null; alerts?: HTMLElement | null };
  /** false = no toolbar / drawing strip at all (an inactive layout cell). */
  chrome?: boolean;
  /** Trading setting: place chart orders without the confirm step. */
  instant?: boolean;
  /** Trading setting: the "+" on the price scale. */
  showPlus?: boolean;
  /** Trading settings shown in the chart settings' Trading tab. */
  trading?: TradingFlags;
  onTrading?: (k: keyof TradingFlags, v: boolean) => void;
  /** Position-average / pending-order levels to mark on the chart. */
  brokerLines?: BrokerLine[];
  /** When set, the "→ Ticket" tool sends a clicked price to the caller. */
  onPickPrice?: (price: number) => void;
  /** Symbol search on the chart switches the host's symbol. */
  onSymbolChange?: (h: InstrumentHit) => void;
  recentSymbols?: InstrumentHit[];
  onAction?: (a: HostAction) => void;
  /** Layout cell id + which things follow the other charts. */
  syncId?: string;
  sync?: SyncFlags;
  maximized?: boolean;
  multi?: boolean;
  privacy?: boolean;
}

type Dialog =
  | { k: "symbol"; q: string; compare?: boolean; select?: boolean }
  | { k: "indicators" }
  | { k: "indSettings"; uid: string }
  | { k: "settings"; tab?: string }
  | { k: "drawSettings"; id: string }
  | { k: "alert"; alert: AlertItem }
  | { k: "goto" }
  | { k: "palette" }
  | { k: "shortcuts" }
  | { k: "interval"; txt: string }
  | { k: "about"; uid: string }
  | { k: "panes" }
  | { k: "insights" }
  | { k: "whatsnew" }
  | { k: "saveTemplate" }
  | null;

interface Ctx {
  x: number;
  y: number;
  price: number;
  time: number | null;
  area: "pane" | "price" | "time" | "drawing";
  drawingId?: string;
  confirm?: ChartOrder;
}

interface Snap {
  label: string;
  drawings: Drawing[];
  indicators: IndicatorInstance[];
  kind: ChartKind;
  iv: string;
  /** Set on "change symbol" steps: undo switches the chart back. */
  symbol?: string;
  hit?: InstrumentHit | null;
  /** Set on "change settings" steps. */
  settings?: ChartSettings;
  /** Set on "reset scales" steps: the view to return to. */
  view?: { auto: boolean; mode: ScaleMode; invert: boolean; range: { from: number; to: number } | null; price: { from: number; to: number } | null };
}

/** Undo history per chart cell, kept across the remount a symbol change
 *  causes — so "Undo change symbol" can bring the previous symbol back. */
interface UndoStore {
  past: Snap[];
  future: Snap[];
  symbol: string | null;
  hit: InstrumentHit | null;
  /** An undo / redo is switching the symbol: don't record it as a new change. */
  restoring: boolean;
}
const UNDO_STORES = new Map<string, UndoStore>();
function undoStore(key: string): UndoStore {
  let st = UNDO_STORES.get(key);
  if (!st) {
    st = { past: [], future: [], symbol: null, hit: null, restoring: false };
    UNDO_STORES.set(key, st);
  }
  return st;
}

/** A cell's history is capped: drop its oldest step and that step's journal entry. */
function dropOldest(st: UndoStore): void {
  st.past.shift();
  for (const [key, v] of UNDO_STORES) {
    if (v !== st) continue;
    const k = UNDO_JOURNAL.past.indexOf(key);
    if (k >= 0) UNDO_JOURNAL.past.splice(k, 1);
  }
}

/** One history for the whole layout, like TradingView's: the cells keep
 *  their own snapshots, the journal keeps the order steps happened in
 *  across cells. Undo / redo in any chart replays the latest step,
 *  whichever chart it was in. */
export const UNDO_JOURNAL: { past: string[]; future: string[] } = { past: [], future: [] };
const UNDO_HANDLERS = new Map<string, { undo: () => boolean; redo: () => boolean; label: (which: "past" | "future") => string }>();
const journalListeners = new Set<() => void>();
function journalChanged(): void {
  journalListeners.forEach((f) => f());
}

/** A new step in `cell`: it goes on the journal, and every redo is gone. */
export function journalPush(cell: string): void {
  UNDO_JOURNAL.past.push(cell);
  if (UNDO_JOURNAL.past.length > 300) UNDO_JOURNAL.past.shift();
  UNDO_JOURNAL.future = [];
  for (const st of UNDO_STORES.values()) st.future = [];
  journalChanged();
}

/** Undo / redo the layout's latest step; false when there's none. Steps
 *  of a chart that's gone (a smaller layout) are skipped. */
export function journalStep(which: "undo" | "redo"): boolean {
  const from = which === "undo" ? UNDO_JOURNAL.past : UNDO_JOURNAL.future;
  const to = which === "undo" ? UNDO_JOURNAL.future : UNDO_JOURNAL.past;
  while (from.length) {
    const cell = from.pop()!;
    if (UNDO_HANDLERS.get(cell)?.[which]()) {
      to.push(cell);
      journalChanged();
      return true;
    }
  }
  journalChanged();
  return false;
}

function computeTheme(s: ChartSettings): ThemeColors {
  const b = readThemeColors();
  const up = s.upColor || b.up;
  const down = s.downColor || b.down;
  const light = s.theme === "light";
  return {
    ...b,
    up,
    down,
    volUp: withAlpha(up, 0.35, b.volUp),
    volDown: withAlpha(down, 0.35, b.volDown),
    ...(light ? { text: LIGHT.dim, grid: LIGHT.grid, border: LIGHT.border, crosshair: LIGHT.faint, bg: LIGHT.bg, draw: "#2962FF" } : {}),
  };
}

export default function ChartPanel(props: ChartPanelProps) {
  const {
    symbol,
    shortName,
    instrument,
    brokerLines,
    onPickPrice,
    position,
    onLevels,
    onClosePosition,
    onChartOrder,
    orderQty,
    onOrderQty,
    toolbarSlot,
    toolsSlot,
    rangeSlot,
    widgetSlots,
    chrome = true,
    instant = false,
    showPlus = true,
    trading,
    onTrading,
    onSymbolChange,
    recentSymbols,
    onAction,
    syncId,
    sync = NO_SYNC,
    maximized = false,
    multi = false,
    privacy = false,
  } = props;
  const [autoId] = useState(() => `chart${++panelSeq}`);
  const myId = syncId ?? autoId;
  const [prefs] = useState(() => loadJson<ChartPrefs>(PREFS_KEY, {}));

  // ---- state: preferences (persisted) ----
  const [iv, setIvState] = useState<string>(() => normalizeInterval(prefs.interval ?? prefs.resolution ?? "5") ?? "5");
  const [chartKind, setChartKindState] = useState<ChartKind>(() =>
    CHART_KINDS.some((k) => k.id === prefs.chartKind) ? prefs.chartKind! : "candles",
  );
  const [indicators, setIndicatorsState] = useState<IndicatorInstance[]>(() =>
    Array.isArray(prefs.indicators)
      ? (prefs.indicators.map(sanitizeInstance).filter(Boolean) as IndicatorInstance[])
      : migrateActive(prefs.active, prefs.volumeOn),
  );
  const [settings, setSettingsState] = useState<ChartSettings>(loadSettings);
  const [magnet, setMagnet] = useState<Magnet>(() =>
    prefs.magnet === true ? "weak" : prefs.magnet === "weak" || prefs.magnet === "strong" ? prefs.magnet : "off",
  );
  const [cursor, setCursor] = useState<CursorMode>(prefs.cursor ?? "cross");
  const [scaleMode, setScaleMode] = useState<ScaleMode>(prefs.scaleMode ?? "normal");
  const scaleModeRef = useRef(scaleMode);
  scaleModeRef.current = scaleMode;
  const [autoScale, setAutoScale] = useState(prefs.autoScale ?? true);
  const autoScaleRef = useRef(autoScale);
  autoScaleRef.current = autoScale;
  const [invert, setInvert] = useState(prefs.invert ?? false);
  const invertRef = useRef(invert);
  invertRef.current = invert;
  const [scalePriceOnly, setScalePriceOnly] = useState(prefs.scalePriceOnly ?? false);
  const [favIntervals, setFavIntervals] = useState<string[]>(prefs.favIntervals ?? DEFAULT_FAV_INTERVALS);
  const [customIntervals, setCustomIntervals] = useState<string[]>(prefs.customIntervals ?? []);
  const [favKinds, setFavKinds] = useState<ChartKind[]>(prefs.favKinds ?? DEFAULT_FAV_KINDS);
  const [favTools, setFavTools] = useState<string[]>(prefs.favTools ?? []);
  const [favIndicators, setFavIndicators] = useState<string[]>(prefs.favIndicators ?? ["volume", "ema", "sma", "vwap", "bb", "supertrend", "rsi", "macd"]);
  const [lastTool, setLastTool] = useState<Partial<Record<ToolGroupId, string>>>(prefs.lastTool ?? {});
  const [stay, setStay] = useState(prefs.stay ?? false);
  const [lockAll, setLockAll] = useState(prefs.lockAll ?? false);
  const [hide, setHide] = useState(prefs.hide ?? { drawings: false, indicators: false, positions: false });
  const [syncDrawings, setSyncDrawings] = useState(prefs.syncDrawings ?? true);
  const [legendCollapsed, setLegendCollapsed] = useState(prefs.legendCollapsed ?? false);
  const [mainHidden, setMainHidden] = useState(prefs.mainHidden === true);
  /** The legend's More (⋯) menu: for the main series or one indicator. */
  const [legendMenu, setLegendMenu] = useState<{ kind: "main" | "ind"; uid?: string; x: number; y: number } | null>(null);
  const [showFavBar, setShowFavBar] = useState(prefs.showFavBar ?? false);
  const [toolsCollapsed, setToolsCollapsed] = useState(prefs.toolsCollapsed ?? false);
  // TradingView / Fyers pin these to the top bar out of the box
  const [pinnedTools, setPinnedTools] = useState<string[]>(prefs.pinnedTools ?? ["flow", "scalper", "positions", "chain", "replay"]);

  // ---- state: transient ----
  const [drawMode, setDrawModeState] = useState<DrawMode>(null);
  const [pendingCount, setPendingCount] = useState(0);
  const [selectedDrawing, setSelectedDrawing] = useState<string | null>(null);
  const [drawVer, setDrawVer] = useState(0);
  const [alerts, setAlerts] = useState<AlertItem[]>(() =>
    (loadJson<unknown[]>(`chart:alerts:${symbol}`, []) ?? []).map(migrateAlert).filter(Boolean) as AlertItem[],
  );
  const [alertLog, setAlertLog] = useState<{ id: number; ts: number; text: string }[]>(() => loadJson(`chart:alertLog:${symbol}`, []));
  const [compares, setCompares] = useState<CompareItem[]>([]);
  /** A compare on the main scale forces it into percent. */
  const pctCompare = compares.some((c) => (c.mode ?? "percent") === "percent");
  const comparesLenRef = useRef(0);
  comparesLenRef.current = pctCompare ? compares.length : 0;
  /** The opposite axis, shown while a compare sits on its own price scale. */
  const cmpOtherSide = compares.some((c) => c.mode === "scale") ? (settings.scaleSide === "right" ? "left" : "right") : null;
  const [compareQuery, setCompareQuery] = useState("");
  /** Interval-menu sections the user folded away (persisted). */
  const [ivCollapsed, setIvCollapsed] = useState<string[]>(() => loadJson<string[]>("chart:ivCollapsed", []) ?? []);
  useEffect(() => saveJson("chart:ivCollapsed", ivCollapsed), [ivCollapsed]);
  const [compareHits, setCompareHits] = useState<{ symbol: string; name: string }[]>([]);
  const [atLive, setAtLive] = useState(true);
  const [toasts, setToasts] = useState<{ id: number; text: string }[]>([]);
  const [toastsOpen, setToastsOpen] = useState(false);
  const [textDraft, setTextDraft] = useState<{ id: string; x: number; y: number; value: string } | null>(null);
  const [fullscreen, setFullscreen] = useState(false);
  const [menuOpen, setMenuOpen] = useState<MenuId>(null);
  const [status, setStatus] = useState<ChartStatus>({ kind: "loading" });
  const [reloadNonce, setReloadNonce] = useState(0);
  const [flowNote, setFlowNote] = useState("");
  const [strategies, setStrategies] = useState<StrategyItem[] | null>(null);
  const [strat, setStrat] = useState<StratRun | null>(null);
  const [dialog, setDialog] = useState<Dialog>(null);
  const [ctx, setCtx] = useState<Ctx | null>(null);
  /** The context menu's Trade ▸ flyout (rendered beside the menu, which scrolls). */
  const [tradeFly, setTradeFly] = useState<{ x: number; y: number } | null>(null);
  const [levelDrag, setLevelDrag] = useState<"sl" | "tp" | null>(null);
  const [replay, setReplayState] = useState({ on: false, selecting: false, playing: false, speed: 1, idx: 0 });
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const [paneRects, setPaneRects] = useState<{ i: number; top: number; height: number }[]>([]);
  /** Width of a visible left price axis — the legends start right of it. */
  const [leftAxisW, setLeftAxisW] = useState(0);
  const [paneMode, setPaneMode] = useState<{ max: number | null; collapsed: number[] }>({ max: null, collapsed: [] });
  const [events, setEvents] = useState<ChartEvent[]>([]);
  const [eventTip, setEventTip] = useState<{ x: number; text: string } | null>(null);
  const [lockedTime, setLockedTime] = useState<number | null>(null);
  const [templates, setTemplates] = useState<IndicatorTemplate[]>(() => loadJson("chart:indTemplates", []));
  const [customIv, setCustomIv] = useState<{ n: number; unit: string }>({ n: 2, unit: "m" });
  const [mainVer, setMainVer] = useState(0);
  const [dataVer, setDataVer] = useState(0);
  const [undoVer, setUndoVer] = useState(0);
  const [qty, setQty] = useState(orderQty ?? 1);
  useEffect(() => {
    if (orderQty != null) setQty(orderQty);
  }, [orderQty]);
  useEffect(() => {
    const on = () => setFullscreen(document.fullscreenElement != null);
    document.addEventListener("fullscreenchange", on);
    return () => document.removeEventListener("fullscreenchange", on);
  }, []);

  // the app skin changes the CSS variables the chart colours come from
  const [skin, setSkin] = useState(getTheme);
  useEffect(() => {
    const on = () => setSkin(getTheme());
    window.addEventListener(THEME_EVENT, on);
    return () => window.removeEventListener(THEME_EVENT, on);
  }, []);
  const theme = useMemo(() => computeTheme(settings), [settings.theme, settings.upColor, settings.downColor, skin]); // eslint-disable-line react-hooks/exhaustive-deps
  const precision = settings.precision ?? (instrument?.tick_size && instrument.tick_size < 0.01 ? 4 : 2);
  const tickRef = useRef(0.05);
  const liveQuoteRef = useRef<ReturnType<typeof useLiveQuote>>(undefined);
  const brokerLinesPropRef = useRef(brokerLines);
  brokerLinesPropRef.current = brokerLines;
  const positionRef = useRef(position);
  positionRef.current = position;
  tickRef.current = instrument?.tick_size && instrument.tick_size > 0 ? instrument.tick_size : 0.05;
  const fmtPrice = (v: number) => fmtNum(v, precision);
  const intraday = isIntraday(iv);
  const ivLabel = intervalLabel(iv);
  const exchange = instrument?.exchange ?? (symbol.includes(":") ? symbol.split(":")[0] : "");
  const description = instrument?.display ? instrument.display.split("·")[0].trim() : shortName;

  // ---- refs (chart internals live outside React) ----
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [hostEl, setHostEl] = useState<HTMLDivElement | null>(null);
  const legendRef = useRef<HTMLSpanElement | null>(null);
  const countdownRef = useRef<HTMLDivElement | null>(null);
  /** The top toolbar scrolls sideways when narrow; arrows show what's hidden. */
  const toolbarRef = useRef<HTMLDivElement | null>(null);
  const [tbScroll, setTbScroll] = useState({ left: false, right: false });
  const rawLabelRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const mainRef = useRef<ISeriesApi<SeriesType> | null>(null);
  const indEntriesRef = useRef<{ uid: string; plot: number; series: ISeriesApi<SeriesType> }[]>([]);
  const indValuesRef = useRef<Map<string, (number | null)[][]>>(new Map());
  const indPaneRef = useRef<Map<string, number>>(new Map());
  const indLegendRefs = useRef<Map<string, HTMLSpanElement>>(new Map());
  const indHasDataRef = useRef<Map<string, boolean[]>>(new Map());
  /** Shaded fills per indicator (bar-indexed, shifted plots included). */
  const indFillDataRef = useRef<Map<string, FillBand[]>>(new Map());
  const indFillPrimsRef = useRef<FillPrimitive[]>([]);
  /** Marker plugins on indicator series (patterns, fractals, divergences). */
  const indMarkerPluginsRef = useRef<Map<ISeriesApi<SeriesType>, { setMarkers: (m: unknown[]) => void }>>(new Map());
  /** Markers per indicator by bar index (legend / data window text). */
  const indMarksRef = useRef<Map<string, Map<number, Mark[]>>>(new Map());
  /** Other symbols' closes by chart time (two-symbol indicators). */
  const otherDataRef = useRef<Map<string, { from: number; closes: Map<number, number> }>>(new Map());
  const otherLoadingRef = useRef<Set<string>>(new Set());
  /** Option-chain open interest for the OI profile. */
  const oiChainRef = useRef<{ symbol: string; at: number; rows: OiStrike[] } | null>(null);
  const compareSeriesRef = useRef<Map<string, ISeriesApi<"Line">>>(new Map());
  const comparesRef = useRef<CompareItem[]>([]);
  comparesRef.current = compares;
  /** First pane index after the indicator panes (compares in "New pane" go here). */
  const indPaneCountRef = useRef(1);
  const alertLinesRef = useRef<Map<string, IPriceLine>>(new Map());
  const brokerLinesRef = useRef<IPriceLine[]>([]);
  const extraLinesRef = useRef<Map<string, IPriceLine>>(new Map());
  const posLinesRef = useRef<{ entry?: IPriceLine; sl?: IPriceLine; tp?: IPriceLine }>({});
  const posTagRefs = useRef<{ entry: HTMLDivElement | null; sl: HTMLDivElement | null; tp: HTMLDivElement | null }>({ entry: null, sl: null, tp: null });
  const levelDragRef = useRef<{ which: "sl" | "tp"; price: number } | null>(null);
  const plusRef = useRef<HTMLButtonElement | null>(null);
  const plusPriceRef = useRef<number | null>(null);
  const overPlusRef = useRef(false);
  const plusHideRef = useRef<number | undefined>(undefined);
  const prevHaRef = useRef<{ open: number; close: number } | null>(null);
  const atLiveRef = useRef(true);
  const colorsRef = useRef<ThemeColors>(theme);
  const candlesRef = useRef<Candle[]>([]);
  const viewRef = useRef<Candle[]>([]);
  const indexByTimeRef = useRef<Map<number, number>>(new Map());
  const fetchSeqRef = useRef(0);
  const loadingOlderRef = useRef<Promise<void> | null>(null);
  const haveMoreRef = useRef(true);
  const drawModeRef = useRef<DrawMode>(null);
  const ivRef = useRef(iv);
  const kindRef = useRef(chartKind);
  const settingsRef = useRef(settings);
  const indicatorsRef = useRef(indicators);
  const statusRef = useRef<ChartStatus["kind"]>("loading");
  const hideRef = useRef(hide);
  const lockAllRef = useRef(lockAll);
  const stayRef = useRef(stay);
  const magnetRef = useRef(magnet);
  const cursorRef = useRef(cursor);
  const chromeRef = useRef(chrome);
  const syncRef = useRef(sync);
  const syncDrawingsRef = useRef(syncDrawings);
  const dialogRef = useRef<Dialog>(null);
  const replayRef = useRef(replay);
  const autoRetriedKeyRef = useRef("");
  const prevLtpRef = useRef<number | null>(null);
  const prevVolRef = useRef<number | null>(null);
  const toastSeqRef = useRef(0);
  const flowRef = useRef<Map<number, [number, number, number]>>(new Map());
  const markersRef = useRef<{ detach: () => void; setMarkers: (m: never[]) => void } | null>(null);
  const eventsByTimeRef = useRef<Map<number, ChartEvent[]>>(new Map());
  const pendingRangeRef = useRef<RangeDef | { at?: number; from?: number; to?: number } | null>(null);
  const syncApplyRef = useRef(0);
  const hoveredRef = useRef(false);
  const pointerRef = useRef<{ x: number; y: number } | null>(null);
  const indRefreshTimer = useRef<number | undefined>(undefined);
  const brickTimer = useRef<number | undefined>(undefined);
  const toolDefaultsRef = useRef<Record<string, Partial<DrawingStyle>>>(loadJson("chart:drawStyles", {}));
  const undoRef = useRef<UndoStore>(undoStore(myId));
  // A new symbol in this cell (search, watchlist, undo …) is a history step.
  useEffect(() => {
    const st = undoRef.current;
    if (st.symbol && st.symbol !== symbol) {
      if (st.restoring) st.restoring = false;
      else {
        st.past.push({ label: "change symbol", drawings: [], indicators: [], kind: kindRef.current, iv: ivRef.current, symbol: st.symbol, hit: st.hit });
        if (st.past.length > 100) dropOldest(st);
        journalPush(myId);
      }
      setUndoVer((v) => v + 1);
    }
    st.symbol = symbol;
    st.hit = instrument ?? st.hit;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol]);
  const clipboardRef = drawingClipboard as { current: Drawing | null }; // shared across charts
  const pendingIconRef = useRef<DrawingData | null>(null);

  // drawing store
  const drawingsRef = useRef<Drawing[]>(loadJson<Drawing[]>(`chart:drawings:${symbol}`, []));
  const pendingRef = useRef<Pending | null>(null);
  const selectedIdRef = useRef<string | null>(null);
  const hoverIdRef = useRef<string | null>(null);
  const primitiveRef = useRef<DrawingsPrimitive | null>(null);
  // In-flight drag of an existing drawing: `mode` is an anchor index
  // (resize) or "move". `start` is the pointer's time/price (and pixels)
  // at drag start; `orig` the drawing as it was.
  const dragRef = useRef<{
    id: string;
    mode: number | "move";
    start: DrawingPoint;
    startPx: { x: number; y: number };
    orig: Drawing;
    snap: Snap;
    moved: boolean;
  } | null>(null);
  const freehandRef = useRef<{ type: string; points: DrawingPoint[]; last: { x: number; y: number } } | null>(null);
  const hoverCursorElRef = useRef<HTMLElement | null>(null);

  ivRef.current = iv;
  kindRef.current = chartKind;
  settingsRef.current = settings;
  indicatorsRef.current = indicators;
  statusRef.current = status.kind;
  hideRef.current = hide;
  lockAllRef.current = lockAll;
  stayRef.current = stay;
  magnetRef.current = magnet;
  cursorRef.current = cursor;
  chromeRef.current = chrome;
  syncRef.current = sync;
  syncDrawingsRef.current = syncDrawings;
  dialogRef.current = dialog;
  replayRef.current = replay;
  colorsRef.current = theme;

  const plan = fetchPlan(iv);
  const side = settings.scaleSide;

  // ------------------------------------------------------------------
  // Coordinate helpers (refs only — safe to capture at first render)
  // ------------------------------------------------------------------

  function barInterval(): number {
    return intervalSeconds(ivRef.current);
  }

  /** Chart-time → fractional bar index of the displayed series. */
  function timeToLogical(t: number): number | null {
    const c = viewRef.current;
    const n = c.length;
    if (n === 0) return null;
    if (t <= c[0].time) return (t - c[0].time) / barInterval();
    if (t >= c[n - 1].time) return n - 1 + (t - c[n - 1].time) / barInterval();
    let lo = 0;
    let hi = n - 1;
    while (lo + 1 < hi) {
      const mid = (lo + hi) >> 1;
      if (c[mid].time <= t) lo = mid;
      else hi = mid;
    }
    return lo + (t - c[lo].time) / (c[hi].time - c[lo].time);
  }

  /** Chart-time → x pixel. Interpolates fractional logical positions so
   *  anchors placed on one timeframe land correctly on another. */
  function timeToX(t: number): number | null {
    const chart = chartRef.current;
    const l = timeToLogical(t);
    if (!chart || l === null) return null;
    return safe(() => chart.timeScale().logicalToCoordinate(l as Logical) as number | null, null) ?? null;
  }

  /** x pixel → chart-time (fractional between bars, extrapolated at edges). */
  function xToTime(x: number): number | null {
    const chart = chartRef.current;
    const c = viewRef.current;
    if (!chart || c.length === 0) return null;
    const l = safe(() => chart.timeScale().coordinateToLogical(x) as number | null, null) ?? null;
    if (l === null) return null;
    const n = c.length;
    const i = Math.floor(l);
    if (i < 0) return c[0].time + l * barInterval();
    if (i >= n - 1) return c[n - 1].time + (l - (n - 1)) * barInterval();
    return c[i].time + (l - i) * (c[i + 1].time - c[i].time);
  }

  function priceToY(p: number): number | null {
    return safe(() => mainRef.current?.priceToCoordinate(p) ?? null, null) ?? null;
  }

  function yToPrice(y: number): number | null {
    const v = safe(() => mainRef.current?.coordinateToPrice(y) ?? null, null);
    return v == null ? null : (v as number);
  }

  function barSpacing(): number {
    const ts = safe(() => chartRef.current!.timeScale());
    const a = safe(() => ts?.logicalToCoordinate(0 as Logical) as number | null, null);
    const b = safe(() => ts?.logicalToCoordinate(1 as Logical) as number | null, null);
    return a != null && b != null && b > a ? b - a : 8;
  }

  function paneDims(): { width: number; height: number } {
    const s = safe(() => chartRef.current?.paneSize(0));
    if (s && s.width > 0) return { width: s.width, height: s.height };
    const el = containerRef.current;
    return { width: el?.clientWidth ?? 0, height: el?.clientHeight ?? 0 };
  }

  function scaleWidth(which: "left" | "right"): number {
    return safe(() => chartRef.current!.priceScale(which).width(), 0) || 0;
  }

  /** x offset of the price pane inside the chart host (left scale). */
  function paneLeft(): number {
    return settingsRef.current.scaleSide === "left" ? scaleWidth("left") : 0;
  }

  function lastClose(): number | null {
    const c = viewRef.current;
    return c.length ? c[c.length - 1].close : null;
  }

  function drawingDeps(): DrawingDeps {
    return {
      drawings: () => drawingsRef.current,
      pending: () => pendingRef.current,
      selectedId: () => selectedIdRef.current,
      hoverId: () => hoverIdRef.current,
      timeToX,
      xToTime,
      priceToY,
      yToPrice,
      timeToLogical,
      barSpacing,
      candles: () => viewRef.current,
      intervalGroup: () => intervalGroup(ivRef.current),
      intervalCount: () => intervalCount(ivRef.current),
      tickSize: () => tickRef.current,
      lineColor: () => colorsRef.current.draw,
      accent: () => colorsRef.current.accent,
      upColor: () => colorsRef.current.up,
      downColor: () => colorsRef.current.down,
      bgColor: () => colorsRef.current.bg,
      priceFormatter: (p) => fmtNum(p, settingsRef.current.precision ?? 2),
      lastPrice: () => prevLtpRef.current ?? lastClose(),
      toolDefaults: (t) => toolDefaultsRef.current[t],
      hidden: () => hideRef.current.drawings,
      repaint: () => repaintDrawings(),
      extras: paintExtras,
    };
  }

  /** Session breaks, the locked vertical cursor and the replay cursor. */
  function paintExtras(ctx: CanvasRenderingContext2D, w: number, h: number): void {
    const s = settingsRef.current;
    const c = viewRef.current;
    ctx.save();
    if (s.sessionBreaks && isIntraday(ivRef.current) && c.length > 1) {
      const r = safe(() => chartRef.current!.timeScale().getVisibleLogicalRange());
      const from = Math.max(1, Math.floor(r?.from ?? 1));
      const to = Math.min(c.length - 1, Math.ceil(r?.to ?? c.length - 1));
      ctx.strokeStyle = withAlpha(colorsRef.current.accent, 0.35, "rgba(255,140,0,0.35)");
      ctx.setLineDash([4, 4]);
      const bs = barSpacing();
      for (let i = from; i <= to; i++) {
        if (Math.floor(c[i].time / 86400) === Math.floor(c[i - 1].time / 86400)) continue;
        const x = timeToX(c[i].time);
        if (x === null) continue;
        ctx.beginPath();
        ctx.moveTo(x - bs / 2, 0);
        ctx.lineTo(x - bs / 2, h);
        ctx.stroke();
      }
    }
    const lt = lockedTimeRef.current;
    if (lt !== null) {
      const x = timeToX(lt);
      if (x !== null) {
        ctx.strokeStyle = colorsRef.current.accent;
        ctx.setLineDash([2, 3]);
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, h);
        ctx.stroke();
      }
    }
    // Short position / order lines when "extended price line" is off.
    if (!s.extendLines && !hideRef.current.positions) {
      const left = s.ordersAlign === "left";
      const stub = (price: number, color: string, dashed: boolean) => {
        const y = priceToY(price);
        if (y == null) return;
        ctx.strokeStyle = color;
        ctx.lineWidth = 1;
        ctx.setLineDash(dashed ? [4, 3] : []);
        ctx.beginPath();
        ctx.moveTo(left ? 0 : w - 90, y);
        ctx.lineTo(left ? 90 : w, y);
        ctx.stroke();
      };
      for (const b of brokerLinesPropRef.current ?? []) stub(b.price, b.kind === "order" ? "#4A90D9" : b.kind === "position-long" ? colorsRef.current.up : colorsRef.current.down, b.kind === "order");
      const pos = positionRef.current;
      if (pos) {
        stub(pos.avg, pos.qty > 0 ? "#2962FF" : colorsRef.current.down, false);
        if (pos.sl != null) stub(pos.sl, colorsRef.current.down, true);
        if (pos.tp != null) stub(pos.tp, colorsRef.current.up, true);
      }
      ctx.setLineDash([]);
    }
    // Chart-drawn indicators: visible-range volume profile, option-chain OI profile.
    if (!hideRef.current.indicators && c.length > 1) {
      for (const inst of indicatorsRef.current) {
        if (!inst.visible || !visibleOnInterval(inst, ivRef.current)) continue;
        if (inst.type === "vpvr") {
          const r = safe(() => chartRef.current!.timeScale().getVisibleLogicalRange());
          const from = Math.max(0, Math.floor(r?.from ?? 0));
          const to = Math.min(c.length - 1, Math.ceil(r?.to ?? c.length - 1));
          const prof = to > from ? volumeProfileRows(c.slice(from, to + 1), Number(inst.inputs.rows) || 24, (Number(inst.inputs.va) || 70) / 100) : null;
          if (prof) {
            paintProfile(ctx, prof, {
              w,
              widthPct: Number(inst.inputs.width) || 30,
              side: inst.inputs.side === "Left" ? "Left" : "Right",
              priceToY,
              up: inst.plots[0]?.color ?? "#26A69A",
              down: inst.plots[1]?.color ?? "#EF5350",
              poc: "#F23645",
            });
          }
        }
        if (inst.type === "oiprofile") {
          const strikes = Number(inst.inputs.strikes) || 10;
          ensureOiChain(strikes);
          const rows = oiChainRef.current?.symbol === symbol ? oiChainRef.current.rows : [];
          if (rows.length) paintOiProfile(ctx, rows, { w, widthPct: Number(inst.inputs.width) || 25, priceToY, text: colorsRef.current.text });
        }
      }
    }
    const p = pointerRef.current;
    if (replayRef.current.selecting && p) {
      ctx.strokeStyle = "#2962FF";
      ctx.setLineDash([]);
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(p.x, 0);
      ctx.lineTo(p.x, h);
      ctx.stroke();
      ctx.fillStyle = "rgba(41,98,255,0.08)";
      ctx.fillRect(p.x, 0, w - p.x, h);
    }
    ctx.restore();
  }
  const lockedTimeRef = useRef<number | null>(null);
  lockedTimeRef.current = lockedTime;

  // ------------------------------------------------------------------
  // Drawing store, undo / redo
  // ------------------------------------------------------------------

  function persistDrawings(): void {
    saveJson(`chart:drawings:${symbol}`, drawingsRef.current.filter((d) => !TOOL_BY_ID.get(d.type)?.temp));
    if (syncDrawingsRef.current) publishSync({ type: "drawings", src: myId, symbol });
  }

  function repaintDrawings(): void {
    primitiveRef.current?.requestUpdate();
  }

  function bumpDrawings(): void {
    setDrawVer((v) => v + 1);
  }

  function snapshot(label: string): Snap {
    return { label, drawings: drawingsRef.current, indicators: indicatorsRef.current, kind: kindRef.current, iv: ivRef.current };
  }

  function pushUndo(label: string, snap?: Snap): void {
    const u = undoRef.current;
    u.past.push(snap ?? snapshot(label));
    if (u.past.length > 100) dropOldest(u);
    journalPush(myId);
    setUndoVer((v) => v + 1);
  }

  function restoreSnap(s: Snap): void {
    drawingsRef.current = s.drawings;
    selectedIdRef.current = null;
    setSelectedDrawing(null);
    persistDrawings();
    indicatorsRef.current = s.indicators;
    setIndicatorsState(s.indicators);
    setChartKindState(s.kind);
    if (s.iv !== ivRef.current) setIvState(s.iv);
    repaintDrawings();
    bumpDrawings();
  }

  /** The state to put on the other stack when `s` is undone / redone. */
  function counterSnap(s: Snap): Snap {
    if (s.symbol !== undefined) return { ...snapshot(s.label), symbol, hit: instrument ?? null };
    if (s.settings) return { ...snapshot(s.label), settings: settingsRef.current };
    if (s.view) return { ...snapshot(s.label), view: viewState() };
    return snapshot(s.label);
  }

  function applySnap(s: Snap): void {
    if (s.symbol !== undefined) {
      // back to the other symbol: the chart remounts and loads its own drawings
      if (s.hit && onSymbolChange && s.symbol !== symbol) {
        undoRef.current.restoring = true;
        onSymbolChange(s.hit);
      }
      return;
    }
    if (s.settings) {
      changeSettings(s.settings);
      return;
    }
    if (s.view) {
      restoreView(s.view);
      return;
    }
    restoreSnap(s);
  }

  /** Undo / redo this cell's latest step (the journal decides which cell). */
  function undoHere(): boolean {
    const u = undoRef.current;
    const s = u.past.pop();
    if (!s) return false;
    u.future.push(counterSnap(s));
    applySnap(s);
    setUndoVer((v) => v + 1);
    return true;
  }

  function redoHere(): boolean {
    const u = undoRef.current;
    const s = u.future.pop();
    if (!s) return false;
    u.past.push(counterSnap(s));
    applySnap(s);
    setUndoVer((v) => v + 1);
    return true;
  }

  // the layout-wide history (the journal) reaches this cell through these
  const undoFnsRef = useRef({ undo: undoHere, redo: redoHere });
  undoFnsRef.current = { undo: undoHere, redo: redoHere };
  useEffect(() => {
    const h = {
      undo: () => undoFnsRef.current.undo(),
      redo: () => undoFnsRef.current.redo(),
      label: (which: "past" | "future") => {
        const l = undoRef.current[which];
        return l.length ? l[l.length - 1].label : "";
      },
    };
    UNDO_HANDLERS.set(myId, h);
    const onJournal = () => setUndoVer((v) => v + 1);
    journalListeners.add(onJournal);
    return () => {
      if (UNDO_HANDLERS.get(myId) === h) UNDO_HANDLERS.delete(myId);
      journalListeners.delete(onJournal);
    };
  }, [myId]);

  function undo(): void {
    if (!journalStep("undo")) undoHere();
  }

  function redo(): void {
    if (!journalStep("redo")) redoHere();
  }

  /** Scale / view state ("reset scales" is undoable). */
  function viewState(): NonNullable<Snap["view"]> {
    const lr = safe(() => chartRef.current!.timeScale().getVisibleLogicalRange());
    const h = paneDims().height;
    const top = safe(() => mainRef.current?.coordinateToPrice(0) as number | null, null);
    const bot = safe(() => mainRef.current?.coordinateToPrice(h) as number | null, null);
    return {
      auto: autoScaleRef.current,
      mode: scaleModeRef.current,
      invert: invertRef.current,
      range: lr ? { from: lr.from as number, to: lr.to as number } : null,
      price: top != null && bot != null ? { from: Math.min(top, bot), to: Math.max(top, bot) } : null,
    };
  }

  function restoreView(v: NonNullable<Snap["view"]>): void {
    setScaleMode(v.mode);
    setInvert(v.invert);
    setAutoScale(v.auto);
    if (v.range) safe(() => chartRef.current!.timeScale().setVisibleLogicalRange({ from: v.range!.from as Logical, to: v.range!.to as Logical }));
    if (!v.auto && v.price) setTimeout(() => safe(() => mainRef.current?.priceScale().setVisibleRange(v.price!)), 0);
  }

  function selectDrawing(id: string | null): void {
    selectedIdRef.current = id;
    setSelectedDrawing(id);
    repaintDrawings();
  }

  function addDrawing(d: Drawing, label = "add drawing"): void {
    if (!TOOL_BY_ID.get(d.type)?.temp) pushUndo(label);
    drawingsRef.current = [...drawingsRef.current, d];
    selectDrawing(d.id);
    persistDrawings();
    bumpDrawings();
  }

  function updateDrawing(id: string, patch: Partial<Drawing> | ((d: Drawing) => Drawing), label?: string): void {
    if (label) pushUndo(label);
    drawingsRef.current = drawingsRef.current.map((d) => (d.id === id ? (typeof patch === "function" ? patch(d) : { ...d, ...patch }) : d));
    persistDrawings();
    repaintDrawings();
    bumpDrawings();
  }

  function deleteDrawing(id: string): void {
    pushUndo("remove drawing");
    drawingsRef.current = drawingsRef.current.filter((d) => d.id !== id);
    if (selectedIdRef.current === id) selectDrawing(null);
    persistDrawings();
    repaintDrawings();
    bumpDrawings();
  }

  function clearDrawings(): void {
    if (drawingsRef.current.length) pushUndo("remove drawings");
    drawingsRef.current = [];
    pendingRef.current = null;
    setPendingCount(0);
    selectDrawing(null);
    persistDrawings();
    bumpDrawings();
  }

  function cloneDrawing(id: string, offset = true): Drawing | null {
    const d = drawingsRef.current.find((x) => x.id === id);
    if (!d) return null;
    const shift = offset ? barInterval() * 3 : 0;
    const copy: Drawing = {
      ...d,
      id: newDrawingId(),
      locked: false,
      points: d.points.map((p) => ({ ...p, time: p.time + shift })),
      screen: d.screen ? { x: Math.min(0.95, d.screen.x + (offset ? 0.02 : 0)), y: Math.min(0.95, d.screen.y + (offset ? 0.02 : 0)) } : undefined,
    };
    addDrawing(copy, "clone drawing");
    return copy;
  }

  function reorderDrawing(id: string, k: "front" | "forward" | "backward" | "back"): void {
    const list = [...drawingsRef.current];
    const i = list.findIndex((d) => d.id === id);
    if (i < 0) return;
    pushUndo("visual order");
    const [d] = list.splice(i, 1);
    const j = k === "front" ? list.length : k === "back" ? 0 : k === "forward" ? Math.min(list.length, i + 1) : Math.max(0, i - 1);
    list.splice(j, 0, d);
    drawingsRef.current = list;
    persistDrawings();
    repaintDrawings();
    bumpDrawings();
  }

  function setDrawMode(mode: DrawMode): void {
    drawModeRef.current = mode;
    setDrawModeState(mode);
    pendingRef.current = null;
    setPendingCount(0);
    if (mode !== null) selectDrawing(null);
    repaintDrawings();
  }

  function setIndicators(next: IndicatorInstance[], label: string | null = "indicators"): void {
    if (label) pushUndo(label);
    indicatorsRef.current = next;
    setIndicatorsState(next);
  }

  function setChartKind(k: ChartKind): void {
    if (k === kindRef.current) return;
    pushUndo("chart style");
    setChartKindState(k);
  }

  function changeInterval(k: string, fromSync = false): void {
    const key = normalizeInterval(k);
    if (!key || key === ivRef.current) return;
    if (!fromSync) pushUndo("interval");
    setIvState(key);
    if (!fromSync && syncRef.current.interval) publishSync({ type: "interval", src: myId, interval: key });
  }

  function changeSettings(s: ChartSettings): void {
    settingsRef.current = s;
    setSettingsState(s);
    saveSettings(s);
  }

  // ------------------------------------------------------------------
  // Toasts & alerts
  // ------------------------------------------------------------------

  function addToast(text: string): void {
    const id = ++toastSeqRef.current;
    setToasts((t) => [...t, { id, text }].slice(-30));
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 8000);
  }

  function saveAlerts(list: AlertItem[]): void {
    setAlerts(list);
  }

  function addAlert(price: number): void {
    setAlerts((a) => [...a, newAlert(Math.round(price * 100) / 100)]);
    addToast(`alert set at ${fmtPrice(price)}`);
    try {
      if (typeof Notification !== "undefined" && Notification.permission === "default") void Notification.requestPermission();
    } catch {
      /* notifications unsupported */
    }
  }

  function openAlertDialog(price?: number, patch: Partial<AlertItem> = {}): void {
    const p = price ?? prevLtpRef.current ?? lastClose() ?? 0;
    setDialog({ k: "alert", alert: newAlert(Math.round(p * 100) / 100, patch) });
  }

  function upsertAlert(a: AlertItem): void {
    setAlerts((list) => (list.some((x) => x.id === a.id) ? list.map((x) => (x.id === a.id ? a : x)) : [...list, a]));
    try {
      if (a.notify && typeof Notification !== "undefined" && Notification.permission === "default") void Notification.requestPermission();
    } catch {
      /* notifications unsupported */
    }
  }

  function removeAlert(id: string): void {
    setAlerts((a) => a.filter((x) => x.id !== id));
  }

  function fireAlert(a: AlertItem, lp: number): void {
    const text = a.message || `${shortName} ${describeCond(a)} (LTP ${fmtPrice(lp)})`;
    if (a.popup) addToast(`🔔 ${text}`);
    if (a.sound && settingsRef.current.sound) beep();
    pushLog("alert", text);
    setAlertLog((l) => {
      const next = [{ id: Date.now() + Math.random(), ts: Date.now(), text }, ...l].slice(0, 100);
      saveJson(`chart:alertLog:${symbol}`, next);
      return next;
    });
    try {
      if (a.notify && typeof Notification !== "undefined" && Notification.permission === "granted") new Notification("Price alert", { body: text });
    } catch {
      /* notifications unsupported */
    }
  }

  function describeCond(a: AlertItem): string {
    const tgt = a.target === "value" ? fmtPrice(a.value) : a.targetLabel ?? "target";
    return `${{ cross: "crossed", crossUp: "crossed up", crossDown: "crossed down", gt: "is above", lt: "is below", enter: "entered", exit: "exited" }[a.cond]} ${tgt}`;
  }

  /** Target band [lo, hi] of an alert right now. */
  function alertTarget(a: AlertItem): [number, number] | null {
    if (a.target === "value") return [a.value, a.value2 ?? a.value];
    const c = viewRef.current;
    if (a.target.startsWith("ind:")) {
      const [, uid, plot] = a.target.split(":");
      const vals = indValuesRef.current.get(uid)?.[Number(plot)];
      const v = vals?.[c.length - 1];
      return v == null ? null : [v, v];
    }
    if (a.target.startsWith("draw:")) {
      const d = drawingsRef.current.find((x) => x.id === a.target.slice(5));
      const last = c[c.length - 1];
      if (!d || !last) return null;
      const v = drawingValueAt(d, last.time, drawingDeps());
      return v == null ? null : [v, v];
    }
    return null;
  }

  function alertTargets(): AlertTarget[] {
    const out: AlertTarget[] = [];
    const n = viewRef.current.length;
    for (const inst of indicatorsRef.current) {
      const def = INDICATOR_BY_TYPE.get(inst.type);
      if (!def || inst.type === "volume") continue;
      def.plots.forEach((p, k) => {
        const v = indValuesRef.current.get(inst.uid)?.[k]?.[n - 1] ?? null;
        out.push({ id: `ind:${inst.uid}:${k}`, label: `${instanceTitle(inst)} ${argsLabel(inst)} · ${p.label}`, value: v });
      });
    }
    const last = viewRef.current[n - 1];
    for (const d of drawingsRef.current) {
      const v = last ? drawingValueAt(d, last.time, drawingDeps()) : null;
      if (v !== null) out.push({ id: `draw:${d.id}`, label: d.name || TOOL_BY_ID.get(d.type)?.label || d.type, value: v });
    }
    return out;
  }

  // ------------------------------------------------------------------
  // Legend
  // ------------------------------------------------------------------

  const hoverRaf = useRef(0);
  function renderLegend(idx: number | null): void {
    const view = viewRef.current;
    const n = view.length;
    const i = idx ?? n - 1;
    const s = settingsRef.current;
    const pf = (v: number) => fmtNum(v, s.precision ?? 2);
    const el = legendRef.current;
    if (el) {
      const c = view[i];
      if (!c) el.innerHTML = "";
      else {
        const prev = view[i - 1];
        const base = prev ? prev.close : c.open;
        const chg = c.close - base;
        const pct = base !== 0 ? (chg / base) * 100 : 0;
        const cls = chg >= 0 ? "up" : "down";
        let html = "";
        if (s.showOhlc) {
          html +=
            `<span>O<b class="${cls}">${pf(c.open)}</b></span>` +
            `<span>H<b class="${cls}">${pf(c.high)}</b></span>` +
            `<span>L<b class="${cls}">${pf(c.low)}</b></span>` +
            `<span>C<b class="${cls}">${pf(c.close)}</b></span>`;
        }
        if (s.showBarChange) html += `<span class="${cls}">${chg >= 0 ? "+" : ""}${pf(chg)} (${chg >= 0 ? "+" : ""}${pct.toFixed(2)}%)</span>`;
        if (s.showVolume) html += `<span>Vol<b>${fmtVol(c.volume)}</b></span>`;
        if (s.showLastDayChange) {
          const ld = lastDayChange();
          if (ld) html += `<span class="${ld.d >= 0 ? "up" : "down"}">Day ${ld.d >= 0 ? "+" : ""}${pf(ld.d)} (${ld.d >= 0 ? "+" : ""}${ld.pct.toFixed(2)}%)</span>`;
        }
        el.innerHTML = html;
      }
    }
    for (const [uid, span] of indLegendRefs.current) {
      const inst = indicatorsRef.current.find((x) => x.uid === uid);
      const def = inst ? INDICATOR_BY_TYPE.get(inst.type) : undefined;
      const vals = indValuesRef.current.get(uid);
      if (!inst || !def || !vals || !s.indValues || !inst.valuesInStatus) {
        span.innerHTML = "";
        continue;
      }
      const prec = inst.precision ?? (def.overlay && !def.ownScale ? s.precision ?? 2 : 2);
      const marks = indMarksRef.current.get(uid)?.get(i) ?? [];
      span.innerHTML =
        def.plots
          .map((p, k) => {
            if (p.kind === "marks" || !inst.plots[k]?.visible || indHasDataRef.current.get(uid)?.[k] === false) return "";
            const v = vals[k]?.[i];
            const txt = v == null ? "∅" : inst.type === "volume" || inst.type === "obv" || inst.type === "flow" || inst.type === "oi" ? fmtVol(v) : fmtNum(v, prec);
            return `<b style="color:${inst.plots[k].color}">${txt}</b>`;
          })
          .join(" ") +
        marks.map((m) => ` <b style="color:${m.color ?? "inherit"}">${escapeHtml(m.title ?? m.text ?? "")}</b>`).join("");
    }
    if (widgetSlotsRef.current?.data) {
      cancelAnimationFrame(hoverRaf.current);
      hoverRaf.current = requestAnimationFrame(() => setHoverIdx(idx));
    }
  }
  const widgetSlotsRef = useRef(widgetSlots);
  widgetSlotsRef.current = widgetSlots;

  /** Today's change (the latest session vs the previous close) — fixed,
   *  not the hovered bar's: the live quote's when there is one, else from bars. */
  function lastDayChange(): { d: number; pct: number } | null {
    const q = liveQuoteRef.current;
    if (q?.change != null && q.change_pct != null) return { d: q.change, pct: q.change_pct };
    const c = candlesRef.current;
    const n = c.length;
    if (n < 2) return null;
    const last = c[n - 1];
    let prev: number | null = null;
    if (isIntraday(ivRef.current)) {
      const day = Math.floor(last.time / 86400);
      let j = n - 1;
      while (j >= 0 && Math.floor(c[j].time / 86400) === day) j--;
      if (j >= 0) prev = c[j].close;
    } else if (intervalGroup(ivRef.current) === "days") prev = c[n - 2].close;
    if (prev == null || !prev) return null;
    return { d: last.close - prev, pct: ((last.close - prev) / prev) * 100 };
  }

  function legendLastBar(): void {
    renderLegend(null);
  }

  // ------------------------------------------------------------------
  // Series data
  // ------------------------------------------------------------------

  /** The candles the chart shows: everything, or the replay window. */
  function sourceCandles(): Candle[] {
    const r = replayRef.current;
    const c = candlesRef.current;
    return r.on && !r.selecting ? c.slice(0, r.idx + 1) : c;
  }

  /** A brick type's box (Kagi: reversal amount): fixed, or the ATR's. */
  function brickBox(src: Bar[], k: BrickKind): number {
    const b = brickInputs(settingsRef.current, k);
    return b.method === "traditional" && b.box > 0 ? b.box : autoBox(src, Math.max(1, Math.round(b.atrLength)));
  }

  /** Rebuild the displayed series (non-time chart types transform the
   *  candles) and its time → index map. */
  function buildView(): void {
    const src = sourceCandles();
    const k = kindRef.current;
    let view: Candle[] = src;
    if (BRICK_KINDS.has(k)) {
      const bk = k as BrickKind;
      const inp = brickInputs(settingsRef.current, bk);
      const box = brickBox(src, bk);
      const out =
        bk === "renko" ? renko(src, box, inp.source)
          : bk === "linebreak" ? lineBreak(src, Math.max(1, Math.round(inp.lines)))
            : bk === "kagi" ? kagi(src, box, inp.source)
              : bk === "pnf" ? pointFigure(src, box, Math.max(1, Math.round(inp.reversal)), inp.source)
                : rangeBars(src, box);
      view = out as Candle[];
      safe(() => mainRef.current?.applyOptions({ box } as never));
    }
    viewRef.current = view;
    const idx = new Map<number, number>();
    for (let i = 0; i < view.length; i++) idx.set(view[i].time, i);
    indexByTimeRef.current = idx;
  }

  function mainPoint(c: Candle, prev: Candle | undefined): Record<string, unknown> {
    const k = kindRef.current;
    const s = settingsRef.current;
    const col = colorsRef.current;
    if (LINE_KINDS.has(k)) return { time: c.time, value: c.close };
    if (k === "columns") return { time: c.time, value: c.close, color: c.close >= (prev?.close ?? c.open) ? col.up : col.down };
    if (CUSTOM_MODE[k]) return { time: c.time, open: c.open, high: c.high, low: c.low, close: c.close, volume: c.volume, flag: c.flag };
    const base = { time: c.time, open: c.open, high: c.high, low: c.low, close: c.close };
    if (k === "hollow") {
      const ref = prev ? prev.close : c.open;
      const cc = c.close >= ref ? col.up : col.down;
      return { ...base, color: c.close > c.open || !s.bodyOn ? "rgba(0,0,0,0)" : cc, borderColor: cc, wickColor: cc };
    }
    if (s.colorPrevClose) {
      const cc = c.close >= (prev?.close ?? c.open) ? col.up : col.down;
      return k === "bars" ? { ...base, color: cc } : { ...base, color: s.bodyOn ? cc : "rgba(0,0,0,0)", borderColor: cc, wickColor: cc };
    }
    return base;
  }

  function setMainData(): void {
    const s = mainRef.current;
    if (!s) return;
    const view = viewRef.current;
    const k = kindRef.current;
    if (k === "baseline") safe(() => s.applyOptions({ baseValue: { type: "price", price: view[0]?.close ?? 0 } } as never));
    if (k === "heikin") {
      const ha = heikinAshi(view);
      prevHaRef.current = ha.length > 1 ? { open: ha[ha.length - 2].open, close: ha[ha.length - 2].close } : null;
      safe(() => s.setData(ha.map((c) => ({ time: c.time as UTCTimestamp, open: c.open, high: c.high, low: c.low, close: c.close })) as never));
      return;
    }
    safe(() => s.setData(view.map((c, i) => mainPoint(c, view[i - 1])) as never));
  }

  /** Push a live update for the forming bar. `rolled` = a NEW bar just
   *  opened (the previous live bar closed). */
  function updateMainBar(bar: Candle, rolled = false): void {
    const s = mainRef.current;
    if (!s) return;
    if (kindRef.current === "heikin") {
      const candles = viewRef.current;
      if (rolled && candles.length > 1) {
        const closed = heikinAshiBar(candles[candles.length - 2], prevHaRef.current);
        prevHaRef.current = { open: closed.open, close: closed.close };
      }
      const ha = heikinAshiBar(bar, prevHaRef.current);
      safe(() => s.update({ time: ha.time as UTCTimestamp, open: ha.open, high: ha.high, low: ha.low, close: ha.close } as never));
      return;
    }
    const v = viewRef.current;
    safe(() => s.update(mainPoint(bar, v[v.length - 2]) as never));
  }

  /** Drop any indicator pane that no longer holds a series. */
  function cleanupPanes(chart: IChartApi): void {
    const panes = safe(() => chart.panes(), []) ?? [];
    for (let i = panes.length - 1; i > 0; i--) {
      if ((safe(() => panes[i].getSeries().length, 1) ?? 1) === 0) safe(() => chart.removePane(i));
    }
  }

  const paneModeRef = useRef(paneMode);
  paneModeRef.current = paneMode;

  function applyPaneSizes(): void {
    const chart = chartRef.current;
    if (!chart) return;
    const panes = safe(() => chart.panes(), []) ?? [];
    const pm = paneModeRef.current;
    panes.forEach((p, i) => {
      let f = i === 0 ? 3 : 1;
      if (pm.max !== null && pm.max < panes.length) f = i === pm.max ? 10 : 0.12;
      else if (pm.collapsed.includes(i)) f = 0.12;
      safe(() => p.setStretchFactor(f));
    });
  }

  function measurePanes(): void {
    const host = containerRef.current;
    const chart = chartRef.current;
    if (!host || !chart) return;
    const panes = safe(() => chart.panes(), []) ?? [];
    const r0 = host.getBoundingClientRect();
    const rects: { i: number; top: number; height: number }[] = [];
    panes.forEach((p, i) => {
      const el = safe(() => p.getHTMLElement());
      if (!el) return;
      const r = el.getBoundingClientRect();
      rects.push({ i, top: r.top - r0.top, height: r.height });
    });
    setPaneRects((prev) =>
      prev.length === rects.length && prev.every((p, k) => p.i === rects[k].i && Math.abs(p.top - rects[k].top) < 1 && Math.abs(p.height - rects[k].height) < 1)
        ? prev
        : rects,
    );
    const lw = scaleWidth("left");
    setLeftAxisW((w) => (Math.abs(w - lw) < 1 ? w : lw));
  }

  const LS = [LineStyle.Solid, LineStyle.Dotted, LineStyle.Dashed];

  /** (Re)create the indicator SERIES for the instances, then fill them
   *  via refreshIndicatorData(). Only runs when the set / their settings
   *  change — data-only updates go through refreshIndicatorData(). */
  function rebuildIndicators(): void {
    const chart = chartRef.current;
    if (!chart) return;
    for (const e of indEntriesRef.current) safe(() => chart.removeSeries(e.series));
    indEntriesRef.current = [];
    indPaneRef.current = new Map();
    indValuesRef.current = new Map();
    indFillPrimsRef.current = [];
    indMarkerPluginsRef.current = new Map();
    const s = settingsRef.current;
    const ivk = ivRef.current;
    const intra = isIntraday(ivk);
    let pane = 1;
    const list = indicatorsRef.current;
    const isGuest = (i: IndicatorInstance) => typeof i.pane === "string" && i.pane !== "own" && i.pane !== "main" && list.some((x) => x.uid === i.pane);
    // Hosts first so an indicator merged into another's pane finds it.
    const order = [...list.filter((i) => !isGuest(i)), ...list.filter(isGuest)];
    for (const inst of order) {
      const def = INDICATOR_BY_TYPE.get(inst.type);
      if (!def || def.tool) continue;
      const shown = !hideRef.current.indicators && inst.visible && visibleOnInterval(inst, ivk) && !(def.intradayOnly && !intra);
      if (!shown) continue;
      let p: number;
      if (def.special || inst.pane === "main") p = 0;
      else if (isGuest(inst) && indPaneRef.current.has(inst.pane as string)) p = indPaneRef.current.get(inst.pane as string) as number;
      else if (def.overlay && inst.pane !== "own") p = 0;
      else p = pane++;
      indPaneRef.current.set(inst.uid, p);
      const onPrice = p === 0;
      const prec = inst.precision ?? (def.overlay && !def.ownScale ? s.precision ?? 2 : 2);
      let first: ISeriesApi<SeriesType> | null = null;
      def.plots.forEach((pd, k) => {
        const st = inst.plots[k];
        if (!st?.visible) return;
        const kind: PlotKind = pd.kind === "marks" ? "marks" : st.kind ?? pd.kind ?? "line";
        // An oscillator moved onto the price pane (or merged into another
        // indicator's pane) keeps its own overlay scale, like TradingView —
        // sharing the price axis would squash the candles.
        const guest = (!def.overlay && onPrice) || isGuest(inst);
        const scaleId = inst.scale === "left" || inst.scale === "right" ? inst.scale
          : inst.scale === "new" || (guest && !def.ownScale) ? `ind-${inst.uid}`
          : def.ownScale ? "vol" : pd.scale ?? (def.overlay && !onPrice ? "right" : s.scaleSide);
        const common: Record<string, unknown> = {
          lastValueVisible: kind !== "marks" && s.indValueLabels && inst.labelsOnScale,
          priceLineVisible: false,
          title: kind !== "marks" && s.indNameLabels ? (def.plots.length > 1 ? `${def.short} ${pd.label}` : def.short) : "",
          priceScaleId: scaleId,
          priceFormat: inst.type === "volume" ? { type: "volume" } : { type: "price", precision: prec, minMove: 10 ** -prec },
          ...((scalePriceOnlyRef.current && def.overlay && !def.ownScale) || kind === "marks" ? { autoscaleInfoProvider: () => null } : {}),
        };
        const lineCommon = {
          color: st.color,
          lineWidth: Math.max(1, Math.min(4, st.width)) as 1 | 2 | 3 | 4,
          lineStyle: LS[st.dash] ?? LineStyle.Solid,
          crosshairMarkerVisible: false,
        };
        let series: ISeriesApi<SeriesType> | undefined;
        if (kind === "hist" || kind === "columns") {
          series = safe(() => chart.addSeries(HistogramSeries, { ...common, color: st.color }, p) as ISeriesApi<SeriesType>);
        } else if (kind === "area") {
          series = safe(() =>
            chart.addSeries(
              AreaSeries,
              { ...common, ...lineCommon, lineColor: st.color, topColor: withAlpha(st.color, 0.3, st.color), bottomColor: withAlpha(st.color, 0.02, st.color) },
              p,
            ) as ISeriesApi<SeriesType>,
          );
        } else {
          series = safe(() =>
            chart.addSeries(
              LineSeries,
              {
                ...common,
                ...lineCommon,
                ...(kind === "points" ? { lineVisible: false, pointMarkersVisible: true, pointMarkersRadius: 1.5 } : {}),
                ...(kind === "circles" ? { lineVisible: false, pointMarkersVisible: true, pointMarkersRadius: Math.max(2.5, st.width + 1.5) } : {}),
                ...(kind === "marks" ? { lineVisible: false, pointMarkersVisible: false } : {}),
                ...(kind === "step" ? { lineType: 1 } : {}),
              },
              p,
            ) as ISeriesApi<SeriesType>,
          );
        }
        if (!series) return;
        if (def.ownScale) safe(() => series!.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } }));
        indEntriesRef.current.push({ uid: inst.uid, plot: k, series });
        if (!first && kind !== "marks") first = series;
      });
      if (!first) first = indEntriesRef.current.find((e) => e.uid === inst.uid)?.series ?? null;
      if (first && def.levels) {
        for (const level of def.levels) {
          safe(() =>
            first!.createPriceLine({ price: level, color: "rgba(153,153,153,0.45)", lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: false, title: "" }),
          );
        }
      }
      if (first && def.fills?.length) {
        const uid = inst.uid;
        const prim = new FillPrimitive(() => indFillDataRef.current.get(uid) ?? [], fillX, fillSpan);
        safe(() => first!.attachPrimitive(prim));
        indFillPrimsRef.current.push(prim);
      }
    }
    indPaneCountRef.current = pane;
    refreshIndicatorData();
    cleanupPanes(chart);
    placeCompares();
    applyPaneSizes();
    setTimeout(measurePanes, 0);
  }

  /** Put each compare on its scale / pane: % compares share the main scale,
   *  the first "New price scale" one takes the opposite axis (later ones get
   *  overlay scales of their own), and "New pane" ones follow the indicator panes. */
  function placeCompares(): void {
    const s = settingsRef.current;
    const main = s.scaleSide;
    const other = main === "right" ? "left" : "right";
    let axisTaken = false;
    let pane = indPaneCountRef.current;
    for (const c of comparesRef.current) {
      const series = compareSeriesRef.current.get(c.symbol);
      if (!series) continue;
      const mode = c.mode ?? "percent";
      const scaleId = mode === "percent" ? main : mode === "pane" ? main : !axisTaken ? other : `cmp-${c.symbol}`;
      if (mode === "scale" && !axisTaken) axisTaken = true;
      safe(() => series.applyOptions({ priceScaleId: scaleId }));
      const want = mode === "pane" ? pane++ : 0;
      const at = safe(() => series.getPane().paneIndex(), 0) ?? 0;
      if (at !== want) safe(() => series.moveToPane(want));
    }
    applyPaneSizes();
    setTimeout(measurePanes, 0);
  }

  /** x of a bar index (logical), also past the last bar for shifted plots. */
  function fillX(j: number): number | null {
    return safe(() => chartRef.current!.timeScale().logicalToCoordinate(j as Logical) as number | null, null) ?? null;
  }

  function fillSpan(): [number, number] {
    const r = safe(() => chartRef.current!.timeScale().getVisibleLogicalRange());
    return r ? [Math.floor(r.from) - 2, Math.ceil(r.to) + 2] : [0, viewRef.current.length + 500];
  }

  /** Fetch another symbol's candles over the loaded span (two-symbol indicators). */
  function ensureOther(sym: string): void {
    const v = viewRef.current;
    if (!v.length || otherLoadingRef.current.has(sym)) return;
    const need = v[0].time;
    const have = otherDataRef.current.get(sym);
    if (have && have.from <= need) return;
    otherLoadingRef.current.add(sym);
    const pl = fetchPlan(ivRef.current);
    const seq = fetchSeqRef.current;
    const iv = ivRef.current;
    void fetchHistory(sym, pl.res, need - IST - 86400, Math.floor(Date.now() / 1000)).then((r) => {
      otherLoadingRef.current.delete(sym);
      if (seq !== fetchSeqRef.current || iv !== ivRef.current) return;
      const rows = pl.aggregate ? (aggregate(r.candles, iv) as Candle[]) : r.candles;
      otherDataRef.current.set(sym, { from: rows.length ? Math.min(need, rows[0].time) : need, closes: new Map(rows.map((k) => [k.time as number, k.close])) });
      refreshIndicatorData();
    });
  }

  /** Option-chain OI for the OI profile (refreshed every 30 s while shown). */
  function ensureOiChain(strikes: number): void {
    const cur = oiChainRef.current;
    const now = Date.now();
    if (cur && cur.symbol === symbol && now - cur.at < 30000) return;
    oiChainRef.current = { symbol, at: now, rows: cur?.symbol === symbol ? cur.rows : [] };
    const qs = new URLSearchParams({ symbol, underlying: shortName ?? "", strikecount: String(strikes) });
    void api
      .get<{ strikes?: { strike: number; ce: { oi: number | null } | null; pe: { oi: number | null } | null }[] }>(`/api/options/chain?${qs.toString()}`)
      .then((r) => {
        if (oiChainRef.current?.symbol !== symbol) return;
        oiChainRef.current = { symbol, at: Date.now(), rows: (r.strikes ?? []).map((k) => ({ strike: k.strike, ce: k.ce?.oi ?? null, pe: k.pe?.oi ?? null })) };
        repaintDrawings();
      })
      .catch(() => undefined);
  }
  const scalePriceOnlyRef = useRef(scalePriceOnly);
  scalePriceOnlyRef.current = scalePriceOnly;

  /** Re-derive every indicator series from the displayed candles.
   *  Cheap (setData only) — used on paging, live bars and data changes. */
  function refreshIndicatorData(): void {
    const view = viewRef.current;
    const n = view.length;
    const ctxC = {
      interval: ivRef.current,
      up: colorsRef.current.volUp,
      down: colorsRef.current.volDown,
      flow: flowRef.current,
      other: (sym: string) => otherDataRef.current.get(sym.trim().toUpperCase())?.closes,
    };
    const uids = [...new Set(indEntriesRef.current.map((e) => e.uid))];
    const interval = barInterval();
    for (const uid of uids) {
      const inst = indicatorsRef.current.find((x) => x.uid === uid);
      const def = inst ? INDICATOR_BY_TYPE.get(inst.type) : undefined;
      if (!inst || !def) continue;
      for (const sym of symbolInputs(def, inst.inputs)) ensureOther(sym);
      let res;
      try {
        res = def.compute(view, inst.inputs, ctxC);
      } catch {
        continue;
      }
      const aligned: (number | null)[][] = res.plots.map((vals, k) => {
        const sh = res.shifts?.[k] ?? 0;
        const out = new Array<number | null>(n).fill(null);
        for (let i = 0; i < vals.length; i++) {
          const j = i + sh;
          if (j >= 0 && j < n) out[j] = vals[i];
        }
        return out;
      });
      indValuesRef.current.set(uid, aligned);
      indHasDataRef.current.set(uid, res.plots.map((vals) => vals.some((v) => v !== null)));
      // shaded fills: bar-indexed, running past the last bar for forward-shifted plots
      if (def.fills?.length) {
        const len = n + Math.max(0, ...(res.shifts ?? [0]));
        const ext = res.plots.map((vals, k) => {
          const sh = res.shifts?.[k] ?? 0;
          const out = new Array<number | null>(len).fill(null);
          vals.forEach((v, i) => {
            const j = i + sh;
            if (j >= 0 && j < len) out[j] = v;
          });
          return out;
        });
        const side = (x: number | { level: number }) => (typeof x === "number" ? ext[x] ?? [] : new Array<number | null>(len).fill(x.level));
        const bands: FillBand[] = [];
        def.fills.forEach((f, k) => {
          const st = inst.fills?.[k];
          if (st && !st.visible) return;
          bands.push({ a: side(f.a), b: side(f.b), color: st?.color || f.color, colorDown: f.colorDown });
        });
        indFillDataRef.current.set(uid, bands);
      }
      const byIdx = new Map<number, Mark[]>();
      for (const m of res.marks ?? []) byIdx.set(m.i, [...(byIdx.get(m.i) ?? []), m]);
      indMarksRef.current.set(uid, byIdx);
      for (const e of indEntriesRef.current) {
        if (e.uid !== uid) continue;
        const vals = res.plots[e.plot] ?? [];
        const sh = res.shifts?.[e.plot] ?? 0;
        const colors = res.colors?.[e.plot] ?? null;
        const kind = inst.plots[e.plot]?.kind ?? def.plots[e.plot]?.kind ?? "line";
        const data: Record<string, unknown>[] = [];
        let started = false;
        for (let i = 0; i < vals.length; i++) {
          const j = i + sh;
          if (j < 0) continue;
          const t = j < n ? view[j].time : n > 0 ? view[n - 1].time + (j - (n - 1)) * interval : null;
          if (t === null) continue;
          const v = vals[i];
          if (v === null || !Number.isFinite(v)) {
            if (started && kind !== "hist" && kind !== "columns") data.push({ time: t });
            continue;
          }
          started = true;
          data.push(colors && colors[i] ? { time: t, value: v, color: colors[i] } : { time: t, value: v });
        }
        safe(() => e.series.setData(data as never));
        // markers ride on their plot's series
        const ms = (res.marks ?? []).filter((m) => m.plot === e.plot);
        const plug = indMarkerPluginsRef.current.get(e.series);
        if (ms.length || plug) {
          const color = inst.plots[e.plot]?.color ?? def.plots[e.plot]?.color;
          const markers = ms
            .map((m) => {
              const j = m.i + sh;
              if (j < 0 || j >= n) return null;
              return {
                time: view[j].time,
                position: m.pos === "above" ? "aboveBar" : m.pos === "below" ? "belowBar" : "inBar",
                shape: m.shape,
                color: m.color ?? color,
                ...(m.text ? { text: m.text } : {}),
                size: 1,
              };
            })
            .filter((m): m is NonNullable<typeof m> => m !== null)
            .sort((a, b) => a.time - b.time);
          if (plug) safe(() => plug.setMarkers(markers));
          else {
            const made = safe(() => createSeriesMarkers(e.series, markers as never[])) as unknown as { setMarkers: (m: unknown[]) => void } | undefined;
            if (made) indMarkerPluginsRef.current.set(e.series, made);
          }
        }
      }
    }
    for (const prim of indFillPrimsRef.current) prim.requestUpdate();
  }

  /** Coalesce indicator recomputes during live ticks (≤ 1 per second). */
  function scheduleIndicatorRefresh(): void {
    if (indRefreshTimer.current !== undefined) return;
    indRefreshTimer.current = window.setTimeout(() => {
      indRefreshTimer.current = undefined;
      refreshIndicatorData();
    }, 1000);
  }

  /** Push the full candle set into every series. */
  function applyData(): void {
    buildView();
    setMainData();
    refreshIndicatorData();
    legendLastBar();
    repaintDrawings();
    updateExtraLines();
    setDataVer((v) => v + 1);
  }

  /** (Re)load a compare overlay across the currently loaded time span. */
  function loadCompareData(compareSymbol: string, series: ISeriesApi<"Line">): void {
    const seq = fetchSeqRef.current;
    const now = Math.floor(Date.now() / 1000);
    const pl = fetchPlan(ivRef.current);
    const oldest = candlesRef.current.length > 0 ? candlesRef.current[0].time - IST : now - pl.initialDays * 86400;
    void fetchHistory(compareSymbol, pl.res, oldest, now).then((r) => {
      if (seq !== fetchSeqRef.current || !compareSeriesRef.current.has(compareSymbol)) return;
      const rows = pl.aggregate ? (aggregate(r.candles, ivRef.current) as Candle[]) : r.candles;
      safe(() => series.setData(rows.map((k) => ({ time: k.time, value: k.close }))));
    });
  }

  /** Page in older history (one chunk); concurrent callers share it. */
  function maybeLoadOlder(): Promise<void> {
    if (loadingOlderRef.current) return loadingOlderRef.current;
    const candles = candlesRef.current;
    if (!haveMoreRef.current || candles.length === 0 || candles.length >= MAX_CANDLES) return Promise.resolve();
    const run = (async () => {
      const seq = fetchSeqRef.current;
      const key = ivRef.current;
      const pl = fetchPlan(key);
      const to = candles[0].time - IST - 1;
      const from = dayStart(to - (pl.chunkDays - 1) * 86400);
      const r = await fetchHistory(symbol, pl.res, from, to);
      if (seq !== fetchSeqRef.current) return;
      const cur = candlesRef.current;
      const firstTime = cur[0]?.time ?? Infinity;
      let older = r.candles.filter((c) => c.time < firstTime);
      if (older.length === 0) {
        haveMoreRef.current = false;
        return;
      }
      if (pl.aggregate) older = aggregate(older, key) as Candle[];
      const merged = (pl.aggregate ? mergeOlder(older, cur, key) : [...older, ...cur]) as Candle[];
      const added = merged.length - cur.length;
      candlesRef.current = merged;
      if (replayRef.current.on && !replayRef.current.selecting) setReplay({ ...replayRef.current, idx: replayRef.current.idx + added });
      applyData();
      for (const [sym, s] of compareSeriesRef.current) loadCompareData(sym, s);
    })().finally(() => {
      loadingOlderRef.current = null;
    });
    loadingOlderRef.current = run;
    return run;
  }

  /** Page back until the chart holds bars at or before chart time `t`. */
  async function ensureHistoryFrom(t: number): Promise<void> {
    for (let k = 0; k < 60; k++) {
      const c = candlesRef.current;
      if (!c.length || c[0].time <= t || !haveMoreRef.current || c.length >= MAX_CANDLES) return;
      await maybeLoadOlder();
    }
  }

  function setReplay(r: typeof replay): void {
    replayRef.current = r;
    setReplayState(r);
  }

  // ------------------------------------------------------------------
  // Price-scale extras: high / low, average close, previous close, bid / ask
  // ------------------------------------------------------------------

  const liveRef = useRef<{ bid: number | null; ask: number | null }>({ bid: null, ask: null });

  function updateExtraLines(): void {
    const main = mainRef.current;
    const chart = chartRef.current;
    if (!main || !chart) return;
    const s = settingsRef.current;
    const view = viewRef.current;
    const n = view.length;
    const r = safe(() => chart.timeScale().getVisibleLogicalRange());
    const from = Math.max(0, Math.floor(r?.from ?? 0));
    const to = Math.min(n - 1, Math.ceil(r?.to ?? n - 1));
    const want: Record<string, { price: number | null; label: boolean; line: boolean; color: string; title: string }> = {};
    let hi = -Infinity, lo = Infinity, sum = 0, cnt = 0;
    for (let i = from; i <= to; i++) {
      const c = view[i];
      if (!c) continue;
      if (c.high > hi) hi = c.high;
      if (c.low < lo) lo = c.low;
      sum += c.close;
      cnt++;
    }
    const col = colorsRef.current;
    want.hi = { price: cnt ? hi : null, label: s.highLowLabels, line: s.highLowLines, color: col.up, title: "High" };
    want.lo = { price: cnt ? lo : null, label: s.highLowLabels, line: s.highLowLines, color: col.down, title: "Low" };
    want.avg = { price: cnt ? sum / cnt : null, label: s.avgCloseLabel, line: s.avgCloseLine, color: "#9C27B0", title: "Avg close" };
    let prevClose: number | null = null;
    if (isIntraday(ivRef.current) && n > 1) {
      const day = Math.floor(view[n - 1].time / 86400);
      for (let i = n - 2; i >= 0; i--) {
        if (Math.floor(view[i].time / 86400) !== day) {
          prevClose = view[i].close;
          break;
        }
      }
    }
    want.prev = { price: prevClose, label: s.prevCloseLabel, line: s.prevCloseLine, color: "#787B86", title: "Prev close" };
    want.bid = { price: liveRef.current.bid, label: s.bidAskLabels, line: s.bidAskLines, color: "#2962FF", title: "Bid" };
    want.ask = { price: liveRef.current.ask, label: s.bidAskLabels, line: s.bidAskLines, color: "#F23645", title: "Ask" };
    for (const [k, w] of Object.entries(want)) {
      const cur = extraLinesRef.current.get(k);
      if (w.price === null || !Number.isFinite(w.price) || (!w.label && !w.line)) {
        if (cur) safe(() => main.removePriceLine(cur));
        extraLinesRef.current.delete(k);
        continue;
      }
      const opts = { price: w.price, color: w.color, lineWidth: 1 as const, lineStyle: LineStyle.Dotted, lineVisible: w.line, axisLabelVisible: w.label, title: w.label ? w.title : "" };
      if (cur) safe(() => cur.applyOptions(opts));
      else {
        const l = safe(() => main.createPriceLine(opts));
        if (l) extraLinesRef.current.set(k, l);
      }
    }
  }

  // ------------------------------------------------------------------
  // Chart event handlers (subscribed once, dispatched via implRef)
  // ------------------------------------------------------------------

  const shiftRef = useRef(false);
  const crossRaf = useRef(0);

  /** Inverse of timeToLogical. */
  function logicalToTime(l: number): number | null {
    const c = viewRef.current;
    const n = c.length;
    if (n === 0) return null;
    if (l <= 0) return c[0].time + l * barInterval();
    if (l >= n - 1) return c[n - 1].time + (l - (n - 1)) * barInterval();
    const i = Math.floor(l);
    return c[i].time + (l - i) * (c[i + 1].time - c[i].time);
  }

  /** Magnet: snap a point to the nearest O/H/L/C of its bar. */
  function snapPoint(time: number, price: number, y: number): DrawingPoint {
    const m = magnetRef.current;
    if (m === "off") return { time, price };
    const l = timeToLogical(time);
    const c = l === null ? undefined : viewRef.current[Math.round(l)];
    if (!c) return { time, price };
    let best = price;
    let bestD = Infinity;
    for (const v of [c.open, c.high, c.low, c.close]) {
      const py = priceToY(v);
      if (py === null) continue;
      const d = Math.abs(py - y);
      if (d < bestD) {
        bestD = d;
        best = v;
      }
    }
    return m === "strong" || bestD <= 30 ? { time: c.time, price: best } : { time, price };
  }

  /** Shift: constrain the next point to 45° steps from the previous one. */
  function constrain(prev: DrawingPoint, x: number, y: number): DrawingPoint | null {
    const px = timeToX(prev.time);
    const py = priceToY(prev.price);
    if (px === null || py === null) return null;
    const s = snapAngle({ x: px, y: py }, { x, y });
    const t = xToTime(s.x);
    const p = yToPrice(s.y);
    return t === null || p === null ? null : { time: t, price: p };
  }

  function onCrosshair(param: MouseEventParams): void {
    const plus = plusRef.current;
    const inMain = (param.paneIndex ?? 0) === 0;
    if (plus) {
      const pr = param.point && inMain ? yToPrice(param.point.y) : null;
      if (param.point && pr != null) {
        window.clearTimeout(plusHideRef.current);
        plusPriceRef.current = Math.round(pr * 100) / 100;
        plus.style.top = `${param.point.y - 9}px`;
        if (settingsRef.current.scaleSide === "left") {
          plus.style.left = `${scaleWidth("left") + 3}px`;
          plus.style.right = "";
        } else {
          plus.style.right = `${(scaleWidth("right") || 60) + 3}px`;
          plus.style.left = "";
        }
        plus.style.display = "grid";
      } else if (!overPlusRef.current) {
        // Moving onto the "+" itself leaves the chart: give the pointer a
        // moment to arrive before hiding it.
        window.clearTimeout(plusHideRef.current);
        plusHideRef.current = window.setTimeout(() => {
          if (overPlusRef.current || !plusRef.current) return;
          plusRef.current.style.display = "none";
          plusPriceRef.current = null;
        }, 250);
      }
    } else if (param.point && inMain) {
      const pr = yToPrice(param.point.y);
      plusPriceRef.current = pr == null ? null : Math.round(pr * 100) / 100;
    } else if (!param.point) {
      plusPriceRef.current = null;
    }
    pointerRef.current = param.point && inMain ? { x: param.point.x, y: param.point.y } : null;
    if (replayRef.current.selecting) repaintDrawings();
    // ghost preview for in-progress drawings
    const pending = pendingRef.current;
    if (pending && param.point && inMain && !freehandRef.current) {
      const t = xToTime(param.point.x);
      const p = yToPrice(param.point.y);
      if (t !== null && p != null) {
        let pt = snapPoint(t, p, param.point.y);
        if (shiftRef.current && pending.points.length) pt = constrain(pending.points[pending.points.length - 1], param.point.x, param.point.y) ?? pt;
        pending.cursor = pt;
        repaintDrawings();
      }
    }
    if (syncRef.current.crosshair) {
      const t = param.time == null ? null : (param.time as number);
      cancelAnimationFrame(crossRaf.current);
      crossRaf.current = requestAnimationFrame(() => publishSync({ type: "crosshair", src: myId, time: t }));
    }
    if (param.time == null || !param.point) {
      if (eventTipRef.current) setEventTip(null);
      legendLastBar();
      return;
    }
    const idx = indexByTimeRef.current.get(param.time as number);
    if (idx === undefined) return;
    const evs = eventsByTimeRef.current.get(param.time as number);
    const tip = evs ? evs.map((e) => `${e.kind} · ${e.text}`).join("\n") : null;
    if ((tip ?? null) !== (eventTipRef.current?.text ?? null)) setEventTip(tip ? { x: param.point.x + paneLeft(), text: tip } : null);
    renderLegend(idx);
  }
  const eventTipRef = useRef(eventTip);
  eventTipRef.current = eventTip;

  function finishDrawing(type: string, points: DrawingPoint[], x?: number, y?: number): void {
    const tool = TOOL_BY_ID.get(type);
    if (!tool) return;
    const dims = paneDims();
    let d: Drawing = { id: newDrawingId(), type, points };
    if (tool.text) d.text = tool.text;
    if (tool.screen && x !== undefined && y !== undefined) d.screen = { x: x / Math.max(1, dims.width), y: y / Math.max(1, dims.height) };
    if (type === "icon" || type === "image") d.data = { ...(pendingIconRef.current ?? {}) };
    d = finalizeDrawing(d, drawingDeps(), dims.width, dims.height);
    pendingRef.current = null;
    setPendingCount(0);
    polyDoneRef.current = performance.now();
    addDrawing(d, `add ${tool.label}`);
    if (!tool.temp) {
      setLastTool((lt) => ({ ...lt, [tool.group]: type }));
    }
    if (!stayRef.current || tool.temp || type === "image") {
      drawModeRef.current = null;
      setDrawModeState(null);
    }
    if (tool.text) openTextEditor(d.id);
  }

  function finishPoly(): void {
    const p = pendingRef.current;
    if (!p || pointsNeeded(p.type) !== "poly") return;
    // a double-click lands the last point twice — drop near-duplicates
    const pts: DrawingPoint[] = [];
    for (const q of p.points) {
      const last = pts[pts.length - 1];
      const a = last ? timeToX(last.time) : null, b = timeToX(q.time);
      const ya = last ? priceToY(last.price) : null, yb = priceToY(q.price);
      if (last && a !== null && b !== null && ya !== null && yb !== null && Math.hypot(a - b, ya - yb) < 4) continue;
      pts.push(q);
    }
    if (pts.length >= 2) finishDrawing(p.type, pts);
    else {
      pendingRef.current = null;
      setPendingCount(0);
      repaintDrawings();
    }
  }

  const polyDoneRef = useRef(0);

  function placePoint(mode: string, x: number, y: number, time: number, price: number): void {
    const tool = TOOL_BY_ID.get(mode);
    if (!tool) return;
    const need = tool.points;
    if (need === "free") return; // brushes draw on mousedown / drag
    const pend = pendingRef.current;
    let pt = snapPoint(time, price, y);
    if (shiftRef.current && pend?.points.length) pt = constrain(pend.points[pend.points.length - 1], x, y) ?? pt;
    if (need === 1) {
      finishDrawing(mode, [pt], x, y);
      return;
    }
    const pts = [...(pend?.points ?? []), pt];
    if (need === "poly") {
      if (mode === "polyline" && pend && pend.points.length >= 2) {
        const fx = timeToX(pend.points[0].time), fy = priceToY(pend.points[0].price);
        if (fx !== null && fy !== null && Math.hypot(fx - x, fy - y) < 8) {
          finishDrawing(mode, [...pend.points, pend.points[0]]);
          return;
        }
      }
      pendingRef.current = { type: mode, points: pts, cursor: null };
      setPendingCount(pts.length);
      repaintDrawings();
      return;
    }
    if (pend && pend.points.length) {
      const last = pend.points[pend.points.length - 1];
      if (last.time === pt.time && last.price === pt.price) return;
    }
    if (pts.length >= need) {
      finishDrawing(mode, pts.slice(0, need), x, y);
      return;
    }
    pendingRef.current = { type: mode, points: pts, cursor: null };
    setPendingCount(pts.length);
    repaintDrawings();
  }

  function hitDrawingAt(x: number, y: number): Drawing | null {
    if (hideRef.current.drawings) return null;
    const deps = drawingDeps();
    const dims = paneDims();
    const list = drawingsRef.current;
    for (let i = list.length - 1; i >= 0; i--) {
      if (!visibleNow(list[i], deps)) continue;
      if (hitTest(list[i], x, y, deps, dims.width, dims.height)) return list[i];
    }
    return null;
  }

  function zoomTo(a: DrawingPoint, b: DrawingPoint): void {
    const ts = safe(() => chartRef.current!.timeScale());
    const la = timeToLogical(Math.min(a.time, b.time));
    const lb = timeToLogical(Math.max(a.time, b.time));
    if (!ts || la === null || lb === null || lb - la < 2) return;
    safe(() => ts.setVisibleLogicalRange({ from: la as Logical, to: lb as Logical }));
    const lo = Math.min(a.price, b.price), hi = Math.max(a.price, b.price);
    safe(() => mainRef.current!.priceScale().setVisibleRange({ from: lo, to: hi }));
    setAutoScale(false);
  }

  /** A click on the price pane (pane coordinates). Detected from DOM
   *  mousedown / mouseup rather than the engine's click event: the engine
   *  drops a second click that lands within 500ms and ≥5px of the first,
   *  which loses points when placing multi-point drawings quickly. */
  function onPaneClick(x: number, y: number): void {
    if (!chartRef.current || !mainRef.current) return;
    const price = yToPrice(y);
    const time = xToTime(x);
    const mode = drawModeRef.current;
    if (replayRef.current.selecting) {
      if (time !== null) startReplayAt(time);
      return;
    }
    if (mode === "alert") {
      if (price != null) addAlert(price);
      setDrawMode(null);
      return;
    }
    if (mode === "ticket") {
      if (price != null && onPickPrice) {
        onPickPrice(price);
        addToast(`price ${fmtPrice(price)} sent to the ticket`);
      }
      setDrawMode(null);
      return;
    }
    if (mode === "zoom") {
      if (price == null || time === null) return;
      const pend = pendingRef.current;
      if (!pend) {
        pendingRef.current = { type: "dprange", points: [{ time, price }], cursor: null };
        setPendingCount(1);
      } else {
        zoomTo(pend.points[0], { time, price });
        setDrawMode(null);
      }
      return;
    }
    if (mode) {
      if (price != null && time !== null) placePoint(mode, x, y, time, price);
      return;
    }
    // no tool → measure results go away, eraser deletes, otherwise select
    if (drawingsRef.current.some((d) => TOOL_BY_ID.get(d.type)?.temp)) {
      drawingsRef.current = drawingsRef.current.filter((d) => !TOOL_BY_ID.get(d.type)?.temp);
      bumpDrawings();
    }
    const hit = hitDrawingAt(x, y);
    if (cursorRef.current === "eraser") {
      if (hit) deleteDrawing(hit.id);
      return;
    }
    selectDrawing(hit?.id ?? null);
  }

  function onVisibleRange(range: LogicalRange | null): void {
    if (range && range.from < 10 && !replayRef.current.on) void maybeLoadOlder();
    // "go to live" affordance when the newest bar is scrolled out of view
    const n = viewRef.current.length;
    if (range && n > 0) {
      const live = range.to >= n - 2;
      if (live !== atLiveRef.current) {
        atLiveRef.current = live;
        setAtLive(live);
      }
    }
    repaintDrawings(); // keep drawings glued to bars while panning
    cancelAnimationFrame(rangeRaf.current);
    rangeRaf.current = requestAnimationFrame(() => {
      updateExtraLines();
      applyRatioLock();
      if (syncRef.current.time && Date.now() - syncApplyRef.current > 200) {
        const tr = safe(() => chartRef.current!.timeScale().getVisibleRange());
        if (tr) publishSync({ type: "range", src: myId, from: tr.from as number, to: tr.to as number });
      }
    });
  }
  const rangeRaf = useRef(0);

  /** Lock price to bar ratio: keep price units per bar width fixed as the
   *  time axis zooms (the price range follows the bar spacing). */
  function applyRatioLock(): void {
    const s = settingsRef.current;
    const main = mainRef.current;
    if (!s.lockRatio || !main || scaleModeRef.current === "log") return;
    const h = paneDims().height;
    const top = safe(() => main.coordinateToPrice(0) as number | null, null);
    const bot = safe(() => main.coordinateToPrice(h) as number | null, null);
    if (!(h > 0) || top == null || bot == null) return;
    const range = Math.abs(top - bot);
    const bs = barSpacing();
    if (!(range > 0) || !(bs > 0)) return;
    let r = s.priceBarRatio;
    if (!(r > 0)) {
      r = (range / h) * bs; // take the current view as the ratio
      changeSettings({ ...s, priceBarRatio: Number(r.toPrecision(6)) });
    }
    const want = (r / bs) * h;
    if (Math.abs(want - range) / range < 0.002) return;
    const mid = (top + bot) / 2;
    if (autoScaleRef.current) setAutoScale(false);
    safe(() => main.priceScale().setVisibleRange({ from: mid - want / 2, to: mid + want / 2 }));
  }

  // ---- drag-to-modify drawings (DOM-level, capture phase) ----

  /** Pointer position relative to the main pane, or null when outside. */
  function paneCoords(e: MouseEvent, clamp = false): { x: number; y: number } | null {
    const host = containerRef.current;
    if (!host) return null;
    const r = host.getBoundingClientRect();
    let x = e.clientX - r.left - paneLeft();
    let y = e.clientY - r.top;
    const dims = paneDims();
    if (clamp) {
      x = Math.max(0, Math.min(x, dims.width));
      y = Math.max(0, Math.min(y, dims.height));
    } else if (x < 0 || y < 0 || x > dims.width || y > dims.height) return null;
    return { x, y };
  }

  /** Capture-phase mousedown on the chart host: brushes start drawing;
   *  a press on a drawing selects it and begins a drag — swallowed so the
   *  chart doesn't pan underneath. */
  function onHostMouseDown(e: MouseEvent): void {
    if (e.button !== 0) return;
    pressRef.current = { x: e.clientX, y: e.clientY };
    const target = e.target as HTMLElement | null;
    if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.closest?.(".chart-overlay-ui"))) return;
    const pt = paneCoords(e);
    if (!pt) return;
    const time = xToTime(pt.x);
    const price = yToPrice(pt.y);
    if (time === null || price == null) return;
    const mode = drawModeRef.current;
    if (mode === "zoom" && !pendingRef.current) {
      // press-and-drag a rectangle (a click without travel keeps the two-click flow)
      zoomDragRef.current = { start: { time, price }, startPt: pt, moved: false };
      e.preventDefault();
      e.stopPropagation();
      return;
    }
    if (mode && pointsNeeded(mode) === "free") {
      freehandRef.current = { type: mode, points: [{ time, price }], last: pt };
      pendingRef.current = { type: mode, points: [{ time, price }], cursor: null };
      e.preventDefault();
      e.stopPropagation();
      return;
    }
    if (mode || pendingRef.current || replayRef.current.selecting) return; // a tool owns clicks
    if (cursorRef.current === "eraser") return;
    const deps = drawingDeps();
    const dims = paneDims();
    const list = drawingsRef.current;
    for (let i = list.length - 1; i >= 0; i--) {
      let d = list[i];
      if (!visibleNow(d, deps) || hideRef.current.drawings) continue;
      // Handles are only visible (and grabbable) on the selected drawing.
      const handle = selectedIdRef.current === d.id ? hitHandle(d, pt.x, pt.y, deps, dims.width, dims.height) : null;
      if (handle === null && !hitTest(d, pt.x, pt.y, deps, dims.width, dims.height)) continue;
      selectDrawing(d.id);
      if (d.locked || lockAllRef.current) return; // selectable, not movable
      const snap = snapshot("move drawing");
      if (e.ctrlKey || e.metaKey) {
        const copy = cloneDrawing(d.id, false);
        if (copy) d = copy;
      }
      dragRef.current = {
        id: d.id,
        mode: handle !== null ? handle : "move",
        start: { time, price },
        startPx: pt,
        orig: d,
        snap,
        moved: false,
      };
      e.preventDefault();
      e.stopPropagation();
      return;
    }
  }

  const pressRef = useRef<{ x: number; y: number } | null>(null);
  const zoomDragRef = useRef<{ start: DrawingPoint; startPt: { x: number; y: number }; moved: boolean } | null>(null);

  /** A press + release without travel on the price pane is a click. */
  function onHostMouseUp(e: MouseEvent): void {
    const p = pressRef.current;
    pressRef.current = null;
    if (!p || e.button !== 0 || freehandRef.current) return;
    if (dragRef.current?.moved) return;
    if (Math.hypot(e.clientX - p.x, e.clientY - p.y) > 4) return; // a pan / drag
    const pt = paneCoords(e);
    if (pt) onPaneClick(pt.x, pt.y);
  }

  function onWindowMouseMove(e: MouseEvent): void {
    shiftRef.current = e.shiftKey;
    const zd = zoomDragRef.current;
    if (zd) {
      const pt = paneCoords(e, true);
      if (!pt) return;
      if (!zd.moved && Math.hypot(pt.x - zd.startPt.x, pt.y - zd.startPt.y) < 6) return;
      zd.moved = true;
      const t = xToTime(pt.x), pr = yToPrice(pt.y);
      if (t === null || pr === null) return;
      pendingRef.current = { type: "dprange", points: [zd.start], cursor: { time: t, price: pr } };
      repaintDrawings();
      return;
    }
    const fh = freehandRef.current;
    if (fh) {
      const pt = paneCoords(e, true);
      if (!pt || Math.hypot(pt.x - fh.last.x, pt.y - fh.last.y) < 3) return;
      const t = xToTime(pt.x), p = yToPrice(pt.y);
      if (t === null || p === null) return;
      fh.points.push({ time: t, price: p });
      fh.last = pt;
      if (pendingRef.current) pendingRef.current.points = [...fh.points];
      repaintDrawings();
      return;
    }
    const drag = dragRef.current;
    if (!drag) {
      updateHoverCursor(e);
      return;
    }
    const pt = paneCoords(e, true);
    if (!pt) return;
    const time = xToTime(pt.x);
    const price = yToPrice(pt.y);
    if (time === null || price == null) return;
    const d = drag.orig;
    const tool = TOOL_BY_ID.get(d.type);
    const dims = paneDims();
    let next: Drawing;
    if (d.screen && tool?.screen) {
      next = {
        ...d,
        screen: {
          x: Math.max(0, Math.min(1, d.screen.x + (pt.x - drag.startPx.x) / Math.max(1, dims.width))),
          y: Math.max(0, Math.min(1, d.screen.y + (pt.y - drag.startPx.y) / Math.max(1, dims.height))),
        },
      };
    } else if (drag.mode === "move") {
      const l0 = timeToLogical(drag.start.time), l1 = timeToLogical(time);
      const dL = l0 !== null && l1 !== null ? l1 - l0 : 0;
      const dP = price - drag.start.price;
      next = {
        ...d,
        points: d.points.map((p) => {
          const lp = timeToLogical(p.time);
          const nt = tool?.axis === "price" || lp === null ? p.time : logicalToTime(lp + dL) ?? p.time;
          return { time: nt, price: tool?.axis === "time" ? p.price : p.price + dP };
        }),
      };
    } else {
      const i = drag.mode;
      let np = snapPoint(time, price, pt.y);
      if (e.shiftKey && d.points.length > 1) np = constrain(d.points[i === 0 ? 1 : i - 1], pt.x, pt.y) ?? np;
      const points = d.points.map((p) => ({ ...p }));
      points[i] = { time: tool?.axis === "price" ? points[i].time : np.time, price: tool?.axis === "time" ? points[i].price : np.price };
      next = normalizeDrawing({ ...d, points }, drawingDeps(), i);
    }
    drag.moved = true;
    drawingsRef.current = drawingsRef.current.map((x) => (x.id === drag.id ? next : x));
    repaintDrawings();
  }

  function onWindowMouseUp(): void {
    const zd = zoomDragRef.current;
    if (zd) {
      // (a release without travel was already handled as a click by onHostMouseUp)
      zoomDragRef.current = null;
      if (zd.moved) {
        const end = pendingRef.current?.cursor ?? null;
        pendingRef.current = null;
        setPendingCount(0);
        if (end) zoomTo(zd.start, end);
        setDrawMode(null);
        repaintDrawings();
      }
      return;
    }
    const fh = freehandRef.current;
    if (fh) {
      freehandRef.current = null;
      pendingRef.current = null;
      if (fh.points.length >= 2) finishDrawing(fh.type, fh.points);
      else repaintDrawings();
      return;
    }
    const drag = dragRef.current;
    if (drag) {
      dragRef.current = null;
      if (drag.moved) {
        const d = drawingsRef.current.find((x) => x.id === drag.id);
        if (d && drag.mode === "move") {
          const n = normalizeDrawing(d, drawingDeps(), -1);
          drawingsRef.current = drawingsRef.current.map((x) => (x.id === drag.id ? n : x));
        }
        pushUndo("move drawing", drag.snap);
        persistDrawings();
        repaintDrawings();
        bumpDrawings();
      }
    }
    // pane separators may have moved
    setTimeout(measurePanes, 0);
  }

  /** Grab-affordance cursor when hovering a drawing (no tool active). */
  function updateHoverCursor(e: MouseEvent): void {
    const host = containerRef.current;
    if (!host || drawModeRef.current) return;
    const target = e.target as HTMLElement | null;
    const inside = !!target && host.contains(target);
    const pt = inside ? paneCoords(e) : null;
    let cursor = "";
    let hover: string | null = null;
    if (pt && !hideRef.current.drawings) {
      const deps = drawingDeps();
      const dims = paneDims();
      for (let i = drawingsRef.current.length - 1; i >= 0; i--) {
        const d = drawingsRef.current[i];
        if (!visibleNow(d, deps)) continue;
        if (selectedIdRef.current === d.id && hitHandle(d, pt.x, pt.y, deps, dims.width, dims.height) !== null) {
          cursor = d.locked || lockAllRef.current ? "pointer" : "nwse-resize";
          hover = d.id;
          break;
        }
        if (hitTest(d, pt.x, pt.y, deps, dims.width, dims.height)) {
          cursor = cursorRef.current === "eraser" ? "not-allowed" : d.locked || lockAllRef.current ? "pointer" : "move";
          hover = d.id;
          break;
        }
      }
    }
    if (hover !== hoverIdRef.current) {
      hoverIdRef.current = hover;
      repaintDrawings();
    }
    if (hoverCursorElRef.current && hoverCursorElRef.current !== target) {
      hoverCursorElRef.current.style.cursor = "";
      hoverCursorElRef.current = null;
    }
    if (cursor && target) {
      target.style.cursor = cursor;
      hoverCursorElRef.current = target;
    } else if (hoverCursorElRef.current) {
      hoverCursorElRef.current.style.cursor = "";
      hoverCursorElRef.current = null;
    }
  }

  function onHostDblClick(e: MouseEvent): void {
    if (pendingRef.current && pointsNeeded(pendingRef.current.type) === "poly") {
      e.preventDefault();
      e.stopPropagation();
      finishPoly();
      return;
    }
    if (drawModeRef.current) return;
    if (performance.now() - polyDoneRef.current < 500) return; // the clicks that just placed a drawing
    const pt = paneCoords(e);
    if (!pt) return;
    const hit = hitDrawingAt(pt.x, pt.y);
    if (!hit) return;
    e.preventDefault();
    e.stopPropagation();
    selectDrawing(hit.id);
    if (TOOL_BY_ID.get(hit.type)?.text) openTextEditor(hit.id);
    else setDialog({ k: "drawSettings", id: hit.id });
  }

  function openTextEditor(id: string): void {
    const d = drawingsRef.current.find((x) => x.id === id);
    if (!d) return;
    const dims = paneDims();
    let x: number | null;
    let y: number | null;
    if (d.screen) {
      x = d.screen.x * dims.width;
      y = d.screen.y * dims.height;
    } else {
      const p = d.type === "callout" && d.points[1] ? d.points[1] : d.points[0];
      x = timeToX(p.time);
      y = priceToY(p.price);
    }
    if (x === null || y === null) return;
    const draft = { id, x: x + paneLeft(), y, value: d.text ?? "" };
    textDraftRef.current = draft;
    setTextDraft(draft);
  }

  const textDraftRef = useRef(textDraft);
  textDraftRef.current = textDraft;

  function commitTextDraft(): void {
    if (!textDraft) return;
    const v = textDraft.value;
    const d = drawingsRef.current.find((x) => x.id === textDraft.id);
    if (d && v.trim() && v !== d.text) updateDrawing(d.id, { text: v.slice(0, 2000) }, "edit text");
    setTextDraft(null);
  }

  function scrollBars(n: number): void {
    const ts = safe(() => chartRef.current!.timeScale());
    const r = safe(() => ts?.getVisibleLogicalRange());
    if (ts && r) safe(() => ts.setVisibleLogicalRange({ from: (r.from + n) as Logical, to: (r.to + n) as Logical }));
  }

  function zoomBy(f: number): void {
    const ts = safe(() => chartRef.current!.timeScale());
    const r = safe(() => ts?.getVisibleLogicalRange());
    if (!ts || !r) return;
    const span = Math.max(5, (r.to - r.from) / f);
    safe(() => ts.setVisibleLogicalRange({ from: (r.to - span) as Logical, to: r.to as Logical }));
  }

  /** Newest bar at the right edge, `bars` wide. */
  function showLatest(bars?: number): void {
    const ts = safe(() => chartRef.current!.timeScale());
    const n = viewRef.current.length;
    if (!ts || n === 0) return;
    const visible = safe(() => ts.getVisibleLogicalRange());
    const span = bars ?? (visible ? visible.to - visible.from : 90);
    safe(() => ts.setVisibleLogicalRange({ from: (n - span) as Logical, to: (n + settingsRef.current.marginRight - 1) as Logical }));
  }

  function resetView(): void {
    pushUndo("reset scales", { ...snapshot("reset scales"), view: viewState() });
    setAutoScale(true);
    safe(() => mainRef.current?.priceScale().applyOptions({ autoScale: true }));
    showLatest(90);
  }

  function onKeyDown(e: KeyboardEvent): void {
    const target = e.target as HTMLElement | null;
    const typing = !!target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT" || target.isContentEditable);
    if (typing || dialogRef.current) return;
    const mine = chartFocus.hover ? chartFocus.hover === myId : chromeRef.current;
    if (!mine) return;
    const ctrl = e.ctrlKey || e.metaKey;
    const letter = e.code?.startsWith("Key") ? e.code.slice(3).toLowerCase() : e.key.toLowerCase();
    // TradingView's price shortcuts, for the chart under the mouse.
    const hp = hoveredRef.current ? plusPriceRef.current : null;
    if (hp != null && !ctrl) {
      if (e.altKey && !e.shiftKey && letter === "a") { e.preventDefault(); addAlert(hp); return; }
      if (e.altKey && !e.shiftKey && letter === "h") { e.preventDefault(); addHLine(hp); return; }
      if (!e.altKey && e.shiftKey && letter === "t" && onPickPrice) { e.preventDefault(); onPickPrice(hp); return; }
      if (e.altKey && e.shiftKey && letter === "b" && onChartOrder) {
        e.preventDefault();
        const o = ctxOrders(hp)[0];
        const w = containerRef.current?.clientWidth ?? 600;
        pickOrder(o, { x: w - 300, y: parseFloat(plusRef.current?.style.top || "40"), price: hp });
        return;
      }
    }
    if (ctrl && !e.altKey) {
      if (letter === "z" && !e.shiftKey) { e.preventDefault(); undo(); return; }
      if (letter === "y" || (letter === "z" && e.shiftKey)) { e.preventDefault(); redo(); return; }
      if (letter === "c" && selectedIdRef.current) {
        clipboardRef.current = drawingsRef.current.find((d) => d.id === selectedIdRef.current) ?? null;
        if (clipboardRef.current) addToast("drawing copied");
        return;
      }
      if (letter === "v" && clipboardRef.current) { e.preventDefault(); pasteDrawing(); return; }
      if (letter === "k") { e.preventDefault(); setDialog({ k: "palette" }); return; }
      if (letter === "s" && e.shiftKey) { e.preventDefault(); void takeSnapshot("copy"); return; }
      if (letter === "s") { e.preventDefault(); onAction?.("save"); return; }
    }
    if (e.altKey && e.shiftKey && !ctrl && letter === "r") { e.preventDefault(); toggleDraw("rect"); return; }
    if (ctrl && e.altKey && letter === "s") { e.preventDefault(); void takeSnapshot("download"); return; }
    if (e.altKey && !ctrl && !e.shiftKey) {
      const map: Record<string, () => void> = {
        t: () => toggleDraw("trend"),
        h: () => toggleDraw("hline"),
        v: () => toggleDraw("vline"),
        j: () => toggleDraw("hray"),
        c: () => toggleDraw("cross"),
        f: () => toggleDraw("fib"),
        i: () => setInvert((v) => !v),
        l: () => setScaleMode((m) => (m === "log" ? "normal" : "log")),
        p: () => setScaleMode((m) => (m === "percent" ? "normal" : "percent")),
        r: () => resetView(),
        a: () => openAlertDialog(),
        w: () => onAction?.("watch:add"),
        s: () => void takeSnapshot("link"),
      };
      if (e.key === "Enter") { e.preventDefault(); onAction?.("maximize"); return; }
      if (map[letter]) { e.preventDefault(); map[letter](); return; }
    }
    if (e.shiftKey && !ctrl && !e.altKey && letter === "f") { e.preventDefault(); toggleFullscreen(); return; }
    if (e.key === "/" && !ctrl && !e.altKey) { e.preventDefault(); setDialog({ k: "indicators" }); return; }
    if (e.key === "?" && !ctrl && !e.altKey) { e.preventDefault(); setDialog({ k: "shortcuts" }); return; }
    if (e.key === "Escape") {
      if (freehandRef.current) {
        freehandRef.current = null;
        pendingRef.current = null;
        repaintDrawings();
        return;
      }
      const drag = dragRef.current;
      if (drag) {
        // abort the drag — put the drawing back
        drawingsRef.current = drawingsRef.current.map((d) => (d.id === drag.id ? drag.orig : d));
        dragRef.current = null;
        repaintDrawings();
        return;
      }
      if (replayRef.current.selecting) { stopReplay(); return; }
      if (drawModeRef.current || pendingRef.current) setDrawMode(null);
      else if (selectedIdRef.current) selectDrawing(null);
      else setMenuOpen(null);
      return;
    }
    if (e.key === "Enter" && pendingRef.current) { finishPoly(); return; }
    if ((e.key === "Delete" || e.key === "Backspace") && selectedIdRef.current) {
      const d = drawingsRef.current.find((x) => x.id === selectedIdRef.current);
      if (d && !d.locked && !lockAllRef.current) deleteDrawing(d.id);
      return;
    }
    if (hoveredRef.current && !ctrl && !e.altKey) {
      if (e.key === "ArrowLeft") { e.preventDefault(); scrollBars(e.shiftKey ? -10 : -1); return; }
      if (e.key === "ArrowRight") { e.preventDefault(); scrollBars(e.shiftKey ? 10 : 1); return; }
      if (!e.shiftKey && e.key === "ArrowUp") { e.preventDefault(); zoomBy(1.25); return; }
      if (!e.shiftKey && e.key === "ArrowDown") { e.preventDefault(); zoomBy(0.8); return; }
    }
    // Typing starts a symbol search — not while a note is being edited or
    // straight after placing a drawing (its text editor is opening).
    const editing = textDraftRef.current !== null || performance.now() - polyDoneRef.current < 400;
    if (!ctrl && !e.altKey && !e.shiftKey && e.key.length === 1 && !editing) {
      if (/^[a-z]$/i.test(e.key) && onSymbolChange) {
        e.preventDefault();
        setDialog({ k: "symbol", q: e.key });
        return;
      }
      if (/^[0-9,]$/.test(e.key)) {
        e.preventDefault();
        setDialog({ k: "interval", txt: e.key === "," ? "" : e.key });
      }
    }
  }

  // Chart subscriptions are attached once at mount; they call through
  // this ref so they always run the latest render's closures.
  /** Touch: one finger draws / drags drawings like the mouse when a tool
   *  is active or a drawing is under the finger; two fingers moving
   *  together pan the time axis (spreading them is left to the engine's
   *  pinch zoom). Everything else goes to the engine untouched. */
  const touchRef = useRef<{ mode: "mouse" | "pan" | null; x: number; dist: number }>({ mode: null, x: 0, dist: 0 });
  function onTouch(e: TouchEvent): void {
    const t0 = e.touches[0] ?? e.changedTouches[0];
    if (!t0) return;
    const asMouse = (t: Touch) =>
      ({ clientX: t.clientX, clientY: t.clientY, button: 0, target: t.target, shiftKey: false, ctrlKey: false, metaKey: false, altKey: false, preventDefault: () => e.preventDefault(), stopPropagation: () => e.stopPropagation() }) as unknown as MouseEvent;
    const st = touchRef.current;
    if (e.type === "touchstart") {
      if (e.touches.length === 2) {
        const [a, b] = [e.touches[0], e.touches[1]];
        touchRef.current = { mode: "pan", x: (a.clientX + b.clientX) / 2, dist: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) };
        return;
      }
      if (e.touches.length !== 1) return;
      const pt = paneCoords(asMouse(t0));
      const grabbing = pt && !drawModeRef.current && hitDrawingAt(pt.x, pt.y);
      if (!drawModeRef.current && !grabbing && !pendingRef.current) {
        touchRef.current = { mode: null, x: 0, dist: 0 };
        return;
      }
      touchRef.current = { mode: "mouse", x: 0, dist: 0 };
      e.preventDefault();
      e.stopPropagation();
      onHostMouseDown(asMouse(t0));
      onWindowMouseMove(asMouse(t0));
      return;
    }
    if (e.type === "touchmove") {
      if (st.mode === "mouse") {
        e.preventDefault();
        e.stopPropagation();
        onWindowMouseMove(asMouse(t0));
        return;
      }
      if (st.mode === "pan" && e.touches.length === 2) {
        const [a, b] = [e.touches[0], e.touches[1]];
        const x = (a.clientX + b.clientX) / 2;
        const dist = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
        if (Math.abs(dist - st.dist) > st.dist * 0.12) return; // a pinch: the engine zooms
        e.preventDefault();
        e.stopPropagation();
        const ts = safe(() => chartRef.current!.timeScale());
        const pos = safe(() => ts!.scrollPosition(), 0) ?? 0;
        safe(() => ts!.scrollToPosition(pos - (x - st.x) / Math.max(1, barSpacing()), false));
        touchRef.current = { mode: "pan", x, dist };
      }
      return;
    }
    // touchend / touchcancel
    if (st.mode === "mouse") {
      e.preventDefault();
      e.stopPropagation();
      const t = e.changedTouches[0];
      if (t) {
        onHostMouseUp(asMouse(t));
        onWindowMouseUp();
      }
    }
    if (e.touches.length === 0) touchRef.current = { mode: null, x: 0, dist: 0 };
  }

  const implRef = useRef({ onCrosshair, onVisibleRange, onKeyDown, onHostMouseDown, onHostMouseUp, onWindowMouseMove, onWindowMouseUp, onHostDblClick, onTouch });
  implRef.current = { onCrosshair, onVisibleRange, onKeyDown, onHostMouseDown, onHostMouseUp, onWindowMouseMove, onWindowMouseUp, onHostDblClick, onTouch };

  // ------------------------------------------------------------------
  // Effects
  // ------------------------------------------------------------------

  const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  function timeFormatter(t: Time): string {
    const s = settingsRef.current;
    const w = wallClock(t as number, s.timezone);
    const date = `${DOW[w.dow]} ${formatDate(w, s.dateFormat)}`;
    return isIntraday(ivRef.current) ? `${date} ${formatClock(w, s.hour12, parseInterval(ivRef.current)?.unit === "S")}` : date;
  }
  function tickFormatter(t: Time, type: number): string {
    const s = settingsRef.current;
    const w = wallClock(t as number, s.timezone);
    switch (type) {
      case 0: return String(w.y);
      case 1: return monthName(w.mo);
      case 2: return String(w.d);
      case 4: return formatClock(w, s.hour12, true);
      default: return formatClock(w, s.hour12);
    }
  }

  // 1) Create the chart once.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const colors = colorsRef.current;
    let chart: IChartApi;
    try {
      chart = createChart(el, {
        autoSize: true,
        layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: colors.text, fontSize: 11, attributionLogo: false },
        grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
        crosshair: {
          mode: CrosshairMode.Normal,
          vertLine: { color: colors.crosshair, labelBackgroundColor: colors.border },
          horzLine: { color: colors.crosshair, labelBackgroundColor: colors.border },
        },
        rightPriceScale: { borderColor: colors.border },
        timeScale: { borderColor: colors.border, timeVisible: true, secondsVisible: false, rightOffset: 5, barSpacing: 8 },
        localization: { locale: "en-IN" },
      });
    } catch {
      setStatus({ kind: "error", message: "chart engine failed to start in this browser" });
      return;
    }
    chartRef.current = chart;
    const moveH = (p: MouseEventParams) => implRef.current.onCrosshair(p);
    const rangeH = (r: LogicalRange | null) => implRef.current.onVisibleRange(r);
    const keyH = (e: KeyboardEvent) => implRef.current.onKeyDown(e);
    const downH = (e: MouseEvent) => implRef.current.onHostMouseDown(e);
    const upH = (e: MouseEvent) => implRef.current.onHostMouseUp(e);
    const dblH = (e: MouseEvent) => implRef.current.onHostDblClick(e);
    const winMoveH = (e: MouseEvent) => implRef.current.onWindowMouseMove(e);
    const winUpH = () => implRef.current.onWindowMouseUp();
    const touchH = (e: TouchEvent) => implRef.current.onTouch(e);
    safe(() => chart.subscribeCrosshairMove(moveH));
    safe(() => chart.timeScale().subscribeVisibleLogicalRangeChange(rangeH));
    window.addEventListener("keydown", keyH);
    // Capture phase so a grab on a drawing wins over the chart's pan.
    el.addEventListener("mousedown", downH, true);
    el.addEventListener("mouseup", upH, true);
    el.addEventListener("dblclick", dblH, true);
    for (const ev of ["touchstart", "touchmove", "touchend", "touchcancel"]) el.addEventListener(ev, touchH as EventListener, { capture: true, passive: false });
    window.addEventListener("mousemove", winMoveH);
    window.addEventListener("mouseup", winUpH);
    return () => {
      fetchSeqRef.current++; // invalidate in-flight fetches
      window.removeEventListener("keydown", keyH);
      el.removeEventListener("mousedown", downH, true);
      el.removeEventListener("mouseup", upH, true);
      el.removeEventListener("dblclick", dblH, true);
      for (const ev of ["touchstart", "touchmove", "touchend", "touchcancel"]) el.removeEventListener(ev, touchH as EventListener, true);
      window.removeEventListener("mousemove", winMoveH);
      window.removeEventListener("mouseup", winUpH);
      window.clearTimeout(indRefreshTimer.current);
      window.clearTimeout(brickTimer.current);
      dragRef.current = null;
      safe(() => chart.unsubscribeCrosshairMove(moveH));
      safe(() => chart.timeScale().unsubscribeVisibleLogicalRangeChange(rangeH));
      safe(() => chart.remove());
      chartRef.current = null;
      mainRef.current = null;
      indEntriesRef.current = [];
      compareSeriesRef.current = new Map();
      alertLinesRef.current = new Map();
      extraLinesRef.current = new Map();
      primitiveRef.current = null;
      if (chartFocus.hover === myId) chartFocus.hover = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 2) (Re)create the main series when the chart type / scale side
  //    changes, and keep the drawings primitive attached to it.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    if (mainRef.current) {
      safe(() => chart.removeSeries(mainRef.current!));
      mainRef.current = null;
    }
    // price lines and markers died with the series
    alertLinesRef.current = new Map();
    extraLinesRef.current = new Map();
    brokerLinesRef.current = [];
    posLinesRef.current = {};
    markersRef.current = null;
    const colors = colorsRef.current;
    const common = { priceScaleId: side };
    let series: ISeriesApi<SeriesType> | undefined;
    const mode = CUSTOM_MODE[chartKind];
    if (mode) {
      series = safe(() => chart.addCustomSeries(new ShapeSeries(), { ...common, mode, upColor: colors.up, downColor: colors.down, box: 1 } as never) as unknown as ISeriesApi<SeriesType>);
      if (!series) series = safe(() => chart.addSeries(CandlestickSeries, common) as ISeriesApi<SeriesType>);
    } else if (chartKind === "bars") series = safe(() => chart.addSeries(BarSeries, { ...common, thinBars: false }) as ISeriesApi<SeriesType>);
    else if (chartKind === "line" || chartKind === "linemarkers" || chartKind === "step") series = safe(() => chart.addSeries(LineSeries, common) as ISeriesApi<SeriesType>);
    else if (chartKind === "area") series = safe(() => chart.addSeries(AreaSeries, common) as ISeriesApi<SeriesType>);
    else if (chartKind === "baseline") series = safe(() => chart.addSeries(BaselineSeries, common) as ISeriesApi<SeriesType>);
    else if (chartKind === "columns") series = safe(() => chart.addSeries(HistogramSeries, common) as ISeriesApi<SeriesType>);
    else series = safe(() => chart.addSeries(CandlestickSeries, common) as ISeriesApi<SeriesType>);
    if (!series) return;
    mainRef.current = series;
    if (!primitiveRef.current) primitiveRef.current = new DrawingsPrimitive(drawingDeps());
    safe(() => series!.attachPrimitive(primitiveRef.current!));
    applyMainStyle();
    buildView();
    setMainData();
    refreshIndicatorData();
    legendLastBar();
    updateExtraLines();
    setMainVer((v) => v + 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chartKind, side]);

  /** The symbol label should show the raw price while the scale shows % / indexed values. */
  function rawPriceLabelOn(): boolean {
    const s = settingsRef.current;
    return s.lastPriceLabel && !s.lastPriceScaleValue && (scaleModeRef.current === "percent" || scaleModeRef.current === "indexed" || comparesLenRef.current > 0);
  }

  function applyMainStyle(): void {
    const main = mainRef.current;
    if (!main) return;
    const s = settingsRef.current;
    const c = colorsRef.current;
    const k = kindRef.current;
    const clear = "rgba(0,0,0,0)";
    const prec = s.precision ?? precision;
    // "Value" (not "according to scale") in % / indexed modes: our own raw-price label replaces the axis one.
    const rawLabel = rawPriceLabelOn();
    const opts: Record<string, unknown> = {
      priceLineVisible: s.lastPriceLine,
      lastValueVisible: s.lastPriceLabel && !rawLabel,
      title: s.symbolNameLabel ? shortName : "",
      priceFormat: { type: "price", precision: prec, minMove: 10 ** -prec },
      priceLineColor: s.lastPriceColor || "",
    };
    if (["candles", "hollow", "heikin", "renko", "linebreak", "range"].includes(k)) {
      Object.assign(opts, {
        upColor: s.bodyOn && k !== "hollow" ? c.up : clear,
        downColor: s.bodyOn ? c.down : clear,
        borderVisible: s.borderOn || k === "hollow",
        borderUpColor: s.borderUp || c.up,
        borderDownColor: s.borderDown || c.down,
        wickVisible: s.wickOn,
        wickUpColor: s.wickUp || withAlpha(c.up, 0.8, c.up),
        wickDownColor: s.wickDown || withAlpha(c.down, 0.8, c.down),
      });
    } else if (k === "bars") Object.assign(opts, { upColor: c.up, downColor: c.down });
    else if (k === "line" || k === "linemarkers" || k === "step") Object.assign(opts, { color: c.accent, lineWidth: 2, pointMarkersVisible: k === "linemarkers", lineType: k === "step" ? 1 : 0 });
    else if (k === "area") Object.assign(opts, { lineColor: c.accent, lineWidth: 2, topColor: withAlpha(c.accent, 0.25, "rgba(255,140,0,0.25)"), bottomColor: withAlpha(c.accent, 0.03, "rgba(255,140,0,0.03)") });
    else if (k === "baseline") Object.assign(opts, {
      topLineColor: c.up, topFillColor1: withAlpha(c.up, 0.25, c.up), topFillColor2: withAlpha(c.up, 0.03, c.up),
      bottomLineColor: c.down, bottomFillColor1: withAlpha(c.down, 0.03, c.down), bottomFillColor2: withAlpha(c.down, 0.25, c.down), lineWidth: 2,
    });
    else if (k === "columns") Object.assign(opts, { color: c.up });
    else if (CUSTOM_MODE[k]) Object.assign(opts, { upColor: c.up, downColor: c.down });
    safe(() => main.applyOptions(opts));
  }

  useEffect(() => {
    applyMainStyle(); // the raw-price label swaps in / out with the scale mode
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scaleMode, pctCompare]);

  // 2b) Main-series style + chart canvas / scales from the settings.
  useEffect(() => {
    applyMainStyle();
    if (BRICK_KINDS.has(kindRef.current)) applyData();
    else setMainData();
    updateExtraLines();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings, theme, mainVer, precision]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const s = settings;
    const c = theme;
    const light = s.theme === "light";
    const bg1 = s.bg1 || (light ? LIGHT.bg : "transparent");
    const background =
      s.bgType === "gradient"
        ? { type: ColorType.VerticalGradient, topColor: s.bg1 || c.bg, bottomColor: s.bg2 || c.bg }
        : { type: ColorType.Solid, color: bg1 };
    const lines = cursor === "cross";
    const chColor = s.crosshairColor || c.crosshair;
    const line = { visible: lines, color: chColor, width: s.crosshairWidth, style: s.crosshairStyle, labelBackgroundColor: light ? "#131722" : c.border };
    const border = s.scaleLineColor || c.border;
    safe(() =>
      chart.applyOptions({
        layout: { background: background as never, textColor: s.textColor || c.text, fontSize: s.fontSize },
        grid: {
          vertLines: { visible: s.grid === "both" || s.grid === "vert", color: s.gridColor || c.grid },
          horzLines: { visible: s.grid === "both" || s.grid === "horz", color: s.gridColor || c.grid },
        },
        crosshair: { mode: magnet === "strong" ? (3 as CrosshairMode) : magnet === "weak" ? CrosshairMode.Magnet : CrosshairMode.Normal, vertLine: line as never, horzLine: line as never },
        // the opposite axis also shows while a compare sits on "New price scale"
        rightPriceScale: { visible: s.scaleSide === "right" || cmpOtherSide === "right", borderColor: border, alignLabels: s.noOverlapLabels },
        leftPriceScale: { visible: s.scaleSide === "left" || cmpOtherSide === "left", borderColor: border, alignLabels: s.noOverlapLabels },
        timeScale: {
          borderColor: border,
          rightOffset: s.marginRight,
          timeVisible: isIntraday(iv),
          secondsVisible: parseInterval(iv)?.unit === "S",
          tickMarkFormatter: (t: Time, type: number) => tickFormatter(t, type),
        },
        localization: { locale: "en-IN", timeFormatter: (t: Time) => timeFormatter(t) },
      }),
    );
    if (cmpOtherSide) safe(() => chart.priceScale(cmpOtherSide).applyOptions({ mode: PriceScaleMode.Normal, autoScale: true }));
    setTimeout(measurePanes, 30);
    repaintDrawings();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings, theme, cursor, magnet, iv, cmpOtherSide]);

  // 2c) Price scale: mode (compare forces percentage), auto, invert, margins.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const mode =
      pctCompare
        ? PriceScaleMode.Percentage
        : scaleMode === "log"
          ? PriceScaleMode.Logarithmic
          : scaleMode === "percent"
            ? PriceScaleMode.Percentage
            : scaleMode === "indexed"
              ? PriceScaleMode.IndexedTo100
              : PriceScaleMode.Normal;
    safe(() =>
      chart.priceScale(side).applyOptions({
        mode,
        autoScale,
        invertScale: invert,
        scaleMargins: { top: settings.marginTop / 100, bottom: settings.marginBottom / 100 },
      }),
    );
    repaintDrawings();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pctCompare, scaleMode, autoScale, invert, side, settings.marginTop, settings.marginBottom, mainVer]);


  // 3) Load candles when the symbol / interval changes (the parent also
  //    remounts with a new `key` per symbol).
  useEffect(() => {
    const seq = ++fetchSeqRef.current;
    candlesRef.current = [];
    viewRef.current = [];
    otherDataRef.current = new Map(); // other symbols' bars follow the interval
    haveMoreRef.current = true;
    loadingOlderRef.current = null;
    setDrawMode(null);
    setStatus({ kind: "loading" });
    if (replayRef.current.on) setReplay({ on: false, selecting: false, playing: false, speed: replayRef.current.speed, idx: 0 });
    const pl = fetchPlan(iv);
    const now = Math.floor(Date.now() / 1000);
    void (async () => {
      const r = await fetchHistory(symbol, pl.res, dayStart(now - (pl.initialDays - 1) * 86400), now);
      if (seq !== fetchSeqRef.current) return;
      candlesRef.current = pl.aggregate ? (aggregate(r.candles, iv) as Candle[]) : r.candles;
      if (r.reason) setStatus({ kind: "error", message: r.reason });
      else if (r.candles.length === 0) setStatus({ kind: "empty" });
      else setStatus({ kind: "ready" });
      statusRef.current = r.reason ? "error" : r.candles.length ? "ready" : "empty";
      applyData();
      // Position the newest bar at the right edge. setVisibleLogicalRange
      // is deterministic even with a market-closed gap (scrollToRealTime
      // targets wall-clock "now", which would leave the last bar off-screen
      // on a weekend / after hours).
      const n = viewRef.current.length;
      if (n > 0) {
        safe(() => chartRef.current?.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 90) as Logical, to: (n + settingsRef.current.marginRight - 1) as Logical }));
        atLiveRef.current = true;
        setAtLive(true);
      }
      if (pendingRangeRef.current) void applyPendingRange();
      // One silent retry per symbol|interval — the first fetch right after
      // a backend restart can fail transiently while the Fyers client warms up.
      const key = `${symbol}|${iv}`;
      if ((r.reason || r.candles.length === 0) && autoRetriedKeyRef.current !== key) {
        autoRetriedKeyRef.current = key;
        setTimeout(() => {
          if (seq === fetchSeqRef.current) setReloadNonce((x) => x + 1);
        }, 4000);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol, iv, reloadNonce]);

  // 4) Rebuild indicator series when the instances / their display change.
  useEffect(() => {
    rebuildIndicators();
    legendLastBar();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [indicators, hide.indicators, iv, side, settings.indNameLabels, settings.indValueLabels, settings.precision, scalePriceOnly, theme]);

  // 4a) Pane maximize / collapse.
  useEffect(() => {
    applyPaneSizes();
    const t = setTimeout(measurePanes, 30);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [paneMode]);

  // 4a') Pane overlays follow resizes.
  useEffect(() => {
    const el = containerRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    let t: number | undefined;
    const ro = new ResizeObserver(() => {
      window.clearTimeout(t);
      t = window.setTimeout(measurePanes, 60);
    });
    ro.observe(el);
    return () => {
      ro.disconnect();
      window.clearTimeout(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 4b) Real order flow from the tick recorder, polled while it is on.
  const flowOn = indicators.some((i) => i.type === "flow" && i.visible);
  useEffect(() => {
    if (!flowOn) {
      flowRef.current = new Map();
      setFlowNote("");
      return;
    }
    let stop = false;
    const load = async () => {
      const c = candlesRef.current;
      const now = Math.floor(Date.now() / 1000);
      const from = c.length ? c[0].time - IST : now - 5 * 86400;
      const res = fetchPlan(ivRef.current).res;
      try {
        const r = await fetch(`/api/algo/ticks/flow?symbol=${encodeURIComponent(symbol)}&resolution=${res}&from=${from}&to=${now + 60}`);
        const j = await r.json();
        if (stop) return;
        flowRef.current = new Map((j.bars ?? []).map((b: number[]) => [b[0] + IST, [b[1], b[2], b[3]] as [number, number, number]]));
        setFlowNote(j.recorded ? `real flow: ${j.key}` : `no ticks recorded for ${j.key} yet — add it on Algo Lab › Data`);
        refreshIndicatorData();
      } catch {
        if (!stop) setFlowNote("order-flow fetch failed");
      }
    };
    void load();
    const id = setInterval(load, 15000);
    return () => {
      stop = true;
      clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flowOn, symbol, iv, reloadNonce]);

  // 4c) Corporate events (dividends / splits / earnings / bonus) from the
  //     filed announcements.
  useEffect(() => {
    if (!settings.showEvents) {
      setEvents([]);
      return;
    }
    let stop = false;
    const tk = shortName.split(/\s+/)[0].toUpperCase();
    void fetch(`/api/announcements/recent?symbol=${encodeURIComponent(tk)}&limit=200`)
      .then((r) => (r.ok ? r.json() : []))
      .then((rows: unknown) => {
        if (stop || !Array.isArray(rows)) return;
        const out: ChartEvent[] = [];
        for (const row of rows as { headline?: string; filed_at?: string | null; received_at?: string }[]) {
          const kind = classifyEvent(row.headline ?? "");
          const ts = Date.parse(row.filed_at ?? row.received_at ?? "");
          if (kind && Number.isFinite(ts)) out.push({ time: Math.floor(ts / 1000) + IST, kind, text: row.headline ?? "" });
        }
        setEvents(out);
      })
      .catch(() => undefined);
    return () => {
      stop = true;
    };
  }, [symbol, shortName, settings.showEvents]);

  // 4c') Executions: the bot's fills on this symbol from the trade book,
  //      refetched when the position changes or any order update arrives.
  const [executions, setExecutions] = useState<{ time: number; side: "BUY" | "SELL"; qty: number; price: number }[]>([]);
  const [execNonce, setExecNonce] = useState(0);
  useEffect(() => {
    const on = () => setExecNonce((n) => n + 1);
    window.addEventListener("broker:order", on);
    return () => window.removeEventListener("broker:order", on);
  }, []);
  useEffect(() => {
    if (!settings.executions) {
      setExecutions([]);
      return;
    }
    let stop = false;
    void fetch("/api/trades?limit=200")
      .then((r) => (r.ok ? r.json() : []))
      .then((rows: unknown) => {
        if (stop || !Array.isArray(rows)) return;
        const out: { time: number; side: "BUY" | "SELL"; qty: number; price: number }[] = [];
        for (const t of rows as { symbol?: string; side?: string; quantity?: number; price?: number; status?: string; executed_at?: string | null }[]) {
          if ((t.symbol ?? "").toUpperCase() !== symbol.toUpperCase() || !t.executed_at || (t.status ?? "").toLowerCase() !== "filled") continue;
          const ts = Date.parse(t.executed_at);
          if (!Number.isFinite(ts) || t.price == null) continue;
          out.push({ time: Math.floor(ts / 1000) + IST, side: t.side === "SELL" ? "SELL" : "BUY", qty: Number(t.quantity ?? 0), price: Number(t.price) });
        }
        setExecutions(out);
      })
      .catch(() => undefined);
    return () => {
      stop = true;
    };
  }, [symbol, settings.executions, execNonce]);

  // A fill moves the position: that is an execution (sound + log), not the order acceptance.
  const lastQtyRef = useRef<number | null>(null);
  useEffect(() => {
    const q = position?.qty ?? 0;
    const prev = lastQtyRef.current;
    lastQtyRef.current = q;
    if (prev === null || prev === q) return;
    if (settingsRef.current.sound) beep();
    pushLog("order", `execution — ${shortName} position ${prev} → ${q}`);
    setExecNonce((n) => n + 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [position?.qty]);

  // 4d) Marks on bars: strategy trades + events (re-attached if the main
  //     series is rebuilt).
  useEffect(() => {
    safe(() => markersRef.current?.detach());
    markersRef.current = null;
    eventsByTimeRef.current = new Map();
    const main = mainRef.current;
    const c = viewRef.current;
    if (!main || !c.length || !settings.showMarks) return;
    const times = c.map((k) => k.time);
    const snapT = (x: number) => {
      let lo = 0, hi = times.length - 1;
      while (lo < hi) {
        const m = (lo + hi + 1) >> 1;
        if (times[m] <= x) lo = m;
        else hi = m - 1;
      }
      return times[lo];
    };
    const col = colorsRef.current;
    const ms: { time: UTCTimestamp; position: "aboveBar" | "belowBar"; color: string; shape: "arrowUp" | "arrowDown" | "circle"; text: string }[] = [];
    for (const t of strat?.trades ?? []) {
      if (t.entry_t + IST < times[0]) continue;
      const buy = t.side === "BUY";
      ms.push({ time: snapT(t.entry_t + IST) as UTCTimestamp, position: buy ? "belowBar" : "aboveBar", color: buy ? col.up : col.down, shape: buy ? "arrowUp" : "arrowDown", text: buy ? "L" : "S" });
      ms.push({ time: snapT(t.exit_t + IST) as UTCTimestamp, position: buy ? "aboveBar" : "belowBar", color: t.net >= 0 ? col.up : col.down, shape: "circle", text: `${t.net >= 0 ? "+" : ""}${Math.round(t.net)}` });
    }
    for (const x of executions) {
      if (x.time < times[0]) continue;
      const buy = x.side === "BUY";
      ms.push({
        time: snapT(x.time) as UTCTimestamp,
        position: buy ? "belowBar" : "aboveBar",
        color: buy ? "#2962FF" : col.down,
        shape: buy ? "arrowUp" : "arrowDown",
        text: settings.executionLabels ? `${buy ? "B" : "S"} ${x.qty} @ ${fmtNum(x.price, 2)}` : "",
      });
    }
    const evColor: Record<ChartEvent["kind"], string> = { D: "#F23645", S: "#2962FF", E: "#FF9800", B: "#9C27B0" };
    for (const ev of events) {
      if (ev.time < times[0]) continue;
      const t = snapT(ev.time);
      ms.push({ time: t as UTCTimestamp, position: "belowBar", color: evColor[ev.kind], shape: "circle", text: ev.kind });
      eventsByTimeRef.current.set(t, [...(eventsByTimeRef.current.get(t) ?? []), ev]);
    }
    if (!ms.length) return;
    ms.sort((a, b) => a.time - b.time);
    markersRef.current = (safe(() => createSeriesMarkers(main, ms as never[])) as unknown as typeof markersRef.current) ?? null;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [strat, events, executions, settings.showMarks, settings.executionLabels, mainVer, dataVer]);

  async function runStrategy(s: StrategyItem): Promise<void> {
    const c = candlesRef.current;
    const now = Math.floor(Date.now() / 1000);
    const from = c.length ? c[0].time - IST : now - 30 * 86400;
    const ymd = (t: number) => new Date((t + IST) * 1000).toISOString().slice(0, 10);
    setStrat({ name: s.name, trades: [], stats: {}, running: true });
    try {
      const r = await fetch("/api/algo/backtest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ spec: { ...s.spec, symbols: [symbol] }, start: ymd(Math.min(from, now - 7 * 86400)), end: ymd(now) }),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail));
      setStrat({ name: s.name, trades: j.trades ?? [], stats: j.stats ?? {} });
    } catch (e) {
      setStrat({ name: s.name, trades: [], stats: {}, error: e instanceof Error ? e.message : String(e) });
    }
  }

  // 5) Alert price lines (value alerts), recreated with the main series.
  useEffect(() => {
    const main = mainRef.current;
    if (!main) return;
    for (const [, line] of alertLinesRef.current) safe(() => main.removePriceLine(line));
    const map = new Map<string, IPriceLine>();
    if (settings.alertLines) {
      const color = settings.alertColor || colorsRef.current.draw;
      for (const a of alerts) {
        if (a.target !== "value") continue;
        const values = a.value2 != null && (a.cond === "enter" || a.cond === "exit") ? [a.value, a.value2] : [a.value];
        values.forEach((v, i) => {
          const l = safe(() =>
            main.createPriceLine({ price: v, color: a.active ? color : "#787B86", lineWidth: 1, lineStyle: LineStyle.LargeDashed, axisLabelVisible: true, title: a.active ? "alert" : "alert (off)" }),
          );
          if (l) map.set(`${a.id}:${i}`, l);
        });
      }
    }
    alertLinesRef.current = map;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [alerts, mainVer, settings.alertLines, settings.alertColor]);

  const alertsRef = useRef(alerts);
  alertsRef.current = alerts;
  useEffect(() => {
    saveJson(`chart:alerts:${symbol}`, alerts);
  }, [alerts, symbol]);

  /** Evaluate every alert for a price move (or a bar close). */
  function runAlerts(prev: number, cur: number, barClosed: boolean): void {
    const list = alertsRef.current;
    if (!list.length) return;
    const c = viewRef.current;
    const barTime = c[c.length - 1]?.time ?? 0;
    let changed = false;
    const now = Date.now();
    const next = list.map((a) => {
      if (a.active && a.expires != null && now > a.expires) {
        changed = true;
        return { ...a, active: false };
      }
      const tg = alertTarget(a);
      if (!tg) return a;
      const r = evaluate(a, prev, cur, tg[0], tg[1], barTime, barClosed, now);
      if (!r) return a;
      changed = true;
      fireAlert(r, cur);
      return r;
    });
    if (changed) {
      alertsRef.current = next;
      setAlerts(next);
    }
  }

  // 5b) Broker-state lines: pending orders.
  useEffect(() => {
    const main = mainRef.current;
    if (!main) return;
    for (const line of brokerLinesRef.current) safe(() => main.removePriceLine(line));
    const next: IPriceLine[] = [];
    if (!hide.positions) {
      const colors = colorsRef.current;
      for (const b of brokerLines ?? []) {
        const l = safe(() =>
          main.createPriceLine({
            price: b.price,
            color: b.kind === "order" ? "#4A90D9" : b.kind === "position-long" ? colors.up : colors.down,
            lineWidth: 1,
            lineStyle: b.kind === "order" ? LineStyle.Dashed : LineStyle.Solid,
            lineVisible: settingsRef.current.extendLines,
            axisLabelVisible: true,
            title: b.title,
          }),
        );
        if (l) next.push(l);
      }
    }
    brokerLinesRef.current = next;
  }, [brokerLines, mainVer, hide.positions, settings.extendLines]);

  const showPos = position && !hide.positions ? position : null;

  // 5b') Position line + stop-loss / target lines.
  useEffect(() => {
    const main = mainRef.current;
    const old = posLinesRef.current;
    for (const l of [old.entry, old.sl, old.tp]) if (l && main) safe(() => main.removePriceLine(l));
    posLinesRef.current = {};
    if (!main || !showPos) return;
    const colors = colorsRef.current;
    const mk = (price: number, color: string, style: LineStyle) =>
      safe(() => main.createPriceLine({ price, color, lineWidth: 1, lineStyle: style, lineVisible: settingsRef.current.extendLines, axisLabelVisible: true, title: "" }));
    posLinesRef.current.entry = mk(showPos.avg, showPos.qty > 0 ? "#2962FF" : colors.down, LineStyle.Solid);
    const sl = levelDrag === "sl" ? levelDragRef.current?.price : showPos.sl;
    const tp = levelDrag === "tp" ? levelDragRef.current?.price : showPos.tp;
    if (sl != null) posLinesRef.current.sl = mk(sl, colors.down, LineStyle.Dashed);
    if (tp != null) posLinesRef.current.tp = mk(tp, colors.up, LineStyle.Dashed);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showPos?.qty, showPos?.avg, showPos?.sl, showPos?.tp, levelDrag, mainVer, settings.extendLines]);

  // 5b'') Glue the tags to their lines every frame and tick the live P&L:
  // follows pans, zooms, resizes, ticks and drags without wiring each event.
  useEffect(() => {
    if (!showPos) return;
    let raf = 0;
    const money = (v: number) => (privacy ? "₹•••" : `${v >= 0 ? "+" : "−"}₹${Math.abs(v).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`);
    const frame = () => {
      raf = requestAnimationFrame(frame);
      const main = mainRef.current;
      if (!main || !chartRef.current) return;
      const h = paneDims().height;
      const leftSide = settingsRef.current.ordersAlign === "left";
      const offset = `${(settingsRef.current.scaleSide === "left" ? 0 : scaleWidth("right") || 60) + 8}px`;
      const ltp = prevLtpRef.current ?? lastClose();
      const drag = levelDragRef.current;
      const levels = { entry: showPos.avg, sl: drag?.which === "sl" ? drag.price : showPos.sl, tp: drag?.which === "tp" ? drag.price : showPos.tp };
      const pctMode = settingsRef.current.plMode === "percent";
      const tickMode = settingsRef.current.plMode === "ticks";
      // Desired tops, clamped into the pane, then pushed apart so tags
      // pinned to an edge (or close together) don't sit on each other.
      const want: { k: "entry" | "sl" | "tp"; y: number; top: number }[] = [];
      for (const k of ["entry", "sl", "tp"] as const) {
        const price = levels[k];
        const y = price == null ? null : priceToY(price);
        if (price != null && y != null) want.push({ k, y, top: Math.max(0, Math.min(h - 22, y - 11)) });
      }
      want.sort((a, b) => a.top - b.top);
      for (let i = 1; i < want.length; i++) if (want[i].top < want[i - 1].top + 24) want[i].top = want[i - 1].top + 24;
      for (let i = want.length - 1; i >= 0; i--) {
        if (want[i].top > h - 22) want[i].top = h - 22;
        if (i > 0 && want[i - 1].top > want[i].top - 24) want[i - 1].top = want[i].top - 24;
      }
      for (const k of ["entry", "sl", "tp"] as const) {
        const el = posTagRefs.current[k];
        const price = levels[k];
        if (!el || price == null) continue;
        const slot = want.find((w) => w.k === k);
        const y = slot ? slot.y : null;
        if (y == null || !slot) {
          el.style.visibility = "hidden";
          continue;
        }
        el.style.visibility = "visible";
        if (leftSide) {
          el.style.left = `${paneLeft() + 8}px`;
          el.style.right = "auto";
        } else {
          el.style.right = offset;
          el.style.left = "auto";
        }
        el.style.transform = `translateY(${slot.top}px)`;
        el.classList.toggle("off", y < 0 || y > h); // pinned to the edge while off-screen
        const pnlEl = el.querySelector<HTMLElement>(".pnl");
        const at = k === "entry" ? ltp : price;
        if (pnlEl && at != null) {
          const pnl = (at - showPos.avg) * showPos.qty;
          const pct = ((at - showPos.avg) / showPos.avg) * 100 * Math.sign(showPos.qty);
          const ticks = Math.round(((at - showPos.avg) / tickRef.current) * Math.sign(showPos.qty));
          const main$ = pctMode ? `${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%` : tickMode ? `${ticks >= 0 ? "+" : ""}${ticks} ticks` : money(pnl);
          pnlEl.textContent = k === "entry" ? main$ : `${fmtNum(price, 2)} · ${money(pnl)} (${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%)`;
          pnlEl.className = `pnl ${pnl >= 0 ? "up" : "down"}`;
        }
        if (drag && k === drag.which) safe(() => posLinesRef.current[k]?.applyOptions({ price }));
      }
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showPos?.qty, showPos?.avg, showPos?.sl, showPos?.tp, privacy]);

  // 6) Compare series — create / remove / hide and (re)load on interval change.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const map = compareSeriesRef.current;
    for (const [sym, s] of [...map]) {
      if (!compares.some((c) => c.symbol === sym)) {
        safe(() => chart.removeSeries(s));
        map.delete(sym);
      }
    }
    for (const c of compares) {
      let s = map.get(c.symbol);
      if (!s) {
        s = safe(() => chart.addSeries(LineSeries, { color: c.color, lineWidth: 1, priceScaleId: side, priceLineVisible: false, title: c.name }));
        if (!s) continue;
        map.set(c.symbol, s);
        loadCompareData(c.symbol, s);
      }
      safe(() => s!.applyOptions({ visible: !c.hidden }));
    }
    placeCompares();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [compares, side]);

  useEffect(() => {
    for (const [sym, s] of compareSeriesRef.current) loadCompareData(sym, s);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [iv, status.kind]);

  // 7) Persist chart preferences.
  useEffect(() => {
    saveJson(PREFS_KEY, {
      interval: iv,
      chartKind,
      indicators,
      magnet,
      cursor,
      scaleMode,
      autoScale,
      invert,
      scalePriceOnly,
      favIntervals,
      customIntervals,
      favKinds,
      favTools,
      favIndicators,
      lastTool,
      stay,
      lockAll,
      hide,
      syncDrawings,
      legendCollapsed,
      showFavBar,
      toolsCollapsed,
      pinnedTools,
      mainHidden,
    } satisfies ChartPrefs);
  }, [iv, chartKind, indicators, magnet, cursor, scaleMode, autoScale, invert, scalePriceOnly, favIntervals, customIntervals, favKinds, favTools, favIndicators, lastTool, stay, lockAll, hide, syncDrawings, legendCollapsed, showFavBar, toolsCollapsed, pinnedTools, mainHidden]);

  // User Settings (outside the chart) edited chart:settings — pick them up.
  useEffect(() => {
    const on = () => {
      const s = loadSettings();
      settingsRef.current = s;
      setSettingsState(s);
    };
    window.addEventListener(CHART_SETTINGS_EVENT, on);
    return () => window.removeEventListener(CHART_SETTINGS_EVENT, on);
  }, []);

  // The legend's eye on the main series (and the object tree's) hides it.
  useEffect(() => {
    safe(() => mainRef.current?.applyOptions({ visible: !mainHidden } as never));
  }, [mainHidden, mainVer]);

  // 8) Compare-symbol search (debounced).
  useEffect(() => {
    const q = compareQuery.trim();
    if (q.length < 2) {
      setCompareHits([]);
      return;
    }
    const handle = setTimeout(() => {
      void api
        .get<SearchResponse>(`/api/search/symbols?q=${encodeURIComponent(q)}&limit=8`)
        .then((r) => setCompareHits((r.hits ?? []).filter((h) => h.symbol !== symbol).map((h) => ({ symbol: h.symbol, name: h.short_name }))))
        .catch(() => setCompareHits([]));
    }, 300);
    return () => clearTimeout(handle);
  }, [compareQuery, symbol]);

  // 9) Live last bar from the `/ws` quote stream + alert triggers.
  const live = useLiveQuote(symbol);
  liveQuoteRef.current = live;
  useEffect(() => {
    if (!live || live.last_price == null) return;
    const lp = live.last_price;
    liveRef.current = { bid: live.bid ?? null, ask: live.ask ?? null };
    const prev = prevLtpRef.current;
    prevLtpRef.current = lp;
    const dayVol = live.volume;
    const pv = prevVolRef.current;
    prevVolRef.current = dayVol ?? null;
    // alert crossings (checked even while the chart is still loading)
    if (prev !== null && prev !== lp) runAlerts(prev, lp, false);
    if (statusRef.current !== "ready") return;
    const candles = candlesRef.current;
    if (!mainRef.current || candles.length === 0) return;
    const last = candles[candles.length - 1];
    const parsed = Date.parse(live.ts);
    const t = Math.floor(Number.isFinite(parsed) ? parsed / 1000 : Date.now() / 1000) + IST;
    const b = liveBucket(last, t, ivRef.current);
    if (b.kind === "stale") return;
    if (b.kind === "new") {
      // Only open a NEW bar during plausible NSE hours (Mon–Fri,
      // 09:00–15:40 IST) — quotes echo the last close on weekends and
      // overnight, which would otherwise mint phantom bars.
      const d = new Date(t * 1000); // t is IST-shifted, so read as UTC
      const dow = d.getUTCDay();
      const mins = d.getUTCHours() * 60 + d.getUTCMinutes();
      if (dow === 0 || dow === 6 || mins < 540 || mins > 940) return;
    }
    const dv = dayVol != null && pv != null && dayVol >= pv ? dayVol - pv : 0;
    let rolled = false;
    if (b.kind === "same") {
      last.close = lp;
      if (lp > last.high) last.high = lp;
      if (lp < last.low) last.low = lp;
      last.volume += dv;
    } else {
      candles.push({ time: b.time as UTCTimestamp, open: lp, high: lp, low: lp, close: lp, volume: dv });
      rolled = true;
    }
    if (rolled && candles.length > 2) runAlerts(candles[candles.length - 3].close, candles[candles.length - 2].close, true);
    if (replayRef.current.on) return;
    if (BRICK_KINDS.has(kindRef.current) || viewRef.current !== candles) {
      if (brickTimer.current === undefined) {
        brickTimer.current = window.setTimeout(() => {
          brickTimer.current = undefined;
          applyData();
        }, 1000);
      }
    } else {
      const bar = candles[candles.length - 1];
      if (rolled) indexByTimeRef.current.set(bar.time, candles.length - 1);
      updateMainBar(bar, rolled);
      if (rolled) refreshIndicatorData();
      else scheduleIndicatorRefresh();
    }
    legendLastBar();
    updateExtraLines();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live]);

  // 10) Bar-close countdown under the last-price label (intraday, market hours).
  useEffect(() => {
    const id = setInterval(() => {
      const el = countdownRef.current;
      if (!el) return;
      const candles = viewRef.current;
      const s = settingsRef.current;
      if (!s.countdown || !isIntraday(ivRef.current) || candles.length === 0 || statusRef.current !== "ready" || replayRef.current.on) {
        el.style.display = "none";
        return;
      }
      const interval = barInterval();
      const nowIst = Math.floor(Date.now() / 1000) + IST;
      const d = new Date(nowIst * 1000);
      const dow = d.getUTCDay();
      const mins = d.getUTCHours() * 60 + d.getUTCMinutes();
      const last = candles[candles.length - 1];
      const remaining = last.time + interval - nowIst;
      if (dow === 0 || dow === 6 || mins < 555 || mins > 930 || remaining <= 0 || remaining > interval) {
        el.style.display = "none";
        return;
      }
      const y = priceToY(last.close);
      if (y === null) {
        el.style.display = "none";
        return;
      }
      const hh = Math.floor(remaining / 3600);
      const mm = Math.floor((remaining % 3600) / 60);
      const ss = remaining % 60;
      el.textContent = `${hh ? `${hh}:${String(mm).padStart(2, "0")}` : mm}:${String(ss).padStart(2, "0")}`;
      el.style.display = "block";
      el.style.top = `${y + 10}px`;
      const left = s.scaleSide === "left";
      const w = scaleWidth(left ? "left" : "right") || 56;
      el.style.width = `${w}px`;
      el.style.left = left ? "0px" : "auto";
      el.style.right = left ? "auto" : "0px";
    }, 1000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 10b) Raw last price on the axis while it shows % / indexed values ("Value" mode).
  useEffect(() => {
    const el = rawLabelRef.current;
    if (!el) return;
    if (!rawPriceLabelOn()) {
      el.style.display = "none";
      return;
    }
    let raf = 0;
    const frame = () => {
      raf = requestAnimationFrame(frame);
      const v = viewRef.current;
      const last = v[v.length - 1];
      const y = last ? priceToY(last.close) : null;
      if (!last || y == null) {
        el.style.display = "none";
        return;
      }
      const s = settingsRef.current;
      const left = s.scaleSide === "left";
      el.style.display = "block";
      el.style.top = `${y - 9}px`;
      el.style.width = `${scaleWidth(left ? "left" : "right") || 56}px`;
      el.style.left = left ? "0px" : "auto";
      el.style.right = left ? "auto" : "0px";
      el.style.background = s.lastPriceColor || (last.close >= last.open ? colorsRef.current.up : colorsRef.current.down);
      el.textContent = fmtNum(last.close, s.precision ?? precision);
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings.lastPriceScaleValue, settings.lastPriceLabel, settings.lastPriceColor, scaleMode, pctCompare]);

  function measureToolbar(): void {
    const el = toolbarRef.current;
    if (!el) return;
    const left = el.scrollLeft > 2;
    const right = el.scrollLeft + el.clientWidth < el.scrollWidth - 2;
    setTbScroll((p) => (p.left === left && p.right === right ? p : { left, right }));
  }
  useEffect(() => {
    const el = toolbarRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => measureToolbar());
    ro.observe(el);
    measureToolbar();
    return () => ro.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [toolbarSlot, chrome]);
  // The scroller clips its children, so an open toolbar menu is positioned
  // against the (unclipped) wrapper, under its button.
  function positionToolbarMenu(): void {
    const bar = toolbarRef.current;
    const wrap = bar?.parentElement;
    if (!bar || !wrap) return;
    const menu = bar.querySelector<HTMLElement>(".chart-menu-wrap > .chart-menu");
    const anchor = menu?.parentElement;
    if (!menu || !anchor) return;
    const a = anchor.getBoundingClientRect();
    const cb = wrap.getBoundingClientRect();
    const w = menu.offsetWidth;
    const alignRight = menu.classList.contains("right");
    const left = alignRight ? a.right - cb.left - w : a.left - cb.left;
    menu.style.left = `${Math.max(0, Math.min(left, cb.width - w))}px`;
    menu.style.right = "auto";
    menu.style.top = `${a.bottom - cb.top + 6}px`;
  }
  // (scrolling the bar — e.g. a half-hidden button taking focus — moves the open menu along)
  useLayoutEffect(() => {
    if (menuOpen) positionToolbarMenu();
  }, [menuOpen]);

  // 11) Close menus on outside click; the context menu on any click / Esc.
  useEffect(() => {
    if (!menuOpen) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as HTMLElement | null;
      if (!t?.closest(".chart-menu-wrap")) setMenuOpen(null);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [menuOpen]);

  useEffect(() => {
    setTradeFly(null);
    if (!ctx) return;
    const close = () => setCtx(null);
    const esc = (e: KeyboardEvent) => {
      if (e.key === "Escape") setCtx(null);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
    };
  }, [ctx]);

  useEffect(() => {
    if (!legendMenu) return;
    const close = (e: MouseEvent) => {
      if ((e.target as HTMLElement | null)?.closest?.("[data-more]")) return; // the toggle button handles itself
      setLegendMenu(null);
    };
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setLegendMenu(null);
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
    };
  }, [legendMenu]);

  // 12) Sync with the other charts of the layout.
  useEffect(
    () =>
      subscribeSync((e) => {
        if (e.src === myId) return;
        const s = syncRef.current;
        const chart = chartRef.current;
        if (e.type === "drawings") {
          if (e.symbol === symbol && syncDrawingsRef.current) {
            drawingsRef.current = loadJson<Drawing[]>(`chart:drawings:${symbol}`, []);
            repaintDrawings();
            bumpDrawings();
          }
        } else if (e.type === "daterange") {
          if (!s.dateRange) return;
          const preset = e.preset ? RANGES.find((r) => r.id === e.preset) : undefined;
          if (preset) applyRange(preset, true);
          else goTo({ at: e.at, from: e.from, to: e.to }, true);
        } else if (e.type === "interval") {
          if (s.interval) changeInterval(e.interval, true);
        } else if (e.type === "range") {
          if (s.time && chart) {
            syncApplyRef.current = Date.now();
            safe(() => chart.timeScale().setVisibleRange({ from: e.from as Time, to: e.to as Time }));
          }
        } else if (e.type === "crosshair" && s.crosshair && chart && mainRef.current) {
          if (e.time === null) {
            safe(() => chart.clearCrosshairPosition());
            return;
          }
          const v = viewRef.current;
          if (!v.length) return;
          let lo = 0, hi = v.length - 1;
          while (lo < hi) {
            const m = (lo + hi + 1) >> 1;
            if (v[m].time <= e.time) lo = m;
            else hi = m - 1;
          }
          safe(() => chart.setCrosshairPosition(v[lo].close, v[lo].time as Time, mainRef.current!));
        }
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [symbol, myId],
  );

  // 13) Bar replay playback.
  useEffect(() => {
    if (!replay.on || !replay.playing) return;
    const id = setInterval(() => stepReplay(1), Math.max(60, 1000 / replay.speed));
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [replay.on, replay.playing, replay.speed]);

  // 14) Which chart the pointer is over (keyboard shortcuts follow it).
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const enter = () => {
      hoveredRef.current = true;
      chartFocus.hover = myId;
    };
    const leave = () => {
      hoveredRef.current = false;
      if (chartFocus.hover === myId) chartFocus.hover = null;
      pointerRef.current = null;
      if (replayRef.current.selecting) repaintDrawings();
    };
    el.addEventListener("mouseenter", enter);
    el.addEventListener("mouseleave", leave);
    return () => {
      el.removeEventListener("mouseenter", enter);
      el.removeEventListener("mouseleave", leave);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 15) Repaint drawings when what they read changes.
  useEffect(() => {
    repaintDrawings();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hide.drawings, lockedTime, theme, iv, settings.sessionBreaks]);

  // 16) Strategies list for the indicators dialog.
  useEffect(() => {
    if (dialog?.k !== "indicators" || strategies !== null) return;
    void fetch("/api/algo/strategies")
      .then((r) => r.json())
      .then((j) => setStrategies(Array.isArray(j?.strategies) ? j.strategies : []))
      .catch(() => setStrategies([]));
  }, [dialog, strategies]);

  // ------------------------------------------------------------------
  // Actions
  // ------------------------------------------------------------------

  // Drag a stop-loss / target (or pull a new one out of the position line).
  function startLevelDrag(e: React.PointerEvent, which: "sl" | "tp"): void {
    if (!onLevels || !showPos || e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    const host = containerRef.current;
    if (!host || !mainRef.current) return;
    const pos = showPos;
    levelDragRef.current = { which, price: pos[which] ?? pos.avg };
    setLevelDrag(which);
    const move = (ev: PointerEvent) => {
      const pr = yToPrice(ev.clientY - host.getBoundingClientRect().top);
      if (pr != null && levelDragRef.current) levelDragRef.current.price = Math.round(pr * 100) / 100;
    };
    const up = async () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      const d = levelDragRef.current;
      if (!d) return;
      const done = () => {
        levelDragRef.current = null;
        setLevelDrag(null);
      };
      if (d.price === pos[which]) return done();
      const ltp = prevLtpRef.current ?? lastClose();
      const long = pos.qty > 0;
      // A level on the wrong side of the market would exit on the next tick.
      const below = which === "sl" ? long : !long;
      if (ltp != null && (below ? d.price >= ltp : d.price <= ltp)) {
        addToast(`${which === "sl" ? "Stop-loss" : "Target"} must be ${below ? "below" : "above"} the LTP ${fmtPrice(ltp)} for a ${long ? "long" : "short"} — not changed`);
        return done();
      }
      try {
        await onLevels(which === "sl" ? d.price : pos.sl, which === "tp" ? d.price : pos.tp);
        addToast(`${which === "sl" ? "Stop-loss" : "Target"} → ${fmtPrice(d.price)}`);
      } catch (err) {
        addToast(`Couldn't save the ${which === "sl" ? "stop-loss" : "target"}: ${err instanceof Error ? err.message : String(err)}`);
      }
      done();
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }

  async function removeLevel(which: "sl" | "tp"): Promise<void> {
    if (!onLevels || !showPos) return;
    try {
      await onLevels(which === "sl" ? null : showPos.sl, which === "tp" ? null : showPos.tp);
      addToast(`${which === "sl" ? "Stop-loss" : "Target"} removed`);
    } catch (err) {
      addToast(`Couldn't remove it: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  function submitOrder(o: ChartOrder): void {
    setCtx(null);
    const s = settingsRef.current;
    onChartOrder?.(o).then(
      (msg) => {
        pushLog("order", `${shortName}: ${msg}`);
        if (s.notifications === "all") addToast(msg);
        // the sound waits for the fill (the position changes), not the acceptance
      },
      (err) => {
        const msg = `Order failed: ${err instanceof Error ? err.message : String(err)}`;
        pushLog("error", `${shortName}: ${msg}`);
        if (s.notifications !== "off") addToast(msg);
      },
    );
  }
  // Instant orders (a trading setting) skip the confirm step.
  const pickOrder = (o: ChartOrder, at: { x: number; y: number; price: number }) =>
    instant ? submitOrder(o) : setCtx({ ...at, time: null, area: "pane", confirm: o });
  // The quick-trade BUY / SELL buttons: one-click (⚡ on) sends at market;
  // off, they open the order window on that side to review and confirm.
  const quickOrder = (side: "BUY" | "SELL") => {
    if (instant) submitOrder({ side, type: "MARKET", price: null });
    else if (onAction) onAction(side === "BUY" ? "ticket:buy" : "ticket:sell");
    else pickOrder({ side, type: "MARKET", price: null }, { x: 8, y: 52, price: lastClose() ?? 0 });
  };

  function addHLine(price: number): void {
    const t = lastClose() !== null ? viewRef.current[viewRef.current.length - 1].time : 0;
    addDrawing({ id: newDrawingId(), type: "hline", points: [{ time: t, price }] }, "horizontal line");
    addToast(`horizontal line at ${fmtPrice(price)}`);
  }

  // TradingView's rule: a buy below the market rests as a LIMIT, above it
  // as a STOP (SL-M); a sell the other way round.
  function ctxOrders(price: number): ChartOrder[] {
    const ltp = prevLtpRef.current ?? lastClose();
    const above = ltp != null && price > ltp;
    const tick = tickRef.current;
    const round = (p: number) => Math.round(p / tick) * tick;
    const out: ChartOrder[] = [
      { side: "BUY", type: above ? "SL-M" : "LIMIT", price },
      { side: "SELL", type: above || ltp == null ? "LIMIT" : "SL-M", price },
    ];
    // stop-limit on the breakout side: trigger at the price, limit one tick through it
    if (ltp != null) out.push(above ? { side: "BUY", type: "STOP_LOSS", price, limit: round(price + tick) } : { side: "SELL", type: "STOP_LOSS", price, limit: round(price - tick) });
    if (ltp != null) out.push({ side: "BUY", type: "MARKET", price: null }, { side: "SELL", type: "MARKET", price: null });
    return out;
  }
  const orderLabel = (o: ChartOrder) =>
    `${o.side === "BUY" ? "Buy" : "Sell"} ${orderQty ?? ""} ${
      o.price == null ? "at market" : o.type === "STOP_LOSS" ? `@ ${fmtPrice(o.price)} stop ${fmtPrice(o.limit ?? o.price)} limit` : `@ ${fmtPrice(o.price)} ${o.type === "LIMIT" ? "limit" : "stop"}`
    }`;

  // Controls render inline, into a shared slot (layouts), or not at all.
  const placeChrome = (el: ReactNode, slot: HTMLElement | null | undefined) => (!chrome ? null : slot ? createPortal(el, slot) : el);

  function openCtx(e: React.MouseEvent): void {
    const host = containerRef.current;
    if (!host || !mainRef.current) return;
    const r = host.getBoundingClientRect();
    const hx = e.clientX - r.left;
    const hy = e.clientY - r.top;
    const x = hx - paneLeft();
    const dims = paneDims();
    e.preventDefault();
    let area: Ctx["area"] = "pane";
    const th = safe(() => chartRef.current!.timeScale().height(), 0) || 0;
    if (x < 0 || x > dims.width) area = "price";
    else if (host.clientHeight > 0 && th > 0 && hy > host.clientHeight - th) area = "time";
    let drawingId: string | undefined;
    if (area === "pane" && hy <= dims.height) {
      const hit = hitDrawingAt(x, hy);
      if (hit) {
        area = "drawing";
        drawingId = hit.id;
        selectDrawing(hit.id);
      }
    }
    const pr = hy <= dims.height ? yToPrice(hy) : null;
    const price = pr ?? lastClose() ?? 0;
    setCtx({ x: hx, y: hy, price: Math.round(price * 100) / 100, time: xToTime(x), area, drawingId });
  }

  function toggleDraw(mode: string): void {
    setMenuOpen(null);
    if (mode === "icon" || mode === "image") {
      setDrawMode(mode);
      return;
    }
    const tool = TOOL_BY_ID.get(mode);
    if (tool && !tool.temp) setLastTool((lt) => ({ ...lt, [tool.group]: mode }));
    setDrawMode(drawModeRef.current === mode ? null : mode);
  }

  function toggleFullscreen(): void {
    // The chart area (toolbars, drawing strip, charts, date-range bar) goes
    // fullscreen — not the side panels or the account manager. Esc exits.
    if (document.fullscreenElement) void document.exitFullscreen();
    // The whole Trade page (chart + right dock + rail) so the panels keep working in fullscreen.
    else void (wrapRef.current?.closest(".trade-page") ?? wrapRef.current?.closest(".tv-layout") ?? wrapRef.current?.closest(".chart-card"))?.requestFullscreen?.();
  }

  /** PNG of the chart: a title strip (symbol, interval, last OHLC, change),
   *  the legend's indicator values, compare symbols and the watermarks. */
  async function snapshotBlob(): Promise<Blob> {
    const chart = chartRef.current;
    if (!chart) throw new Error("no chart");
    const canvas = chart.takeScreenshot();
    const out = document.createElement("canvas");
    const head = 30;
    out.width = canvas.width;
    out.height = canvas.height + head;
    const g = out.getContext("2d");
    if (!g) throw new Error("no canvas");
    const light = getTheme() === "light" || settingsRef.current.theme === "light";
    const fg = light ? "#131722" : "#E8E8E8";
    const faint = light ? "rgba(19,23,34,0.10)" : "rgba(232,232,232,0.08)";
    g.fillStyle = light ? "#FFFFFF" : colorsRef.current.bg;
    g.fillRect(0, 0, out.width, out.height);
    g.drawImage(canvas, 0, head);
    const v = viewRef.current;
    const c = v[v.length - 1];
    const p = v[v.length - 2];
    const s = settingsRef.current;
    // watermarks (the chart canvas doesn't contain the HTML ones)
    if (s.watermark) {
      g.save();
      g.fillStyle = faint;
      g.textAlign = "center";
      g.textBaseline = "middle";
      g.font = `700 ${Math.round(out.width / 14)}px "JetBrains Mono", monospace`;
      g.fillText(shortName, out.width / 2, head + canvas.height * 0.42);
      g.font = `600 ${Math.round(out.width / 40)}px "JetBrains Mono", monospace`;
      g.fillText(ivLabel, out.width / 2, head + canvas.height * 0.42 + out.width / 18);
      g.restore();
    }
    if (s.logoWatermark) {
      g.save();
      g.globalAlpha = 0.55;
      g.font = '800 12px "JetBrains Mono", monospace';
      g.fillStyle = colorsRef.current.accent;
      g.fillText("◆", 12, head + canvas.height - 36);
      g.fillStyle = light ? "#787B86" : "#666666";
      g.fillText("TRADEBOT", 26, head + canvas.height - 36);
      g.restore();
    }
    // title strip
    g.font = '13px "JetBrains Mono", monospace';
    g.fillStyle = fg;
    let x = 10;
    const put = (t: string, color = fg) => {
      g.fillStyle = color;
      g.fillText(t, x, 20);
      x += g.measureText(t).width + 10;
    };
    put(`${shortName} · ${ivLabel} · ${exchange}`);
    if (c) {
      put(`O ${fmtPrice(c.open)} H ${fmtPrice(c.high)} L ${fmtPrice(c.low)} C ${fmtPrice(c.close)}`);
      if (p) {
        const ch = c.close - p.close;
        put(`${ch >= 0 ? "+" : ""}${fmtPrice(ch)} (${ch >= 0 ? "+" : ""}${((ch / p.close) * 100).toFixed(2)}%)`, ch >= 0 ? colorsRef.current.up : colorsRef.current.down);
      }
    }
    put(new Date().toLocaleString("en-IN"), light ? "#787B86" : "#999999");
    // legend: compares + indicator values at the last bar
    g.font = '11px "JetBrains Mono", monospace';
    let y = head + 18;
    const line = (parts: [string, string][]) => {
      let lx = 10;
      for (const [t, col] of parts) {
        g.fillStyle = col;
        g.fillText(t, lx, y);
        lx += g.measureText(t).width + 6;
      }
      y += 15;
    };
    for (const cmp of compares.filter((q) => !q.hidden)) line([["●", cmp.color], [cmp.name, fg]]);
    const i = v.length - 1;
    for (const inst of indicatorsRef.current) {
      const def = INDICATOR_BY_TYPE.get(inst.type);
      const vals = indValuesRef.current.get(inst.uid);
      if (!def || !inst.visible || !indPaneRef.current.has(inst.uid)) continue;
      const prec = inst.precision ?? 2;
      const parts: [string, string][] = [[`${def.short} ${argsLabel(inst)}`.trim(), fg]];
      def.plots.forEach((pd, k) => {
        const val = vals?.[k]?.[i];
        if (pd.kind === "marks" || !inst.plots[k]?.visible || val == null) return;
        parts.push([inst.type === "volume" ? fmtVol(val) : fmtNum(val, prec), inst.plots[k].color]);
      });
      line(parts);
      if (y > head + canvas.height * 0.45) break;
    }
    const blob: Blob | null = await new Promise((res) => out.toBlob((b) => res(b)));
    if (!blob) throw new Error("no image");
    return blob;
  }

  /** Upload the image to this terminal's snapshot store; returns its absolute URL. */
  async function snapshotLink(): Promise<string> {
    const blob = await snapshotBlob();
    const r = await fetch("/api/snapshots", { method: "POST", headers: { "Content-Type": "image/png" }, body: blob });
    if (!r.ok) throw new Error(`upload failed (${r.status})`);
    const j = (await r.json()) as { url: string };
    return new URL(j.url, window.location.origin).toString();
  }

  async function takeSnapshot(kind: "download" | "copy" | "open" | "link" | "tweet"): Promise<void> {
    setMenuOpen(null);
    try {
      if (kind === "link" || kind === "tweet") {
        const url = await snapshotLink();
        if (kind === "link") {
          await navigator.clipboard?.writeText(url).catch(() => undefined);
          addToast("snapshot link copied — it opens for anyone signed in to this terminal");
        } else {
          const text = `${shortName} ${ivLabel} chart`;
          window.open(`https://twitter.com/intent/tweet?text=${encodeURIComponent(text)}&url=${encodeURIComponent(url)}`, "_blank", "noopener");
        }
        return;
      }
      const blob = await snapshotBlob();
      if (kind === "copy") {
        const CI = (window as unknown as { ClipboardItem?: new (d: Record<string, Blob>) => unknown }).ClipboardItem;
        if (!CI || !navigator.clipboard?.write) throw new Error("clipboard images aren't supported here");
        await navigator.clipboard.write([new CI({ "image/png": blob }) as ClipboardItem]);
        addToast("chart image copied");
        return;
      }
      const url = URL.createObjectURL(blob);
      if (kind === "open") window.open(url, "_blank");
      else {
        const a = document.createElement("a");
        a.href = url;
        a.download = `${shortName}_${ivLabel}_${new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-")}.png`;
        a.click();
      }
      setTimeout(() => URL.revokeObjectURL(url), 30000);
    } catch (e) {
      addToast(`snapshot failed: ${e instanceof Error ? e.message : "this browser"}`);
    }
  }

  // ---- bar replay ----

  function startReplaySelect(): void {
    setMenuOpen(null);
    if (!candlesRef.current.length) return;
    setDrawMode(null);
    setReplay({ on: true, selecting: true, playing: false, speed: replayRef.current.speed || 1, idx: candlesRef.current.length - 1 });
    addToast("Bar replay: click the bar to start from");
  }

  function startReplayAt(time: number): void {
    const c = candlesRef.current;
    let lo = 0, hi = c.length - 1;
    while (lo < hi) {
      const m = (lo + hi + 1) >> 1;
      if (c[m].time <= time) lo = m;
      else hi = m - 1;
    }
    setReplay({ on: true, selecting: false, playing: false, speed: replayRef.current.speed || 1, idx: Math.max(1, lo) });
    applyData();
    showLatest();
  }

  function stepReplay(n: number): void {
    const r = replayRef.current;
    if (!r.on || r.selecting) return;
    const max = candlesRef.current.length - 1;
    const idx = Math.min(max, r.idx + n);
    setReplay({ ...r, idx, playing: idx >= max ? false : r.playing });
    applyData();
    if (atLiveRef.current) showLatest();
  }

  function stopReplay(): void {
    setReplay({ on: false, selecting: false, playing: false, speed: replayRef.current.speed || 1, idx: 0 });
    applyData();
    showLatest();
  }

  // ---- date ranges / go to ----

  function indexAtOrAfter(t: number): number {
    const v = viewRef.current;
    let lo = 0, hi = v.length - 1;
    while (lo < hi) {
      const m = (lo + hi) >> 1;
      if (v[m].time < t) lo = m + 1;
      else hi = m;
    }
    return lo;
  }

  async function applyPendingRange(): Promise<void> {
    const r = pendingRangeRef.current;
    pendingRangeRef.current = null;
    if (!r) return;
    const v0 = viewRef.current;
    if (!v0.length) return;
    const last: number = v0[v0.length - 1].time;
    const ts = safe(() => chartRef.current!.timeScale());
    if (!ts) return;
    if ("id" in r) {
      let from: number = last;
      if (r.sessions) {
        let days = 0;
        let prevDay = Math.floor(last / 86400) + 1;
        for (let i = v0.length - 1; i >= 0; i--) {
          const d = Math.floor(v0[i].time / 86400);
          if (d !== prevDay) {
            days++;
            prevDay = d;
            if (days > r.sessions) break;
          }
          from = v0[i].time;
        }
        if (days <= r.sessions) await ensureHistoryFrom(last - r.sessions * 2 * 86400);
      } else if (r.ytd) from = Date.UTC(new Date(last * 1000).getUTCFullYear(), 0, 1) / 1000;
      else if (r.days) from = last - r.days * 86400;
      if (r.all) {
        for (let k = 0; k < 40 && haveMoreRef.current; k++) await maybeLoadOlder();
        from = viewRef.current[0]?.time ?? last;
      } else if (!r.sessions) await ensureHistoryFrom(from);
      const v = viewRef.current;
      const i0 = indexAtOrAfter(from);
      safe(() => ts.setVisibleLogicalRange({ from: (i0 - 0.5) as Logical, to: (v.length - 1 + settingsRef.current.marginRight) as Logical }));
      return;
    }
    if (r.at !== undefined) {
      await ensureHistoryFrom(r.at - 30 * barInterval());
      const v = viewRef.current;
      const i = indexAtOrAfter(r.at);
      const vis = safe(() => ts.getVisibleLogicalRange());
      const span = vis ? vis.to - vis.from : 90;
      safe(() => ts.setVisibleLogicalRange({ from: (i - span / 2) as Logical, to: (i + span / 2) as Logical }));
      setLockedTime(v[i]?.time ?? null);
      setTimeout(() => setLockedTime((x) => (x === (v[i]?.time ?? null) ? null : x)), 4000);
      return;
    }
    if (r.from !== undefined && r.to !== undefined) {
      await ensureHistoryFrom(r.from);
      const i0 = indexAtOrAfter(r.from);
      const i1 = indexAtOrAfter(r.to);
      safe(() => ts.setVisibleLogicalRange({ from: (i0 - 0.5) as Logical, to: (Math.max(i0 + 2, i1) + 0.5) as Logical }));
    }
  }

  function applyRange(r: RangeDef, fromSync = false): void {
    pendingRangeRef.current = r;
    if (normalizeInterval(r.interval) !== iv) changeInterval(r.interval, fromSync);
    else void applyPendingRange();
    if (!fromSync && syncRef.current.dateRange) publishSync({ type: "daterange", src: myId, preset: r.id });
  }

  function goTo(r: { at?: number; from?: number; to?: number }, fromSync = false): void {
    pendingRangeRef.current = r;
    void applyPendingRange();
    if (!fromSync && syncRef.current.dateRange) publishSync({ type: "daterange", src: myId, ...r });
  }

  // ---- indicators ----

  function addIndicator(type: string): void {
    const def = INDICATOR_BY_TYPE.get(type);
    if (def?.tool) {
      // e.g. the fixed-range volume profile is a drawing: arm it and let the user pick the range
      setDialog(null);
      toggleDraw(def.tool);
      addToast(`${def.name}: click the first and the last bar of the range`);
      return;
    }
    const defaults = loadJson<Record<string, Partial<IndicatorInstance>>>("chart:indDefaults", {})[type];
    const base = newInstance(type, defaults?.inputs);
    if (!base) return;
    // saved defaults cover styles, outputs and visibility too (sanitize merges them)
    const inst = defaults ? sanitizeInstance({ ...base, ...defaults, inputs: base.inputs, uid: base.uid, type }) ?? base : base;
    setIndicators([...indicatorsRef.current, inst], `add ${instanceTitle(inst)}`);
  }

  function updateIndicator(inst: IndicatorInstance): void {
    setIndicators(indicatorsRef.current.map((x) => (x.uid === inst.uid ? inst : x)), null);
  }

  function removeIndicator(uid: string): void {
    setIndicators(indicatorsRef.current.filter((x) => x.uid !== uid), "remove indicator");
  }

  function toggleIndicator(uid: string): void {
    setIndicators(indicatorsRef.current.map((x) => (x.uid === uid ? { ...x, visible: !x.visible } : x)), "hide indicator");
  }

  /** Legend More → Move to: its own new pane, the price pane, or another indicator's pane. */
  function moveIndicatorTo(uid: string, target: "own" | "main" | string): void {
    setIndicators(indicatorsRef.current.map((x) => (x.uid === uid ? { ...x, pane: target } : x)), "move indicator");
    setPaneMode({ max: null, collapsed: [] });
  }

  function pinIndicatorScale(uid: string, scale: "left" | "right" | "new" | undefined): void {
    setIndicators(indicatorsRef.current.map((x) => (x.uid === uid ? { ...x, scale } : x)), "pin to scale");
  }

  /** Visual order: later series paint on top. */
  function reorderIndicator(uid: string, to: "front" | "back"): void {
    const list = indicatorsRef.current;
    const it = list.find((x) => x.uid === uid);
    if (!it) return;
    const rest = list.filter((x) => x.uid !== uid);
    setIndicators(to === "front" ? [...rest, it] : [it, ...rest], "visual order");
  }

  function copyIndicator(uid: string): void {
    const it = indicatorsRef.current.find((x) => x.uid === uid);
    if (!it) return;
    indicatorClipboard.current = JSON.parse(JSON.stringify(it));
    addToast(`${instanceTitle(it)} copied — paste it on any chart (right-click → Paste indicator)`);
  }

  function pasteIndicator(): void {
    const raw = indicatorClipboard.current as IndicatorInstance | null;
    const inst = raw ? sanitizeInstance({ ...raw, uid: newUid(), pane: undefined }) : null;
    if (inst) setIndicators([...indicatorsRef.current, inst], `paste ${instanceTitle(inst)}`);
  }

  /** Panes below the price pane: index → the indicators in it. */
  function paneList(): { i: number; insts: IndicatorInstance[] }[] {
    const by = new Map<number, IndicatorInstance[]>();
    for (const x of indicatorsRef.current) {
      const p = indPaneRef.current.get(x.uid);
      if (p === undefined) continue;
      by.set(p, [...(by.get(p) ?? []), x]);
    }
    return [...by.entries()].sort((a, b) => a[0] - b[0]).map(([i, insts]) => ({ i, insts }));
  }

  /** Merge every indicator of pane `from` into pane `to` (0 = price pane). */
  function mergePanes(from: number, to: number): void {
    const host = to === 0 ? "main" : paneList().find((p) => p.i === to)?.insts[0]?.uid;
    if (!host) return;
    setIndicators(indicatorsRef.current.map((x) => (indPaneRef.current.get(x.uid) === from ? { ...x, pane: host } : x)), "merge panes");
    setPaneMode({ max: null, collapsed: [] });
  }

  /** Move a pane up / down by swapping it with its neighbour pane. */
  function movePane(p: number, by: -1 | 1): void {
    const list = [...indicatorsRef.current];
    const inPane = (q: number) => list.findIndex((x) => indPaneRef.current.get(x.uid) === q);
    const a = inPane(p);
    const b = inPane(p + by);
    if (a < 0 || b < 0) return;
    [list[a], list[b]] = [list[b], list[a]];
    setIndicators(list, "move pane");
    setPaneMode({ max: null, collapsed: [] });
  }

  function deletePane(p: number): void {
    setIndicators(indicatorsRef.current.filter((x) => indPaneRef.current.get(x.uid) !== p), "remove pane");
    setPaneMode({ max: null, collapsed: [] });
  }

  function saveIndTemplate(): void {
    setMenuOpen(null);
    setDialog({ k: "saveTemplate" });
  }

  function storeIndTemplate(name: string, withSymbol: boolean, withInterval: boolean): void {
    const t: IndicatorTemplate = {
      name,
      items: indicatorsRef.current,
      ...(withSymbol ? { symbol: { symbol, name: shortName, hit: instrument ?? null } } : {}),
      ...(withInterval ? { interval: ivRef.current } : {}),
    };
    const next = [...templates.filter((x) => x.name !== name), t];
    setTemplates(next);
    saveJson("chart:indTemplates", next);
    addToast(`template "${name}" saved`);
  }

  function renameIndTemplate(t: IndicatorTemplate): void {
    const name = window.prompt("Rename template", t.name)?.trim();
    if (!name || name === t.name) return;
    if (templates.some((x) => x.name === name) && !window.confirm(`Replace the template "${name}"?`)) return;
    const next = templates.filter((x) => x.name !== name).map((x) => (x.name === t.name ? { ...x, name } : x));
    setTemplates(next);
    saveJson("chart:indTemplates", next);
  }

  function applyIndTemplate(t: IndicatorTemplate): void {
    setMenuOpen(null);
    const items = t.items.map((i) => sanitizeInstance({ ...i, uid: newUid() })).filter(Boolean) as IndicatorInstance[];
    setIndicators(items, "apply template");
    const iv2 = t.interval && parseInterval(t.interval) ? t.interval : null;
    if (iv2 && iv2 !== ivRef.current) changeInterval(iv2);
    if (t.symbol && t.symbol.symbol !== symbol && onSymbolChange) {
      // The chart remounts for the new symbol and reads its prefs on mount:
      // hand it the template's indicators (and interval) before switching.
      saveJson(PREFS_KEY, { ...loadJson<ChartPrefs>(PREFS_KEY, {}), indicators: items, ...(iv2 ? { interval: iv2 } : {}) });
      const sym = t.symbol;
      const [ex] = sym.symbol.split(":");
      onSymbolChange(sym.hit ?? { symbol: sym.symbol, short_name: sym.name, exchange: ex || "NSE", segment: "EQ", instrument_type: "EQ", lot_size: 1, tick_size: 0.05, expiry: null, strike: null, underlying: null, display: sym.name });
    }
  }

  function deleteIndTemplate(name: string): void {
    const next = templates.filter((t) => t.name !== name);
    setTemplates(next);
    saveJson("chart:indTemplates", next);
  }

  // ---- named drawing templates (per tool) ----
  const DRAW_TEMPLATES_KEY = "chart:drawTemplates";
  function drawTemplates(type: string): { name: string; style: Partial<DrawingStyle> }[] {
    return loadJson<Record<string, { name: string; style: Partial<DrawingStyle> }[]>>(DRAW_TEMPLATES_KEY, {})[type] ?? [];
  }
  function saveDrawTemplate(d: Drawing): void {
    const name = window.prompt(`Save ${TOOL_BY_ID.get(d.type)?.label ?? "drawing"} template as`, "");
    if (!name) return;
    const all = loadJson<Record<string, { name: string; style: Partial<DrawingStyle> }[]>>(DRAW_TEMPLATES_KEY, {});
    all[d.type] = [...(all[d.type] ?? []).filter((t) => t.name !== name), { name, style: JSON.parse(JSON.stringify(d.style ?? {})) }];
    saveJson(DRAW_TEMPLATES_KEY, all);
    addToast(`template "${name}" saved`);
  }
  function applyDrawTemplate(d: Drawing, name: string | null): void {
    const style = name === null ? toolDefaultsRef.current[d.type] ?? {} : drawTemplates(d.type).find((t) => t.name === name)?.style;
    if (!style) return;
    updateDrawing(d.id, { style: JSON.parse(JSON.stringify(style)) }, "apply template");
  }
  function removeDrawTemplate(type: string, name: string): void {
    const all = loadJson<Record<string, { name: string; style: Partial<DrawingStyle> }[]>>(DRAW_TEMPLATES_KEY, {});
    all[type] = (all[type] ?? []).filter((t) => t.name !== name);
    saveJson(DRAW_TEMPLATES_KEY, all);
  }

  function pasteDrawing(): void {
    const c = clipboardRef.current;
    if (!c) return;
    const copy: Drawing = { ...c, id: newDrawingId(), locked: false, points: c.points.map((p) => ({ ...p, time: p.time + barInterval() * 3 })) };
    addDrawing(copy, "paste drawing");
  }

  function setHideKind(k: "drawings" | "indicators" | "positions" | "all"): void {
    setHide((h) => {
      if (k === "all") {
        const on = !(h.drawings && h.indicators && h.positions);
        return { drawings: on, indicators: on, positions: on };
      }
      return { ...h, [k]: !h[k] };
    });
  }

  function removeKind(k: "drawings" | "indicators" | "all"): void {
    if (k === "drawings" || k === "all") clearDrawings();
    if (k === "indicators" || k === "all") setIndicators([], "remove indicators");
  }

  function runTool(id: string): void {
    setMenuOpen(null);
    const item = TOOLS_MENU.find((t) => t.id === id);
    if (item?.host) {
      onAction?.(item.host);
      return;
    }
    switch (id) {
      case "replay": return replayRef.current.on ? stopReplay() : startReplaySelect();
      case "popout": {
        // a window with just this chart, on this symbol and interval
        const q = new URLSearchParams({ popout: "1", symbol, name: shortName, iv: ivRef.current });
        window.open(`${window.location.pathname}#/trade?${q.toString()}`, "_blank", "popup,width=1200,height=760");
        return;
      }
      case "refresh": return setReloadNonce((n) => n + 1);
      case "theme": {
        // the whole app flips skin; the chart follows it ("App theme")
        const next = toggleTheme();
        if (settingsRef.current.theme !== "app") changeSettings({ ...settingsRef.current, theme: "app" });
        addToast(`${next === "light" ? "Light" : "Dark"} theme`);
        return;
      }
      case "settings": return onAction ? onAction("usersettings") : setDialog({ k: "settings" });
      case "shortcuts": return setDialog({ k: "shortcuts" });
    }
  }

  function commands(): Command[] {
    const out: Command[] = [];
    for (const sec of INTERVAL_SECTIONS) for (const k of sec.keys) out.push({ id: `iv:${k}`, label: `Interval ${intervalLongLabel(k)}`, group: "Interval", run: () => changeInterval(k) });
    for (const k of CHART_KINDS) out.push({ id: `kind:${k.id}`, label: `Chart type: ${k.label}`, group: "Chart style", run: () => setChartKind(k.id) });
    for (const d of [...INDICATOR_BY_TYPE.values()]) out.push({ id: `ind:${d.type}`, label: `Add ${d.name}`, group: "Indicators", run: () => addIndicator(d.type) });
    for (const t of TOOL_BY_ID.values()) out.push({ id: `draw:${t.id}`, label: t.label, group: "Drawing tools", run: () => toggleDraw(t.id) });
    for (const r of RANGES) out.push({ id: `range:${r.id}`, label: `Range ${r.id} — ${r.title}`, group: "Date range", run: () => applyRange(r) });
    for (const t of TOOLS_MENU) if (!t.host || onAction) out.push({ id: `tool:${t.id}`, label: t.label, group: "Tools", run: () => runTool(t.id) });
    out.push(
      { id: "a:search", label: "Symbol search", group: "Actions", run: () => setDialog({ k: "symbol", q: shortName ?? "", select: true }) },
      { id: "a:compare", label: "Compare or add symbol", group: "Actions", run: () => setMenuOpen("compare") },
      { id: "a:indicators", label: "Indicators & strategies", group: "Actions", hint: "/", run: () => setDialog({ k: "indicators" }) },
      { id: "a:alert", label: "Create alert", group: "Actions", hint: "Alt+A", run: () => openAlertDialog() },
      { id: "a:goto", label: "Go to date", group: "Actions", run: () => setDialog({ k: "goto" }) },
      { id: "a:reset", label: "Reset chart view", group: "Actions", hint: "Alt+R", run: resetView },
      { id: "a:live", label: "Scroll to the most recent bar", group: "Actions", run: () => showLatest() },
      { id: "a:undo", label: "Undo", group: "Actions", hint: "Ctrl+Z", run: undo },
      { id: "a:redo", label: "Redo", group: "Actions", hint: "Ctrl+Y", run: redo },
      { id: "a:snap", label: "Take a snapshot (download)", group: "Actions", hint: "Ctrl+Alt+S", run: () => void takeSnapshot("download") },
      { id: "a:copyimg", label: "Copy chart image", group: "Actions", run: () => void takeSnapshot("copy") },
      { id: "a:full", label: "Fullscreen", group: "Actions", hint: "Shift+F", run: toggleFullscreen },
      { id: "a:settings", label: "Chart settings…", group: "Settings", run: () => setDialog({ k: "settings" }) },
      { id: "a:log", label: "Log scale", group: "Settings", hint: "Alt+L", toggle: scaleMode === "log", run: () => setScaleMode((m) => (m === "log" ? "normal" : "log")) },
      { id: "a:pct", label: "Percent scale", group: "Settings", hint: "Alt+P", toggle: scaleMode === "percent", run: () => setScaleMode((m) => (m === "percent" ? "normal" : "percent")) },
      { id: "a:idx", label: "Indexed to 100 scale", group: "Settings", toggle: scaleMode === "indexed", run: () => setScaleMode((m) => (m === "indexed" ? "normal" : "indexed")) },
      { id: "a:auto", label: "Auto scale", group: "Settings", toggle: autoScale, run: () => setAutoScale((v) => !v) },
      { id: "a:inv", label: "Invert scale", group: "Settings", hint: "Alt+I", toggle: invert, run: () => setInvert((v) => !v) },
      ...([
        ["sessionBreaks", "Session breaks"],
        ["countdown", "Countdown to bar close"],
        ["noOverlapLabels", "No overlapping labels"],
        ["lockRatio", "Lock price to bar ratio"],
        ["symbolNameLabel", "Symbol name label"],
        ["prevCloseLine", "Previous day close line"],
        ["highLowLabels", "High and low price labels"],
        ["bidAskLines", "Bid and ask lines"],
        ["showEvents", "Corporate events on bars"],
        ["showMarks", "Marks on bars"],
        ["watermark", "Symbol watermark"],
        ["logoWatermark", "Logo watermark"],
        ["showVolume", "Volume in the status line"],
        ["showLastDayChange", "Last day change in the status line"],
        ["indValues", "Indicator values in the status line"],
        ["executions", "Executions on the chart"],
        ["extendLines", "Extended position / order lines"],
        ["buySellButtons", "Buy / sell buttons"],
        ["sound", "Sound for executions and alerts"],
      ] as [keyof ChartSettings, string][]).map(([k, label]): Command => ({
        id: `set:${k}`,
        label,
        group: "Settings",
        toggle: settings[k] === true,
        run: () => changeSettings({ ...settingsRef.current, [k]: !settingsRef.current[k] }),
      })),
      { id: "a:magnet", label: "Magnet mode", group: "Drawing", toggle: magnet !== "off", run: () => setMagnet((m) => (m === "off" ? "weak" : "off")) },
      { id: "a:lock", label: "Lock all drawings", group: "Drawing", toggle: lockAll, run: () => setLockAll((v) => !v) },
      { id: "a:hide", label: "Hide drawings", group: "Drawing", toggle: hide.drawings, run: () => setHideKind("drawings") },
      { id: "a:stay", label: "Stay in drawing mode", group: "Drawing", toggle: stay, run: () => setStay((v) => !v) },
      { id: "a:rmd", label: "Remove all drawings", group: "Drawing", run: () => clearDrawings() },
      { id: "a:rmi", label: "Remove all indicators", group: "Drawing", run: () => removeKind("indicators") },

      { id: "a:keys", label: "Keyboard shortcuts", group: "Help", hint: "?", run: () => setDialog({ k: "shortcuts" }) },
    );
    if (onAction) {
      out.push(
        { id: "h:save", label: "Save layout", group: "Layout", hint: "Ctrl+S", run: () => onAction("save") },
        { id: "h:layouts", label: "Manage layouts", group: "Layout", run: () => onAction("layouts") },
        { id: "h:watch", label: `Add ${shortName} to watchlist`, group: "Watchlist", hint: "Alt+W", run: () => onAction("watch:add") },
        { id: "h:details", label: "Symbol details", group: "Panels", run: () => onAction("panel:details") },
        { id: "h:data", label: "Data window", group: "Panels", run: () => onAction("panel:data") },
        { id: "h:tree", label: "Object tree", group: "Panels", run: () => onAction("panel:tree") },
        { id: "h:alerts", label: "Alerts manager", group: "Panels", run: () => onAction("panel:alerts") },
      );
    }
    return out;
  }

  // ---- widgets ----

  const drawingsList = useMemo(() => drawingsRef.current, [drawVer]); // eslint-disable-line react-hooks/exhaustive-deps

  function dataWindowGroups(): { title: string; groups: { title: string; rows: DataRow[] }[] } {
    const v = viewRef.current;
    const i = hoverIdx ?? v.length - 1;
    const c = v[i];
    const groups: { title: string; rows: DataRow[] }[] = [];
    if (c) {
      const w = wallClock(c.time, settings.timezone);
      const prev = v[i - 1];
      const chg = prev ? c.close - prev.close : c.close - c.open;
      const col = chg >= 0 ? theme.up : theme.down;
      groups.push({
        title: `${shortName} · ${ivLabel}`,
        rows: [
          { label: "Date", value: formatDate(w, settings.dateFormat) },
          ...(intraday ? [{ label: "Time", value: formatClock(w, settings.hour12) }] : []),
          { label: "Open", value: fmtPrice(c.open), color: col },
          { label: "High", value: fmtPrice(c.high), color: col },
          { label: "Low", value: fmtPrice(c.low), color: col },
          { label: "Close", value: fmtPrice(c.close), color: col },
          { label: "Change", value: `${chg >= 0 ? "+" : ""}${fmtPrice(chg)} (${((chg / (prev?.close ?? c.open)) * 100).toFixed(2)}%)`, color: col },
          { label: "Volume", value: fmtVol(c.volume) },
        ],
      });
      for (const inst of indicators) {
        const def = INDICATOR_BY_TYPE.get(inst.type);
        const vals = indValuesRef.current.get(inst.uid);
        if (!def || !vals) continue;
        const marks = indMarksRef.current.get(inst.uid)?.get(i) ?? [];
        groups.push({
          title: `${def.short} ${argsLabel(inst)}`,
          rows: [
            ...def.plots.filter((p, k) => p.kind !== "marks" && indHasDataRef.current.get(inst.uid)?.[k] !== false).map((p) => {
              const k = def.plots.indexOf(p);
              const x = vals[k]?.[i];
              return { label: p.label, value: x == null ? "∅" : inst.type === "volume" || inst.type === "obv" || inst.type === "oi" ? fmtVol(x) : fmtNum(x, inst.precision ?? 2), color: inst.plots[k]?.color };
            }),
            ...marks.map((m) => ({ label: "Signal", value: m.title ?? m.text ?? "●", color: m.color })),
          ],
        });
      }
    }
    return { title: "Data Window", groups };
  }

  function treePanes(): { title: string; pane: number; items: TreeItem[] }[] {
    const mainItems: TreeItem[] = [{ id: symbol, kind: "series", label: `${shortName} · ${ivLabel} · ${CHART_KINDS.find((k) => k.id === chartKind)?.label}`, visible: !mainHidden }];
    for (const c of compares) mainItems.push({ id: c.symbol, kind: "compare", label: c.name, visible: !c.hidden });
    const panes = new Map<number, TreeItem[]>();
    for (const inst of indicators) {
      const p = indPaneRef.current.get(inst.uid) ?? (INDICATOR_BY_TYPE.get(inst.type)?.overlay ? 0 : -1);
      const it: TreeItem = { id: inst.uid, kind: "indicator", label: `${instanceTitle(inst)} ${argsLabel(inst)}`, visible: inst.visible };
      if (p === 0) mainItems.push(it);
      else panes.set(p, [...(panes.get(p) ?? []), it]);
    }
    for (const d of [...drawingsList].reverse()) {
      if (TOOL_BY_ID.get(d.type)?.temp) continue;
      mainItems.push({ id: d.id, kind: "drawing", label: d.name || TOOL_BY_ID.get(d.type)?.label || d.type, visible: !d.hidden, locked: d.locked, selected: d.id === selectedDrawing, group: d.group });
    }
    const out = [{ title: "Main pane", pane: 0, items: mainItems }];
    for (const [p, items] of [...panes.entries()].sort((a, b) => a[0] - b[0])) out.push({ title: p < 0 ? "Hidden" : `Pane ${p + 1}`, pane: p, items });
    return out;
  }

  /** The indicator that owns pane `p` (others in it are its guests). */
  function paneHost(p: number): string | null {
    const inPane = indicatorsRef.current.filter((x) => indPaneRef.current.get(x.uid) === p);
    const host = inPane.find((x) => !(typeof x.pane === "string" && x.pane !== "own" && x.pane !== "main")) ?? inPane[0];
    return host?.uid ?? null;
  }

  /** Object tree drag and drop. */
  function treeDrop(dragged: TreeItem, at: TreeDrop): void {
    if (dragged.kind === "drawing") {
      if (at.kind === "group") return updateDrawing(dragged.id, { group: at.group }, "group drawing");
      if (at.kind === "pane") return updateDrawing(dragged.id, { group: undefined }, "ungroup drawing");
      if (at.kind !== "item" || at.item.kind !== "drawing") return;
      const list = [...drawingsRef.current];
      const from = list.findIndex((d) => d.id === dragged.id);
      const to = list.findIndex((d) => d.id === at.item.id);
      if (from < 0 || to < 0) return;
      pushUndo("visual order");
      const [d] = list.splice(from, 1);
      list.splice(to, 0, { ...d, group: at.item.group });
      drawingsRef.current = list;
      persistDrawings();
      repaintDrawings();
      bumpDrawings();
      return;
    }
    if (dragged.kind !== "indicator") return;
    if (at.kind === "newpane") return moveIndicatorTo(dragged.id, "own");
    const targetPane = at.kind === "pane" ? at.pane : at.kind === "item" ? indPaneRef.current.get(at.item.id) ?? 0 : null;
    if (targetPane == null || targetPane < 0) return;
    const fromPane = indPaneRef.current.get(dragged.id);
    if (at.kind === "item" && targetPane === fromPane) {
      // same pane: take the target's place in the visual (paint) order
      const list = [...indicatorsRef.current];
      const from = list.findIndex((x) => x.uid === dragged.id);
      const to = list.findIndex((x) => x.uid === at.item.id);
      if (from < 0 || to < 0 || from === to) return;
      const [it] = list.splice(from, 1);
      list.splice(to, 0, it);
      return setIndicators(list, "visual order");
    }
    if (targetPane === fromPane) return;
    const host = targetPane === 0 ? "main" : paneHost(targetPane);
    if (host && host !== dragged.id) moveIndicatorTo(dragged.id, host);
  }

  function groupDrawings(ids: string[]): void {
    const used = new Set(drawingsRef.current.map((d) => d.group).filter(Boolean));
    let n = 1;
    while (used.has(`Group ${n}`)) n++;
    pushUndo("group drawings");
    drawingsRef.current = drawingsRef.current.map((d) => (ids.includes(d.id) ? { ...d, group: `Group ${n}` } : d));
    persistDrawings();
    bumpDrawings();
  }

  function groupAction(group: string, a: GroupAction): void {
    const inGroup = (d: Drawing) => d.group === group;
    if (a === "delete") {
      const n = drawingsRef.current.filter(inGroup).length;
      if (!window.confirm(`Delete the ${n} drawing(s) in "${group}"?`)) return;
      pushUndo("delete group");
      drawingsRef.current = drawingsRef.current.filter((d) => !inGroup(d));
      if (selectedIdRef.current && !drawingsRef.current.some((d) => d.id === selectedIdRef.current)) selectDrawing(null);
    } else {
      if (typeof a === "object" && drawingsRef.current.some((d) => d.group === a.rename)) {
        addToast(`a group named "${a.rename}" already exists`);
        return;
      }
      pushUndo(typeof a === "object" ? "rename group" : `${a} group`);
      drawingsRef.current = drawingsRef.current.map((d) => {
        if (!inGroup(d)) return d;
        if (typeof a === "object") return { ...d, group: a.rename };
        if (a === "ungroup") return { ...d, group: undefined };
        if (a === "hide" || a === "show") return { ...d, hidden: a === "hide" };
        return { ...d, locked: a === "lock" };
      });
    }
    persistDrawings();
    repaintDrawings();
    bumpDrawings();
  }

  const selDrawing = selectedDrawing ? drawingsList.find((d) => d.id === selectedDrawing) ?? null : null;
  const drawCount = drawingsList.filter((d) => !TOOL_BY_ID.get(d.type)?.temp).length;
  const marketStatus = (() => {
    const now = Math.floor(Date.now() / 1000) + IST;
    const d = new Date(now * 1000);
    const dow = d.getUTCDay();
    const m = d.getUTCHours() * 60 + d.getUTCMinutes();
    if (dow === 0 || dow === 6) return { k: "closed", t: "Market closed" };
    if (m >= 540 && m < 555) return { k: "pre", t: "Pre-market" };
    if (m >= 555 && m < 930) return { k: "open", t: "Market open" };
    return { k: "closed", t: "Market closed" };
  })();
  const activeTool = drawMode ? TOOL_BY_ID.get(drawMode) : undefined;
  const drawHint =
    replay.selecting
      ? "Bar replay — click the bar to start from (Esc cancels)"
      : drawMode === "alert"
        ? "click a price to set an alert"
        : drawMode === "ticket"
          ? "click a price to load it into the ticket as a LIMIT"
          : drawMode === "zoom"
            ? pendingCount ? "click the opposite corner" : "click the first corner of the area to zoom into"
            : drawMode === "icon"
              ? "click where the icon goes"
              : drawMode === "image"
                ? "click where the image goes"
                : activeTool
                  ? activeTool.points === "free"
                    ? `${activeTool.label}: press and drag (Esc cancels)`
                    : activeTool.points === "poly"
                      ? `${activeTool.label}: ${pendingCount ? `${pendingCount} point(s) — ` : ""}click points, double-click or Enter to finish`
                      : `${activeTool.label}: ${pendingCount ? `point ${pendingCount + 1} of ${activeTool.points}` : `click ${activeTool.points === 1 ? "a point" : "the first point"}`}${stay ? " · stay in drawing mode" : ""} (Shift snaps 45°, Esc cancels)`
                  : null;
  void undoVer;
  // the layout's history: the latest step in any chart (else this chart's own)
  const jPast = UNDO_JOURNAL.past.filter((c) => UNDO_HANDLERS.has(c));
  const jFuture = UNDO_JOURNAL.future.filter((c) => UNDO_HANDLERS.has(c));
  const canUndo = jPast.length > 0 || undoRef.current.past.length > 0;
  const canRedo = jFuture.length > 0 || undoRef.current.future.length > 0;
  const stepLabel = (cell: string | undefined, which: "past" | "future") => {
    if (!cell) return undoRef.current[which].length ? undoRef.current[which][undoRef.current[which].length - 1].label : "";
    const l = UNDO_HANDLERS.get(cell)?.label(which) ?? "";
    return cell === myId || !multi ? l : `${l} (another chart)`;
  };
  const undoLabel = canUndo ? stepLabel(jPast[jPast.length - 1], "past") : "";
  const redoLabel = canRedo ? stepLabel(jFuture[jFuture.length - 1], "future") : "";
  const lightVars = settings.theme === "light"
    ? ({
        "--bg-panel": LIGHT.panel, "--bg-surface": LIGHT.surface, "--bg-elev": LIGHT.elev, "--bg-input": LIGHT.bg,
        "--text": LIGHT.text, "--text-dim": LIGHT.dim, "--text-faint": LIGHT.faint, "--border": LIGHT.border, "--border-hi": LIGHT.border,
      } as React.CSSProperties)
    : undefined;

  // snapshot of the drawing when its settings dialog opens (one undo step)
  const dialogSnapRef = useRef<Snap | null>(null);
  useEffect(() => {
    dialogSnapRef.current =
      dialog?.k === "drawSettings" ? snapshot("edit drawing")
      : dialog?.k === "settings" ? { ...snapshot("change settings"), settings: settingsRef.current }
      : dialog?.k === "indSettings" ? snapshot("change indicator settings")
      : null;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dialog?.k, dialog?.k === "drawSettings" ? dialog.id : null]);

  useEffect(() => {
    setHostEl(containerRef.current);
  }, []);

  // ------------------------------------------------------------------
  // Render
  // ------------------------------------------------------------------

  const menuBtn = (id: Exclude<MenuId, null>) => () => setMenuOpen(menuOpen === id ? null : id);
  const star = (on: boolean, toggle: () => void, label: string) => (
    <button type="button" className={`dtb-star${on ? " on" : ""}`} onClick={(e) => { e.stopPropagation(); toggle(); }} title={on ? "Remove from favorites" : "Add to favorites"} aria-label={label}>★</button>
  );
  const toggleIn = <T,>(list: T[], v: T): T[] => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);
  const titleText = settings.titleMode === "ticker" ? shortName : settings.titleMode === "both" ? `${shortName} · ${description}` : description;
  const legendBgBase = settings.legendBgColor || (theme.bg.startsWith("#") ? theme.bg : "#111111");
  const legendStyle =
    settings.legendBg || leftAxisW > 0
      ? { ...(settings.legendBg ? { background: withAlpha(legendBgBase, settings.legendBgOpacity, legendBgBase) } : {}), ...(leftAxisW > 0 ? { left: leftAxisW + 10 } : {}) }
      : undefined;
  const knownPanes = new Set(paneRects.map((r) => r.i));
  const mainLegendInds = indicators.filter((inst) => {
    const p = indPaneRef.current.get(inst.uid);
    return p === undefined || p === 0 || !knownPanes.has(p);
  });
  const qtyStep = instrument?.lot_size && instrument.lot_size > 1 ? instrument.lot_size : 1;
  const bid = live?.bid ?? null;
  const ask = live?.ask ?? null;
  const ltpNow = live?.last_price ?? lastClose();
  const quickTrade = settings.buySellButtons && !!onChartOrder && !hide.positions;

  /** Open the legend's More menu next to the clicked button. */
  const openLegendMenu = (e: React.MouseEvent, kind: "main" | "ind", uid?: string) => {
    const host = containerRef.current?.getBoundingClientRect();
    const b = (e.currentTarget as HTMLElement).getBoundingClientRect();
    if (!host) return;
    setLegendMenu((m) => (m && m.kind === kind && m.uid === uid ? null : { kind, uid, x: b.left - host.left, y: b.bottom - host.top + 2 }));
  };

  const indRow = (inst: IndicatorInstance) => {
    const def = INDICATOR_BY_TYPE.get(inst.type);
    if (!def) return null;
    const args = argsLabel(inst);
    const shown = indPaneRef.current.has(inst.uid);
    return (
      <div key={inst.uid} className={`lg-row lg-ind${inst.visible && shown ? "" : " off"}`} onDoubleClick={() => setDialog({ k: "indSettings", uid: inst.uid })} data-testid={`legend-${inst.type}`}>
        {settings.indTitles && <span className="lg-name" title={def.name}>{def.short}</span>}
        {settings.indArgs && args && <span className="lg-args">{args}</span>}
        <span
          ref={(el) => {
            if (el) indLegendRefs.current.set(inst.uid, el);
            else indLegendRefs.current.delete(inst.uid);
          }}
          className="lg-vals"
        />
        {!shown && inst.visible && <span className="hint">{def.intradayOnly && !intraday ? "intraday only" : "hidden on this interval"}</span>}
        <span className="lg-acts">
          <button type="button" title={inst.visible ? "Hide" : "Show"} onClick={() => toggleIndicator(inst.uid)}>{inst.visible ? "👁" : "◌"}</button>
          <button type="button" title="Settings" onClick={() => setDialog({ k: "indSettings", uid: inst.uid })} data-testid={`ind-settings-${inst.type}`}>⚙</button>
          {inst.type !== "volume" && (
            <button type="button" title="Add alert on this indicator" onClick={() => openAlertDialog(undefined, { target: `ind:${inst.uid}:0`, targetLabel: `${def.short} ${args} · ${def.plots[0].label}` })}>🔔</button>
          )}
          <button type="button" title="Remove" onClick={() => removeIndicator(inst.uid)} data-testid={`ind-remove-${inst.type}`}>✕</button>
          <button type="button" title="More" data-more="" onClick={(e) => openLegendMenu(e, "ind", inst.uid)} data-testid={`ind-more-${inst.type}`}>⋯</button>
        </span>
      </div>
    );
  };

  const scaleMenu = (close: () => void) => (
    <>
      <button type="button" className={`chart-menu-item${autoScale ? " on" : ""}`} onClick={() => { setAutoScale((v) => !v); close(); }}>Auto (fits data to screen)</button>
      <button
        type="button"
        className={`chart-menu-item${settings.lockRatio ? " on" : ""}`}
        disabled={scaleMode === "log"}
        onClick={() => {
          changeSettings({ ...settings, lockRatio: !settings.lockRatio, priceBarRatio: settings.lockRatio ? settings.priceBarRatio : 0 });
          close();
          setTimeout(applyRatioLock, 0);
        }}
        data-testid="scale-lock-ratio"
      >
        Lock price to bar ratio{settings.lockRatio && settings.priceBarRatio ? ` (${settings.priceBarRatio})` : ""}
      </button>
      <button type="button" className={`chart-menu-item${scalePriceOnly ? " on" : ""}`} onClick={() => { setScalePriceOnly((v) => !v); close(); }}>Scale price chart only</button>
      <button type="button" className={`chart-menu-item${invert ? " on" : ""}`} onClick={() => { setInvert((v) => !v); close(); }}>Invert scale<span className="kbd">Alt+I</span></button>
      <div className="chart-menu-sep" />
      {([["normal", "Regular"], ["percent", "Percent"], ["indexed", "Indexed to 100"], ["log", "Logarithmic"]] as [ScaleMode, string][]).map(([m, l]) => (
        <button key={m} type="button" className={`chart-menu-item${scaleMode === m ? " on" : ""}`} disabled={pctCompare} onClick={() => { setScaleMode(m); close(); }}>
          {scaleMode === m ? "● " : "○ "}{l}{m === "percent" ? <span className="kbd">Alt+P</span> : m === "log" ? <span className="kbd">Alt+L</span> : null}
        </button>
      ))}
      <div className="chart-menu-sep" />
      <button type="button" className="chart-menu-item" onClick={() => { changeSettings({ ...settings, scaleSide: side === "right" ? "left" : "right" }); close(); }}>Move scale to {side === "right" ? "left" : "right"}</button>
      <div className="chart-menu-head">Labels</div>
      {([
        ["symbolNameLabel", "Symbol name label"],
        ["lastPriceLabel", "Symbol last price label"],
        ["prevCloseLabel", "Previous day close price label"],
        ["highLowLabels", "High and low price labels"],
        ["avgCloseLabel", "Average close price label"],
        ["bidAskLabels", "Bid and ask labels"],
        ["indNameLabels", "Indicators name labels"],
        ["indValueLabels", "Indicators value labels"],
        ["countdown", "Countdown to bar close"],
      ] as [keyof ChartSettings, string][]).map(([k, l]) => (
        <button key={k} type="button" className="chart-menu-item" disabled={k === "prevCloseLabel" && !intraday} onClick={() => changeSettings({ ...settings, [k]: !settings[k] })}>
          {settings[k] ? "☑ " : "☐ "}{l}
        </button>
      ))}
      <div className="chart-menu-head">Lines</div>
      {([
        ["lastPriceLine", "Price line"],
        ["prevCloseLine", "Previous day close price line"],
        ["highLowLines", "High and low price lines"],
        ["avgCloseLine", "Average close price line"],
        ["bidAskLines", "Bid and ask lines"],
      ] as [keyof ChartSettings, string][]).map(([k, l]) => (
        <button key={k} type="button" className="chart-menu-item" disabled={k === "prevCloseLine" && !intraday} onClick={() => changeSettings({ ...settings, [k]: !settings[k] })}>
          {settings[k] ? "☑ " : "☐ "}{l}
        </button>
      ))}
      {onTrading && trading && (
        <button type="button" className="chart-menu-item" onClick={() => onTrading("plus", !trading.plus)}>{trading.plus ? "☑ " : "☐ "}Plus button</button>
      )}
      <div className="chart-menu-sep" />
      <button type="button" className="chart-menu-item" onClick={() => { setDialog({ k: "settings", tab: "scales" }); close(); }}>More settings…</button>
    </>
  );

  const timeMenu = (close: () => void) => (
    <>
      <div className="chart-menu-head">Time zone</div>
      {TIMEZONES.map((t) => (
        <button key={t.id} type="button" className={`chart-menu-item${settings.timezone === t.id ? " on" : ""}`} onClick={() => { changeSettings({ ...settings, timezone: t.id }); close(); }}>{t.label}</button>
      ))}
      <div className="chart-menu-sep" />
      <button type="button" className="chart-menu-item" onClick={() => changeSettings({ ...settings, hour12: !settings.hour12 })}>{settings.hour12 ? "☑" : "☐"} 12-hour clock</button>
      <button type="button" className="chart-menu-item" onClick={() => changeSettings({ ...settings, sessionBreaks: !settings.sessionBreaks })}>{settings.sessionBreaks ? "☑" : "☐"} Session breaks</button>
      <button type="button" className="chart-menu-item" onClick={() => { setDialog({ k: "settings", tab: "scales" }); close(); }}>Date format…</button>
    </>
  );

  const toolbar = (
    <div className={`chart-toolbar-wrap${tbScroll.left || tbScroll.right ? " scrolls" : ""}`}>
    <div className="chart-toolbar" data-testid="chart-toolbar" ref={toolbarRef} onScroll={() => { measureToolbar(); positionToolbarMenu(); }}>
      {onAction && (
        <div className="chart-group chart-menu-wrap">
          <button type="button" className={`chart-btn tb-logo${menuOpen === "products" ? " on" : ""}`} onClick={menuBtn("products")} title="Products" data-testid="chart-products">◆▾</button>
          {menuOpen === "products" && (
            <div className="chart-menu cdrop" data-testid="products-menu">
              <div className="chart-menu-head">Fyers products</div>
              <a className="chart-menu-item" href="https://trade.fyers.in/" target="_blank" rel="noopener noreferrer" onClick={() => setMenuOpen(null)}>FYERS Web ↗</a>
              <button type="button" className="chart-menu-item" onClick={() => { setMenuOpen(null); onAction("panel:flow"); }}>Smart Charts (order flow)</button>
              <button type="button" className="chart-menu-item" onClick={() => { setMenuOpen(null); onAction("scalper"); }}>Option Scalper</button>
              <button type="button" className="chart-menu-item" onClick={() => { setMenuOpen(null); setDialog({ k: "whatsnew" }); }} data-testid="whats-new">What's New</button>
            </div>
          )}
        </div>
      )}
      <button type="button" className="chart-btn chart-sym-btn" onClick={() => setDialog({ k: "symbol", q: shortName ?? "", select: true })} title={onSymbolChange ? "Symbol search (type on the chart)" : symbol} disabled={!onSymbolChange} data-testid="chart-symbol">
        <span className="ico">⌕</span><span className="sym">{shortName}</span>
      </button>
      {/* compare */}
      <div className="chart-group chart-menu-wrap">
        <button type="button" className={`chart-btn${compares.length > 0 ? " on" : ""}`} onClick={menuBtn("compare")} title="Compare or add symbol" data-testid="chart-compare-btn">
          ⊕{compares.length > 0 ? ` ${compares.length}` : ""}
        </button>
        {menuOpen === "compare" && (
          <div className="chart-menu cmp-menu" data-testid="chart-compare-menu">
            <div className="chart-menu-head">Compare symbol</div>
            <input type="text" className="chart-menu-input" placeholder="search symbol…" value={compareQuery} onChange={(e) => setCompareQuery(e.target.value)} autoFocus data-testid="chart-compare-input" />
            {compareHits
              .filter((h) => !compares.some((c) => c.symbol === h.symbol))
              .map((h) => (
                <div key={h.symbol} className="cmp-hit">
                  <span className="cmp-name">{h.name} <span className="hint">{h.symbol}</span></span>
                  <span className="cmp-adds">
                    {COMPARE_MODES.map((m) => (
                      <button
                        key={m.v}
                        type="button"
                        className="cmp-add"
                        disabled={compares.length >= 4}
                        title={compares.length >= 4 ? "Up to 4 symbols" : `Add on ${m.l.toLowerCase()}`}
                        onClick={() => {
                          setCompares((c) => (c.length >= 4 || c.some((x) => x.symbol === h.symbol) ? c : [...c, { symbol: h.symbol, name: h.name, color: COMPARE_COLORS[c.length % COMPARE_COLORS.length], mode: m.v }]));
                          setCompareQuery("");
                          setCompareHits([]);
                        }}
                        data-testid={m.v === "percent" ? `chart-compare-add-${h.symbol}` : `chart-compare-add-${m.v}-${h.symbol}`}
                      >
                        {m.l}
                      </button>
                    ))}
                  </span>
                </div>
              ))}
            {compares.length === 0 && compareHits.length === 0 && (
              <div className="cmp-empty" data-testid="chart-compare-empty">
                <svg width="56" height="40" viewBox="0 0 56 40" aria-hidden="true">
                  <polyline points="2,34 14,22 24,28 36,12 54,18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" />
                  <polyline points="2,26 14,30 24,16 36,22 54,6" fill="none" stroke="#42A5F5" strokeWidth="2" strokeDasharray="4 3" strokeLinejoin="round" />
                </svg>
                <div>No symbols here yet — why not add some?</div>
                <div className="hint">Search above, then pick Same % scale, New price scale or New pane.</div>
              </div>
            )}
            {compares.length > 0 && <div className="chart-menu-sep" />}
            {compares.map((c) => (
              <div key={c.symbol} className="chart-menu-row">
                <span><span className="chart-dot" style={{ background: c.color }} />{c.name}</span>
                <span className="cmp-row-acts">
                  <select
                    className="cmp-mode"
                    value={c.mode ?? "percent"}
                    onChange={(e) => setCompares((l) => l.map((x) => (x.symbol === c.symbol ? { ...x, mode: e.target.value as CompareMode } : x)))}
                    aria-label={`${c.name} scale`}
                    data-testid={`chart-compare-mode-${c.symbol}`}
                  >
                    {COMPARE_MODES.map((m) => <option key={m.v} value={m.v}>{m.l}</option>)}
                  </select>
                  <button type="button" className="chart-menu-x" onClick={() => setCompares((l) => l.map((x) => (x.symbol === c.symbol ? { ...x, hidden: !x.hidden } : x)))} title={c.hidden ? "Show" : "Hide"}>{c.hidden ? "◌" : "👁"}</button>
                  <button type="button" className="chart-menu-x" onClick={() => setCompares((list) => list.filter((x) => x.symbol !== c.symbol))} title="Remove" data-testid={`chart-compare-del-${c.symbol}`}>✕</button>
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
      <span className="chart-tsep" />
      {/* intervals */}
      {favIntervals.filter((k) => parseInterval(k)).map((k) => (
        <button key={k} type="button" className={`chart-btn iv${iv === k ? " on" : ""}`} onClick={() => changeInterval(k)} title={intervalLongLabel(k)} data-testid={`chart-iv-${k}`}>{intervalLabel(k)}</button>
      ))}
      <div className="chart-group chart-menu-wrap">
        <button type="button" className={`chart-btn${!favIntervals.includes(iv) ? " on" : ""}`} onClick={menuBtn("interval")} title="Time interval" data-testid="chart-tf">
          {!favIntervals.includes(iv) ? `${ivLabel} ` : ""}▾
        </button>
        {menuOpen === "interval" && (
          <div className="chart-menu chart-iv-menu">
            <div className="chart-menu-head">Add custom interval</div>
            <div className="chart-menu-row">
              <input type="number" className="chart-menu-input" min={1} value={customIv.n} onChange={(e) => setCustomIv((c) => ({ ...c, n: Math.max(1, Math.floor(Number(e.target.value) || 1)) }))} aria-label="Custom interval" style={{ width: 60 }} />
              <select className="cform-sel" value={customIv.unit} onChange={(e) => setCustomIv((c) => ({ ...c, unit: e.target.value }))} aria-label="Unit">
                <option value="S">seconds</option>
                <option value="m">minutes</option>
                <option value="H">hours</option>
                <option value="D">days</option>
                <option value="W">weeks</option>
                <option value="M">months</option>
              </select>
              <button
                type="button"
                className="cbtn"
                onClick={() => {
                  const key = normalizeInterval(customIv.unit === "m" ? String(customIv.n) : `${customIv.n}${customIv.unit}`);
                  if (!key) return;
                  if (parseInterval(key)?.unit === "S" && parseInterval(key)!.n % 5) {
                    addToast("Seconds intervals must be a multiple of 5");
                    return;
                  }
                  setCustomIntervals((l) => (l.includes(key) || INTERVAL_SECTIONS.some((s) => s.keys.includes(key)) ? l : [...l, key]));
                  changeInterval(key);
                  setMenuOpen(null);
                }}
              >
                Add
              </button>
            </div>
            {[...INTERVAL_SECTIONS, ...(customIntervals.length ? [{ title: "Custom", keys: customIntervals }] : [])].map((sec) => (
              <div key={sec.title} className="chart-menu-section">
                <button
                  type="button"
                  className="chart-menu-head iv-sec-head"
                  onClick={() => setIvCollapsed((l) => toggleIn(l, sec.title))}
                  aria-expanded={!ivCollapsed.includes(sec.title)}
                  data-testid={`chart-iv-sec-${sec.title}`}
                >
                  <span>{sec.title}</span>
                  <span className="caret">{ivCollapsed.includes(sec.title) ? "▸" : "▾"}</span>
                </button>
                {!ivCollapsed.includes(sec.title) && sec.keys.map((k) => (
                  <div key={k} className={`chart-menu-row iv-row${iv === k ? " on" : ""}`}>
                    <button type="button" className="chart-menu-item" onClick={() => { changeInterval(k); setMenuOpen(null); }} data-testid={`chart-ivm-${k}`}>{intervalLongLabel(k)}</button>
                    {star(favIntervals.includes(k), () => setFavIntervals((l) => toggleIn(l, k)), `Favorite ${intervalLongLabel(k)}`)}
                    {sec.title === "Custom" && <button type="button" className="chart-menu-x" title="Remove" onClick={() => setCustomIntervals((l) => l.filter((x) => x !== k))}>✕</button>}
                  </div>
                ))}
              </div>
            ))}
          </div>
        )}
      </div>
      <span className="chart-tsep" />
      {/* chart style */}
      {favKinds.map((k) => {
        const d = CHART_KINDS.find((x) => x.id === k);
        return d ? <button key={k} type="button" className={`chart-btn${chartKind === k ? " on" : ""}`} onClick={() => setChartKind(k)} title={d.label}>{d.icon}</button> : null;
      })}
      <div className="chart-group chart-menu-wrap">
        <button type="button" className={`chart-btn${!favKinds.includes(chartKind) ? " on" : ""}`} onClick={menuBtn("kind")} title={`Bar's style: ${CHART_KINDS.find((k) => k.id === chartKind)?.label}`} data-testid="chart-type">
          {!favKinds.includes(chartKind) ? `${CHART_KINDS.find((k) => k.id === chartKind)?.icon} ` : ""}▾
        </button>
        {menuOpen === "kind" && (
          <div className="chart-menu">
            {CHART_KINDS.map((k) => (
              <div key={k.id} className={`chart-menu-row${chartKind === k.id ? " on" : ""}`}>
                <button type="button" className="chart-menu-item" onClick={() => { setChartKind(k.id); setMenuOpen(null); }} data-testid={`chart-kind-${k.id}`}>
                  <span className="ico">{k.icon}</span> {k.label}
                </button>
                {star(favKinds.includes(k.id), () => setFavKinds((l) => toggleIn(l, k.id)), `Pin ${k.label}`)}
              </div>
            ))}
            {BRICK_KINDS.has(chartKind) && (
              <>
                <div className="chart-menu-sep" />
                <button type="button" className="chart-menu-item" onClick={() => { setDialog({ k: "settings", tab: "symbol" }); setMenuOpen(null); }} data-testid="chart-kind-inputs">
                  ⚙ {CHART_KINDS.find((k) => k.id === chartKind)?.label} inputs…
                </button>
              </>
            )}
          </div>
        )}
      </div>
      <span className="chart-tsep" />
      <button type="button" className={`chart-btn${dialog?.k === "indicators" ? " on" : ""}`} onClick={() => setDialog({ k: "indicators" })} title="Indicators, metrics & strategies ( / )" data-testid="chart-indicators-btn">
        ƒx Indicators
      </button>
      <div className="chart-group chart-menu-wrap">
        <button type="button" className="chart-btn" onClick={menuBtn("templates")} title="Indicator templates" data-testid="chart-templates-btn">▦</button>
        {menuOpen === "templates" && (
          <div className="chart-menu">
            <button type="button" className="chart-menu-item" onClick={saveIndTemplate} data-testid="chart-template-save">Save indicator template…</button>
            {templates.length > 0 && <div className="chart-menu-sep" />}
            {templates.map((t) => (
              <div key={t.name} className="chart-menu-row">
                <button type="button" className="chart-menu-item" onClick={() => applyIndTemplate(t)} title={templateScope(t, intervalLongLabel) ? `Also switches to ${templateScope(t, intervalLongLabel)}` : undefined}>
                  {t.name} <span className="hint">{t.items.length}{templateScope(t, intervalLabel) ? ` · ${templateScope(t, intervalLabel)}` : ""}</span>
                </button>
                <button type="button" className="chart-menu-x" title="Rename template" onClick={() => renameIndTemplate(t)} data-testid={`chart-template-rename-${t.name}`}>✎</button>
                <button type="button" className="chart-menu-x" title="Delete template" onClick={() => deleteIndTemplate(t.name)}>✕</button>
              </div>
            ))}
          </div>
        )}
      </div>
      <span className="chart-tsep" />
      {onChartOrder && (
        <button
          type="button"
          className={`chart-btn qt-toggle${instant ? " on" : ""}`}
          onClick={() => {
            if (onTrading) {
              onTrading("instant", !instant);
              if (!settings.buySellButtons) changeSettings({ ...settings, buySellButtons: true });
            } else changeSettings({ ...settings, buySellButtons: !settings.buySellButtons });
          }}
          role="switch"
          aria-checked={instant}
          title={onTrading ? `Quick trade: one-click trading ${instant ? "ON — BUY / SELL send at market" : "OFF — BUY / SELL open the order window"}` : "Quick trade — buy / sell buttons on the chart"}
          data-testid="chart-quick-trade"
        >
          ⚡<span className={`qt-switch${instant ? " on" : ""}`} aria-hidden="true"><i /></span>
        </button>
      )}
      {/* alerts */}
      <div className="chart-group chart-menu-wrap">
        <button type="button" className={`chart-btn${drawMode === "alert" || alerts.some((a) => a.active) ? " on" : ""}`} onClick={menuBtn("alerts")} title="Alerts" data-testid="chart-alerts-btn">
          🔔{alerts.filter((a) => a.active).length > 0 ? ` ${alerts.filter((a) => a.active).length}` : ""}
        </button>
        {menuOpen === "alerts" && (
          <div className="chart-menu" data-testid="chart-alerts-menu">
            <button type="button" className="chart-menu-item" onClick={() => { setMenuOpen(null); setDrawMode("alert"); }} data-testid="chart-alert-add">⊕ Add alert (click a price)</button>
            <button type="button" className="chart-menu-item" onClick={() => { setMenuOpen(null); openAlertDialog(); }} data-testid="chart-alert-create">Create alert…<span className="kbd">Alt+A</span></button>
            {onAction && <button type="button" className="chart-menu-item" onClick={() => { setMenuOpen(null); onAction("panel:alerts"); }}>Alerts manager & log</button>}
            {alerts.length > 0 && <div className="chart-menu-sep" />}
            {alerts.map((a) => (
              <div key={a.id} className={`chart-menu-row${a.active ? "" : " off"}`}>
                <button type="button" className="chart-menu-item" onClick={() => { setMenuOpen(null); setDialog({ k: "alert", alert: a }); }}>{describeCond(a)}</button>
                <button type="button" className="chart-menu-x" onClick={() => removeAlert(a.id)} title="Remove alert" data-testid={`chart-alert-del-${a.id}`}>✕</button>
              </div>
            ))}
          </div>
        )}
      </div>
      {pinnedTools.map((id) => {
        const t = TOOLS_MENU.find((x) => x.id === id);
        if (!t || (t.host && !onAction)) return null;
        if (t.href)
          return (
            <a key={id} className="chart-btn pinned-tool" href={t.href} target="_blank" rel="noopener noreferrer" title={`${t.label} (opens Fyers)`} data-testid={`chart-tool-${id}`}>
              {t.icon}<span className="pt-label">{t.label}</span>
            </a>
          );
        return (
          <button key={id} type="button" className={`chart-btn pinned-tool${id === "replay" && replay.on ? " on" : ""}`} onClick={() => runTool(id)} title={t.label} data-testid={`chart-tool-${id}`}>
            {t.icon}
            {t.short && <span className="pt-label">{t.short}</span>}
            {t.badge && <span className="ind-new">{t.badge}</span>}
          </button>
        );
      })}
      <div className="chart-group chart-menu-wrap">
        <button type="button" className={`chart-btn${menuOpen === "tools" ? " on" : ""}`} onClick={menuBtn("tools")} title="Tools" data-testid="chart-tools-menu">Tools ▾</button>
        {menuOpen === "tools" && (
          <div className="chart-menu">
            {TOOLS_MENU.filter((t) => !t.host || onAction).map((t) => (
              <div key={t.id} className="chart-menu-row">
                {t.href ? (
                  <a className="chart-menu-item" href={t.href} target="_blank" rel="noopener noreferrer" onClick={() => setMenuOpen(null)} title="Opens Fyers in a new tab">
                    <span className="ico">{t.icon}</span> {t.label} <span className="hint">↗</span>
                  </a>
                ) : (
                <button type="button" className="chart-menu-item" onClick={() => runTool(t.id)}>
                  <span className="ico">{t.icon}</span> {t.label}{t.badge && <span className="badge-new">{t.badge}</span>}
                  {t.id === "theme" && <span className="hint"> ({settings.theme === "light" ? "light" : "dark"})</span>}
                  {t.id === "privacy" && privacy && <span className="hint"> (on)</span>}
                </button>
                )}
                {star(pinnedTools.includes(t.id), () => setPinnedTools((l) => toggleIn(l, t.id)), `Pin ${t.label}`)}
              </div>
            ))}
          </div>
        )}
      </div>
      <span className="chart-tsep" />
      <button type="button" className="chart-btn" disabled={!canUndo} onClick={undo} title={canUndo ? `Undo ${undoLabel} (Ctrl+Z)` : "Nothing to undo"} data-testid="chart-undo">↶</button>
      <button type="button" className="chart-btn" disabled={!canRedo} onClick={redo} title={canRedo ? `Redo ${redoLabel} (Ctrl+Y)` : "Nothing to redo"} data-testid="chart-redo">↷</button>
      <div className="chart-group chart-right" role="group" aria-label="chart controls">
        <button type="button" className="chart-btn" onClick={() => setDialog({ k: "palette" })} title="Quick search (Ctrl+K)" data-testid="chart-quick-search">⌕</button>
        <button type="button" className="chart-btn" onClick={() => setDialog({ k: "settings" })} title="Settings" data-testid="chart-settings-btn">⚙</button>
        {multi && onAction && (
          <button type="button" className={`chart-btn${maximized ? " on" : ""}`} onClick={() => onAction("maximize")} title={maximized ? "Restore chart (Alt+Enter)" : "Maximize chart (Alt+Enter)"} data-testid="chart-maximize">{maximized ? "❐" : "⬚"}</button>
        )}
        <button type="button" className={`chart-btn${fullscreen ? " on" : ""}`} onClick={toggleFullscreen} title={fullscreen ? "Exit fullscreen (Esc)" : "Fullscreen (Shift+F)"} data-testid="chart-fullscreen-btn">
          {fullscreen ? "🗕" : "⛶"}
        </button>
        <div className="chart-menu-wrap">
          <button type="button" className="chart-btn" onClick={menuBtn("snapshot")} title="Take a snapshot" data-testid="chart-screenshot">📷</button>
          {menuOpen === "snapshot" && (
            <div className="chart-menu cdrop right">
              <button type="button" className="chart-menu-item" onClick={() => void takeSnapshot("download")} data-testid="chart-snapshot-download">Download image<span className="kbd">Ctrl+Alt+S</span></button>
              <button type="button" className="chart-menu-item" onClick={() => void takeSnapshot("copy")}>Copy image<span className="kbd">Ctrl+Shift+S</span></button>
              <button type="button" className="chart-menu-item" onClick={() => void takeSnapshot("link")} data-testid="chart-snapshot-link">Copy link<span className="kbd">Alt+S</span></button>
              <button type="button" className="chart-menu-item" onClick={() => void takeSnapshot("open")}>Open in new tab</button>
              <button type="button" className="chart-menu-item" onClick={() => void takeSnapshot("tweet")}>Tweet image</button>
            </div>
          )}
        </div>
        {onAction && (
          <button type="button" className="chart-btn" onClick={() => onAction("logout")} title="Logout from the trading terminal (disconnects Fyers)" data-testid="chart-logout">⏻</button>
        )}
      </div>
    </div>
      {tbScroll.left && (
        <button type="button" className="tb-arrow left" onClick={() => toolbarRef.current?.scrollBy({ left: -240, behavior: "smooth" })} title="Scroll left" aria-label="Scroll toolbar left">‹</button>
      )}
      {tbScroll.right && (
        <button type="button" className="tb-arrow right" onClick={() => toolbarRef.current?.scrollBy({ left: 240, behavior: "smooth" })} title="More tools" aria-label="Scroll toolbar right" data-testid="toolbar-more">›</button>
      )}
    </div>
  );

  const rangeBar = (
    <div className="chart-rangebar" data-testid="chart-rangebar">
      {RANGES.map((r) => (
        <button key={r.id} type="button" className="crange-btn" title={r.title} onClick={() => applyRange(r)} data-testid={`chart-range-${r.id}`}>{r.id}</button>
      ))}
      <span className="crange-sep" />
      <button type="button" className="crange-btn" onClick={() => setDialog({ k: "goto" })} title="Go to date" data-testid="chart-goto">📅</button>
      <span className="grow" />
      <div className="chart-menu-wrap">
        <Clock tz={settings.timezone} hour12={settings.hour12} onClick={menuBtn("tz")} />
        {menuOpen === "tz" && <div className="chart-menu cdrop up right">{timeMenu(() => setMenuOpen(null))}</div>}
      </div>
      <span className="crange-sep" />
      <button type="button" className={`crange-btn${scaleMode === "percent" || pctCompare ? " on" : ""}`} onClick={() => setScaleMode((m) => (m === "percent" ? "normal" : "percent"))} disabled={pctCompare} title="Toggle percentage (Alt+P)" data-testid="chart-scale-pct">%</button>
      <button type="button" className={`crange-btn${scaleMode === "log" ? " on" : ""}`} onClick={() => setScaleMode((m) => (m === "log" ? "normal" : "log"))} disabled={pctCompare} title="Toggle log scale (Alt+L)" data-testid="chart-scale-log">log</button>
      <button type="button" className={`crange-btn${autoScale ? " on" : ""}`} onClick={() => setAutoScale((v) => !v)} title="Toggle auto scale" data-testid="chart-scale-auto">auto</button>
    </div>
  );

  const legendMenuEl = legendMenu && (() => {
    const close = () => setLegendMenu(null);
    const item = (label: ReactNode, run: () => void, extra?: { testid?: string; on?: boolean; disabled?: boolean; kbd?: string }) => (
      <button type="button" className={`chart-menu-item${extra?.on ? " on" : ""}`} disabled={extra?.disabled} onClick={() => { run(); close(); }} data-testid={extra?.testid}>
        {label}
        {extra?.kbd && <span className="kbd">{extra.kbd}</span>}
      </button>
    );
    const hostH = containerRef.current?.clientHeight ?? 600;
    const style = { left: Math.max(0, Math.min(legendMenu.x, (containerRef.current?.clientWidth ?? 600) - 250)), top: legendMenu.y, maxHeight: hostH - 8, overflowY: "auto" as const };
    // keep the menu inside the chart: slide it up when it would run off the bottom
    const fit = (el: HTMLDivElement | null) => {
      if (!el) return;
      const maxTop = hostH - el.offsetHeight - 4;
      if (el.offsetTop > maxTop) el.style.top = `${Math.max(4, maxTop)}px`;
    };
    if (legendMenu.kind === "main") {
      const last = viewRef.current[viewRef.current.length - 1]?.close;
      return (
        <div ref={fit} className="chart-menu chart-ctx chart-overlay-ui" style={style} onMouseDown={(e) => e.stopPropagation()} data-testid="legend-menu">
          <div className="chart-menu-head">{titleText}</div>
          {item(mainHidden ? "Show series" : "Hide series", () => setMainHidden((v) => !v))}
          {item("Settings…", () => setDialog({ k: "settings", tab: "symbol" }))}
          {onSymbolChange && item("Change symbol…", () => setDialog({ k: "symbol", q: shortName ?? "", select: true }))}
          {item("Change interval…", () => setDialog({ k: "interval", txt: "" }))}
          {item("Create alert…", () => openAlertDialog(), { kbd: "Alt+A" })}
          {last != null && item(`Copy last price ${fmtPrice(last)}`, () => { void navigator.clipboard?.writeText(String(last)).catch(() => undefined); addToast(`copied ${fmtPrice(last)}`); })}
          {onAction && item(`Add ${shortName} to watchlist`, () => onAction("watch:add"), { kbd: "Alt+W" })}
          {onAction && item("Symbol details", () => onAction("panel:details"))}
          {item("Insights", () => setDialog({ k: "insights" }))}
        </div>
      );
    }
    const inst = indicators.find((x) => x.uid === legendMenu.uid);
    const def = inst ? INDICATOR_BY_TYPE.get(inst.type) : undefined;
    if (!inst || !def) return null;
    const myPane = indPaneRef.current.get(inst.uid);
    const panes = paneList().filter((p) => p.i !== myPane);
    return (
      <div ref={fit} className="chart-menu chart-ctx chart-overlay-ui" style={style} onMouseDown={(e) => e.stopPropagation()} data-testid="legend-ind-menu">
        <div className="chart-menu-head">{def.short} {argsLabel(inst)}</div>
        {def.plots[0] && inst.type !== "volume" && item(`Add alert on ${def.short}…`, () => openAlertDialog(undefined, { target: `ind:${inst.uid}:0`, targetLabel: `${def.short} ${argsLabel(inst)} · ${def.plots[0].label}` }))}
        {item("Settings…", () => setDialog({ k: "indSettings", uid: inst.uid }))}
        {item(inst.visible ? "Hide" : "Show", () => toggleIndicator(inst.uid))}
        <div className="chart-menu-head">Visual order</div>
        {item("Bring to front", () => reorderIndicator(inst.uid, "front"))}
        {item("Send to back", () => reorderIndicator(inst.uid, "back"))}
        <div className="chart-menu-head">Move to</div>
        {item("New pane below", () => moveIndicatorTo(inst.uid, "own"), { disabled: myPane !== 0 && inst.pane !== "main" && !def.overlay && inst.pane === undefined && panes.length === 0, testid: "ind-move-new" })}
        {myPane !== 0 && item("Price pane", () => moveIndicatorTo(inst.uid, "main"), { testid: "ind-move-main" })}
        {panes.filter((p) => p.i !== 0).map((p) => (
          <button key={p.i} type="button" className="chart-menu-item" onClick={() => { moveIndicatorTo(inst.uid, p.insts[0].uid); close(); }}>
            Pane {p.i}: {p.insts.map((x) => INDICATOR_BY_TYPE.get(x.type)?.short ?? x.type).join(", ")}
          </button>
        ))}
        <div className="chart-menu-head">Pin to scale</div>
        {item("Default", () => pinIndicatorScale(inst.uid, undefined), { on: !inst.scale })}
        {item("Left scale", () => pinIndicatorScale(inst.uid, "left"), { on: inst.scale === "left" })}
        {item("Right scale", () => pinIndicatorScale(inst.uid, "right"), { on: inst.scale === "right" })}
        {item("New scale (overlay)", () => pinIndicatorScale(inst.uid, "new"), { on: inst.scale === "new" })}
        <div className="chart-menu-sep" />
        {item("Copy", () => copyIndicator(inst.uid), { testid: "ind-copy" })}
        {item("Duplicate", () => setIndicators([...indicatorsRef.current, { ...inst, uid: newUid() }], "duplicate indicator"))}
        {item("Save as default", () => {
          const all = loadJson<Record<string, Partial<IndicatorInstance>>>("chart:indDefaults", {});
          all[inst.type] = instanceDefaults(inst);
          saveJson("chart:indDefaults", all);
          addToast(`saved as the default for new ${def.short}`);
        })}
        {item("About…", () => setDialog({ k: "about", uid: inst.uid }))}
        <div className="chart-menu-sep" />
        {item(<span className="down">Remove</span>, () => removeIndicator(inst.uid))}
      </div>
    );
  })();

  function openTradeFly(row: HTMLElement): void {
    const host = containerRef.current?.getBoundingClientRect();
    if (!host) return;
    const r = row.getBoundingClientRect();
    const w = 290;
    const right = r.right - host.left + 4;
    const x = right + w > host.width ? Math.max(0, r.left - host.left - w - 4) : right;
    setTradeFly({ x, y: Math.max(0, Math.min(r.top - host.top - 8, host.height - 200)) });
  }

  const tradeFlyEl = ctx && tradeFly && !ctx.confirm && ctx.area === "pane" && (
    <div className="chart-menu chart-ctx chart-overlay-ui ctx-fly" style={{ left: tradeFly.x, top: tradeFly.y }} onMouseDown={(e) => e.stopPropagation()} data-testid="ctx-trade-menu">
      {onChartOrder &&
        ctxOrders(ctx.price).filter((o) => o.price != null).map((o, i) => (
          <button key={`${o.side}-${o.type}`} type="button" className={`chart-menu-item ctx-${o.side.toLowerCase()}`} onClick={() => pickOrder(o, ctx)}>
            {o.side === "BUY" ? "⌃" : "⌄"} {orderLabel(o)}{i === 0 && <span className="kbd">Alt+Shift+B</span>}
          </button>
        ))}
      {onPickPrice && (
        <button type="button" className="chart-menu-item" onClick={() => { onPickPrice(ctx.price); setCtx(null); }}>
          ⤷ Create new order at {fmtPrice(ctx.price)}…<span className="kbd">Shift+T</span>
        </button>
      )}
      {onChartOrder && trading && <button type="button" className="chart-menu-item" onClick={() => { setDialog({ k: "settings", tab: "trading" }); setCtx(null); }}>Trading settings…</button>}
    </div>
  );

  const ctxEl = ctx && (
    <div
      className="chart-menu chart-ctx chart-overlay-ui"
      style={{
        left: Math.max(0, Math.min(ctx.x, (containerRef.current?.clientWidth ?? 600) - 250)),
        top: Math.max(0, Math.min(ctx.y, Math.max(0, (containerRef.current?.clientHeight ?? 400) - 320))),
      }}
      onMouseDown={(e) => e.stopPropagation()}
      onMouseOver={(e) => {
        if (tradeFly && !(e.target as HTMLElement).closest('[data-testid="ctx-trade"]')) setTradeFly(null);
      }}
      ref={(el) => {
        const host = containerRef.current;
        if (!el || !host) return;
        const maxTop = host.clientHeight - el.offsetHeight - 4;
        if (el.offsetTop > maxTop) el.style.top = `${Math.max(0, maxTop)}px`;
      }}
      data-testid="chart-ctx"
    >
      {ctx.confirm ? (
        <>
          <div className="chart-menu-head">Confirm — real order</div>
          <div className="chart-ctx-confirm">{orderLabel(ctx.confirm)} · {shortName} · intraday</div>
          <div className="chart-ctx-actions">
            <button type="button" className={`ticket-side-btn ${ctx.confirm.side === "BUY" ? "buy" : "sell"} on`} autoFocus onClick={() => submitOrder(ctx.confirm!)} data-testid="chart-ctx-place">
              Place {ctx.confirm.side}
            </button>
            <button type="button" className="ticket-side-btn" onClick={() => setCtx(null)}>Cancel</button>
          </div>
        </>
      ) : ctx.area === "price" ? (
        scaleMenu(() => setCtx(null))
      ) : ctx.area === "time" ? (
        timeMenu(() => setCtx(null))
      ) : ctx.area === "drawing" && ctx.drawingId ? (
        (() => {
          const d = drawingsList.find((x) => x.id === ctx.drawingId);
          if (!d) return null;
          const close = () => setCtx(null);
          return (
            <>
              <div className="chart-menu-head">{d.name || TOOL_BY_ID.get(d.type)?.label}</div>
              <button type="button" className="chart-menu-item" onClick={() => { cloneDrawing(d.id); close(); }}>Clone</button>
              <button type="button" className="chart-menu-item" onClick={() => { clipboardRef.current = d; addToast("drawing copied"); close(); }}>Copy<span className="kbd">Ctrl+C</span></button>
              <div className="chart-menu-sep" />
              <button type="button" className="chart-menu-item" onClick={() => { reorderDrawing(d.id, "front"); close(); }}>Bring to front</button>
              <button type="button" className="chart-menu-item" onClick={() => { reorderDrawing(d.id, "forward"); close(); }}>Bring forward</button>
              <button type="button" className="chart-menu-item" onClick={() => { reorderDrawing(d.id, "backward"); close(); }}>Send backward</button>
              <button type="button" className="chart-menu-item" onClick={() => { reorderDrawing(d.id, "back"); close(); }}>Send to back</button>
              <div className="chart-menu-sep" />
              <button type="button" className="chart-menu-item" onClick={() => { updateDrawing(d.id, { hidden: true }, "hide drawing"); selectDrawing(null); close(); }}>Hide</button>
              <button type="button" className="chart-menu-item" onClick={() => { updateDrawing(d.id, { locked: !d.locked }, d.locked ? "unlock" : "lock"); close(); }}>{d.locked ? "Unlock" : "Lock"}</button>
              {drawingValueAt(d, viewRef.current[viewRef.current.length - 1]?.time ?? 0, drawingDeps()) !== null && (
                <button type="button" className="chart-menu-item" onClick={() => { openAlertDialog(undefined, { target: `draw:${d.id}`, targetLabel: d.name || TOOL_BY_ID.get(d.type)?.label }); close(); }}>Add alert on this line</button>
              )}
              <div className="chart-menu-head">Template</div>
              {drawTemplates(d.type).map((t) => (
                <button key={t.name} type="button" className="chart-menu-item" onClick={() => { applyDrawTemplate(d, t.name); close(); }}>▦ {t.name}</button>
              ))}
              <button type="button" className="chart-menu-item" onClick={() => { applyDrawTemplate(d, null); close(); }}>Apply default</button>
              <button type="button" className="chart-menu-item" onClick={() => { close(); saveDrawTemplate(d); }}>Save drawing template as…</button>
              <button type="button" className="chart-menu-item" onClick={() => { toolDefaultsRef.current = { ...toolDefaultsRef.current, [d.type]: { ...(d.style ?? {}), levels: d.style?.levels } }; saveJson("chart:drawStyles", toolDefaultsRef.current); addToast("saved as the default style"); close(); }}>Save as default</button>
              <div className="chart-menu-sep" />
              <button type="button" className="chart-menu-item" onClick={() => { setDialog({ k: "drawSettings", id: d.id }); close(); }}>Settings…</button>
              <div className="chart-menu-sep" />
              <button type="button" className="chart-menu-item ctx-sell" onClick={() => { deleteDrawing(d.id); close(); }}>Remove<span className="kbd">Del</span></button>
            </>
          );
        })()
      ) : (
        <>
          <div className="chart-menu-head">{shortName} · {fmtPrice(ctx.price)}</div>
          <button type="button" className="chart-menu-item" onClick={() => { resetView(); setCtx(null); }}>Reset chart view<span className="kbd">Alt+R</span></button>
          <button type="button" className="chart-menu-item" onClick={() => { void navigator.clipboard?.writeText(String(ctx.price)).catch(() => undefined); addToast(`copied ${fmtPrice(ctx.price)}`); setCtx(null); }}>Copy price {fmtPrice(ctx.price)}</button>
          {clipboardRef.current && <button type="button" className="chart-menu-item" onClick={() => { pasteDrawing(); setCtx(null); }}>Paste<span className="kbd">Ctrl+V</span></button>}
          {indicatorClipboard.current != null && <button type="button" className="chart-menu-item" onClick={() => { pasteIndicator(); setCtx(null); }} data-testid="ctx-paste-indicator">Paste indicator</button>}
          <div className="chart-menu-sep" />
          <button type="button" className="chart-menu-item" onClick={() => { addAlert(ctx.price); setCtx(null); }}>
            🔔 Add alert on {shortName} at {fmtPrice(ctx.price)}<span className="kbd">Alt+A</span>
          </button>
          <button type="button" className="chart-menu-item" onClick={() => { openAlertDialog(ctx.price); setCtx(null); }}>Create alert…</button>
          {(onChartOrder || onPickPrice) && (
            <button
              type="button"
              className={`chart-menu-item chart-menu-subrow${tradeFly ? " on" : ""}`}
              data-testid="ctx-trade"
              onMouseEnter={(e) => openTradeFly(e.currentTarget)}
              onClick={(e) => openTradeFly(e.currentTarget)}
            >
              Trade<span className="kbd">▸</span>
            </button>
          )}
          <div className="chart-menu-sep" />
          <button type="button" className="chart-menu-item" onClick={() => { addHLine(ctx.price); setCtx(null); }}>
            ─ Draw horizontal line at {fmtPrice(ctx.price)}<span className="kbd">Alt+H</span>
          </button>
          {onAction && <button type="button" className="chart-menu-item" onClick={() => { onAction("watch:add"); setCtx(null); }}>Add {shortName} to watchlist<span className="kbd">Alt+W</span></button>}
          <button type="button" className="chart-menu-item" onClick={() => { setLockedTime(lockedTime === null && ctx.time !== null ? ctx.time : null); setCtx(null); }}>
            {lockedTime !== null ? "Unlock vertical cursor line" : "Lock vertical cursor line by time"}
          </button>
          {onAction && <button type="button" className="chart-menu-item" onClick={() => { onAction("panel:tree"); setCtx(null); }}>Object Tree…</button>}
          {indicators.length > 0 && <button type="button" className="chart-menu-item" onClick={() => { removeKind("indicators"); setCtx(null); }}>Remove {indicators.length} indicator{indicators.length === 1 ? "" : "s"}</button>}
          {drawCount > 0 && <button type="button" className="chart-menu-item" onClick={() => { clearDrawings(); setCtx(null); }}>Remove {drawCount} drawing{drawCount === 1 ? "" : "s"}</button>}
          <button type="button" className="chart-menu-item" onClick={() => { changeSettings({ ...settings, showMarks: !settings.showMarks }); setCtx(null); }}>{settings.showMarks ? "Hide marks on bars" : "Show marks on bars"}</button>
          <button type="button" className="chart-menu-item" onClick={() => { changeSettings({ ...settings, highLowLabels: !settings.highLowLabels, highLowLines: !settings.highLowLabels }); setCtx(null); }}>{settings.highLowLabels ? "☑" : "☐"} Highs & lows</button>
          {onAction && <button type="button" className="chart-menu-item" onClick={() => { onAction("panel:chain"); setCtx(null); }}>Option chain</button>}
          {onAction && <button type="button" className="chart-menu-item" onClick={() => { onAction("panel:details"); setCtx(null); }}>Symbol details</button>}
          {onAction && <button type="button" className="chart-menu-item" onClick={() => { onAction("panel:futures"); setCtx(null); }}>Futures chain</button>}
          {onAction && <button type="button" className="chart-menu-item" onClick={() => { changeSettings({ ...settings, showEvents: true, showMarks: true }); onAction("panel:details"); setCtx(null); }} title="Filed announcements as marks on bars + the headlines list">Corporate actions</button>}
          {onAction && <button type="button" className="chart-menu-item" onClick={() => { onAction("panel:tape"); setCtx(null); }}>Time & sales</button>}
          {onAction && <button type="button" className="chart-menu-item" onClick={() => { onAction("panel:depth"); setCtx(null); }}>Market depth</button>}
          {onAction && <button type="button" className="chart-menu-item" onClick={() => { onAction("panel:strategy"); setCtx(null); }}>Strategy builder</button>}
          <button type="button" className="chart-menu-item" onClick={() => { setDialog({ k: "insights" }); setCtx(null); }} data-testid="ctx-insights">Insights</button>
          {paneRects.length > 1 && <button type="button" className="chart-menu-item" onClick={() => { setDialog({ k: "panes" }); setCtx(null); }}>Manage panes…</button>}
          <button type="button" className="chart-menu-item" onClick={() => { setDialog({ k: "settings" }); setCtx(null); }}>Settings…</button>
          {onChartOrder && <div className="chart-menu-sep" />}
          {onChartOrder &&
            ctxOrders(ctx.price).filter((o) => o.price == null).map((o) => (
              <button key={`${o.side}-mkt`} type="button" className={`chart-menu-item ctx-${o.side.toLowerCase()}`} onClick={() => pickOrder(o, ctx)}>{orderLabel(o)}</button>
            ))}
        </>
      )}
    </div>
  );

  const dlg = dialog;
  const dlgDrawing = dlg?.k === "drawSettings" ? drawingsList.find((d) => d.id === dlg.id) : undefined;
  const dlgInd = dlg?.k === "indSettings" ? indicators.find((i) => i.uid === dlg.uid) : undefined;

  return (
    <section
      className={`trade-card chart-card${settings.theme === "light" ? " chart-light" : ""}${maximized ? " maximized" : ""}`}
      data-testid="trade-chart"
      style={lightVars}
    >
      {placeChrome(toolbar, toolbarSlot)}

      <div className="chart-body">
        {placeChrome(
          toolsCollapsed ? (
            <div className="chart-tools collapsed">
              <button type="button" className="chart-tool" onClick={() => setToolsCollapsed(false)} title="Show drawings toolbar" data-testid="chart-tools-expand">»</button>
            </div>
          ) : (
            <LeftToolbar
              drawMode={drawMode}
              cursor={cursor}
              onCursor={(c) => { setCursor(c); setDrawMode(null); }}
              onTool={toggleDraw}
              lastTool={lastTool}
              favorites={favTools}
              onFav={(id) => setFavTools((l) => toggleIn(l, id))}
              magnet={magnet}
              onMagnet={setMagnet}
              stay={stay}
              onStay={() => setStay((v) => !v)}
              lockAll={lockAll}
              onLockAll={() => setLockAll((v) => !v)}
              hide={hide}
              onHide={setHideKind}
              syncDrawings={syncDrawings}
              onSyncDrawings={() => setSyncDrawings((v) => !v)}
              drawingCount={drawCount}
              indicatorCount={indicators.length}
              onRemove={removeKind}
              selected={!!selectedDrawing}
              onDeleteSelected={() => selectedDrawing && deleteDrawing(selectedDrawing)}
              showFavBar={showFavBar}
              onFavBar={() => setShowFavBar((v) => !v)}
              onCollapse={() => setToolsCollapsed(true)}
              onIcon={(d) => { pendingIconRef.current = d; toggleDraw("icon"); }}
              onImage={(src, w, h) => { pendingIconRef.current = { src, w, h }; toggleDraw("image"); }}
              alertMode={drawMode === "alert"}
              onAlert={() => setDrawMode(drawModeRef.current === "alert" ? null : "alert")}
              pickMode={drawMode === "ticket"}
              onPick={onPickPrice ? () => setDrawMode(drawModeRef.current === "ticket" ? null : "ticket") : undefined}
            />
          ),
          toolsSlot,
        )}
        <div
          ref={wrapRef}
          className={`chart-container cur-${cursor} nav-${settings.navButtons} panebtn-${settings.paneButtons}${drawMode ? " drawing" : ""}`}
          onContextMenu={openCtx}
        >
          <div ref={containerRef} className="chart-host" />
          {cursor === "demo" && <LaserCanvas host={hostEl} />}
          {settings.watermark && (
            <div className="chart-watermark" style={settings.watermarkColor ? { color: settings.watermarkColor, opacity: 0.25 } : undefined}>
              {shortName}<span>{ivLabel}</span>
            </div>
          )}
          {settings.logoWatermark && (
            <div
              className="chart-logo-wm"
              style={{
                left: (side === "left" ? scaleWidth("left") : 0) + 10,
                // bottom-left of the price pane (indicator panes sit below it)
                top: (paneRects.find((r) => r.i === 0) ? (paneRects.find((r) => r.i === 0)!.top + paneRects.find((r) => r.i === 0)!.height) : (containerRef.current?.clientHeight ?? 400) - 28) - 24,
              }}
              aria-hidden
            >
              <b>◆</b> TRADEBOT
            </div>
          )}
          <div className="chart-legend chart-overlay-ui" style={legendStyle}>
            <div className="lg-row lg-main">
              {settings.showTitle && (
                <button type="button" className="lg-sym" onClick={() => onSymbolChange && setDialog({ k: "symbol", q: shortName ?? "", select: true })} title="Change symbol">{titleText}</button>
              )}
              <button type="button" className="lg-tf" onClick={() => setDialog({ k: "interval", txt: "" })} title="Change interval">{ivLabel}</button>
              {exchange && <span className="lg-exch">{exchange}</span>}
              <span className={`lg-dot ${marketStatus.k}`} title={marketStatus.t} />
              {replay.on && !replay.selecting && <span className="lg-replay">REPLAY</span>}
              <span ref={legendRef} className="lg-vals" />
              <span className="lg-acts">
                <button type="button" title={mainHidden ? "Show the series" : "Hide the series"} onClick={() => setMainHidden((v) => !v)} data-testid="legend-main-eye">{mainHidden ? "◌" : "👁"}</button>
                <button type="button" title="Chart settings" onClick={() => setDialog({ k: "settings", tab: "symbol" })}>⚙</button>
                <button type="button" title="More" data-more="" onClick={(e) => openLegendMenu(e, "main")} data-testid="legend-main-more">⋯</button>
                <button
                  type="button"
                  className={legendCollapsed && indicators.length ? "lg-count" : ""}
                  title={legendCollapsed ? `Show indicators legend (${indicators.length})` : "Hide indicators legend"}
                  onClick={() => setLegendCollapsed((v) => !v)}
                  data-testid="legend-collapse"
                >
                  {legendCollapsed ? `⌄${indicators.length ? ` ${indicators.length}` : ""}` : "⌃"}
                </button>
              </span>
            </div>
            {quickTrade && (
              <div className="lg-row lg-trade" data-testid="chart-quick-row">
                <button type="button" className="qt sell" onClick={() => quickOrder("SELL")} title={instant ? "Sell at market — one click" : "Sell — opens the order window"} data-testid="chart-quick-sell">
                  <span>SELL</span><b>{bid != null ? fmtPrice(bid) : ltpNow != null ? fmtPrice(ltpNow) : "—"}</b>
                </button>
                <span className="qt-spread" title="Spread">{bid != null && ask != null ? fmtNum(ask - bid, 2) : "—"}</span>
                <input
                  className="qt-qty"
                  type="number"
                  min={1}
                  step={qtyStep}
                  value={qty}
                  aria-label="Quantity"
                  title={onOrderQty ? "Quantity (the ticket's)" : "Quantity"}
                  onChange={(e) => {
                    const n = Math.max(1, Math.floor(Number(e.target.value) || 1));
                    setQty(n);
                    onOrderQty?.(n);
                  }}
                />
                <button type="button" className="qt buy" onClick={() => quickOrder("BUY")} title={instant ? "Buy at market — one click" : "Buy — opens the order window"} data-testid="chart-quick-buy">
                  <span>BUY</span><b>{ask != null ? fmtPrice(ask) : ltpNow != null ? fmtPrice(ltpNow) : "—"}</b>
                </button>
              </div>
            )}
            {!legendCollapsed && (
              <>
                {compares.map((c) => (
                  <div key={c.symbol} className={`lg-row lg-ind${c.hidden ? " off" : ""}`}>
                    <span className="chart-dot" style={{ background: c.color }} />
                    <span className="lg-name">{c.name}</span>
                    <span className="lg-acts">
                      <button type="button" title={c.hidden ? "Show" : "Hide"} onClick={() => setCompares((l) => l.map((x) => (x.symbol === c.symbol ? { ...x, hidden: !x.hidden } : x)))}>{c.hidden ? "◌" : "👁"}</button>
                      <button type="button" title="Remove" onClick={() => setCompares((l) => l.filter((x) => x.symbol !== c.symbol))}>✕</button>
                    </span>
                  </div>
                ))}
                {mainLegendInds.map(indRow)}
                {strat && (
                  <div className="lg-row lg-ind">
                    <span className="lg-name">⚙ {strat.name}</span>
                    <span className={strat.error ? "warn-text" : "hint"}>
                      {strat.running ? "running…" : strat.error ? strat.error : `${strat.stats.trades ?? 0} trades · win ${strat.stats.win_rate ?? "—"}% · PF ${strat.stats.profit_factor ?? "—"} · net ₹${Math.round(Number(strat.stats.net_pnl ?? 0)).toLocaleString("en-IN")}`}
                    </span>
                    <span className="lg-acts"><button type="button" title="Clear the strategy's marks" onClick={() => setStrat(null)}>✕</button></span>
                  </div>
                )}
                {flowOn && flowNote && <div className="lg-row hint">{flowNote}</div>}
              </>
            )}
          </div>
          {/* per-pane legends + pane buttons */}
          {paneRects.filter((r) => r.i > 0).map((r) => {
            const insts = indicators.filter((x) => indPaneRef.current.get(x.uid) === r.i);
            const last = Math.max(...paneRects.map((p) => p.i));
            return (
              <div key={r.i}>
                {!legendCollapsed && <div className="chart-legend pane-legend chart-overlay-ui" style={{ top: r.top + 2, ...(legendStyle ?? {}) }}>{insts.map(indRow)}</div>}
                <div className="pane-btns chart-overlay-ui" style={{ top: r.top + 2, [side === "left" ? "left" : "right"]: (side === "left" ? scaleWidth("left") : scaleWidth("right") || 60) + 4 }}>
                  <button type="button" title="Move pane up" disabled={r.i <= 1} onClick={() => movePane(r.i, -1)}>▲</button>
                  <button type="button" title="Move pane down" disabled={r.i >= last} onClick={() => movePane(r.i, 1)}>▼</button>
                  <button type="button" title={paneMode.max === r.i ? "Restore pane" : "Maximize pane"} onClick={() => setPaneMode((m) => ({ max: m.max === r.i ? null : r.i, collapsed: [] }))}>{paneMode.max === r.i ? "❐" : "⬚"}</button>
                  <button type="button" title={paneMode.collapsed.includes(r.i) ? "Restore pane" : "Collapse pane"} onClick={() => setPaneMode((m) => ({ max: null, collapsed: m.collapsed.includes(r.i) ? m.collapsed.filter((x) => x !== r.i) : [...m.collapsed, r.i] }))}>{paneMode.collapsed.includes(r.i) ? "▢" : "▁"}</button>
                  <button type="button" title="Delete pane" onClick={() => deletePane(r.i)}>✕</button>
                  <button type="button" title="Manage panes" onClick={() => setDialog({ k: "panes" })} data-testid={`pane-manage-${r.i}`}>⚙</button>
                </div>
              </div>
            );
          })}
          {paneRects.length > 1 && (
            <div className="pane-btns chart-overlay-ui" style={{ top: 4, [side === "left" ? "left" : "right"]: (side === "left" ? scaleWidth("left") : scaleWidth("right") || 60) + 4 }}>
              <button type="button" title={paneMode.max === 0 ? "Restore panes" : "Maximize price pane"} onClick={() => setPaneMode((m) => ({ max: m.max === 0 ? null : 0, collapsed: [] }))}>{paneMode.max === 0 ? "❐" : "⬚"}</button>
            </div>
          )}
          <div ref={countdownRef} className="chart-axis-countdown" style={{ display: "none" }} />
          <div ref={rawLabelRef} className="chart-raw-label" style={{ display: "none" }} data-testid="chart-raw-label" />
          {eventTip && <div className="chart-event-tip chart-overlay-ui" style={{ left: Math.min(eventTip.x + 12, (containerRef.current?.clientWidth ?? 600) - 260) }}>{eventTip.text}</div>}
          {settings.scaleModesButtons !== "never" && (
            <div className={`chart-scale-modes chart-overlay-ui sm-${settings.scaleModesButtons}`} style={side === "left" ? { left: 26, right: "auto" } : undefined} onMouseDown={(e) => e.stopPropagation()}>
              <button type="button" className={autoScale ? "on" : ""} onClick={() => setAutoScale((v) => !v)} title="Auto (fits data to screen)" data-testid="chart-scale-a">A</button>
              <button type="button" className={scaleMode === "log" ? "on" : ""} disabled={pctCompare} onClick={() => setScaleMode((m) => (m === "log" ? "normal" : "log"))} title="Logarithmic scale (Alt+L)" data-testid="chart-scale-l">L</button>
            </div>
          )}
          <button type="button" className="chart-scale-gear chart-overlay-ui" style={side === "left" ? { left: 2, right: "auto" } : undefined} onClick={(e) => { e.stopPropagation(); setCtx({ x: side === "left" ? 4 : (containerRef.current?.clientWidth ?? 600) - 250, y: Math.max(0, (containerRef.current?.clientHeight ?? 400) - 330), price: 0, time: null, area: "price" }); }} onMouseDown={(e) => e.stopPropagation()} title="Price scale settings" data-testid="chart-scale-gear">⚙</button>
          {showPos && status.kind === "ready" && (
            <div className="pos-layer">
              <div ref={(el) => { posTagRefs.current.entry = el; }} className={`pos-tag ${showPos.qty > 0 ? "long" : "short"}`} data-testid="chart-position">
                <span className="side">{showPos.qty > 0 ? "LONG" : "SHORT"} {Math.abs(showPos.qty)}</span>
                <span className="pnl" />
                {onLevels && showPos.tp == null && levelDrag !== "tp" && (
                  <span className="grab tp" onPointerDown={(e) => startLevelDrag(e, "tp")} title="Drag up / down to set a target">TP</span>
                )}
                {onLevels && showPos.sl == null && levelDrag !== "sl" && (
                  <span className="grab sl" onPointerDown={(e) => startLevelDrag(e, "sl")} title="Drag up / down to set a stop-loss">SL</span>
                )}
                {onChartOrder && settings.reverseButton && (
                  <button
                    type="button"
                    className="x rev"
                    title="Reverse the position at market"
                    onClick={() => {
                      const q = Math.abs(showPos.qty) * 2;
                      if (window.confirm(`Reverse: ${showPos.qty > 0 ? "SELL" : "BUY"} ${q} ${shortName} at market?`)) {
                        submitOrder({ side: showPos.qty > 0 ? "SELL" : "BUY", type: "MARKET", price: null });
                      }
                    }}
                  >
                    ⇅
                  </button>
                )}
                {onClosePosition && (
                  <button
                    type="button"
                    className="x"
                    title="Close the position at market"
                    onClick={() => {
                      if (window.confirm(`Close ${showPos.qty > 0 ? "LONG" : "SHORT"} ${Math.abs(showPos.qty)} ${shortName} at market?`)) {
                        onClosePosition().catch((err) => addToast(`Close failed: ${err instanceof Error ? err.message : String(err)}`));
                      }
                    }}
                  >
                    ✕
                  </button>
                )}
              </div>
              {(["tp", "sl"] as const).map((k) =>
                showPos[k] != null || levelDrag === k ? (
                  <div key={k} ref={(el) => { posTagRefs.current[k] = el; }} className={`pos-tag lvl ${k}`} onPointerDown={(e) => startLevelDrag(e, k)} title={`Drag to move the ${k === "sl" ? "stop-loss" : "target"}`} data-testid={`chart-${k}`}>
                    <span className="side">{k.toUpperCase()}</span>
                    <span className="pnl" />
                    {onLevels && showPos[k] != null && (
                      <button type="button" className="x" title="Remove" onPointerDown={(e) => e.stopPropagation()} onClick={() => void removeLevel(k)}>✕</button>
                    )}
                  </div>
                ) : null,
              )}
            </div>
          )}
          {showPlus && (
            <button
              ref={plusRef}
              type="button"
              className="chart-plus"
              style={{ display: "none" }}
              title="Alert, orders and a line at this price"
              onMouseEnter={() => { overPlusRef.current = true; window.clearTimeout(plusHideRef.current); }}
              onMouseLeave={() => { overPlusRef.current = false; }}
              onMouseDown={(e) => e.stopPropagation()}
              onClick={() => {
                const pr = plusPriceRef.current;
                if (pr == null) return;
                const w = containerRef.current?.clientWidth ?? 600;
                setCtx({ x: w - scaleWidth("right") - 250, y: parseFloat(plusRef.current?.style.top || "0") + 22, price: pr, time: null, area: "pane" });
              }}
              data-testid="chart-plus"
            >
              +
            </button>
          )}
          {ctxEl}
          {tradeFlyEl}
          {legendMenuEl}
          {selDrawing && !drawMode && (
            <DrawingFloatBar
              drawing={selDrawing}
              style={styleOf(selDrawing, drawingDeps())}
              fallbackColor={theme.draw}
              onStyle={(s) => updateDrawing(selDrawing.id, (d) => ({ ...d, style: { ...(d.style ?? {}), ...s } }), "style")}
              onText={TOOL_BY_ID.get(selDrawing.type)?.text ? () => openTextEditor(selDrawing.id) : undefined}
              onSettings={() => setDialog({ k: "drawSettings", id: selDrawing.id })}
              onLock={() => updateDrawing(selDrawing.id, { locked: !selDrawing.locked }, selDrawing.locked ? "unlock" : "lock")}
              onHide={() => { updateDrawing(selDrawing.id, { hidden: true }, "hide drawing"); selectDrawing(null); }}
              onClone={() => cloneDrawing(selDrawing.id)}
              onDelete={() => deleteDrawing(selDrawing.id)}
              onOrder={(k) => reorderDrawing(selDrawing.id, k)}
              onSaveDefault={() => {
                toolDefaultsRef.current = { ...toolDefaultsRef.current, [selDrawing.type]: { ...(selDrawing.style ?? {}) } };
                saveJson("chart:drawStyles", toolDefaultsRef.current);
                addToast("saved as the default style");
              }}
              onAlert={() => openAlertDialog(undefined, { target: `draw:${selDrawing.id}`, targetLabel: selDrawing.name || TOOL_BY_ID.get(selDrawing.type)?.label })}
              canAlert={drawingValueAt(selDrawing, viewRef.current[viewRef.current.length - 1]?.time ?? 0, drawingDeps()) !== null}
              templates={drawTemplates(selDrawing.type).map((t) => t.name)}
              onApplyTemplate={(name) => applyDrawTemplate(selDrawing, name)}
              onSaveTemplate={() => saveDrawTemplate(selDrawing)}
              onRemoveTemplate={(name) => { removeDrawTemplate(selDrawing.type, name); bumpDrawings(); }}
            />
          )}
          {showFavBar && chrome && <FavoritesBar favorites={favTools} drawMode={drawMode} onTool={toggleDraw} onClose={() => setShowFavBar(false)} />}
          {status.kind === "loading" && <div className="chart-status">loading chart…</div>}
          {status.kind === "error" && (
            <div className="chart-status warn-text" data-testid="chart-error">
              <span>{status.message}</span>
              <button type="button" className="chart-retry" onClick={() => setReloadNonce((n) => n + 1)} data-testid="chart-retry">↻ Retry</button>
            </div>
          )}
          {status.kind === "empty" && (
            <div className="chart-status" data-testid="chart-empty">
              <span>no chart data for this symbol / interval</span>
              <button type="button" className="chart-retry" onClick={() => setReloadNonce((n) => n + 1)} data-testid="chart-retry">↻ Retry</button>
            </div>
          )}
          {drawHint && <div className="chart-drawhint" data-testid="chart-drawhint">{drawHint}</div>}
          {selDrawing && !drawMode && !replay.on && (
            <div className="chart-drawhint" data-testid="chart-selecthint">
              {selDrawing.locked || lockAll ? "locked — unlock to move" : "drag to move, drag a handle to resize, Ctrl+drag clones, double-click edits, Delete removes"}
            </div>
          )}
          {replay.on && !replay.selecting && (
            <div className="chart-replay chart-overlay-ui" data-testid="chart-replay" onMouseDown={(e) => e.stopPropagation()}>
              <b>Replay</b>
              <button type="button" onClick={() => setReplay({ ...replay, selecting: true, playing: false })} title="Select a new start bar">⇤ start</button>
              <button type="button" onClick={() => setReplay({ ...replay, playing: !replay.playing })} title={replay.playing ? "Pause" : "Play"} data-testid="replay-play">{replay.playing ? "⏸" : "▶"}</button>
              <button type="button" onClick={() => stepReplay(1)} title="Step forward" data-testid="replay-step">⏭</button>
              <select value={replay.speed} onChange={(e) => setReplay({ ...replay, speed: Number(e.target.value) })} aria-label="Replay speed">
                {[0.5, 1, 2, 3, 5, 10].map((s) => <option key={s} value={s}>{s}×</option>)}
              </select>
              <span className="hint">{replay.idx + 1} / {candlesRef.current.length}</span>
              <button type="button" onClick={stopReplay} title="Jump to real-time and exit replay" data-testid="replay-exit">⇥ real-time</button>
            </div>
          )}
          {textDraft && (
            <textarea
              className="chart-text-input"
              style={{
                left: Math.max(4, Math.min(textDraft.x, (containerRef.current?.clientWidth ?? 400) - 220)),
                top: Math.max(4, textDraft.y - 12),
              }}
              value={textDraft.value}
              placeholder="text… (Enter saves, Shift+Enter new line)"
              autoFocus
              rows={Math.min(6, Math.max(1, textDraft.value.split("\n").length))}
              onChange={(e) => setTextDraft({ ...textDraft, value: e.target.value })}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  commitTextDraft();
                } else if (e.key === "Escape") setTextDraft(null);
              }}
              onBlur={commitTextDraft}
              onFocus={(e) => e.currentTarget.select()}
              data-testid="chart-text-input"
            />
          )}
          <div className="chart-nav chart-overlay-ui" onMouseDown={(e) => e.stopPropagation()}>
            <button type="button" onClick={() => zoomBy(0.8)} title="Zoom out">−</button>
            <button type="button" onClick={() => zoomBy(1.25)} title="Zoom in">+</button>
            <button type="button" onClick={() => scrollBars(-10)} title="Scroll to the left">‹</button>
            <button type="button" onClick={() => scrollBars(10)} title="Scroll to the right">›</button>
            <button type="button" onClick={resetView} title="Reset chart view (Alt+R)">⟲</button>
          </div>
          {!atLive && status.kind === "ready" && (
            <button type="button" className="chart-golive" onClick={() => showLatest()} title="Scroll to the most recent bar" data-testid="chart-golive">⇥ live</button>
          )}
          <div className="chart-toasts">
            {(toastsOpen ? toasts : toasts.slice(-3)).map((t) => (
              <div key={t.id} className="chart-toast" data-testid="chart-toast">
                <span>{t.text}</span>
                <button type="button" className="chart-menu-x" onClick={() => setToasts((l) => l.filter((x) => x.id !== t.id))} aria-label="Dismiss">✕</button>
              </div>
            ))}
            {toasts.length > 3 && (
              <button type="button" className="chart-toast more" onClick={() => setToastsOpen((v) => !v)}>
                {toastsOpen ? "collapse" : `+${toasts.length - 3} more`}
              </button>
            )}
          </div>
        </div>
      </div>
      {placeChrome(rangeBar, rangeSlot)}

      {chrome && widgetSlots?.data && createPortal(<DataWindow {...dataWindowGroups()} />, widgetSlots.data)}
      {chrome && widgetSlots?.tree &&
        createPortal(
          <ObjectTree
            panes={treePanes()}
            onVisible={(it) => {
              if (it.kind === "indicator") toggleIndicator(it.id);
              else if (it.kind === "series") setMainHidden((v) => !v);
              else if (it.kind === "drawing") updateDrawing(it.id, (d) => ({ ...d, hidden: !d.hidden }), "hide drawing");
              else if (it.kind === "compare") setCompares((l) => l.map((x) => (x.symbol === it.id ? { ...x, hidden: !x.hidden } : x)));
            }}
            onLock={(it) => updateDrawing(it.id, (d) => ({ ...d, locked: !d.locked }), "lock")}
            onDelete={(it) => {
              if (it.kind === "indicator") removeIndicator(it.id);
              else if (it.kind === "drawing") deleteDrawing(it.id);
              else if (it.kind === "compare") setCompares((l) => l.filter((x) => x.symbol !== it.id));
            }}
            onRename={(it, name) => updateDrawing(it.id, { name: name.trim() || undefined }, "rename")}
            onSelect={(it) => it.kind === "drawing" && selectDrawing(it.id)}
            onMove={(it, by) => reorderDrawing(it.id, by > 0 ? "forward" : "backward")}
            onSettings={(it) => setDialog(it.kind === "indicator" ? { k: "indSettings", uid: it.id } : { k: "drawSettings", id: it.id })}
            onDrop={treeDrop}
            onGroup={groupDrawings}
            onGroupAction={groupAction}
          />,
          widgetSlots.tree,
        )}
      {chrome && widgetSlots?.alerts &&
        createPortal(
          <AlertsPanel
            symbolName={shortName}
            alerts={alerts}
            log={alertLog}
            fmt={fmtPrice}
            onCreate={() => openAlertDialog()}
            onEdit={(a) => setDialog({ k: "alert", alert: a })}
            onToggle={(a) => saveAlerts(alerts.map((x) => (x.id === a.id ? { ...x, active: !x.active } : x)))}
            onDelete={(a) => removeAlert(a.id)}
            onClearLog={() => { setAlertLog([]); saveJson(`chart:alertLog:${symbol}`, []); }}
          />,
          widgetSlots.alerts,
        )}

      {dlg?.k === "symbol" && (
        <SymbolSearchDialog
          initial={dlg.q}
          select={dlg.select}
          recent={recentSymbols ?? []}
          onPick={(h) => {
            if (dlg.compare) setCompares((c) => (c.length >= 4 || c.some((x) => x.symbol === h.symbol) ? c : [...c, { symbol: h.symbol, name: h.short_name, color: COMPARE_COLORS[c.length % COMPARE_COLORS.length] }]));
            else onSymbolChange?.(h);
          }}
          onClose={() => setDialog(null)}
        />
      )}
      {dlg?.k === "saveTemplate" && (
        <SaveTemplateDialog
          symbolLabel={shortName}
          intervalLabel={intervalLongLabel(iv)}
          existing={templates.map((t) => t.name)}
          onSave={storeIndTemplate}
          onClose={() => setDialog(null)}
        />
      )}
      {dlg?.k === "indicators" && (
        <IndicatorPicker
          favorites={favIndicators}
          onFav={(t) => setFavIndicators((l) => toggleIn(l, t))}
          onAdd={addIndicator}
          onClose={() => setDialog(null)}
          strategies={strategies}
          onRunStrategy={(s) => void runStrategy(s)}
          templates={templates}
          onApplyTemplate={applyIndTemplate}
          intraday={intraday}
        />
      )}
      {dlg?.k === "indSettings" && dlgInd && (
        <IndicatorSettings
          inst={dlgInd}
          onChange={updateIndicator}
          onClose={() => {
            const snap = dialogSnapRef.current;
            if (snap && JSON.stringify(snap.indicators) !== JSON.stringify(indicatorsRef.current)) pushUndo(snap.label, snap);
            setDialog(null);
          }}
          onSaveDefault={(i) => {
            const all = loadJson<Record<string, Partial<IndicatorInstance>>>("chart:indDefaults", {});
            all[i.type] = instanceDefaults(i);
            saveJson("chart:indDefaults", all);
            addToast("saved as the default for new copies");
          }}
        />
      )}
      {dlg?.k === "settings" && (
        <ChartSettingsDialog
          value={settings}
          onChange={changeSettings}
          onClose={() => {
            const snap = dialogSnapRef.current;
            if (snap?.settings && JSON.stringify(snap.settings) !== JSON.stringify(settingsRef.current)) pushUndo(snap.label, snap);
            setDialog(null);
          }}
          theme={{ up: theme.up, down: theme.down, text: theme.text, grid: theme.grid, bg: theme.bg, accent: theme.accent, crosshair: theme.crosshair, border: theme.border }}
          trading={trading}
          onTrading={onTrading}
          initialTab={dlg.tab}
          chartKind={chartKind}
        />
      )}
      {dlg?.k === "drawSettings" && dlgDrawing && (
        <DrawingSettingsDialog
          drawing={dlgDrawing}
          deps={drawingDeps()}
          fmt={fmtPrice}
          onChange={(d) => updateDrawing(d.id, () => d)}
          onClose={(commit) => {
            if (commit && dialogSnapRef.current) pushUndo("edit drawing", dialogSnapRef.current);
            setDialog(null);
          }}
        />
      )}
      {dlg?.k === "alert" && (
        <AlertDialog alert={dlg.alert} symbolName={shortName} targets={alertTargets()} fmt={fmtPrice} onSave={upsertAlert} onClose={() => setDialog(null)} />
      )}
      {dlg?.k === "goto" && <GoToDialog onGo={goTo} onClose={() => setDialog(null)} last={viewRef.current[viewRef.current.length - 1]?.time ?? null} />}
      {dlg?.k === "palette" && <CommandPalette commands={commands()} onClose={() => setDialog(null)} />}
      {dlg?.k === "shortcuts" && <ShortcutsDialog onClose={() => setDialog(null)} />}
      {dlg?.k === "about" && (() => {
        const inst = indicators.find((x) => x.uid === dlg.uid);
        const def = inst ? INDICATOR_BY_TYPE.get(inst.type) : undefined;
        return def ? <AboutIndicatorDialog def={def} onClose={() => setDialog(null)} /> : null;
      })()}
      {dlg?.k === "panes" && (
        <ManagePanesDialog
          panes={paneList().map((p) => ({ i: p.i, names: p.insts.map((x) => `${INDICATOR_BY_TYPE.get(x.type)?.short ?? x.type} ${argsLabel(x)}`.trim()) })).concat(paneList().some((p) => p.i === 0) ? [] : [{ i: 0, names: [] }]).sort((a, b) => a.i - b.i)}
          maxPane={paneMode.max}
          collapsed={paneMode.collapsed}
          onMove={movePane}
          onMerge={mergePanes}
          onDelete={deletePane}
          onMax={(i) => setPaneMode((m) => ({ max: m.max === i ? null : i, collapsed: [] }))}
          onCollapse={(i) => setPaneMode((m) => ({ max: null, collapsed: m.collapsed.includes(i) ? m.collapsed.filter((x) => x !== i) : [...m.collapsed, i] }))}
          onClose={() => setDialog(null)}
        />
      )}
      {dlg?.k === "insights" && <InsightsDialog symbol={symbol} name={shortName} onClose={() => setDialog(null)} />}
      {dlg?.k === "whatsnew" && <WhatsNewDialog onClose={() => setDialog(null)} />}
      {dlg?.k === "interval" && (
        <IntervalPrompt
          initial={dlg.txt}
          onApply={(k) => {
            if (normalizeInterval(k)) changeInterval(k);
            setDialog(null);
          }}
          onClose={() => setDialog(null)}
        />
      )}
    </section>
  );
}

/** Type-to-change-interval box ("5" → 5m, "1h", "D", "W", "3M"). */
function IntervalPrompt({ initial, onApply, onClose }: { initial: string; onApply: (k: string) => void; onClose: () => void }) {
  const [txt, setTxt] = useState(initial);
  const key = normalizeInterval(txt);
  return createPortal(
    <div className="cmodal-back" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="iv-prompt" data-testid="interval-prompt">
        <div className="hint">Change interval</div>
        <input
          autoFocus
          value={txt}
          onChange={(e) => setTxt(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") onApply(txt);
            if (e.key === "Escape") onClose();
          }}
          aria-label="Interval"
          data-testid="interval-prompt-input"
        />
        <div className={key ? "" : "warn-text"}>{key ? intervalLongLabel(key) : "e.g. 5, 15, 1h, 4h, D, W, 3M, 30S"}</div>
      </div>
    </div>,
    document.fullscreenElement ?? document.body,
  );
}
