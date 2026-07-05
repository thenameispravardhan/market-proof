// ChartPanel — TradingView-style interactive chart for the Trade page.
//
// Rendered when the operator selects a symbol. Candles come from
// GET /api/market/history (Fyers-only, like every other price in the
// app — no public-feed fallback), and the last bar is kept live from
// the `/ws` quote stream. Built on lightweight-charts v5, TradingView's
// own open-source chart engine.
//
// Features:
//   - timeframes 1m / 3m / 5m / 15m / 30m / 1h / 1D
//   - candles, bars, line, area
//   - volume histogram + bar-close countdown
//   - overlays: SMA, EMA, WMA, VWAP, Bollinger, Supertrend, PSAR,
//     Ichimoku, Donchian
//   - oscillator panes: RSI, MACD, Stochastic, ADX/DI, ATR, OBV, CCI,
//     MFI, Williams %R
//   - drawing tools: trend line, ray, horizontal/vertical line,
//     rectangle, fib retracement, text note — click-select + Delete,
//     persisted per symbol in localStorage
//   - price alerts: click a price, fires on live-tick cross (toast +
//     browser notification), persisted per symbol
//   - compare symbols on a percentage scale
//   - price-scale modes (log / percent), crosshair magnet, PNG
//     screenshot, fullscreen
//   - OHLC legend, infinite scroll-back pagination
//
// Times: Fyers candles are epoch seconds. lightweight-charts renders
// times as UTC, so every timestamp is shifted by +05:30 before it is
// handed to the chart — labels then read as IST market time. The same
// shift is undone when paging older history from the API.

import { useEffect, useRef, useState } from "react";
import {
  AreaSeries,
  BarSeries,
  CandlestickSeries,
  ColorType,
  createChart,
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
  type UTCTimestamp,
} from "lightweight-charts";
import { api } from "../../api/client";
import { useLiveQuote } from "../../hooks/useQuotes";
import {
  adx,
  atr,
  bollinger,
  cci,
  donchian,
  ema,
  ichimoku,
  macd,
  mfi,
  obv,
  psar,
  rsi,
  sma,
  stochastic,
  supertrend,
  vwap,
  williamsR,
  wma,
  type OhlcvCandle,
} from "../../lib/indicators";
import type { HistoryResponse, SearchResponse } from "../../types";
import {
  DrawingsPrimitive,
  hitHandle,
  hitTest,
  newDrawingId,
  pointsNeeded,
  type Drawing,
  type DrawingDeps,
  type DrawingPoint,
  type DrawingType,
} from "./drawings";

const IST_OFFSET = 19800; // +05:30 — see the file header

interface Candle extends OhlcvCandle {
  time: UTCTimestamp;
}

interface Timeframe {
  label: string;
  res: string; // Fyers resolution code
  initialDays: number; // calendar days fetched up-front
  chunkDays: number; // calendar days per scroll-back page
}

// Fyers caps intraday history at 100 days per request, daily at 366.
const TIMEFRAMES: Timeframe[] = [
  { label: "1m", res: "1", initialDays: 7, chunkDays: 7 },
  { label: "3m", res: "3", initialDays: 15, chunkDays: 15 },
  { label: "5m", res: "5", initialDays: 20, chunkDays: 20 },
  { label: "15m", res: "15", initialDays: 60, chunkDays: 60 },
  { label: "30m", res: "30", initialDays: 90, chunkDays: 90 },
  { label: "1h", res: "60", initialDays: 100, chunkDays: 100 },
  { label: "1D", res: "D", initialDays: 365, chunkDays: 365 },
];

type ChartKind = "candles" | "bars" | "line" | "area";

const CHART_KINDS: { id: ChartKind; label: string; title: string }[] = [
  { id: "candles", label: "Candle", title: "Candlestick chart" },
  { id: "bars", label: "Bar", title: "OHLC bar chart" },
  { id: "line", label: "Line", title: "Line chart (close)" },
  { id: "area", label: "Area", title: "Area chart (close)" },
];

type IndicatorId =
  | "sma"
  | "ema"
  | "wma"
  | "vwap"
  | "bb"
  | "supertrend"
  | "psar"
  | "ichimoku"
  | "donchian"
  | "rsi"
  | "macd"
  | "stoch"
  | "adx"
  | "atr"
  | "obv"
  | "cci"
  | "mfi"
  | "wpr";

const INDICATOR_DEFS: { id: IndicatorId; label: string; group: "Overlays" | "Oscillators" }[] = [
  { id: "sma", label: "SMA 20", group: "Overlays" },
  { id: "ema", label: "EMA 50", group: "Overlays" },
  { id: "wma", label: "WMA 20", group: "Overlays" },
  { id: "vwap", label: "VWAP (session)", group: "Overlays" },
  { id: "bb", label: "Bollinger 20, 2", group: "Overlays" },
  { id: "supertrend", label: "Supertrend 10, 3", group: "Overlays" },
  { id: "psar", label: "Parabolic SAR", group: "Overlays" },
  { id: "ichimoku", label: "Ichimoku 9, 26, 52", group: "Overlays" },
  { id: "donchian", label: "Donchian 20", group: "Overlays" },
  { id: "rsi", label: "RSI 14", group: "Oscillators" },
  { id: "macd", label: "MACD 12, 26, 9", group: "Oscillators" },
  { id: "stoch", label: "Stochastic 14, 3, 3", group: "Oscillators" },
  { id: "adx", label: "ADX / DI 14", group: "Oscillators" },
  { id: "atr", label: "ATR 14", group: "Oscillators" },
  { id: "obv", label: "OBV", group: "Oscillators" },
  { id: "cci", label: "CCI 20", group: "Oscillators" },
  { id: "mfi", label: "MFI 14", group: "Oscillators" },
  { id: "wpr", label: "Williams %R 14", group: "Oscillators" },
];

const DEFAULT_ACTIVE: Record<IndicatorId, boolean> = Object.fromEntries(
  INDICATOR_DEFS.map((d) => [d.id, false]),
) as Record<IndicatorId, boolean>;

const DRAW_TOOLS: { id: DrawingType; label: string; hint: string }[] = [
  { id: "trend", label: "╱ Trend line", hint: "click two points" },
  { id: "ray", label: "→ Ray", hint: "click two points" },
  { id: "hline", label: "─ Horizontal line", hint: "click a price" },
  { id: "vline", label: "│ Vertical line", hint: "click a time" },
  { id: "rect", label: "▭ Rectangle", hint: "click two corners" },
  { id: "fib", label: "𝑓 Fib retracement", hint: "click swing start, then end" },
  { id: "text", label: "T Text note", hint: "click where the note goes" },
];

type DrawMode = DrawingType | "alert" | null;
type MenuId = "ind" | "draw" | "alerts" | "compare" | null;
type ScaleMode = "normal" | "log" | "percent";

const COMPARE_COLORS = ["#42A5F5", "#AB47BC", "#26C6DA", "#FFCA28"];

// Hard ceiling on how many candles we keep — beyond this the scroll-back
// pagination stops asking for more.
const MAX_CANDLES = 20000;

const PREFS_KEY = "chart:prefs";

interface AlertItem {
  id: string;
  price: number;
}

interface CompareItem {
  symbol: string;
  name: string;
  color: string;
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
      const v = getComputedStyle(document.documentElement)
        .getPropertyValue(name)
        .trim();
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
  };
}

function fmtPrice(v: number): string {
  return v.toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
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

interface ChartPrefs {
  resolution?: string;
  chartKind?: ChartKind;
  active?: Partial<Record<IndicatorId, boolean>>;
  magnet?: boolean;
  scaleMode?: ScaleMode;
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
    const r = await api.get<HistoryResponse>(`/api/market/history?${qs.toString()}`);
    if (!r.ok) return { candles: [], reason: r.reason ?? "chart data unavailable" };
    const rows = Array.isArray(r.candles) ? r.candles : [];
    const out: Candle[] = [];
    for (const row of rows) {
      if (!Array.isArray(row) || row.length < 5) continue;
      const [ts, o, h, l, c, v] = row;
      const nums = [ts, o, h, l, c];
      if (!nums.every((x) => typeof x === "number" && Number.isFinite(x))) continue;
      out.push({
        time: (Math.floor(ts) + IST_OFFSET) as UTCTimestamp,
        open: o,
        high: h,
        low: l,
        close: c,
        volume: typeof v === "number" && Number.isFinite(v) ? v : 0,
      });
    }
    out.sort((a, b) => a.time - b.time);
    const dedup: Candle[] = [];
    for (const c of out) {
      if (dedup.length > 0 && dedup[dedup.length - 1].time === c.time) {
        dedup[dedup.length - 1] = c;
      } else {
        dedup.push(c);
      }
    }
    return { candles: dedup, reason: null };
  } catch (e) {
    return {
      candles: [],
      reason: (e as Error).message || "history request failed",
    };
  }
}

