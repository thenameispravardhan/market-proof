// chartSettings — the Chart Settings dialog's model (Symbol, Status line,
// Scales and lines, Canvas, Trading, Events, Alerts), its defaults, and
// the per-browser persistence + named templates. Empty color strings mean
// "follow the app theme".

import type { DateFormat } from "./chartData";

export type LineVisibility = "hover" | "always" | "never";

/** The chart types built from bricks / columns instead of time bars. */
export type BrickKind = "renko" | "kagi" | "pnf" | "linebreak" | "range";

/** One brick type's inputs (TradingView shows these per chart type). */
export interface BrickInputs {
  /** Box size (Kagi: reversal amount) from the ATR or a fixed value. */
  method: "atr" | "traditional";
  atrLength: number;
  /** Traditional box size / Kagi reversal amount / Range size. */
  box: number;
  /** P&F: boxes needed to reverse a column. */
  reversal: number;
  /** Build from closes, or from each bar's high and low. */
  source: "close" | "hl";
  /** Line break: lines to break for a reversal. */
  lines: number;
}

export const DEFAULT_BRICKS: Record<BrickKind, BrickInputs> = {
  renko: { method: "atr", atrLength: 14, box: 0, reversal: 2, source: "close", lines: 3 },
  kagi: { method: "atr", atrLength: 14, box: 0, reversal: 1, source: "close", lines: 3 },
  pnf: { method: "atr", atrLength: 14, box: 0, reversal: 3, source: "close", lines: 3 },
  linebreak: { method: "atr", atrLength: 14, box: 0, reversal: 3, source: "close", lines: 3 },
  range: { method: "atr", atrLength: 14, box: 0, reversal: 1, source: "close", lines: 3 },
};

/** A brick type's effective inputs; settings saved before per-type inputs
 *  (one shared box size / reversal / lines) still apply. */
export function brickInputs(s: ChartSettings, k: BrickKind): BrickInputs {
  const legacy: Partial<BrickInputs> = {
    ...(s.boxSize > 0 ? { method: "traditional" as const, box: s.boxSize } : {}),
    ...(k === "pnf" && s.reversal ? { reversal: s.reversal } : {}),
    ...(k === "linebreak" && s.lineBreak ? { lines: s.lineBreak } : {}),
  };
  return { ...DEFAULT_BRICKS[k], ...legacy, ...(s.bricks?.[k] ?? {}) };
}

export interface ChartSettings {
  // ---- Symbol ----
  colorPrevClose: boolean;
  bodyOn: boolean;
  upColor: string;
  downColor: string;
  borderOn: boolean;
  borderUp: string;
  borderDown: string;
  wickOn: boolean;
  wickUp: string;
  wickDown: string;
  precision: number | null;
  timezone: string;
  /** Legacy shared brick inputs (pre per-type inputs); see `bricks`. */
  boxSize: number;
  reversal: number;
  lineBreak: number;
  /** Per chart type inputs (Renko, Kagi, P&F, Line break, Range). */
  bricks: Partial<Record<BrickKind, Partial<BrickInputs>>>;
  // ---- Status line ----
  showTitle: boolean;
  titleMode: "description" | "ticker" | "both";
  showOhlc: boolean;
  showBarChange: boolean;
  showVolume: boolean;
  showLastDayChange: boolean;
  indTitles: boolean;
  indArgs: boolean;
  indValues: boolean;
  legendBg: boolean;
  legendBgOpacity: number;
  /** Legend background colour ("" = the panel colour). */
  legendBgColor: string;
  // ---- Scales and lines ----
  lastPriceLabel: boolean;
  lastPriceLine: boolean;
  symbolNameLabel: boolean;
  prevCloseLabel: boolean;
  prevCloseLine: boolean;
  highLowLabels: boolean;
  highLowLines: boolean;
  avgCloseLabel: boolean;
  avgCloseLine: boolean;
  bidAskLabels: boolean;
  bidAskLines: boolean;
  indNameLabels: boolean;
  indValueLabels: boolean;
  countdown: boolean;
  scaleSide: "right" | "left";
  /** The dialog's "Scales placement": Auto keeps the price scale on the right. */
  scalePlacement: "auto" | "right" | "left";
  /** Price-axis labels are nudged apart instead of overlapping. */
  noOverlapLabels: boolean;
  /** Keep price-per-bar fixed while zooming the time axis. */
  lockRatio: boolean;
  /** Price units per bar width when locked (0 = take the current view). */
  priceBarRatio: number;
  /** Symbol label in scale units (%, indexed) — off shows the raw price. */
  lastPriceScaleValue: boolean;
  /** Last-price line / label colour ("" = the last bar's direction). */
  lastPriceColor: string;
  scaleModesButtons: LineVisibility;
  dateFormat: DateFormat;
  hour12: boolean;
  // ---- Canvas ----
  theme: "app" | "dark" | "light";
  bgType: "solid" | "gradient";
  bg1: string;
  bg2: string;
  grid: "both" | "vert" | "horz" | "none";
  gridColor: string;
  crosshairColor: string;
  crosshairWidth: number;
  crosshairStyle: 0 | 1 | 2 | 3;
  watermark: boolean;
  watermarkColor: string;
  /** Brand logo, bottom-left of the price pane. */
  logoWatermark: boolean;
  textColor: string;
  fontSize: number;
  scaleLineColor: string;
  navButtons: LineVisibility;
  paneButtons: LineVisibility;
  marginTop: number;
  marginBottom: number;
  marginRight: number;
  // ---- Trading ----
  buySellButtons: boolean;
  sound: boolean;
  notifications: "all" | "rejections" | "off";
  plMode: "money" | "percent" | "ticks";
  reverseButton: boolean;
  ordersAlign: "right" | "left";
  /** Fills from the trade book as marks on the bars, optionally labelled. */
  executions: boolean;
  executionLabels: boolean;
  /** Position / order lines across the whole pane (off = a short stub at the label). */
  extendLines: boolean;
  // ---- Events ----
  showEvents: boolean;
  sessionBreaks: boolean;
  showMarks: boolean;
  // ---- Alerts ----
  alertLines: boolean;
  alertColor: string;
}

