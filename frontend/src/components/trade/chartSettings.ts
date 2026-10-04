// chartSettings — the Chart Settings dialog's model (Symbol, Status line,
// Scales and lines, Canvas, Trading, Events, Alerts), its defaults, and
// the per-browser persistence + named templates. Empty color strings mean
// "follow the app theme".

import type { DateFormat } from "./chartData";

export type LineVisibility = "hover" | "always" | "never";

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
  /** Renko / Kagi / P&F / Range box: 0 = auto (ATR). */
  boxSize: number;
  reversal: number;
  lineBreak: number;
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
  plMode: "money" | "percent";
  reverseButton: boolean;
  ordersAlign: "right" | "left";
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