interface ChartStatus {
  kind: "loading" | "ready" | "empty" | "error";
  message?: string;
}

export default function ChartPanel({
  symbol,
  shortName,
}: {
  symbol: string;
  shortName: string;
}) {
  const prefs = useRef(loadJson<ChartPrefs>(PREFS_KEY, {})).current;

  // ---- state (drives the toolbar) ----
  const [resolution, setResolution] = useState(
    TIMEFRAMES.some((t) => t.res === prefs.resolution) ? prefs.resolution! : "5",
  );
  const [chartKind, setChartKind] = useState<ChartKind>(
    CHART_KINDS.some((k) => k.id === prefs.chartKind) ? prefs.chartKind! : "candles",
  );
  const [active, setActive] = useState<Record<IndicatorId, boolean>>({
    ...DEFAULT_ACTIVE,
    ...(prefs.active ?? {}),
  });
  const [drawMode, setDrawModeState] = useState<DrawMode>(null);
  const [pendingPoint, setPendingPoint] = useState(false);
  const [selectedDrawing, setSelectedDrawing] = useState<string | null>(null);
  const [alerts, setAlerts] = useState<AlertItem[]>(() =>
    loadJson<AlertItem[]>(`chart:alerts:${symbol}`, []),
  );
  const [compares, setCompares] = useState<CompareItem[]>([]);
  const [compareQuery, setCompareQuery] = useState("");
  const [compareHits, setCompareHits] = useState<{ symbol: string; name: string }[]>([]);
  const [scaleMode, setScaleMode] = useState<ScaleMode>(prefs.scaleMode ?? "normal");
  const [magnet, setMagnet] = useState<boolean>(prefs.magnet ?? false);
  const [toasts, setToasts] = useState<{ id: number; text: string }[]>([]);
  const [textDraft, setTextDraft] = useState<{
    x: number;
    y: number;
    point: DrawingPoint;
    value: string;
  } | null>(null);
  const [fullscreen, setFullscreen] = useState(false);
  const [menuOpen, setMenuOpen] = useState<MenuId>(null);
  const [status, setStatus] = useState<ChartStatus>({ kind: "loading" });
  const [reloadNonce, setReloadNonce] = useState(0);

  // ---- refs (chart internals live outside React) ----
  const containerRef = useRef<HTMLDivElement | null>(null);
  const legendRef = useRef<HTMLDivElement | null>(null);
  const countdownRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const mainRef = useRef<ISeriesApi<SeriesType> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const indicatorSeriesRef = useRef<ISeriesApi<SeriesType>[]>([]);
  const compareSeriesRef = useRef<Map<string, ISeriesApi<"Line">>>(new Map());
  const alertLinesRef = useRef<Map<string, IPriceLine>>(new Map());
  const colorsRef = useRef<ThemeColors>(readThemeColors());
  const candlesRef = useRef<Candle[]>([]);
  const indexByTimeRef = useRef<Map<number, number>>(new Map());
  const fetchSeqRef = useRef(0);
  const loadingOlderRef = useRef(false);
  const haveMoreRef = useRef(true);
  const drawModeRef = useRef<DrawMode>(null);
  const resolutionRef = useRef(resolution);
  const statusRef = useRef<ChartStatus["kind"]>("loading");
  const autoRetriedKeyRef = useRef(""); // one silent retry per symbol|resolution
  const prevLtpRef = useRef<number | null>(null);
  const toastSeqRef = useRef(0);

  // drawing store
  const drawingsRef = useRef<Drawing[]>(
    loadJson<Drawing[]>(`chart:drawings:${symbol}`, []),
  );
  const pendingRef = useRef<{
    type: DrawingType;
    from: DrawingPoint;
    cursor: DrawingPoint | null;
  } | null>(null);
  const selectedIdRef = useRef<string | null>(null);
  const primitiveRef = useRef<DrawingsPrimitive | null>(null);
  // In-flight drag of an existing drawing: `mode` is an anchor index
  // (resize) or "move" (translate the whole drawing). `start` is the
  // pointer's time/price at drag start; `orig` the points at drag start.
  const dragRef = useRef<{
    id: string;
    mode: number | "move";
    start: DrawingPoint;
    orig: DrawingPoint[];
  } | null>(null);
  const hoverCursorElRef = useRef<HTMLElement | null>(null);

  resolutionRef.current = resolution;
  statusRef.current = status.kind;

  const tf = TIMEFRAMES.find((t) => t.res === resolution) ?? TIMEFRAMES[2];

  // ------------------------------------------------------------------
  // Coordinate helpers (refs only — safe to capture at first render)
  // ------------------------------------------------------------------

  function barInterval(): number {
    const res = resolutionRef.current;
    return res === "D" ? 86400 : (Number(res) || 5) * 60;
  }

  /** Chart-time → x pixel. Interpolates fractional logical positions so
   *  anchors placed on one timeframe land correctly on another. */
  function timeToX(t: number): number | null {
    const chart = chartRef.current;
    const candles = candlesRef.current;
    if (!chart || candles.length === 0) return null;
    const ts = chart.timeScale();
    const n = candles.length;
    let frac: number;
    if (t <= candles[0].time) {
      frac = (t - candles[0].time) / barInterval();
    } else if (t >= candles[n - 1].time) {
      frac = n - 1 + (t - candles[n - 1].time) / barInterval();
    } else {
      let lo = 0;
      let hi = n - 1;
      while (lo + 1 < hi) {
        const mid = (lo + hi) >> 1;
        if (candles[mid].time <= t) lo = mid;
        else hi = mid;
      }
      frac = lo + (t - candles[lo].time) / (candles[hi].time - candles[lo].time);
    }
    try {
      return ts.logicalToCoordinate(frac as Logical);
    } catch {
      return null;
    }
  }

  /** x pixel → chart-time (fractional between bars, extrapolated at edges). */
  function xToTime(x: number): number | null {
    const chart = chartRef.current;
    const candles = candlesRef.current;
    if (!chart || candles.length === 0) return null;
    let l: number | null = null;
    try {
      l = chart.timeScale().coordinateToLogical(x);
    } catch {
      return null;
    }
    if (l === null) return null;
    const n = candles.length;
    const i = Math.floor(l);
    if (i < 0) return candles[0].time + l * barInterval();
    if (i >= n - 1) return candles[n - 1].time + (l - (n - 1)) * barInterval();
    return candles[i].time + (l - i) * (candles[i + 1].time - candles[i].time);
  }

  function priceToY(p: number): number | null {
    try {
      return mainRef.current?.priceToCoordinate(p) ?? null;
    } catch {
      return null;
    }
  }

  function paneDims(): { width: number; height: number } {
    try {
      const s = chartRef.current?.paneSize(0);
      if (s) return { width: s.width, height: s.height };
    } catch {
      /* fall through */
    }
    const el = containerRef.current;
    return { width: el?.clientWidth ?? 0, height: el?.clientHeight ?? 0 };
  }

  function drawingDeps(): DrawingDeps {
    return {
      drawings: () => drawingsRef.current,
      pending: () => pendingRef.current,
      selectedId: () => selectedIdRef.current,
      timeToX,
      priceToY,
      lineColor: () => colorsRef.current.draw,
      selectedColor: () => colorsRef.current.accent,
      fillAlpha: () => withAlpha(colorsRef.current.draw, 0.08, "rgba(255,215,0,0.08)"),
      priceFormatter: fmtPrice,
    };
  }

  // ------------------------------------------------------------------
  // Drawing store helpers
  // ------------------------------------------------------------------

  function persistDrawings(): void {
    saveJson(`chart:drawings:${symbol}`, drawingsRef.current);
  }

  function repaintDrawings(): void {
    primitiveRef.current?.requestUpdate();
  }

  function addDrawing(d: Drawing): void {
    drawingsRef.current = [...drawingsRef.current, d];
    selectedIdRef.current = d.id;
    setSelectedDrawing(d.id);
    persistDrawings();
    repaintDrawings();
  }

  function deleteDrawing(id: string): void {
    drawingsRef.current = drawingsRef.current.filter((d) => d.id !== id);
    if (selectedIdRef.current === id) {
      selectedIdRef.current = null;
      setSelectedDrawing(null);
    }
    persistDrawings();
    repaintDrawings();
  }

  function clearDrawings(): void {
    drawingsRef.current = [];
    pendingRef.current = null;
    selectedIdRef.current = null;
    setSelectedDrawing(null);
    setPendingPoint(false);
    persistDrawings();
    repaintDrawings();
  }

  function setDrawMode(mode: DrawMode): void {
    drawModeRef.current = mode;
    setDrawModeState(mode);
    pendingRef.current = null;
    setPendingPoint(false);
    if (mode !== null) {
      selectedIdRef.current = null;
      setSelectedDrawing(null);
    }
    repaintDrawings();
  }

  // ------------------------------------------------------------------
  // Alerts & toasts
  // ------------------------------------------------------------------

  function addToast(text: string): void {
    const id = ++toastSeqRef.current;
    setToasts((t) => [...t, { id, text }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 8000);
  }

  function addAlert(price: number): void {
    const item: AlertItem = { id: newDrawingId(), price };
    setAlerts((a) => [...a, item]);
    addToast(`alert set at ${fmtPrice(price)}`);
    try {
      if (typeof Notification !== "undefined" && Notification.permission === "default") {
        void Notification.requestPermission();
      }
    } catch {
      /* notifications unsupported */
    }
  }

  function removeAlert(id: string): void {
    setAlerts((a) => a.filter((x) => x.id !== id));
  }

  // ------------------------------------------------------------------
  // Legend
  // ------------------------------------------------------------------

  function renderLegend(c: Candle | null, prev: Candle | null): void {
    const el = legendRef.current;
    if (!el) return;
    if (!c) {
      el.innerHTML = "";
      return;
    }
    const base = prev ? prev.close : c.open;
    const chg = c.close - base;
    const pct = base !== 0 ? (chg / base) * 100 : 0;
    const cls = chg >= 0 ? "up" : "down";
    el.innerHTML =
      `<span class="lg-sym">${shortName}</span>` +
      `<span class="lg-tf">${tf.label}</span>` +
      `<span>O <b>${fmtPrice(c.open)}</b></span>` +
      `<span>H <b>${fmtPrice(c.high)}</b></span>` +
      `<span>L <b>${fmtPrice(c.low)}</b></span>` +
      `<span>C <b class="${cls}">${fmtPrice(c.close)}</b></span>` +
      `<span class="${cls}">${chg >= 0 ? "+" : ""}${fmtPrice(chg)} (${pct.toFixed(2)}%)</span>` +
      `<span>Vol <b>${fmtVol(c.volume)}</b></span>`;
  }

  function legendLastBar(): void {
    const candles = candlesRef.current;
    const n = candles.length;
    renderLegend(n > 0 ? candles[n - 1] : null, n > 1 ? candles[n - 2] : null);
  }

  // ------------------------------------------------------------------
  // Series data
  // ------------------------------------------------------------------

  function setMainData(): void {
    const s = mainRef.current;
    if (!s) return;
    const candles = candlesRef.current;
    if (chartKind === "line" || chartKind === "area") {
      s.setData(candles.map((c) => ({ time: c.time, value: c.close })));
    } else {
      s.setData(
        candles.map((c) => ({
          time: c.time,
          open: c.open,
          high: c.high,
          low: c.low,
          close: c.close,
        })),
      );
    }
  }

  function setVolumeData(): void {
    const colors = colorsRef.current;
    volumeRef.current?.setData(
      candlesRef.current.map((c) => ({
        time: c.time,
        value: c.volume,
        color: c.close >= c.open ? colors.volUp : colors.volDown,
      })),
    );
  }

  function updateMainBar(bar: Candle): void {
    const s = mainRef.current;
    if (!s) return;
    if (chartKind === "line" || chartKind === "area") {
      s.update({ time: bar.time, value: bar.close });
    } else {
      s.update({
        time: bar.time,
        open: bar.open,
        high: bar.high,
        low: bar.low,
        close: bar.close,
      });
    }
  }

  /** Drop any oscillator pane that no longer holds a series. */
  function cleanupPanes(chart: IChartApi): void {
    try {
      const panes = chart.panes();
      for (let i = panes.length - 1; i > 0; i--) {
        if (panes[i].getSeries().length === 0) chart.removePane(i);
      }
    } catch {
      /* an empty pane is harmless */
    }
  }

  function rebuildIndicators(): void {
    const chart = chartRef.current;
    if (!chart) return;
    for (const s of indicatorSeriesRef.current) {
      try {
        chart.removeSeries(s);
      } catch {
        /* already gone */
      }
    }
    indicatorSeriesRef.current = [];
    const candles = candlesRef.current;
    if (candles.length === 0) {
      cleanupPanes(chart);
      return;
    }
    const closes = candles.map((c) => c.close);
    const times = candles.map((c) => c.time);
    const colors = colorsRef.current;
    const interval = barInterval();

    /** times[i + shift], extrapolating past the last bar. */
    const shiftedTime = (i: number, shift: number): UTCTimestamp | null => {
      const j = i + shift;
      if (j < 0) return null;
      if (j < times.length) return times[j];
      return (times[times.length - 1] + (j - (times.length - 1)) * interval) as UTCTimestamp;
    };

    const lineData = (values: (number | null)[], shift = 0) => {
      const data: { time: UTCTimestamp; value: number }[] = [];
      for (let i = 0; i < values.length; i++) {
        const v = values[i];
        if (v === null) continue;
        const t = shiftedTime(i, shift);
        if (t !== null) data.push({ time: t, value: v });
      }
      return data;
    };
    // Line data with whitespace gaps where `mask` is false — used for
    // regime-colored lines (e.g. supertrend up vs down segments).
    const maskedLineData = (values: (number | null)[], mask: (boolean | null)[]) => {
      const data: ({ time: UTCTimestamp; value: number } | { time: UTCTimestamp })[] = [];
      for (let i = 0; i < values.length; i++) {
        const v = values[i];
        if (v === null) continue;
        if (mask[i]) data.push({ time: times[i], value: v });
        else data.push({ time: times[i] });
      }
      return data;
    };
    const addLine = (
      data: ({ time: UTCTimestamp; value: number } | { time: UTCTimestamp })[],
      color: string,
      paneIndex = 0,
      width: 1 | 2 = 1,
      style: LineStyle = LineStyle.Solid,
      extra: Record<string, unknown> = {},
    ): ISeriesApi<"Line"> => {
      const s = chart.addSeries(
        LineSeries,
        {
          color,
          lineWidth: width,
          lineStyle: style,
          lastValueVisible: false,
          priceLineVisible: false,
          crosshairMarkerVisible: false,
          ...extra,
        },
        paneIndex,
      );
      s.setData(data);
      indicatorSeriesRef.current.push(s);
      return s;
    };
    const guides = (s: ISeriesApi<"Line">, levels: number[]) => {
      try {
        for (const level of levels) {
          s.createPriceLine({
            price: level,
            color: "rgba(153,153,153,0.4)",
            lineWidth: 1,
            lineStyle: LineStyle.Dotted,
            axisLabelVisible: false,
            title: "",
          });
        }
      } catch {
        /* cosmetic */
      }
    };
    const sizePane = (p: number) => {
      try {
        chart.panes()[p]?.setHeight(90);
      } catch {
        /* cosmetic */
      }
    };

    // ---- overlays ----
    if (active.sma) addLine(lineData(sma(closes, 20)), "#42A5F5");
    if (active.ema) addLine(lineData(ema(closes, 50)), "#FFB74D");
    if (active.wma) addLine(lineData(wma(closes, 20)), "#7E57C2");
    if (active.vwap && resolution !== "D") addLine(lineData(vwap(candles)), "#CE93D8");
    if (active.bb) {
      const b = bollinger(closes, 20, 2);
      addLine(lineData(b.upper), "rgba(66,165,245,0.55)");
      addLine(lineData(b.middle), "rgba(66,165,245,0.3)");
      addLine(lineData(b.lower), "rgba(66,165,245,0.55)");
    }
    if (active.supertrend) {
      const st = supertrend(candles, 10, 3);
      addLine(maskedLineData(st.value, st.up), colors.up, 0, 2);
      addLine(maskedLineData(st.value, st.up.map((u) => u === false)), colors.down, 0, 2);
    }
    if (active.psar) {
      addLine(lineData(psar(candles)), "#FFD54F", 0, 1, LineStyle.Solid, {
        lineVisible: false,
        pointMarkersVisible: true,
        pointMarkersRadius: 1.5,
      });
    }
    if (active.ichimoku) {
      const ic = ichimoku(candles);
      addLine(lineData(ic.tenkan), "#2962FF");
      addLine(lineData(ic.kijun), "#B71C1C");
      addLine(lineData(ic.spanA, 26), "rgba(76,175,80,0.8)");
      addLine(lineData(ic.spanB, 26), "rgba(244,67,54,0.8)");
      addLine(lineData(ic.chikou, -26), "#43A047", 0, 1, LineStyle.Dashed);
    }
    if (active.donchian) {
      const dc = donchian(candles, 20);
      addLine(lineData(dc.upper), "rgba(38,198,218,0.7)");
      addLine(lineData(dc.middle), "rgba(38,198,218,0.35)", 0, 1, LineStyle.Dashed);
      addLine(lineData(dc.lower), "rgba(38,198,218,0.7)");
    }

    // ---- oscillator panes (assigned in a stable order) ----
    let pane = 1;
    if (active.rsi) {
      const p = pane++;
      const s = addLine(lineData(rsi(closes, 14)), "#B39DDB", p);
      guides(s, [70, 30]);
      sizePane(p);
    }
    if (active.macd) {
      const p = pane++;
      const m = macd(closes, 12, 26, 9);
      const hist = chart.addSeries(
        HistogramSeries,
        { lastValueVisible: false, priceLineVisible: false },
        p,
      );
      const histData: { time: UTCTimestamp; value: number; color: string }[] = [];
      for (let i = 0; i < m.histogram.length; i++) {
        const v = m.histogram[i];
        if (v !== null) {
          histData.push({
            time: times[i],
            value: v,
            color: v >= 0 ? colors.volUp : colors.volDown,
          });
        }
      }
      hist.setData(histData);
      indicatorSeriesRef.current.push(hist);
      addLine(lineData(m.macd), "#42A5F5", p);
      addLine(lineData(m.signal), "#FF7043", p);
      sizePane(p);
    }
    if (active.stoch) {
      const p = pane++;
      const st = stochastic(candles, 14, 3, 3);
      const s = addLine(lineData(st.k), "#2962FF", p);
      addLine(lineData(st.d), "#FF6D00", p);
      guides(s, [80, 20]);
      sizePane(p);
    }
    if (active.adx) {
      const p = pane++;
      const a = adx(candles, 14);
      addLine(lineData(a.adx), "#F23645", p, 2);
      addLine(lineData(a.plusDi), colors.up, p);
      addLine(lineData(a.minusDi), colors.down, p);
      sizePane(p);
    }
    if (active.atr) {
      const p = pane++;
      addLine(lineData(atr(candles, 14)), "#FF8C00", p);
      sizePane(p);
    }
    if (active.obv) {
      const p = pane++;
      addLine(lineData(obv(candles)), "#26C6DA", p);
      sizePane(p);
    }
    if (active.cci) {
      const p = pane++;
      const s = addLine(lineData(cci(candles, 20)), "#AB47BC", p);
      guides(s, [100, -100]);
      sizePane(p);
    }
    if (active.mfi) {
      const p = pane++;
      const s = addLine(lineData(mfi(candles, 14)), "#FFCA28", p);
      guides(s, [80, 20]);
      sizePane(p);
    }
    if (active.wpr) {
      const p = pane++;
      const s = addLine(lineData(williamsR(candles, 14)), "#EC407A", p);
      guides(s, [-20, -80]);
      sizePane(p);
    }
    cleanupPanes(chart);
  }

  /** Push the full candle set into every series + rebuild the time index. */
  function applyData(): void {
    const candles = candlesRef.current;
    const idx = new Map<number, number>();
    for (let i = 0; i < candles.length; i++) idx.set(candles[i].time, i);
    indexByTimeRef.current = idx;
    setMainData();
    setVolumeData();
    rebuildIndicators();
    legendLastBar();
    repaintDrawings();
  }

  async function maybeLoadOlder(): Promise<void> {
    if (loadingOlderRef.current || !haveMoreRef.current) return;
    const candles = candlesRef.current;
    if (candles.length === 0 || candles.length >= MAX_CANDLES) return;
    loadingOlderRef.current = true;
    const seq = fetchSeqRef.current;
    const oldestExch = candles[0].time - IST_OFFSET;
    const to = oldestExch - 1;
    const from = to - tf.chunkDays * 86400;
    const r = await fetchHistory(symbol, resolution, from, to);
    if (seq !== fetchSeqRef.current) {
      loadingOlderRef.current = false;
      return;
    }
    const firstTime = candlesRef.current[0]?.time ?? Infinity;
    const older = r.candles.filter((c) => c.time < firstTime);
    if (older.length === 0) {
      haveMoreRef.current = false;
      loadingOlderRef.current = false;
      return;
    }
    candlesRef.current = [...older, ...candlesRef.current];
    applyData();
    loadingOlderRef.current = false;
  }

  // ------------------------------------------------------------------
  // Chart event handlers (subscribed once, dispatched via implRef)
  // ------------------------------------------------------------------

  function onCrosshair(param: MouseEventParams): void {
    // ghost preview for in-progress two-click drawings
    const pending = pendingRef.current;
    if (pending && param.point && (param.paneIndex ?? 0) === 0) {
      const t = xToTime(param.point.x);
      const p = mainRef.current?.coordinateToPrice(param.point.y);
      if (t !== null && p != null) {
        pending.cursor = { time: t, price: p as number };
        repaintDrawings();
      }
    }
    if (param.time == null || !param.point) {
      legendLastBar();
      return;
    }
    const idx = indexByTimeRef.current.get(param.time as number);
    if (idx === undefined) return;
    const candles = candlesRef.current;
    renderLegend(candles[idx], idx > 0 ? candles[idx - 1] : null);
  }

  function onChartClick(param: MouseEventParams): void {
    const chart = chartRef.current;
    const main = mainRef.current;
    if (!param.point || !chart || !main) return;
    if ((param.paneIndex ?? 0) !== 0) return; // draw/select only on the price pane
    const price = main.coordinateToPrice(param.point.y);
    const time = xToTime(param.point.x);
    const mode = drawModeRef.current;

    if (mode === "alert") {
      if (price != null) addAlert(price as number);
      setDrawMode(null);
      return;
    }
    if (mode) {
      if (price == null || time === null) return;
      const point: DrawingPoint = { time, price: price as number };
      if (mode === "text") {
        setTextDraft({ x: param.point.x, y: param.point.y, point, value: "" });
        setDrawMode(null);
        return;
      }
      if (pointsNeeded(mode) === 1) {
        addDrawing({ id: newDrawingId(), type: mode, points: [point] });
        setDrawMode(null);
        return;
      }
      const pending = pendingRef.current;
      if (!pending) {
        pendingRef.current = { type: mode, from: point, cursor: null };
        setPendingPoint(true);
        return;
      }
      if (pending.from.time === point.time && pending.from.price === point.price) return;
      addDrawing({
        id: newDrawingId(),
        type: mode,
        points: [pending.from, point],
      });
      pendingRef.current = null;
      setPendingPoint(false);
      setDrawMode(null);
      return;
    }

    // no tool active → hit-test for selection (topmost first)
    const dims = paneDims();
    const deps = drawingDeps();
    const list = drawingsRef.current;
    let hit: string | null = null;
    for (let i = list.length - 1; i >= 0; i--) {
      if (hitTest(list[i], param.point.x, param.point.y, deps, dims.width, dims.height)) {
        hit = list[i].id;
        break;
      }
    }
    selectedIdRef.current = hit;
    setSelectedDrawing(hit);
    repaintDrawings();
  }

  function onVisibleRange(range: LogicalRange | null): void {
    if (range && range.from < 10) void maybeLoadOlder();
    repaintDrawings(); // keep drawings glued to bars while panning
  }

  // ---- drag-to-modify drawings (DOM-level, capture phase) ----

  /** Pointer position relative to the main pane, or null when outside. */
  function paneCoords(e: MouseEvent): { x: number; y: number } | null {
    const host = containerRef.current;
    if (!host) return null;
    const r = host.getBoundingClientRect();
    const x = e.clientX - r.left;
    const y = e.clientY - r.top;
    const dims = paneDims();
    if (x < 0 || y < 0 || x > dims.width || y > dims.height) return null;
    return { x, y };
  }

  /** Capture-phase mousedown on the chart host: if it lands on a
   *  drawing, select it and begin a drag — the event is swallowed so
   *  the chart doesn't start panning underneath. */
  function onHostMouseDown(e: MouseEvent): void {
    if (e.button !== 0) return;
    if (drawModeRef.current || pendingRef.current) return; // a tool owns clicks
    const target = e.target as HTMLElement | null;
    if (target && target.tagName === "INPUT") return; // text-note editor
    const pt = paneCoords(e);
    if (!pt) return;
    const time = xToTime(pt.x);
    const price = mainRef.current?.coordinateToPrice(pt.y);
    if (time === null || price == null) return;
    const deps = drawingDeps();
    const dims = paneDims();
    const list = drawingsRef.current;
    for (let i = list.length - 1; i >= 0; i--) {
      const d = list[i];
      // Handles are only visible (and grabbable) on the selected drawing.
      const handle =
        selectedIdRef.current === d.id ? hitHandle(d, pt.x, pt.y, deps) : null;
      if (handle === null && !hitTest(d, pt.x, pt.y, deps, dims.width, dims.height)) {
        continue;
      }
      selectedIdRef.current = d.id;
      setSelectedDrawing(d.id);
      dragRef.current = {
        id: d.id,
        mode: handle !== null ? handle : "move",
        start: { time, price: price as number },
        orig: d.points.map((p) => ({ ...p })),
      };
      e.preventDefault();
      e.stopPropagation();
      repaintDrawings();
      return;
    }
  }

  function onWindowMouseMove(e: MouseEvent): void {
    const drag = dragRef.current;
    if (!drag) {
      updateHoverCursor(e);
      return;
    }
    const host = containerRef.current;
    if (!host) return;
    const r = host.getBoundingClientRect();
    const dims = paneDims();
    const x = Math.max(0, Math.min(e.clientX - r.left, dims.width));
    const y = Math.max(0, Math.min(e.clientY - r.top, dims.height));
    const time = xToTime(x);
    const price = mainRef.current?.coordinateToPrice(y);
    if (time === null || price == null) return;
    const list = drawingsRef.current;
    const idx = list.findIndex((d) => d.id === drag.id);
    if (idx < 0) return;
    const d = list[idx];
    // hline anchors are price-only, vline anchors time-only — dragging
    // must not smear them onto the other axis.
    const lockTime = d.type === "hline";
    const lockPrice = d.type === "vline";
    let points: DrawingPoint[];
    if (drag.mode === "move") {
      const dT = time - drag.start.time;
      const dP = (price as number) - drag.start.price;
      points = drag.orig.map((p) => ({
        time: lockTime ? p.time : p.time + dT,
        price: lockPrice ? p.price : p.price + dP,
      }));
    } else {
      points = drag.orig.map((p) => ({ ...p }));
      const i = drag.mode;
      points[i] = {
        time: lockTime ? points[i].time : time,
        price: lockPrice ? points[i].price : (price as number),
      };
    }
    const next = [...list];
    next[idx] = { ...d, points };
    drawingsRef.current = next;
    repaintDrawings();
  }

  function onWindowMouseUp(): void {
    if (!dragRef.current) return;
    dragRef.current = null;
    persistDrawings();
  }

  /** Grab-affordance cursor when hovering a drawing (no tool active). */
  function updateHoverCursor(e: MouseEvent): void {
    const host = containerRef.current;
    if (!host || drawModeRef.current) return;
    const target = e.target as HTMLElement | null;
    if (!target || !host.contains(target)) {
      if (hoverCursorElRef.current) {
        hoverCursorElRef.current.style.cursor = "";
        hoverCursorElRef.current = null;
      }
      return;
    }
    const pt = paneCoords(e);
    let cursor = "";
    if (pt) {
      const deps = drawingDeps();
      const dims = paneDims();
      for (let i = drawingsRef.current.length - 1; i >= 0; i--) {
        const d = drawingsRef.current[i];
        if (selectedIdRef.current === d.id && hitHandle(d, pt.x, pt.y, deps) !== null) {
          cursor = "nwse-resize";
          break;
        }
        if (hitTest(d, pt.x, pt.y, deps, dims.width, dims.height)) {
          cursor = "move";
          break;
        }
      }
    }
    if (hoverCursorElRef.current && hoverCursorElRef.current !== target) {
      hoverCursorElRef.current.style.cursor = "";
      hoverCursorElRef.current = null;
    }
    if (cursor) {
      target.style.cursor = cursor;
      hoverCursorElRef.current = target;
    } else if (hoverCursorElRef.current) {
      hoverCursorElRef.current.style.cursor = "";
      hoverCursorElRef.current = null;
    }
  }

  function onKeyDown(e: KeyboardEvent): void {
    const target = e.target as HTMLElement | null;
    const typing =
      target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA");
    if (e.key === "Escape") {
      if (typing) return;
      const drag = dragRef.current;
      if (drag) {
        // abort the drag — restore the drawing's points from dragstart
        const list = drawingsRef.current;
        const idx = list.findIndex((d) => d.id === drag.id);
        if (idx >= 0) {
          const next = [...list];
          next[idx] = { ...next[idx], points: drag.orig };
          drawingsRef.current = next;
        }
        dragRef.current = null;
        repaintDrawings();
        return;
      }
      if (drawModeRef.current || pendingRef.current) {
        setDrawMode(null);
      } else if (selectedIdRef.current) {
        selectedIdRef.current = null;
        setSelectedDrawing(null);
        repaintDrawings();
      } else {
        setMenuOpen(null);
        setFullscreen(false);
      }
      return;
    }
    if ((e.key === "Delete" || e.key === "Backspace") && !typing) {
      if (selectedIdRef.current) deleteDrawing(selectedIdRef.current);
    }
  }

  // Chart subscriptions are attached once at mount; they call through
  // this ref so they always run the latest render's closures.
  const implRef = useRef({
    onCrosshair,
    onChartClick,
    onVisibleRange,
    onKeyDown,
    onHostMouseDown,
    onWindowMouseMove,
    onWindowMouseUp,
  });
  implRef.current = {
    onCrosshair,
    onChartClick,
    onVisibleRange,
    onKeyDown,
    onHostMouseDown,
    onWindowMouseMove,
    onWindowMouseUp,
  };

  // ------------------------------------------------------------------
  // Effects
  // ------------------------------------------------------------------

  // 1) Create the chart once.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const colors = readThemeColors();
    colorsRef.current = colors;
    let chart: IChartApi;
    try {
      chart = createChart(el, {
        autoSize: true,
        layout: {
          background: { type: ColorType.Solid, color: "transparent" },
          textColor: colors.text,
          fontSize: 11,
          attributionLogo: false,
        },
        grid: {
          vertLines: { color: colors.grid },
          horzLines: { color: colors.grid },
        },
        crosshair: {
          mode: CrosshairMode.Normal,
          vertLine: { color: colors.crosshair, labelBackgroundColor: colors.border },
          horzLine: { color: colors.crosshair, labelBackgroundColor: colors.border },
        },
        rightPriceScale: { borderColor: colors.border },
        timeScale: {
          borderColor: colors.border,
          timeVisible: true,
          secondsVisible: false,
          rightOffset: 5,
          barSpacing: 8,
        },
        localization: { locale: "en-IN" },
      });
    } catch {
      setStatus({
        kind: "error",
        message: "chart engine failed to start in this browser",
      });
      return;
    }
    chartRef.current = chart;
    const vol = chart.addSeries(HistogramSeries, {
      priceScaleId: "vol",
      priceFormat: { type: "volume" },
      lastValueVisible: false,
      priceLineVisible: false,
    });
    try {
      vol.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    } catch {
      /* volume placement is cosmetic */
    }
    volumeRef.current = vol;

    const moveH = (p: MouseEventParams) => implRef.current.onCrosshair(p);
    const clickH = (p: MouseEventParams) => implRef.current.onChartClick(p);
    const rangeH = (r: LogicalRange | null) => implRef.current.onVisibleRange(r);
    const keyH = (e: KeyboardEvent) => implRef.current.onKeyDown(e);
    const downH = (e: MouseEvent) => implRef.current.onHostMouseDown(e);
    const winMoveH = (e: MouseEvent) => implRef.current.onWindowMouseMove(e);
    const winUpH = () => implRef.current.onWindowMouseUp();
    chart.subscribeCrosshairMove(moveH);
    chart.subscribeClick(clickH);
    chart.timeScale().subscribeVisibleLogicalRangeChange(rangeH);
    window.addEventListener("keydown", keyH);
    // Capture phase so a grab on a drawing wins over the chart's pan.
    el.addEventListener("mousedown", downH, true);
    window.addEventListener("mousemove", winMoveH);
    window.addEventListener("mouseup", winUpH);

    return () => {
      fetchSeqRef.current++; // invalidate in-flight fetches
      window.removeEventListener("keydown", keyH);
      el.removeEventListener("mousedown", downH, true);
      window.removeEventListener("mousemove", winMoveH);
      window.removeEventListener("mouseup", winUpH);
      dragRef.current = null;
      try {
        chart.unsubscribeCrosshairMove(moveH);
        chart.unsubscribeClick(clickH);
        chart.timeScale().unsubscribeVisibleLogicalRangeChange(rangeH);
      } catch {
        /* tearing down anyway */
      }
      try {
        chart.remove();
      } catch {
        /* tearing down anyway */
      }
      chartRef.current = null;
      mainRef.current = null;
      volumeRef.current = null;
      indicatorSeriesRef.current = [];
      compareSeriesRef.current = new Map();
      alertLinesRef.current = new Map();
      primitiveRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 2) (Re)create the main series when the chart type changes, and keep
  //    the drawings primitive attached to it.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    if (mainRef.current) {
      try {
        chart.removeSeries(mainRef.current);
      } catch {
        /* already gone */
      }
      mainRef.current = null;
      alertLinesRef.current = new Map(); // price lines died with the series
    }
    const colors = colorsRef.current;
    let series: ISeriesApi<SeriesType>;
    switch (chartKind) {
      case "bars":
        series = chart.addSeries(BarSeries, {
          upColor: colors.up,
          downColor: colors.down,
          thinBars: false,
        });
        break;
      case "line":
        series = chart.addSeries(LineSeries, {
          color: colors.accent,
          lineWidth: 2,
        });
        break;
      case "area":
        series = chart.addSeries(AreaSeries, {
          lineColor: colors.accent,
          lineWidth: 2,
          topColor: withAlpha(colors.accent, 0.25, "rgba(255,140,0,0.25)"),
          bottomColor: withAlpha(colors.accent, 0.03, "rgba(255,140,0,0.03)"),
        });
        break;
      default:
        series = chart.addSeries(CandlestickSeries, {
          upColor: colors.up,
          downColor: colors.down,
          borderUpColor: colors.up,
          borderDownColor: colors.down,
          wickUpColor: withAlpha(colors.up, 0.7, colors.up),
          wickDownColor: withAlpha(colors.down, 0.7, colors.down),
        });
    }
    mainRef.current = series;
    if (!primitiveRef.current) primitiveRef.current = new DrawingsPrimitive(drawingDeps());
    try {
      series.attachPrimitive(primitiveRef.current);
    } catch {
      /* drawings are best-effort */
    }
    setMainData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chartKind]);

  // 3) Load candles when the timeframe changes (symbol is fixed per
  //    instance — the parent remounts with a new `key` per symbol).
  useEffect(() => {
    const seq = ++fetchSeqRef.current;
    candlesRef.current = [];
    haveMoreRef.current = true;
    loadingOlderRef.current = false;
    setDrawMode(null);
    setStatus({ kind: "loading" });
    chartRef.current?.timeScale().applyOptions({ timeVisible: resolution !== "D" });
    const now = Math.floor(Date.now() / 1000);
    void (async () => {
      const r = await fetchHistory(symbol, resolution, now - tf.initialDays * 86400, now);
      if (seq !== fetchSeqRef.current) return;
      candlesRef.current = r.candles;
      if (r.reason) setStatus({ kind: "error", message: r.reason });
      else if (r.candles.length === 0) setStatus({ kind: "empty" });
      else setStatus({ kind: "ready" });
      applyData();
      try {
        chartRef.current?.timeScale().scrollToRealTime();
      } catch {
        /* cosmetic */
      }
      // One silent retry per symbol|timeframe — the first fetch right
      // after a backend restart can fail transiently while the Fyers
      // client warms up.
      const key = `${symbol}|${resolution}`;
      if (
        (r.reason || r.candles.length === 0) &&
        autoRetriedKeyRef.current !== key
      ) {
        autoRetriedKeyRef.current = key;
        setTimeout(() => {
          if (seq === fetchSeqRef.current) setReloadNonce((n) => n + 1);
        }, 4000);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol, resolution, reloadNonce]);

  // 4) Rebuild indicator series when the toggles change.
  useEffect(() => {
    rebuildIndicators();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);

  // 5) Keep alert price lines in sync (recreated when the main series
  //    changes, since price lines belong to a series).
  useEffect(() => {
    const main = mainRef.current;
    if (!main) return;
    for (const [, line] of alertLinesRef.current) {
      try {
        main.removePriceLine(line);
      } catch {
        /* already gone */
      }
    }
    const map = new Map<string, IPriceLine>();
    for (const a of alerts) {
      try {
        map.set(
          a.id,
          main.createPriceLine({
            price: a.price,
            color: colorsRef.current.draw,
            lineWidth: 1,
            lineStyle: LineStyle.LargeDashed,
            axisLabelVisible: true,
            title: "alert",
          }),
        );
      } catch {
        /* cosmetic */
      }
    }
    alertLinesRef.current = map;
    saveJson(`chart:alerts:${symbol}`, alerts);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [alerts, chartKind, status.kind]);

  // 6) Compare series — create/remove and (re)load on timeframe change.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const map = compareSeriesRef.current;
    for (const [sym, s] of [...map]) {
      if (!compares.some((c) => c.symbol === sym)) {
        try {
          chart.removeSeries(s);
        } catch {
          /* already gone */
        }
        map.delete(sym);
      }
    }
    const seq = fetchSeqRef.current;
    const now = Math.floor(Date.now() / 1000);
    for (const c of compares) {
      let s = map.get(c.symbol);
      if (!s) {
        s = chart.addSeries(LineSeries, {
          color: c.color,
          lineWidth: 1,
          priceScaleId: "right",
          priceLineVisible: false,
          title: c.name,
        });
        map.set(c.symbol, s);
      }
      const series = s;
      void fetchHistory(c.symbol, resolution, now - tf.initialDays * 86400, now).then((r) => {
        if (seq !== fetchSeqRef.current || !compareSeriesRef.current.has(c.symbol)) return;
        series.setData(r.candles.map((k) => ({ time: k.time, value: k.close })));
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [compares, resolution, status.kind]);

  // 7) Price-scale mode: compare forces percentage; else the toggle.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const mode =
      compares.length > 0
        ? PriceScaleMode.Percentage
        : scaleMode === "log"
          ? PriceScaleMode.Logarithmic
          : scaleMode === "percent"
            ? PriceScaleMode.Percentage
            : PriceScaleMode.Normal;
    try {
      chart.priceScale("right").applyOptions({ mode });
    } catch {
      /* cosmetic */
    }
  }, [compares, scaleMode]);

  // 8) Crosshair magnet toggle.
  useEffect(() => {
    chartRef.current?.applyOptions({
      crosshair: { mode: magnet ? CrosshairMode.Magnet : CrosshairMode.Normal },
    });
  }, [magnet]);

  // 9) Persist chart preferences.
  useEffect(() => {
    saveJson(PREFS_KEY, {
      resolution,
      chartKind,
      active,
      magnet,
      scaleMode,
    } satisfies ChartPrefs);
  }, [resolution, chartKind, active, magnet, scaleMode]);

  // 10) Compare-symbol search (debounced).
  useEffect(() => {
    const q = compareQuery.trim();
    if (q.length < 2) {
      setCompareHits([]);
      return;
    }
    const handle = setTimeout(() => {
      void api
        .get<SearchResponse>(`/api/search/symbols?q=${encodeURIComponent(q)}&limit=8`)
        .then((r) => {
          setCompareHits(
            (r.hits ?? [])
              .filter((h) => h.symbol !== symbol)
              .map((h) => ({ symbol: h.symbol, name: h.short_name })),
          );
        })
        .catch(() => setCompareHits([]));
    }, 300);
    return () => clearTimeout(handle);
  }, [compareQuery, symbol]);

  // 11) Live last bar from the `/ws` quote stream + alert triggers.
  const live = useLiveQuote(symbol);
  useEffect(() => {
    if (!live || live.last_price == null) return;
    const lp = live.last_price;

    // alert crossings (checked even while the chart is still loading)
    const prev = prevLtpRef.current;
    prevLtpRef.current = lp;
    if (prev !== null && prev !== lp && alerts.length > 0) {
      const fired = alerts.filter(
        (a) => (prev < a.price && lp >= a.price) || (prev > a.price && lp <= a.price),
      );
      if (fired.length > 0) {
        setAlerts((list) => list.filter((a) => !fired.some((f) => f.id === a.id)));
        for (const f of fired) {
          const text = `${shortName} crossed ${fmtPrice(f.price)} (LTP ${fmtPrice(lp)})`;
          addToast(`🔔 ${text}`);
          try {
            if (typeof Notification !== "undefined" && Notification.permission === "granted") {
              new Notification("Price alert", { body: text });
            }
          } catch {
            /* notifications unsupported */
          }
        }
      }
    }

    if (statusRef.current !== "ready") return;
    const candles = candlesRef.current;
    if (!mainRef.current || candles.length === 0) return;
    const last = candles[candles.length - 1];
    const parsed = Date.parse(live.ts);
    const tsSec = Number.isFinite(parsed) ? parsed / 1000 : Date.now() / 1000;
    const t = Math.floor(tsSec) + IST_OFFSET;
    const interval = barInterval();
    if (t < last.time) return; // stale tick
    // Buckets anchor to the last bar, not to midnight — NSE sessions
    // start at 09:15, so modulo-from-midnight would misalign 30m/1h bars.
    const n = Math.floor((t - last.time) / interval);
    if (n > 0) {
      // Only open a NEW bar during plausible NSE hours (Mon–Fri,
      // 09:00–15:40 IST) — quotes echo the last close on weekends and
      // overnight, which would otherwise mint phantom bars.
      const d = new Date(t * 1000); // t is IST-shifted, so read as UTC
      const dow = d.getUTCDay();
      const mins = d.getUTCHours() * 60 + d.getUTCMinutes();
      if (dow === 0 || dow === 6 || mins < 540 || mins > 940) return;
    }
    if (n === 0) {
      last.close = lp;
      if (lp > last.high) last.high = lp;
      if (lp < last.low) last.low = lp;
      updateMainBar(last);
    } else {
      const bar: Candle = {
        time: (last.time + n * interval) as UTCTimestamp,
        open: lp,
        high: lp,
        low: lp,
        close: lp,
        volume: 0,
      };
      candles.push(bar);
      indexByTimeRef.current.set(bar.time, candles.length - 1);
      updateMainBar(bar);
      try {
        volumeRef.current?.update({ time: bar.time, value: 0 });
      } catch {
        /* cosmetic */
      }
    }
    legendLastBar();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live]);

  // 12) Bar-close countdown (intraday, market hours only).
  useEffect(() => {
    const id = setInterval(() => {
      const el = countdownRef.current;
      if (!el) return;
      const candles = candlesRef.current;
      const res = resolutionRef.current;
      if (res === "D" || candles.length === 0 || statusRef.current !== "ready") {
        el.textContent = "";
        return;
      }
      const interval = barInterval();
      const nowIst = Math.floor(Date.now() / 1000) + IST_OFFSET;
      const d = new Date(nowIst * 1000);
      const dow = d.getUTCDay();
      const mins = d.getUTCHours() * 60 + d.getUTCMinutes();
      if (dow === 0 || dow === 6 || mins < 555 || mins > 930) {
        el.textContent = "market closed";
        return;
      }
      const last = candles[candles.length - 1];
      const remaining = last.time + interval - nowIst;
      if (remaining <= 0 || remaining > interval) {
        el.textContent = "";
        return;
      }
      const mm = Math.floor(remaining / 60);
      const ss = remaining % 60;
      el.textContent = `bar closes in ${mm}:${String(ss).padStart(2, "0")}`;
    }, 1000);
    return () => clearInterval(id);
  }, []);

  // 13) Close menus on outside click.
  useEffect(() => {
    if (!menuOpen) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as HTMLElement | null;
      if (!t?.closest(".chart-menu-wrap")) setMenuOpen(null);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [menuOpen]);

  // ------------------------------------------------------------------
  // Toolbar actions
  // ------------------------------------------------------------------

  function screenshot(): void {
    const chart = chartRef.current;
    if (!chart) return;
    try {
      const canvas = chart.takeScreenshot();
      canvas.toBlob((b) => {
        if (!b) return;
        const url = URL.createObjectURL(b);
        const a = document.createElement("a");
        a.href = url;
        a.download = `${shortName}_${tf.label}.png`;
        a.click();
        setTimeout(() => URL.revokeObjectURL(url), 5000);
      });
    } catch {
      addToast("screenshot failed in this browser");
    }
  }

  function commitTextDraft(): void {
    if (!textDraft) return;
    const value = textDraft.value.trim();
    if (value) {
      addDrawing({
        id: newDrawingId(),
        type: "text",
        points: [textDraft.point],
        text: value.slice(0, 80),
      });
    }
    setTextDraft(null);
  }

  const toggleDraw = (mode: Exclude<DrawMode, null>) => {
    setMenuOpen(null);
    setDrawMode(drawModeRef.current === mode ? null : mode);
  };

  const activeTool = DRAW_TOOLS.find((t) => t.id === drawMode);
  const drawHint =
    drawMode === "alert"
      ? "click a price to set an alert"
      : activeTool
        ? pendingPoint
          ? "click the second point (Esc cancels)"
          : `${activeTool.hint} (Esc cancels)`
        : null;

  // ------------------------------------------------------------------
  // Render
  // ------------------------------------------------------------------

  return (
    <section
      className={`trade-card chart-card${fullscreen ? " chart-fullscreen" : ""}`}
      data-testid="trade-chart"
    >
      <div className="chart-toolbar">
        <div className="chart-symbol" title={symbol}>
          <span className="sym">{shortName}</span>
          <span className="exch">{symbol}</span>
        </div>

        <div className="chart-group" role="group" aria-label="timeframe">
          {TIMEFRAMES.map((t) => (
            <button
              key={t.res}
              type="button"
              className={`chart-btn${resolution === t.res ? " on" : ""}`}
              onClick={() => setResolution(t.res)}
              data-testid={`chart-tf-${t.label}`}
            >
              {t.label}
            </button>
          ))}
        </div>

        <div className="chart-group" role="group" aria-label="chart type">
          {CHART_KINDS.map((k) => (
            <button
              key={k.id}
              type="button"
              className={`chart-btn${chartKind === k.id ? " on" : ""}`}
              onClick={() => setChartKind(k.id)}
              title={k.title}
              data-testid={`chart-type-${k.id}`}
            >
              {k.label}
            </button>
          ))}
        </div>

        {/* indicators */}
        <div className="chart-group chart-menu-wrap">
          <button
            type="button"
            className={`chart-btn${menuOpen === "ind" ? " on" : ""}`}
            onClick={() => setMenuOpen(menuOpen === "ind" ? null : "ind")}
            data-testid="chart-indicators-btn"
          >
            Indicators ▾
          </button>
          {menuOpen === "ind" && (
            <div className="chart-menu chart-ind-menu" data-testid="chart-ind-menu">
              {(["Overlays", "Oscillators"] as const).map((group) => (
                <div key={group} className="chart-menu-section">
                  <div className="chart-menu-head">{group}</div>
                  {INDICATOR_DEFS.filter((d) => d.group === group).map((d) => {
                    const disabled = d.id === "vwap" && resolution === "D";
                    return (
                      <label key={d.id} className={disabled ? "disabled" : ""}>
                        <input
                          type="checkbox"
                          checked={active[d.id]}
                          disabled={disabled}
                          onChange={() =>
                            setActive((a) => ({ ...a, [d.id]: !a[d.id] }))
                          }
                          data-testid={`chart-ind-${d.id}`}
                        />
                        {d.label}
                        {disabled && <span className="hint"> (intraday)</span>}
                      </label>
                    );
                  })}
                </div>
              ))}
            </div>
          )}
        </div>

        {/* drawing tools */}
        <div className="chart-group chart-menu-wrap">
          <button
            type="button"
            className={`chart-btn${drawMode && drawMode !== "alert" ? " on" : ""}`}
            onClick={() => setMenuOpen(menuOpen === "draw" ? null : "draw")}
            data-testid="chart-draw-btn"
          >
            {activeTool ? activeTool.label : "Draw"} ▾
          </button>
          {menuOpen === "draw" && (
            <div className="chart-menu" data-testid="chart-draw-menu">
              {DRAW_TOOLS.map((t) => (
                <button
                  key={t.id}
                  type="button"
                  className={`chart-menu-item${drawMode === t.id ? " on" : ""}`}
                  onClick={() => toggleDraw(t.id)}
                  data-testid={`chart-draw-${t.id}`}
                >
                  {t.label}
                </button>
              ))}
              <div className="chart-menu-sep" />
              {selectedDrawing && (
                <button
                  type="button"
                  className="chart-menu-item"
                  onClick={() => {
                    deleteDrawing(selectedDrawing);
                    setMenuOpen(null);
                  }}
                  data-testid="chart-draw-delete"
                >
                  ✕ Delete selected
                </button>
              )}
              <button
                type="button"
                className="chart-menu-item"
                onClick={() => {
                  clearDrawings();
                  setMenuOpen(null);
                }}
                data-testid="chart-draw-clear"
              >
                ✕ Clear all drawings
              </button>
            </div>
          )}
        </div>

        {/* alerts */}
        <div className="chart-group chart-menu-wrap">
          <button
            type="button"
            className={`chart-btn${drawMode === "alert" || alerts.length > 0 ? " on" : ""}`}
            onClick={() => setMenuOpen(menuOpen === "alerts" ? null : "alerts")}
            data-testid="chart-alerts-btn"
          >
            🔔 {alerts.length > 0 ? alerts.length : ""} ▾
          </button>
          {menuOpen === "alerts" && (
            <div className="chart-menu" data-testid="chart-alerts-menu">
              <button
                type="button"
                className="chart-menu-item"
                onClick={() => {
                  setMenuOpen(null);
                  setDrawMode("alert");
                }}
                data-testid="chart-alert-add"
              >
                ⊕ Add alert (click a price)
              </button>
              {alerts.length > 0 && <div className="chart-menu-sep" />}
              {alerts.map((a) => (
                <div key={a.id} className="chart-menu-row">
                  <span>{fmtPrice(a.price)}</span>
                  <button
                    type="button"
                    className="chart-menu-x"
                    onClick={() => removeAlert(a.id)}
                    title="Remove alert"
                    data-testid={`chart-alert-del-${a.id}`}
                  >
                    ✕
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* compare */}
        <div className="chart-group chart-menu-wrap">
          <button
            type="button"
            className={`chart-btn${compares.length > 0 ? " on" : ""}`}
            onClick={() => setMenuOpen(menuOpen === "compare" ? null : "compare")}
            data-testid="chart-compare-btn"
          >
            ⇄ Compare{compares.length > 0 ? ` ${compares.length}` : ""} ▾
          </button>
          {menuOpen === "compare" && (
            <div className="chart-menu" data-testid="chart-compare-menu">
              <input
                type="text"
                className="chart-menu-input"
                placeholder="search symbol…"
                value={compareQuery}
                onChange={(e) => setCompareQuery(e.target.value)}
                autoFocus
                data-testid="chart-compare-input"
              />
              {compareHits
                .filter((h) => !compares.some((c) => c.symbol === h.symbol))
                .map((h) => (
                  <button
                    key={h.symbol}
                    type="button"
                    className="chart-menu-item"
                    onClick={() => {
                      if (compares.length >= 4) return;
                      setCompares((c) => [
                        ...c,
                        {
                          symbol: h.symbol,
                          name: h.name,
                          color: COMPARE_COLORS[c.length % COMPARE_COLORS.length],
                        },
                      ]);
                      setCompareQuery("");
                      setCompareHits([]);
                    }}
                    data-testid={`chart-compare-add-${h.symbol}`}
                  >
                    {h.name} <span className="hint">{h.symbol}</span>
                  </button>
                ))}
              {compares.length > 0 && <div className="chart-menu-sep" />}
              {compares.map((c) => (
                <div key={c.symbol} className="chart-menu-row">
                  <span>
                    <span className="chart-dot" style={{ background: c.color }} />
                    {c.name}
                  </span>
                  <button
                    type="button"
                    className="chart-menu-x"
                    onClick={() =>
                      setCompares((list) => list.filter((x) => x.symbol !== c.symbol))
                    }
                    title="Remove"
                    data-testid={`chart-compare-del-${c.symbol}`}
                  >
                    ✕
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* scale / misc controls */}
        <div className="chart-group chart-right" role="group" aria-label="chart controls">
          <button
            type="button"
            className={`chart-btn${scaleMode === "log" ? " on" : ""}`}
            onClick={() => setScaleMode((m) => (m === "log" ? "normal" : "log"))}
            disabled={compares.length > 0}
            title="Logarithmic price scale"
            data-testid="chart-scale-log"
          >
            log
          </button>
          <button
            type="button"
            className={`chart-btn${scaleMode === "percent" || compares.length > 0 ? " on" : ""}`}
            onClick={() => setScaleMode((m) => (m === "percent" ? "normal" : "percent"))}
            disabled={compares.length > 0}
            title="Percentage price scale"
            data-testid="chart-scale-pct"
          >
            %
          </button>
          <button
            type="button"
            className={`chart-btn${magnet ? " on" : ""}`}
            onClick={() => setMagnet((m) => !m)}
            title="Crosshair magnet — snap to OHLC values"
            data-testid="chart-magnet"
          >
            🧲
          </button>
          <button
            type="button"
            className="chart-btn"
            onClick={screenshot}
            title="Download chart as PNG"
            data-testid="chart-screenshot"
          >
            📷
          </button>
          <button
            type="button"
            className={`chart-btn${fullscreen ? " on" : ""}`}
            onClick={() => setFullscreen((f) => !f)}
            title={fullscreen ? "Exit fullscreen (Esc)" : "Fullscreen"}
            data-testid="chart-fullscreen-btn"
          >
            {fullscreen ? "🗕 Exit" : "⛶ Full"}
          </button>
        </div>
      </div>

      <div className="chart-container">
        <div ref={containerRef} className="chart-host" />
        <div className="chart-watermark">{shortName}</div>
        <div className="chart-legend">
          <div ref={legendRef} className="chart-legend-main" />
          <div ref={countdownRef} className="chart-countdown" />
        </div>
        {status.kind === "loading" && (
          <div className="chart-status">loading chart…</div>
        )}
        {status.kind === "error" && (
          <div className="chart-status warn-text" data-testid="chart-error">
            <span>{status.message}</span>
            <button
              type="button"
              className="chart-retry"
              onClick={() => setReloadNonce((n) => n + 1)}
              data-testid="chart-retry"
            >
              ↻ Retry
            </button>
          </div>
        )}
        {status.kind === "empty" && (
          <div className="chart-status" data-testid="chart-empty">
            <span>no chart data for this symbol / timeframe</span>
            <button
              type="button"
              className="chart-retry"
              onClick={() => setReloadNonce((n) => n + 1)}
              data-testid="chart-retry"
            >
              ↻ Retry
            </button>
          </div>
        )}
        {drawHint && (
          <div className="chart-drawhint" data-testid="chart-drawhint">
            {drawHint}
          </div>
        )}
        {selectedDrawing && !drawMode && (
          <div className="chart-drawhint" data-testid="chart-selecthint">
            drawing selected — drag to move, drag a handle to resize, Delete
            removes, Esc deselects
          </div>
        )}
        {textDraft && (
          <input
            type="text"
            className="chart-text-input"
            style={{
              left: Math.max(4, Math.min(textDraft.x, (containerRef.current?.clientWidth ?? 400) - 160)),
              top: Math.max(4, textDraft.y - 12),
            }}
            value={textDraft.value}
            placeholder="note… (Enter)"
            autoFocus
            onChange={(e) => setTextDraft({ ...textDraft, value: e.target.value })}
            onKeyDown={(e) => {
              if (e.key === "Enter") commitTextDraft();
              else if (e.key === "Escape") setTextDraft(null);
            }}
            onBlur={commitTextDraft}
            data-testid="chart-text-input"
          />
        )}
        <div className="chart-toasts">
          {toasts.map((t) => (
            <div key={t.id} className="chart-toast" data-testid="chart-toast">
              {t.text}
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