export const DEFAULT_SETTINGS: ChartSettings = {
  colorPrevClose: false,
  bodyOn: true,
  upColor: "",
  downColor: "",
  borderOn: true,
  borderUp: "",
  borderDown: "",
  wickOn: true,
  wickUp: "",
  wickDown: "",
  precision: null,
  timezone: "exchange",
  boxSize: 0,
  reversal: 3,
  lineBreak: 3,
  bricks: {},
  showTitle: true,
  titleMode: "description",
  showOhlc: true,
  showBarChange: true,
  showVolume: true,
  showLastDayChange: false,
  indTitles: true,
  indArgs: true,
  indValues: true,
  legendBg: false,
  legendBgOpacity: 0.5,
  legendBgColor: "",
  lastPriceLabel: true,
  lastPriceLine: true,
  symbolNameLabel: false,
  prevCloseLabel: false,
  prevCloseLine: false,
  highLowLabels: false,
  highLowLines: false,
  avgCloseLabel: false,
  avgCloseLine: false,
  bidAskLabels: false,
  bidAskLines: false,
  indNameLabels: false,
  indValueLabels: true,
  countdown: true,
  scaleSide: "right",
  scalePlacement: "auto",
  noOverlapLabels: true,
  lockRatio: false,
  priceBarRatio: 0,
  lastPriceScaleValue: true,
  lastPriceColor: "",
  scaleModesButtons: "hover",
  dateFormat: "dd MMM 'yy",
  hour12: false,
  theme: "app",
  bgType: "solid",
  bg1: "",
  bg2: "",
  grid: "both",
  gridColor: "",
  crosshairColor: "",
  crosshairWidth: 1,
  crosshairStyle: 3,
  watermark: true,
  watermarkColor: "",
  logoWatermark: true,
  textColor: "",
  fontSize: 11,
  scaleLineColor: "",
  navButtons: "hover",
  paneButtons: "hover",
  marginTop: 10,
  marginBottom: 8,
  marginRight: 5,
  buySellButtons: true,
  sound: true,
  notifications: "all",
  plMode: "money",
  reverseButton: true,
  ordersAlign: "right",
  executions: true,
  executionLabels: false,
  extendLines: true,
  showEvents: true,
  sessionBreaks: false,
  showMarks: true,
  alertLines: true,
  alertColor: "",
};

export const SETTINGS_KEY = "chart:settings";
export const SETTINGS_TEMPLATES_KEY = "chart:settingsTemplates";

export function loadSettings(): ChartSettings {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    return raw ? { ...DEFAULT_SETTINGS, ...(JSON.parse(raw) as Partial<ChartSettings>) } : { ...DEFAULT_SETTINGS };
  } catch {
    return { ...DEFAULT_SETTINGS };
  }
}

export function saveSettings(s: ChartSettings): void {
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(s));
  } catch {
    /* best-effort */
  }
}

/** Light canvas palette (the "Change theme" tool). */
export const LIGHT = {
  bg: "#FFFFFF",
  panel: "#FFFFFF",
  surface: "#F0F3FA",
  elev: "#E0E3EB",
  text: "#131722",
  dim: "#434651",
  faint: "#787B86",
  border: "#D1D4DC",
  grid: "#F0F3FA",
};
