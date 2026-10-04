// drawings — the chart drawing layer for ChartPanel.
//
// Every drawing lives in a plain array owned by ChartPanel and is painted
// by DrawingsPrimitive, a lightweight-charts series primitive attached to
// the main series. Each tool is a GEOMETRY function: projected anchor
// points in, a list of plain shapes (lines, polygons, rects, ellipses,
// curves, text, candles, images) out. One painter draws shapes and one
// hit-tester tests them, so ~85 tools share the same rendering, selection
// and drag code.
//
// Coordinates: points are {time, price} where `time` is chart time
// (IST-shifted epoch seconds — the same convention as the candle data), so
// drawings survive timeframe switches and reloads. ChartPanel supplies the
// projections (timeToX interpolates fractional logical positions so a 5m
// anchor lands correctly on a 1D chart). Screen-anchored tools (anchored
// text / note) keep a pane-relative position instead.

import type {
  IPrimitivePaneView,
  ISeriesPrimitive,
  SeriesAttachedParameter,
  Time,
} from "lightweight-charts";
import { formatSpan, type Bar, type IntervalGroup } from "./chartData";
import { linearRegression } from "../../lib/indicators";

// ---------------------------------------------------------------------------
// Model
// ---------------------------------------------------------------------------

export type DrawingType = string;

export interface DrawingPoint {
  time: number; // chart time (IST-shifted epoch seconds)
  price: number;
}

export interface Level {
  v: number;
  color: string;
  on: boolean;
}

export interface DrawingStyle {
  color: string; // "" = the theme's drawing color
  width: number;
  dash: 0 | 1 | 2; // solid, dotted, dashed
  fill: boolean;
  fillColor: string; // "" = the line color
  fillOpacity: number;
  textColor: string; // "" = the line color
  fontSize: number;
  bold: boolean;
  italic: boolean;
  extendLeft: boolean;
  extendRight: boolean;
  labels: boolean;
  levels?: Level[];
}

export interface DrawingData {
  /** Long / short position: account size, risk %, fixed qty (0 = sized from risk), lot. */
  account?: number;
  risk?: number;
  qty?: number;
  lot?: number;
  /** Size the position by a cash risk instead of a % of the account. */
  riskMode?: "pct" | "amount";
  riskAmount?: number;
  emoji?: string;
  /** Icons tab: a monochrome glyph painted in the drawing's colour. */
  glyph?: boolean;
  /** Stickers tab: an emoji + caption badge. */
  sticker?: { text: string; color: string };
  src?: string; // image data URL
  w?: number;
  h?: number;
}

/** Per-timeframe visibility: on/off, or on within a range of interval counts. */
export type DrawingVis = boolean | { on: boolean; min: number; max: number };

export interface Drawing {
  id: string;
  type: DrawingType;
  points: DrawingPoint[];
  text?: string;
  style?: Partial<DrawingStyle>;
  locked?: boolean;
  hidden?: boolean;
  name?: string;
  /** Pane-relative position (0..1) for screen-anchored tools. */
  screen?: { x: number; y: number };
  /** Per-timeframe visibility; absent / true = shown. */
  vis?: Partial<Record<IntervalGroup, DrawingVis>>;
  data?: DrawingData;
}

export interface Pending {
  type: DrawingType;
  points: DrawingPoint[];
  cursor: DrawingPoint | null;
  data?: DrawingData;
}

export interface DrawingDeps {
  drawings: () => Drawing[];
  pending: () => Pending | null;
  selectedId: () => string | null;
  hoverId: () => string | null;
  timeToX: (t: number) => number | null;
  xToTime: (x: number) => number | null;
  priceToY: (p: number) => number | null;
  yToPrice: (y: number) => number | null;
  /** Fractional bar index of a chart time. */
  timeToLogical: (t: number) => number | null;
  barSpacing: () => number;
  candles: () => Bar[];
  intervalGroup: () => IntervalGroup;
  /** The interval's count within its group (5 for 5m) — visibility ranges. */
  intervalCount?: () => number;
  /** Instrument tick size (position tools show distances in ticks). */
  tickSize?: () => number;
  lineColor: () => string;
  accent: () => string;
  upColor: () => string;
  downColor: () => string;
  bgColor: () => string;
  priceFormatter: (p: number) => string;
  lastPrice: () => number | null;
  /** The user's saved default style for a tool. */
  toolDefaults: (type: DrawingType) => Partial<DrawingStyle> | undefined;
  hidden: () => boolean;
  repaint: () => void;
  /** Extra painting under the drawings (session breaks, locked cursor …). */
  extras?: (ctx: CanvasRenderingContext2D, w: number, h: number) => void;
}

// ---------------------------------------------------------------------------
// Tool catalog
// ---------------------------------------------------------------------------

export type ToolGroupId = "lines" | "fib" | "patterns" | "forecast" | "shapes" | "annotate" | "icons";

export interface ToolDef {
  id: DrawingType;
  label: string;
  group: ToolGroupId;
  section?: string;
  icon: string;
  points: number | "free" | "poly";
  /** Default text — the tool asks for text after it is placed. */
  text?: string;
  screen?: boolean;
  /** Anchors move on one axis only. */
  axis?: "price" | "time";
  defaults?: Partial<DrawingStyle>;
  /** Not persisted (measure). */
  temp?: boolean;
  hint?: string;
  /** Keyboard shortcut shown in the flyout. */
  kbd?: string;
}

const lv = (v: number, color: string, on = true): Level => ({ v, color, on });

export const FIB_LEVELS: Level[] = [
  lv(0, "#787B86"), lv(0.236, "#F23645"), lv(0.382, "#FF9800"), lv(0.5, "#4CAF50"),
  lv(0.618, "#089981"), lv(0.786, "#00BCD4"), lv(1, "#787B86"), lv(1.618, "#2962FF"),
  lv(2.618, "#F23645", false), lv(3.618, "#9C27B0", false), lv(4.236, "#E91E63", false),
];
const FIB_EXT_LEVELS: Level[] = [
  lv(0, "#787B86"), lv(0.382, "#FF9800"), lv(0.618, "#089981"), lv(1, "#787B86"),
  lv(1.272, "#00BCD4", false), lv(1.618, "#2962FF"), lv(2, "#9C27B0", false), lv(2.618, "#F23645"),
  lv(3.618, "#E91E63", false), lv(4.236, "#673AB7", false),
];
const FIB_TIME_LEVELS: Level[] = [0, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144].map((v) => lv(v, "#2962FF"));
const FIB_TREND_TIME_LEVELS: Level[] = [0, 0.382, 0.5, 0.618, 1, 1.382, 1.618, 2, 2.382, 2.618, 3, 3.618, 4.236].map((v, i) =>
  lv(v, ["#787B86", "#F23645", "#FF9800", "#4CAF50", "#089981", "#00BCD4", "#2962FF", "#9C27B0", "#E91E63", "#673AB7", "#787B86", "#F23645", "#FF9800"][i], i < 8),
);
const FAN_LEVELS: Level[] = [
  lv(0, "#787B86"), lv(0.25, "#F23645"), lv(0.382, "#FF9800"), lv(0.5, "#4CAF50"),
  lv(0.618, "#089981"), lv(0.75, "#00BCD4"), lv(1, "#787B86"),
];
const CIRCLE_LEVELS: Level[] = [
  lv(0.236, "#F23645", false), lv(0.382, "#FF9800"), lv(0.5, "#4CAF50"), lv(0.618, "#089981"),
  lv(0.786, "#00BCD4", false), lv(1, "#787B86"), lv(1.618, "#2962FF"), lv(2.618, "#9C27B0", false),
];

export const TOOLS: ToolDef[] = [
  // ---- trend line tools ----
  { id: "trend", label: "Trend Line", group: "lines", section: "Lines", icon: "╱", points: 2, kbd: "Alt+T" },
  { id: "ray", label: "Ray", group: "lines", section: "Lines", icon: "↗", points: 2 },
  { id: "info", label: "Info Line", group: "lines", section: "Lines", icon: "ⓘ", points: 2 },
  { id: "extended", label: "Extended Line", group: "lines", section: "Lines", icon: "⟷", points: 2 },
  { id: "angle", label: "Trend Angle", group: "lines", section: "Lines", icon: "∠", points: 2 },
  { id: "hline", label: "Horizontal Line", group: "lines", section: "Lines", icon: "─", points: 1, axis: "price", kbd: "Alt+H" },
  { id: "hray", label: "Horizontal Ray", group: "lines", section: "Lines", icon: "⇥", points: 1, kbd: "Alt+J" },
  { id: "vline", label: "Vertical Line", group: "lines", section: "Lines", icon: "│", points: 1, axis: "time", kbd: "Alt+V" },
  { id: "cross", label: "Cross Line", group: "lines", section: "Lines", icon: "┼", points: 1, kbd: "Alt+C" },
  { id: "channel", label: "Parallel Channel", group: "lines", section: "Channels", icon: "▱", points: 3 },
  { id: "regression", label: "Regression Trend", group: "lines", section: "Channels", icon: "≈", points: 2 },
  { id: "flattop", label: "Flat Top/Bottom", group: "lines", section: "Channels", icon: "⫠", points: 3 },
  { id: "disjoint", label: "Disjoint Channel", group: "lines", section: "Channels", icon: "⋎", points: 3 },
  { id: "pitchfork", label: "Pitchfork", group: "lines", section: "Pitchforks", icon: "⋔", points: 3 },
  { id: "schiff", label: "Schiff Pitchfork", group: "lines", section: "Pitchforks", icon: "⋔", points: 3 },
  { id: "mschiff", label: "Modified Schiff Pitchfork", group: "lines", section: "Pitchforks", icon: "⋔", points: 3 },
  { id: "ipitchfork", label: "Inside Pitchfork", group: "lines", section: "Pitchforks", icon: "⋔", points: 3 },
  // ---- gann & fibonacci ----
  { id: "fib", label: "Fib Retracement", group: "fib", section: "Fibonacci", icon: "≣", points: 2, defaults: { levels: FIB_LEVELS }, kbd: "Alt+F" },
  { id: "fibext", label: "Trend-Based Fib Extension", group: "fib", section: "Fibonacci", icon: "⩸", points: 3, defaults: { levels: FIB_EXT_LEVELS } },
  { id: "fibchannel", label: "Fib Channel", group: "fib", section: "Fibonacci", icon: "⫽", points: 3, defaults: { levels: FIB_LEVELS.slice(0, 8) } },
  { id: "fibtime", label: "Fib Time Zone", group: "fib", section: "Fibonacci", icon: "⦙", points: 2, defaults: { levels: FIB_TIME_LEVELS } },
  { id: "fibfan", label: "Fib Speed Resistance Fan", group: "fib", section: "Fibonacci", icon: "⟀", points: 2, defaults: { levels: FAN_LEVELS } },
  { id: "fibtrendtime", label: "Trend-Based Fib Time", group: "fib", section: "Fibonacci", icon: "⫼", points: 3, defaults: { levels: FIB_TREND_TIME_LEVELS } },
  { id: "fibcircles", label: "Fib Circles", group: "fib", section: "Fibonacci", icon: "◎", points: 2, defaults: { levels: CIRCLE_LEVELS } },
  { id: "fibspiral", label: "Fib Spiral", group: "fib", section: "Fibonacci", icon: "꩜", points: 2 },
  { id: "fibarcs", label: "Fib Speed Resistance Arcs", group: "fib", section: "Fibonacci", icon: "◠", points: 2, defaults: { levels: CIRCLE_LEVELS.slice(0, 6) } },
  { id: "fibwedge", label: "Fib Wedge", group: "fib", section: "Fibonacci", icon: "◺", points: 3, defaults: { levels: CIRCLE_LEVELS.slice(0, 6) } },
  { id: "pitchfan", label: "Pitchfan", group: "fib", section: "Fibonacci", icon: "⋲", points: 3, defaults: { levels: FAN_LEVELS } },
  { id: "gannbox", label: "Gann Box", group: "fib", section: "Gann", icon: "▦", points: 2, defaults: { levels: FAN_LEVELS } },
  { id: "gannsqfixed", label: "Gann Square Fixed", group: "fib", section: "Gann", icon: "⊞", points: 2 },
  { id: "gannsquare", label: "Gann Square", group: "fib", section: "Gann", icon: "⊠", points: 2 },
  { id: "gannfan", label: "Gann Fan", group: "fib", section: "Gann", icon: "⟁", points: 2 },
  // ---- patterns ----
  { id: "xabcd", label: "XABCD Pattern", group: "patterns", section: "Chart patterns", icon: "Ⅹ", points: 5 },
  { id: "cypher", label: "Cypher Pattern", group: "patterns", section: "Chart patterns", icon: "Ⓒ", points: 5 },
  { id: "hs", label: "Head and Shoulders", group: "patterns", section: "Chart patterns", icon: "⌓", points: 7 },
  { id: "abcd", label: "ABCD Pattern", group: "patterns", section: "Chart patterns", icon: "Ⓐ", points: 4 },
  { id: "trianglepat", label: "Triangle Pattern", group: "patterns", section: "Chart patterns", icon: "◬", points: 4 },
  { id: "threedrives", label: "Three Drives Pattern", group: "patterns", section: "Chart patterns", icon: "③", points: 7 },
  { id: "ew_impulse", label: "Elliott Impulse Wave (12345)", group: "patterns", section: "Elliott waves", icon: "⑤", points: 6 },
  { id: "ew_correction", label: "Elliott Correction Wave (ABC)", group: "patterns", section: "Elliott waves", icon: "Ⓒ", points: 4 },
  { id: "ew_triangle", label: "Elliott Triangle Wave (ABCDE)", group: "patterns", section: "Elliott waves", icon: "Ⓔ", points: 6 },
  { id: "ew_double", label: "Elliott Double Combo Wave (WXY)", group: "patterns", section: "Elliott waves", icon: "Ⓨ", points: 4 },
  { id: "ew_triple", label: "Elliott Triple Combo Wave (WXYXZ)", group: "patterns", section: "Elliott waves", icon: "Ⓩ", points: 6 },
  { id: "cyclic", label: "Cyclic Lines", group: "patterns", section: "Cycles", icon: "⫴", points: 2 },
  { id: "timecycles", label: "Time Cycles", group: "patterns", section: "Cycles", icon: "◡", points: 2 },
  { id: "sine", label: "Sine Line", group: "patterns", section: "Cycles", icon: "∿", points: 2 },
  // ---- forecasting & measurement ----
  { id: "long", label: "Long Position", group: "forecast", section: "Projection", icon: "⬈", points: 1 },
  { id: "short", label: "Short Position", group: "forecast", section: "Projection", icon: "⬊", points: 1 },
  { id: "forecast", label: "Forecast", group: "forecast", section: "Projection", icon: "⇢", points: 2 },
  { id: "barspattern", label: "Bars Pattern", group: "forecast", section: "Projection", icon: "▥", points: 2 },
  { id: "ghostfeed", label: "Ghost Feed", group: "forecast", section: "Projection", icon: "⋰", points: "poly", hint: "click points, double-click to finish" },
  { id: "projection", label: "Projection", group: "forecast", section: "Projection", icon: "◿", points: 3 },
  { id: "avwap", label: "Anchored VWAP", group: "forecast", section: "Volume-based", icon: "⚓", points: 1 },
  { id: "frvp", label: "Fixed Range Volume Profile", group: "forecast", section: "Volume-based", icon: "▤", points: 2 },
  { id: "daterange", label: "Date Range", group: "forecast", section: "Measurer", icon: "↔", points: 2 },
  { id: "pricerange", label: "Price Range", group: "forecast", section: "Measurer", icon: "↕", points: 2 },
  { id: "dprange", label: "Date and Price Range", group: "forecast", section: "Measurer", icon: "⤡", points: 2 },
  // ---- geometric shapes: brushes, arrows, shapes ----
  { id: "brush", label: "Brush", group: "shapes", section: "Brushes", icon: "✎", points: "free", hint: "press and drag" },
  { id: "highlighter", label: "Highlighter", group: "shapes", section: "Brushes", icon: "▌", points: "free", hint: "press and drag", defaults: { color: "#FFEB3B", width: 10 } },
  { id: "arrowmarker", label: "Arrow Marker", group: "shapes", section: "Arrows", icon: "➚", points: 2 },
  { id: "arrowline", label: "Arrow", group: "shapes", section: "Arrows", icon: "➝", points: 2 },
  { id: "arrowup", label: "Arrow Mark Up", group: "shapes", section: "Arrows", icon: "⬆", points: 1 },
  { id: "arrowdown", label: "Arrow Mark Down", group: "shapes", section: "Arrows", icon: "⬇", points: 1 },
  { id: "arrowleft", label: "Arrow Mark Left", group: "shapes", section: "Arrows", icon: "⬅", points: 1 },
  { id: "arrowright", label: "Arrow Mark Right", group: "shapes", section: "Arrows", icon: "➡", points: 1 },
  { id: "rect", label: "Rectangle", group: "shapes", section: "Shapes", icon: "▭", points: 2, kbd: "Alt+Shift+R" },
  { id: "rotrect", label: "Rotated Rectangle", group: "shapes", section: "Shapes", icon: "◇", points: 3 },
  { id: "path", label: "Path", group: "shapes", section: "Shapes", icon: "↝", points: "poly", hint: "click points, double-click to finish" },
  { id: "circle", label: "Circle", group: "shapes", section: "Shapes", icon: "◯", points: 2 },
  { id: "ellipse", label: "Ellipse", group: "shapes", section: "Shapes", icon: "⬭", points: 2 },
  { id: "polyline", label: "Polyline", group: "shapes", section: "Shapes", icon: "⌇", points: "poly", hint: "click points, double-click (or click the first point) to finish" },
  { id: "triangle", label: "Triangle", group: "shapes", section: "Shapes", icon: "△", points: 3 },
  { id: "arc", label: "Arc", group: "shapes", section: "Shapes", icon: "⌒", points: 3 },
  { id: "curve", label: "Curve", group: "shapes", section: "Shapes", icon: "∽", points: 3 },
  { id: "dcurve", label: "Double Curve", group: "shapes", section: "Shapes", icon: "∾", points: 3 },
  // ---- text & notes ----
  { id: "text", label: "Text", group: "annotate", section: "Text & notes", icon: "T", points: 1, text: "Text" },
  { id: "atext", label: "Anchored Text", group: "annotate", section: "Text & notes", icon: "Ŧ", points: 1, text: "Text", screen: true },
  { id: "note", label: "Note", group: "annotate", section: "Text & notes", icon: "✍", points: 1, text: "Note" },
  { id: "anote", label: "Anchored Note", group: "annotate", section: "Text & notes", icon: "✍", points: 1, text: "Note", screen: true },
  { id: "pricenote", label: "Price Note", group: "annotate", section: "Text & notes", icon: "₹", points: 2 },
  { id: "pin", label: "Pin", group: "annotate", section: "Text & notes", icon: "📌", points: 1, text: "Pin" },
  { id: "table", label: "Table", group: "annotate", section: "Text & notes", icon: "▦", points: 1, text: "Col 1, Col 2\nA, B" },
  { id: "callout", label: "Callout", group: "annotate", section: "Text & notes", icon: "❝", points: 2, text: "Callout" },
  { id: "comment", label: "Comment", group: "annotate", section: "Text & notes", icon: "❞", points: 1, text: "Comment" },
  { id: "pricelabel", label: "Price Label", group: "annotate", section: "Text & notes", icon: "⌖", points: 1 },
  { id: "signpost", label: "Signpost", group: "annotate", section: "Text & notes", icon: "⇞", points: 1, text: "Signpost" },
  { id: "flag", label: "Flag Mark", group: "annotate", section: "Text & notes", icon: "⚑", points: 1 },
  { id: "image", label: "Image", group: "annotate", section: "Content", icon: "▣", points: 1 },
  // ---- icons / measure ----
  { id: "icon", label: "Icon", group: "icons", icon: "☺", points: 1, defaults: { fontSize: 28 } },
  { id: "measure", label: "Measure", group: "forecast", icon: "📏", points: 2, temp: true },
];

export const TOOL_BY_ID = new Map(TOOLS.map((t) => [t.id, t]));

export const TOOL_GROUPS: { id: ToolGroupId; label: string }[] = [
  { id: "lines", label: "Trend line tools" },
  { id: "fib", label: "Gann and Fibonacci tools" },
  { id: "patterns", label: "Patterns" },
  { id: "forecast", label: "Forecasting and measurement tools" },
  { id: "shapes", label: "Geometric shapes" },
  { id: "annotate", label: "Annotation tools" },
  { id: "icons", label: "Icons" },
];

const EMOJI_ROWS: [string, string][] = [
  ["Smileys & people", "😀 smile happy|😃 grin|😄 laugh|😁 beam|😆 squint laugh|😂 joy tears|🤣 rofl|😊 blush|😇 angel|🙂 slight smile|😉 wink|😍 love eyes|🤩 star struck|😎 cool sunglasses|🤔 think|🤨 raised brow|😐 neutral|😑 expressionless|🙄 eye roll|😏 smirk|😬 grimace|😮 surprised|😱 scream fear|😨 fearful|😰 anxious sweat|😥 sad relieved|😢 cry|😭 sob|😤 triumph|😡 angry|🤬 swear|🤯 mind blown|😳 flushed|🥵 hot|🥶 cold|😴 sleep|🤑 money face|🤗 hug|🤫 shush|🤥 lie|🥳 party|🤠 cowboy|🤡 clown|💀 skull rekt|👻 ghost|🤖 robot|👍 thumbs up|👎 thumbs down|👏 clap|🙌 raise hands|🙏 pray thanks|💪 strong|👀 eyes watch|🤝 deal handshake|✌️ peace|👌 ok|🤞 fingers crossed|👋 wave|☝️ point up|👇 point down|👉 point right|👈 point left|🧠 brain|🫡 salute"],
  ["Animals & nature", "🐂 bull|🐻 bear|🐳 whale|🐋 humpback whale|🦄 unicorn|🐢 turtle slow|🦅 eagle|🐍 snake|🐺 wolf|🦈 shark|🐝 bee|🦋 butterfly|🐌 snail slow|🐎 horse|🦁 lion|🐯 tiger|🐶 dog|🐱 cat|🦊 fox|🐼 panda|🐸 frog|🦍 gorilla ape|🐉 dragon|🌱 seedling grow|🌳 tree|🍀 clover luck|🌵 cactus|🌸 blossom|🌹 rose|🌞 sun|🌙 moon|⭐ star|🌟 glowing star|✨ sparkles|⚡ lightning|🔥 fire hot|🌊 wave|❄️ snow|🌈 rainbow|☔ rain|🌪️ tornado"],
  ["Objects", "🚀 rocket moon|💰 money bag|💵 dollar|💸 money wings|🪙 coin|💎 diamond|🏦 bank|📈 chart up|📉 chart down|📊 bar chart|🧾 receipt|💼 briefcase|📌 pin|📍 location pin|📎 paperclip|🔑 key|🔒 lock|🔓 unlock|🔔 bell alert|🔕 mute|⏰ alarm clock time|⏳ hourglass|⌛ time up|💡 idea bulb|🔍 search|🧲 magnet|⚙️ gear|🛠️ tools|🧯 extinguisher|💣 bomb|🎯 target|🏆 trophy|🥇 gold medal|🎉 celebration|🎁 gift|📰 news|📣 megaphone|📢 loudspeaker|📅 calendar|🗓️ schedule|☕ coffee|🍕 pizza|🍺 beer|🎲 dice gamble|🃏 joker|🎰 slot machine"],
  ["Travel & places", "✈️ plane|🛫 takeoff|🛬 landing|🚂 train|🚗 car|🏎️ race car fast|🛥️ boat|⛵ sailboat|🛰️ satellite|🌍 earth|🗽 statue liberty|🏛️ exchange building|🏭 factory|🏠 home|⛰️ mountain peak|🌋 volcano|🏝️ island|🚧 construction|🚦 traffic light|⛽ fuel oil"],
  ["Symbols", "✅ check ok|❌ cross no|❎ cross mark|⚠️ warning|❗ exclamation|❓ question|‼️ double exclamation|⛔ no entry stop|🚫 prohibited|💯 hundred|🔝 top|🆕 new|🆗 ok button|🔄 cycle repeat|🔃 reverse|♻️ recycle|➕ plus|➖ minus|✖️ multiply|➗ divide|💲 dollar sign|💹 yen chart|⬆️ up arrow|⬇️ down arrow|➡️ right arrow|⬅️ left arrow|↗️ up right|↘️ down right|↙️ down left|↖️ up left|🔼 up button|🔽 down button|⏫ fast up|⏬ fast down|🟢 green circle|🔴 red circle|🟡 yellow circle|🔵 blue circle|🟠 orange circle|🟣 purple circle|⚫ black circle|⚪ white circle|🟩 green square|🟥 red square|🟨 yellow square|🟦 blue square|🔺 red triangle up|🔻 red triangle down|🔶 orange diamond|🔷 blue diamond|♾️ infinity|☯️ yin yang"],
  ["Flags", "🇮🇳 india flag|🇺🇸 usa flag|🇬🇧 uk flag|🇯🇵 japan flag|🇨🇳 china flag|🇪🇺 eu flag|🏁 finish flag|🚩 red flag|🏳️ white flag|🏴 black flag|🎌 crossed flags"],
];

export const EMOJIS: { e: string; k: string; cat: string }[] = EMOJI_ROWS.flatMap(([cat, row]) =>
  row.split("|").map((item) => {
    const i = item.indexOf(" ");
    return { e: item.slice(0, i), k: item.slice(i + 1), cat };
  }),
);

/** Stickers tab: caption badges. */
export const STICKERS: { emoji: string; text: string; color: string }[] = [
  { emoji: "🚀", text: "TO THE MOON", color: "#7E57C2" },
  { emoji: "🐂", text: "BULLISH", color: "#089981" },
  { emoji: "🐻", text: "BEARISH", color: "#F23645" },
  { emoji: "💎", text: "HODL", color: "#2962FF" },
  { emoji: "🔥", text: "BREAKOUT", color: "#FF6D00" },
  { emoji: "📉", text: "BUY THE DIP", color: "#00897B" },
  { emoji: "😱", text: "FOMO", color: "#E91E63" },
  { emoji: "💀", text: "REKT", color: "#424242" },
  { emoji: "💰", text: "PROFIT", color: "#43A047" },
  { emoji: "⛔", text: "STOP LOSS", color: "#D32F2F" },
  { emoji: "🎯", text: "TARGET HIT", color: "#00ACC1" },
  { emoji: "⚠️", text: "CAUTION", color: "#F9A825" },
  { emoji: "🐳", text: "WHALE ALERT", color: "#1565C0" },
  { emoji: "📰", text: "NEWS", color: "#5D4037" },
  { emoji: "🧘", text: "PATIENCE", color: "#6A1B9A" },
  { emoji: "✅", text: "CONFIRMED", color: "#2E7D32" },
];

/** Icons tab: monochrome glyphs painted in the drawing's colour. */
export const GLYPHS = "★ ☆ ✓ ✗ ✔ ✘ ⚑ ⚐ ♥ ♦ ♣ ♠ ☀ ☁ ☂ ⚡ ❄ ☃ ♻ ⚠ ☢ ☣ ⚙ ⚖ ⚔ ⚓ ✈ ⌛ ⏰ ☎ ✉ ✂ ✏ ✒ ☕ ♛ ♚ ☯ ☮ ✿ ❀ ☘ ♫ ♪ ⌂ ☺ ☹ ➤ ➜ ⇧ ⇩ ⇦ ⇨ ◆ ◇ ● ○ ■ □ ▲ △ ▼ ▽ ✚ ✖ ∞ § © ® ™ $ € £ ¥ ₹ % # @ ? !".split(" ");

export function pointsNeeded(type: DrawingType): number | "free" | "poly" {
  return TOOL_BY_ID.get(type)?.points ?? 2;
}

export const BASE_STYLE: DrawingStyle = {
  color: "",
  width: 1,
  dash: 0,
  fill: true,
  fillColor: "",
  fillOpacity: 0.12,
  textColor: "",
  fontSize: 12,
  bold: false,
  italic: false,
  extendLeft: false,
  extendRight: false,
  labels: true,
};

export function styleOf(d: { type: DrawingType; style?: Partial<DrawingStyle> }, deps: Pick<DrawingDeps, "toolDefaults">): DrawingStyle {
  return {
    ...BASE_STYLE,
    ...(TOOL_BY_ID.get(d.type)?.defaults ?? {}),
    ...(deps.toolDefaults(d.type) ?? {}),
    ...(d.style ?? {}),
  };
}

let idCounter = 0;

/** Cheap unique id for a new drawing. */
export function newDrawingId(): string {
  idCounter += 1;
  return `d${Date.now().toString(36)}_${idCounter}`;
}

// ---------------------------------------------------------------------------
// Shapes
// ---------------------------------------------------------------------------

export interface XY {
  x: number;
  y: number;
}

interface Common {
  color?: string;
  width?: number;
  dash?: number[];
  alpha?: number;
}

export type Shape =
  | (Common & { k: "line"; a: XY; b: XY; arrow?: "end" | "both" })
  | (Common & { k: "poly"; pts: XY[]; closed?: boolean; fill?: string; noStroke?: boolean })
  | (Common & { k: "rect"; x: number; y: number; w: number; h: number; fill?: string; noStroke?: boolean })
  | (Common & { k: "ellipse"; cx: number; cy: number; rx: number; ry: number; start?: number; end?: number; fill?: string })
  | (Common & { k: "bezier"; a: XY; c1: XY; c2: XY; b: XY; fill?: string })
  | (Common & {
      k: "text";
      x: number;
      y: number;
      text: string;
      align?: CanvasTextAlign;
      base?: "top" | "middle" | "bottom";
      size?: number;
      bold?: boolean;
      italic?: boolean;
      bg?: string;
      border?: string;
      pad?: number;
    })
  | (Common & { k: "candle"; x: number; w: number; o: number; h: number; l: number; c: number; up: boolean })
  | (Common & { k: "image"; x: number; y: number; w: number; h: number; src: string });

const DASH: Record<number, number[]> = { 0: [], 1: [2, 3], 2: [6, 4] };
const FONT = '"JetBrains Mono", ui-monospace, monospace';
const HIT_PX = 6;
const HANDLE_PX = 8;

/** #RGB / #RRGGBB → rgba() at the given alpha (other colors pass through). */
export function rgba(hex: string, a: number): string {
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec((hex || "").trim());
  if (!m) return hex;
  let h = m[1];
  if (h.length === 3) h = h.split("").map((c) => c + c).join("");
  return `rgba(${parseInt(h.slice(0, 2), 16)},${parseInt(h.slice(2, 4), 16)},${parseInt(h.slice(4, 6), 16)},${a})`;
}

/** Readable text color on a solid background color. */
export function contrastText(bg: string): string {
  const m = /^#([0-9a-f]{6})$/i.exec((bg || "").trim());
  if (!m) return "#FFFFFF";
  const h = m[1];
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.55 ? "#131722" : "#FFFFFF";
}

function textLines(t: string): string[] {
  return t.split("\n");
}

function textBox(s: Extract<Shape, { k: "text" }>, measure?: (t: string) => number): { x: number; y: number; w: number; h: number } {
  const size = s.size ?? 11;
  const lines = textLines(s.text);
  const pad = s.pad ?? (s.bg || s.border ? 4 : 0);
  const tw = Math.max(...lines.map((l) => (measure ? measure(l) : l.length * size * 0.62)));
  const lh = size * 1.3;
  const w = tw + pad * 2;
  const h = lines.length * lh + pad * 2 - (size * 0.3);
  const align = s.align ?? "left";
  const x = align === "center" ? s.x - w / 2 : align === "right" ? s.x - w : s.x - pad;
  const base = s.base ?? "middle";
  const y = base === "top" ? s.y - pad : base === "bottom" ? s.y - h + pad : s.y - h / 2;
  return { x, y, w, h };
}

const imageCache = new Map<string, HTMLImageElement | null>();

function paintShape(
  ctx: CanvasRenderingContext2D,
  s: Shape,
  def: { color: string; width: number; dash: number[] },
  repaint: () => void,
  pane?: { w: number; h: number },
): void {
  ctx.save();
  const color = s.color ?? def.color;
  ctx.strokeStyle = color;
  ctx.fillStyle = color;
  ctx.lineWidth = s.width ?? def.width;
  ctx.setLineDash(s.dash ?? def.dash);
  if (s.alpha != null) ctx.globalAlpha = s.alpha;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  switch (s.k) {
    case "line": {
      ctx.beginPath();
      ctx.moveTo(s.a.x, s.a.y);
      ctx.lineTo(s.b.x, s.b.y);
      ctx.stroke();
      if (s.arrow) {
        ctx.setLineDash([]);
        const head = (from: XY, to: XY) => {
          const ang = Math.atan2(to.y - from.y, to.x - from.x);
          const len = 8 + ctx.lineWidth * 2;
          ctx.beginPath();
          ctx.moveTo(to.x, to.y);
          ctx.lineTo(to.x - len * Math.cos(ang - 0.4), to.y - len * Math.sin(ang - 0.4));
          ctx.lineTo(to.x - len * Math.cos(ang + 0.4), to.y - len * Math.sin(ang + 0.4));
          ctx.closePath();
          ctx.fill();
        };
        head(s.a, s.b);
        if (s.arrow === "both") head(s.b, s.a);
      }
      break;
    }
    case "poly": {
      if (s.pts.length < 2) break;
      ctx.beginPath();
      ctx.moveTo(s.pts[0].x, s.pts[0].y);
      for (let i = 1; i < s.pts.length; i++) ctx.lineTo(s.pts[i].x, s.pts[i].y);
      if (s.closed) ctx.closePath();
      if (s.fill) {
        ctx.fillStyle = s.fill;
        ctx.fill();
      }
      if (!s.noStroke) ctx.stroke();
      break;
    }
    case "rect": {
      if (s.fill) {
        ctx.fillStyle = s.fill;
        ctx.fillRect(s.x, s.y, s.w, s.h);
      }
      if (!s.noStroke) ctx.strokeRect(s.x, s.y, s.w, s.h);
      break;
    }
    case "ellipse": {
      ctx.beginPath();
      ctx.ellipse(s.cx, s.cy, Math.max(0, s.rx), Math.max(0, s.ry), 0, s.start ?? 0, s.end ?? Math.PI * 2);
      if (s.fill) {
        ctx.fillStyle = s.fill;
        ctx.fill();
      }
      ctx.stroke();
      break;
    }
    case "bezier": {
      ctx.beginPath();
      ctx.moveTo(s.a.x, s.a.y);
      ctx.bezierCurveTo(s.c1.x, s.c1.y, s.c2.x, s.c2.y, s.b.x, s.b.y);
      if (s.fill) {
        ctx.closePath();
        ctx.fillStyle = s.fill;
        ctx.fill();
        ctx.beginPath();
        ctx.moveTo(s.a.x, s.a.y);
        ctx.bezierCurveTo(s.c1.x, s.c1.y, s.c2.x, s.c2.y, s.b.x, s.b.y);
      }
      ctx.stroke();
      break;
    }
    case "text": {
      const size = s.size ?? 11;
      ctx.setLineDash([]);
      ctx.font = `${s.italic ? "italic " : ""}${s.bold ? "bold " : ""}${size}px ${FONT}`;
      const box = textBox(s, (t) => ctx.measureText(t).width);
      // Labels with a background stay inside the pane (position tool,
      // info line … near the price scale).
      if (pane && (s.bg || s.border)) {
        box.x = Math.max(2, Math.min(box.x, pane.w - box.w - 2));
        box.y = Math.max(2, Math.min(box.y, pane.h - box.h - 2));
      }
      if (s.bg) {
        ctx.fillStyle = s.bg;
        ctx.fillRect(box.x, box.y, box.w, box.h);
      }
      if (s.border) {
        ctx.strokeStyle = s.border;
        ctx.lineWidth = 1;
        ctx.strokeRect(box.x, box.y, box.w, box.h);
      }
      ctx.fillStyle = color;
      ctx.textBaseline = "top";
      ctx.textAlign = "left";
      const pad = s.pad ?? (s.bg || s.border ? 4 : 0);
      const lh = size * 1.3;
      textLines(s.text).forEach((line, i) => {
        const lw = ctx.measureText(line).width;
        const inner = box.w - pad * 2;
        const lx = (s.align ?? "left") === "center" ? box.x + pad + (inner - lw) / 2 : (s.align ?? "left") === "right" ? box.x + pad + inner - lw : box.x + pad;
        ctx.fillText(line, lx, box.y + pad + i * lh);
      });
      break;
    }
    case "candle": {
      ctx.setLineDash([]);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(s.x, s.h);
      ctx.lineTo(s.x, s.l);
      ctx.stroke();
      const top = Math.min(s.o, s.c);
      const hgt = Math.max(1, Math.abs(s.c - s.o));
      ctx.fillRect(s.x - s.w / 2, top, s.w, hgt);
      break;
    }
    case "image": {
      let img = imageCache.get(s.src);
      if (img === undefined && typeof Image !== "undefined") {
        const el = new Image();
        imageCache.set(s.src, null);
        el.onload = () => {
          imageCache.set(s.src, el);
          repaint();
        };
        el.src = s.src;
        img = null;
      }
      if (img) ctx.drawImage(img, s.x, s.y, s.w, s.h);
      else ctx.strokeRect(s.x, s.y, s.w, s.h);
      break;
    }
  }
  ctx.restore();
}

function distToSegment(px: number, py: number, a: XY, b: XY): number {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const lenSq = dx * dx + dy * dy;
  let t = lenSq > 0 ? ((px - a.x) * dx + (py - a.y) * dy) / lenSq : 0;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(px - (a.x + t * dx), py - (a.y + t * dy));
}

function insidePoly(x: number, y: number, pts: XY[]): boolean {
  let inside = false;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const a = pts[i], b = pts[j];
    if (a.y > y !== b.y > y && x < ((b.x - a.x) * (y - a.y)) / (b.y - a.y) + a.x) inside = !inside;
  }
  return inside;
}

function bezierPts(s: Extract<Shape, { k: "bezier" }>, n = 24): XY[] {
  const out: XY[] = [];
  for (let i = 0; i <= n; i++) {
    const t = i / n;
    const u = 1 - t;
    out.push({
      x: u * u * u * s.a.x + 3 * u * u * t * s.c1.x + 3 * u * t * t * s.c2.x + t * t * t * s.b.x,
      y: u * u * u * s.a.y + 3 * u * u * t * s.c1.y + 3 * u * t * t * s.c2.y + t * t * t * s.b.y,
    });
  }
  return out;
}

function hitShape(s: Shape, x: number, y: number): boolean {
  const tol = HIT_PX + ((s.width ?? 1) > 3 ? (s.width ?? 1) / 2 : 0);
  switch (s.k) {
    case "line":
      return distToSegment(x, y, s.a, s.b) <= tol;
    case "poly": {
      for (let i = 1; i < s.pts.length; i++) if (distToSegment(x, y, s.pts[i - 1], s.pts[i]) <= tol) return true;
      if (s.closed && s.pts.length > 2) {
        if (distToSegment(x, y, s.pts[s.pts.length - 1], s.pts[0]) <= tol) return true;
        if (s.fill && insidePoly(x, y, s.pts)) return true;
      }
      return false;
    }
    case "rect": {
      const x0 = Math.min(s.x, s.x + s.w), x1 = Math.max(s.x, s.x + s.w);
      const y0 = Math.min(s.y, s.y + s.h), y1 = Math.max(s.y, s.y + s.h);
      if (x < x0 - tol || x > x1 + tol || y < y0 - tol || y > y1 + tol) return false;
      if (s.fill) return true;
      return Math.abs(x - x0) <= tol || Math.abs(x - x1) <= tol || Math.abs(y - y0) <= tol || Math.abs(y - y1) <= tol;
    }
    case "ellipse": {
      if (s.rx <= 0 || s.ry <= 0) return Math.hypot(x - s.cx, y - s.cy) <= tol;
      const nx = (x - s.cx) / s.rx, ny = (y - s.cy) / s.ry;
      const r = Math.hypot(nx, ny);
      if (s.start != null || s.end != null) {
        let a = Math.atan2(ny, nx);
        const st = s.start ?? 0, en = s.end ?? Math.PI * 2;
        while (a < st) a += Math.PI * 2;
        if (a > en) return false;
      }
      if (s.fill && r <= 1) return true;
      return Math.abs(r - 1) * Math.min(s.rx, s.ry) <= tol;
    }
    case "bezier": {
      const pts = bezierPts(s);
      for (let i = 1; i < pts.length; i++) if (distToSegment(x, y, pts[i - 1], pts[i]) <= tol) return true;
      return !!s.fill && insidePoly(x, y, pts);
    }
    case "text": {
      const b = textBox(s);
      return x >= b.x - 2 && x <= b.x + b.w + 2 && y >= b.y - 2 && y <= b.y + b.h + 2;
    }
    case "candle":
      return x >= s.x - s.w / 2 - 1 && x <= s.x + s.w / 2 + 1 && y >= Math.min(s.h, s.l) && y <= Math.max(s.h, s.l);
    case "image":
      return x >= s.x && x <= s.x + s.w && y >= s.y && y <= s.y + s.h;
  }
}

// ---------------------------------------------------------------------------
// Geometry helpers
// ---------------------------------------------------------------------------

const mid = (a: XY, b: XY): XY => ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });
const add = (a: XY, b: XY): XY => ({ x: a.x + b.x, y: a.y + b.y });
const sub = (a: XY, b: XY): XY => ({ x: a.x - b.x, y: a.y - b.y });
const mul = (a: XY, k: number): XY => ({ x: a.x * k, y: a.y * k });
const lerp = (a: XY, b: XY, t: number): XY => ({ x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t });

/** Where the ray from `a` through `b` leaves the pane (beyond `b`). */
function rayEnd(a: XY, b: XY, w: number, h: number): XY {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  if (dx === 0 && dy === 0) return b;
  let t = Infinity;
  if (dx > 0) t = Math.min(t, (w + 10 - b.x) / dx);
  else if (dx < 0) t = Math.min(t, (-10 - b.x) / dx);
  if (dy > 0) t = Math.min(t, (h + 10 - b.y) / dy);
  else if (dy < 0) t = Math.min(t, (-10 - b.y) / dy);
  if (!Number.isFinite(t) || t < 0) t = 0;
  return { x: b.x + dx * t, y: b.y + dy * t };
}

function extendSeg(a: XY, b: XY, left: boolean, right: boolean, w: number, h: number): [XY, XY] {
  return [left ? rayEnd(b, a, w, h) : a, right ? rayEnd(a, b, w, h) : b];
}

/** y of the a→b line at x (in pixels). */
function yAt(a: XY, b: XY, x: number): number {
  if (b.x === a.x) return a.y;
  return a.y + ((b.y - a.y) * (x - a.x)) / (b.x - a.x);
}

function fmtVol(v: number): string {
  const a = Math.abs(v);
  if (a >= 1e7) return (v / 1e7).toFixed(2) + "Cr";
  if (a >= 1e5) return (v / 1e5).toFixed(2) + "L";
  if (a >= 1e3) return (v / 1e3).toFixed(1) + "K";
  return String(Math.round(v));
}

function seeded(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function hashStr(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619);
  return h >>> 0;
}

interface G {
  d: Drawing;
  s: DrawingStyle;
  deps: DrawingDeps;
  w: number;
  h: number;
  P: XY[];
  col: string;
  fill: string;
  tcol: string;
  sel: boolean;
}

function label(g: G, x: number, y: number, text: string, o: Partial<Extract<Shape, { k: "text" }>> = {}): Shape {
  return { k: "text", x, y, text, size: 11, color: contrastText(g.col), bg: rgba(g.col, 0.85), pad: 4, align: "center", base: "middle", ...o };
}

/** Bar range [i0, i1] (inclusive, clamped) covered by two chart times. */
function barRange(deps: DrawingDeps, t0: number, t1: number): [number, number] | null {
  const c = deps.candles();
  const l0 = deps.timeToLogical(t0);
  const l1 = deps.timeToLogical(t1);
  if (l0 === null || l1 === null || c.length === 0) return null;
  let i0 = Math.round(Math.min(l0, l1));
  let i1 = Math.round(Math.max(l0, l1));
  i0 = Math.max(0, i0);
  i1 = Math.min(c.length - 1, i1);
  return i1 >= i0 ? [i0, i1] : null;
}

function levelsOn(g: G): Level[] {
  return (g.s.levels ?? []).filter((l) => l.on);
}

function polyWithLabels(g: G, labels: string[]): Shape[] {
  const out: Shape[] = [{ k: "poly", pts: g.P }];
  g.P.forEach((p, i) => {
    const t = labels[i];
    if (!t) return;
    const prev = g.P[i - 1], next = g.P[i + 1];
    const high = (prev ? p.y <= prev.y : true) && (next ? p.y <= next.y : true);
    out.push({ k: "text", x: p.x, y: high ? p.y - 8 : p.y + 8, text: t, align: "center", base: high ? "bottom" : "top", size: 11, bold: true, color: g.tcol });
  });
  return out;
}

function ratio(a: number, b: number): string {
  return b !== 0 ? Math.abs(a / b).toFixed(3) : "—";
}

const PATTERN_LABELS: Record<string, string[]> = {
  xabcd: ["X", "A", "B", "C", "D"],
  cypher: ["X", "A", "B", "C", "D"],
  abcd: ["A", "B", "C", "D"],
  trianglepat: ["A", "B", "C", "D"],
  hs: ["", "Left Shoulder", "", "Head", "", "Right Shoulder", ""],
  threedrives: ["", "1", "A", "2", "B", "3", ""],
  ew_impulse: ["", "(1)", "(2)", "(3)", "(4)", "(5)"],
  ew_correction: ["", "(A)", "(B)", "(C)"],
  ew_triangle: ["", "(A)", "(B)", "(C)", "(D)", "(E)"],
  ew_double: ["", "(W)", "(X)", "(Y)"],
  ew_triple: ["", "(W)", "(X)", "(Y)", "(X)", "(Z)"],
};

// ---------------------------------------------------------------------------
// Geometry per tool
// ---------------------------------------------------------------------------

function textShape(g: G, x: number, y: number, o: Partial<Extract<Shape, { k: "text" }>> = {}): Shape {
  return {
    k: "text", x, y, text: g.d.text ?? "", size: g.s.fontSize, bold: g.s.bold, italic: g.s.italic, color: g.tcol,
    ...(g.s.fill && g.s.fillColor ? { bg: rgba(g.s.fillColor, g.s.fillOpacity * 4 > 1 ? 1 : Math.max(g.s.fillOpacity * 4, 0.2)) } : {}),
    ...o,
  };
}

/** Cash at risk for a position tool: a fixed amount, or a % of the account. */
export function positionRisk(data: DrawingData): number {
  if (data.riskMode === "amount" && (data.riskAmount ?? 0) > 0) return data.riskAmount as number;
  return ((data.account ?? 100000) * (data.risk ?? 1)) / 100;
}

function posGeometry(g: G, long: boolean): Shape[] {
  const [E, T, S] = g.P;
  if (!E || !T || !S) return [];
  const pE = g.d.points[0].price, pT = g.d.points[1].price, pS = g.d.points[2].price;
  const x0 = Math.min(E.x, T.x), x1 = Math.max(E.x, T.x);
  const wdt = Math.max(x1 - x0, 2);
  const up = g.deps.upColor(), down = g.deps.downColor();
  const data = g.d.data ?? {};
  const lot = Math.max(1, data.lot ?? 1);
  const riskPer = Math.abs(pE - pS);
  const cash = positionRisk(data);
  const qty = data.qty && data.qty > 0
    ? data.qty
    : riskPer > 0 ? Math.max(lot, Math.floor(cash / riskPer / lot) * lot) : lot;
  const f = g.deps.priceFormatter;
  const pct = (p: number) => ((p - pE) / pE) * 100;
  const rr = riskPer > 0 ? Math.abs(pT - pE) / riskPer : 0;
  const tick = g.deps.tickSize?.() || 0;
  const ticks = (d: number) => (tick > 0 ? ` · ${Math.round(d / tick)} ticks` : "");
  const tgtTxt = `Target: ${f(pT)} (${pct(pT).toFixed(2)}%) ${f(Math.abs(pT - pE))}${ticks(Math.abs(pT - pE))}, Amount: ${f(Math.abs(pT - pE) * qty)}`;
  const stpTxt = `Stop: ${f(pS)} (${pct(pS).toFixed(2)}%) ${f(riskPer)}${ticks(riskPer)}, Amount: ${f(riskPer * qty)}`;
  const last = g.deps.lastPrice();
  const open = last != null ? (last - pE) * qty * (long ? 1 : -1) : null;
  const midTxt = `${long ? "Long" : "Short"} · Qty: ${qty} · Risk/Reward Ratio: ${rr.toFixed(2)}${open != null ? `\nOpen P&L: ${open >= 0 ? "+" : ""}${f(open)}` : ""}`;
  const out: Shape[] = [
    { k: "rect", x: x0, y: Math.min(E.y, T.y), w: wdt, h: Math.abs(T.y - E.y), fill: rgba(up, 0.2), noStroke: true },
    { k: "rect", x: x0, y: Math.min(E.y, S.y), w: wdt, h: Math.abs(S.y - E.y), fill: rgba(down, 0.2), noStroke: true },
    { k: "line", a: { x: x0, y: E.y }, b: { x: x1, y: E.y }, color: "#9E9E9E" },
    { k: "line", a: { x: x0, y: T.y }, b: { x: x1, y: T.y }, color: up },
    { k: "line", a: { x: x0, y: S.y }, b: { x: x1, y: S.y }, color: down },
  ];
  if (g.s.labels) {
    const cx = x0 + wdt / 2;
    const tAbove = T.y < E.y;
    out.push({ k: "text", x: cx, y: T.y + (tAbove ? -4 : 4), text: tgtTxt, align: "center", base: tAbove ? "bottom" : "top", size: 10, color: "#FFFFFF", bg: rgba(up, 0.9), pad: 3 });
    const sAbove = S.y < E.y;
    out.push({ k: "text", x: cx, y: S.y + (sAbove ? -4 : 4), text: stpTxt, align: "center", base: sAbove ? "bottom" : "top", size: 10, color: "#FFFFFF", bg: rgba(down, 0.9), pad: 3 });
    out.push({ k: "text", x: cx, y: E.y, text: midTxt, align: "center", base: "middle", size: 10, color: "#FFFFFF", bg: rgba(open == null ? "#787B86" : open >= 0 ? up : down, 0.9), pad: 3 });
  }
  return out;
}

const GEO: Record<string, (g: G) => Shape[]> = {
  trend: (g) => {
    const [a, b] = extendSeg(g.P[0], g.P[1], g.s.extendLeft, g.s.extendRight, g.w, g.h);
    const out: Shape[] = [{ k: "line", a, b }];
    if (g.d.text) out.push(textShape(g, mid(g.P[0], g.P[1]).x, mid(g.P[0], g.P[1]).y - 6, { align: "center", base: "bottom" }));
    return out;
  },
  arrowline: (g) => [{ k: "line", a: g.P[0], b: g.P[1], arrow: "end" }],
  ray: (g) => [{ k: "line", a: g.P[0], b: rayEnd(g.P[0], g.P[1], g.w, g.h) }],
  extended: (g) => {
    const [a, b] = extendSeg(g.P[0], g.P[1], true, true, g.w, g.h);
    return [{ k: "line", a, b }];
  },
  info: (g) => {
    const [a, b] = g.P;
    const [p0, p1] = g.d.points;
    const dp = p1.price - p0.price;
    const l0 = g.deps.timeToLogical(p0.time), l1 = g.deps.timeToLogical(p1.time);
    const bars = l0 !== null && l1 !== null ? Math.round(l1 - l0) : 0;
    const ang = (Math.atan2(a.y - b.y, b.x - a.x) * 180) / Math.PI;
    const txt = `${dp >= 0 ? "+" : ""}${g.deps.priceFormatter(dp)} (${((dp / p0.price) * 100).toFixed(2)}%)\n${bars} bars, ${formatSpan(p1.time - p0.time)}\n∠ ${ang.toFixed(1)}°`;
    const [ea, eb] = extendSeg(a, b, g.s.extendLeft, g.s.extendRight, g.w, g.h);
    return [{ k: "line", a: ea, b: eb }, label(g, b.x + 8, b.y, txt, { align: "left", bg: rgba(g.deps.bgColor(), 0.9), color: g.col, border: g.col })];
  },
  angle: (g) => {
    const [a, b] = g.P;
    const ang = Math.atan2(a.y - b.y, b.x - a.x);
    const len = Math.max(40, Math.abs(b.x - a.x));
    const r = 28;
    return [
      { k: "line", a, b: rayEnd(a, b, g.w, g.h) },
      { k: "line", a, b: { x: a.x + len, y: a.y }, dash: DASH[2] },
      { k: "ellipse", cx: a.x, cy: a.y, rx: r, ry: r, start: Math.min(0, -ang), end: Math.max(0, -ang) },
      { k: "text", x: a.x + r + 6, y: a.y + (ang > 0 ? -10 : 10), text: `${((ang * 180) / Math.PI).toFixed(1)}°`, size: 11, color: g.tcol, base: "middle" },
    ];
  },
  hline: (g) => {
    const y = g.P[0].y;
    const out: Shape[] = [{ k: "line", a: { x: 0, y }, b: { x: g.w, y } }];
    if (g.s.labels) out.push(label(g, g.w - 4, y, g.deps.priceFormatter(g.d.points[0].price), { align: "right" }));
    if (g.d.text) out.push(textShape(g, 8, y - 4, { base: "bottom" }));
    return out;
  },
  hray: (g) => {
    const a = g.P[0];
    const out: Shape[] = [{ k: "line", a, b: { x: g.w, y: a.y } }];
    if (g.s.labels) out.push(label(g, g.w - 4, a.y, g.deps.priceFormatter(g.d.points[0].price), { align: "right" }));
    return out;
  },
  vline: (g) => {
    const x = g.P[0].x;
    const out: Shape[] = [{ k: "line", a: { x, y: 0 }, b: { x, y: g.h } }];
    if (g.d.text) out.push(textShape(g, x + 4, 12, { base: "top" }));
    return out;
  },
  cross: (g) => {
    const { x, y } = g.P[0];
    return [
      { k: "line", a: { x: 0, y }, b: { x: g.w, y } },
      { k: "line", a: { x, y: 0 }, b: { x, y: g.h } },
      ...(g.s.labels ? [label(g, g.w - 4, y, g.deps.priceFormatter(g.d.points[0].price), { align: "right" })] : []),
    ];
  },
  channel: (g) => {
    const [a, b, c] = g.P;
    if (!c) return [{ k: "line", a, b }];
    const dy = c.y - yAt(a, b, c.x);
    const [a1, b1] = extendSeg(a, b, g.s.extendLeft, g.s.extendRight, g.w, g.h);
    const a2 = { x: a1.x, y: a1.y + dy }, b2 = { x: b1.x, y: b1.y + dy };
    const out: Shape[] = [
      { k: "poly", pts: [a1, b1, b2, a2], closed: true, fill: g.s.fill ? g.fill : undefined, noStroke: true },
      { k: "line", a: a1, b: b1 },
      { k: "line", a: a2, b: b2 },
      { k: "line", a: mid(a1, a2), b: mid(b1, b2), dash: DASH[2], alpha: 0.7 },
    ];
    return out;
  },
  regression: (g) => {
    const [t0, t1] = [g.d.points[0].time, g.d.points[1].time];
    const r = barRange(g.deps, t0, t1);
    if (!r) return [{ k: "line", a: g.P[0], b: g.P[1] }];
    const c = g.deps.candles();
    const fit = linearRegression(c.slice(r[0], r[1] + 1).map((k) => k.close));
    const x0 = g.deps.timeToX(c[r[0]].time), x1raw = g.deps.timeToX(c[r[1]].time);
    if (!fit || x0 === null || x1raw === null) return [];
    const slope = r[1] > r[0] ? (fit.end - fit.start) / (r[1] - r[0]) : 0;
    const bs = Math.max(g.deps.barSpacing(), 0.0001);
    const extraBars = g.s.extendRight ? (g.w - x1raw) / bs : 0;
    const x1 = x1raw + extraBars * bs;
    const endP = fit.end + slope * extraBars;
    const Y = (p: number) => g.deps.priceToY(p);
    const ys = [Y(fit.start), Y(endP), Y(fit.start + 2 * fit.dev), Y(endP + 2 * fit.dev), Y(fit.start - 2 * fit.dev), Y(endP - 2 * fit.dev)];
    if (ys.some((v) => v === null)) return [];
    const [m0, m1, u0, u1, l0, l1] = ys as number[];
    return [
      { k: "poly", pts: [{ x: x0, y: u0 }, { x: x1, y: u1 }, { x: x1, y: l1 }, { x: x0, y: l0 }], closed: true, fill: g.s.fill ? g.fill : undefined, noStroke: true },
      { k: "line", a: { x: x0, y: m0 }, b: { x: x1, y: m1 }, dash: DASH[2] },
      { k: "line", a: { x: x0, y: u0 }, b: { x: x1, y: u1 } },
      { k: "line", a: { x: x0, y: l0 }, b: { x: x1, y: l1 } },
    ];
  },
  flattop: (g) => {
    const [a, b, c] = g.P;
    if (!c) return [{ k: "line", a, b }];
    const [a1, b1] = extendSeg(a, b, g.s.extendLeft, g.s.extendRight, g.w, g.h);
    return [
      { k: "poly", pts: [a1, b1, { x: b1.x, y: c.y }, { x: a1.x, y: c.y }], closed: true, fill: g.s.fill ? g.fill : undefined, noStroke: true },
      { k: "line", a: a1, b: b1 },
      { k: "line", a: { x: a1.x, y: c.y }, b: { x: b1.x, y: c.y } },
    ];
  },
  disjoint: (g) => {
    const [a, b, c] = g.P;
    if (!c) return [{ k: "line", a, b }];
    const a2 = { x: a.x, y: c.y };
    const b2 = { x: b.x, y: b.y - (c.y - a.y) };
    return [
      { k: "poly", pts: [a, b, b2, a2], closed: true, fill: g.s.fill ? g.fill : undefined, noStroke: true },
      { k: "line", a, b },
      { k: "line", a: a2, b: b2 },
    ];
  },
  pitchfork: (g) => forkGeo(g, "andrews"),
  schiff: (g) => forkGeo(g, "schiff"),
  mschiff: (g) => forkGeo(g, "mschiff"),
  ipitchfork: (g) => forkGeo(g, "inside"),
  fib: (g) => {
    const [a, b] = g.P;
    const [p0, p1] = g.d.points;
    const xL = Math.min(a.x, b.x), xR = Math.max(a.x, b.x);
    const [L, R] = [g.s.extendLeft ? 0 : xL, g.s.extendRight ? g.w : xR];
    const out: Shape[] = [{ k: "line", a, b, dash: DASH[2], alpha: 0.6 }];
    let prev: { y: number } | null = null;
    for (const l of levelsOn(g)) {
      const price = p1.price + (p0.price - p1.price) * l.v;
      const y = g.deps.priceToY(price);
      if (y === null) continue;
      if (prev && g.s.fill) out.push({ k: "rect", x: L, y: Math.min(prev.y, y), w: R - L, h: Math.abs(y - prev.y), fill: rgba(l.color, g.s.fillOpacity), noStroke: true });
      out.push({ k: "line", a: { x: L, y }, b: { x: R, y }, color: l.color });
      if (g.s.labels) out.push({ k: "text", x: L + 2, y: y - 2, text: `${l.v} (${g.deps.priceFormatter(price)})`, size: 10, color: l.color, base: "bottom" });
      prev = { y };
    }
    return out;
  },
  fibext: (g) => {
    const [a, b, c] = g.P;
    if (!c) return [{ k: "line", a, b, dash: DASH[2] }];
    const [p0, p1, p2] = g.d.points;
    const L = g.s.extendLeft ? 0 : c.x;
    const R = g.s.extendRight ? g.w : c.x + Math.max(60, Math.abs(b.x - a.x));
    const out: Shape[] = [{ k: "poly", pts: [a, b, c], dash: DASH[2], alpha: 0.6 }];
    let prevY: number | null = null;
    for (const l of levelsOn(g)) {
      const price = p2.price + (p1.price - p0.price) * l.v;
      const y = g.deps.priceToY(price);
      if (y === null) continue;
      if (prevY !== null && g.s.fill) out.push({ k: "rect", x: L, y: Math.min(prevY, y), w: R - L, h: Math.abs(y - prevY), fill: rgba(l.color, g.s.fillOpacity), noStroke: true });
      out.push({ k: "line", a: { x: L, y }, b: { x: R, y }, color: l.color });
      if (g.s.labels) out.push({ k: "text", x: L + 2, y: y - 2, text: `${l.v} (${g.deps.priceFormatter(price)})`, size: 10, color: l.color, base: "bottom" });
      prevY = y;
    }
    return out;
  },
  fibchannel: (g) => {
    const [a, b, c] = g.P;
    if (!c) return [{ k: "line", a, b }];
    const dy = c.y - yAt(a, b, c.x);
    const out: Shape[] = [];
    let prev: [XY, XY] | null = null;
    for (const l of levelsOn(g)) {
      const [s0, s1] = extendSeg({ x: a.x, y: a.y + dy * l.v }, { x: b.x, y: b.y + dy * l.v }, g.s.extendLeft, g.s.extendRight, g.w, g.h);
      if (prev && g.s.fill) out.push({ k: "poly", pts: [prev[0], prev[1], s1, s0], closed: true, fill: rgba(l.color, g.s.fillOpacity), noStroke: true });
      out.push({ k: "line", a: s0, b: s1, color: l.color });
      if (g.s.labels) out.push({ k: "text", x: s0.x + 2, y: s0.y - 2, text: String(l.v), size: 10, color: l.color, base: "bottom" });
      prev = [s0, s1];
    }
    return out;
  },
  fibtime: (g) => {
    const [a, b] = g.P;
    const dx = b.x - a.x;
    const out: Shape[] = [{ k: "line", a, b, dash: DASH[2], alpha: 0.5 }];
    for (const l of levelsOn(g)) {
      const x = a.x + dx * l.v;
      if (x < -10 || x > g.w + 10) continue;
      out.push({ k: "line", a: { x, y: 0 }, b: { x, y: g.h }, color: l.color });
      if (g.s.labels) out.push({ k: "text", x: x + 3, y: 14, text: String(l.v), size: 10, color: l.color, base: "top" });
    }
    return out;
  },
  fibfan: (g) => {
    const [a, b] = g.P;
    const out: Shape[] = [{ k: "rect", x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), w: Math.abs(b.x - a.x), h: Math.abs(b.y - a.y), dash: DASH[1], alpha: 0.4 }];
    for (const l of levelsOn(g)) {
      const pp = { x: b.x, y: a.y + (b.y - a.y) * l.v };
      const pt = { x: a.x + (b.x - a.x) * l.v, y: b.y };
      if (l.v > 0) out.push({ k: "line", a, b: rayEnd(a, pp, g.w, g.h), color: l.color });
      if (l.v > 0 && l.v < 1) out.push({ k: "line", a, b: rayEnd(a, pt, g.w, g.h), color: l.color });
      if (g.s.labels) out.push({ k: "text", x: b.x + 3, y: pp.y, text: String(l.v), size: 10, color: l.color, base: "middle" });
    }
    return out;
  },
  fibtrendtime: (g) => {
    const [a, b, c] = g.P;
    if (!c) return [{ k: "line", a, b, dash: DASH[2] }];
    const dx = b.x - a.x;
    const out: Shape[] = [{ k: "poly", pts: [a, b, c], dash: DASH[2], alpha: 0.6 }];
    for (const l of levelsOn(g)) {
      const x = c.x + dx * l.v;
      if (x < -10 || x > g.w + 10) continue;
      out.push({ k: "line", a: { x, y: 0 }, b: { x, y: g.h }, color: l.color });
      if (g.s.labels) out.push({ k: "text", x: x + 3, y: 14, text: String(l.v), size: 10, color: l.color, base: "top" });
    }
    return out;
  },
  fibcircles: (g) => {
    const [a, b] = g.P;
    const r = Math.hypot(b.x - a.x, b.y - a.y);
    const out: Shape[] = [{ k: "line", a, b, dash: DASH[2], alpha: 0.6 }];
    for (const l of levelsOn(g)) {
      out.push({ k: "ellipse", cx: a.x, cy: a.y, rx: r * l.v, ry: r * l.v, color: l.color });
      if (g.s.labels) out.push({ k: "text", x: a.x + r * l.v + 2, y: a.y, text: String(l.v), size: 10, color: l.color, base: "middle" });
    }
    return out;
  },
  fibspiral: (g) => {
    const [a, b] = g.P;
    const r0 = Math.hypot(b.x - a.x, b.y - a.y);
    const th0 = Math.atan2(b.y - a.y, b.x - a.x);
    const k = Math.log((1 + Math.sqrt(5)) / 2) / (Math.PI / 2);
    const pts: XY[] = [];
    for (let th = th0 - 8 * Math.PI; th <= th0 + 1.5 * Math.PI; th += Math.PI / 36) {
      const r = r0 * Math.exp(k * (th - th0));
      if (r > 4 * Math.max(g.w, g.h)) break;
      pts.push({ x: a.x + r * Math.cos(th), y: a.y + r * Math.sin(th) });
    }
    return [{ k: "line", a, b, dash: DASH[2], alpha: 0.6 }, { k: "poly", pts }];
  },
  fibarcs: (g) => {
    const [a, b] = g.P;
    const r = Math.hypot(b.x - a.x, b.y - a.y);
    const phi = Math.atan2(b.y - a.y, b.x - a.x);
    const out: Shape[] = [{ k: "line", a, b, dash: DASH[2], alpha: 0.6 }];
    for (const l of levelsOn(g)) {
      out.push({ k: "ellipse", cx: a.x, cy: a.y, rx: r * l.v, ry: r * l.v, start: phi - Math.PI / 2, end: phi + Math.PI / 2, color: l.color });
      if (g.s.labels) out.push({ k: "text", x: a.x + Math.cos(phi) * r * l.v, y: a.y + Math.sin(phi) * r * l.v, text: String(l.v), size: 10, color: l.color, base: "bottom" });
    }
    return out;
  },
  fibwedge: (g) => {
    const [a, b, c] = g.P;
    if (!c) return [{ k: "line", a, b }];
    const r = Math.hypot(b.x - a.x, b.y - a.y);
    let p1 = Math.atan2(b.y - a.y, b.x - a.x);
    let p2 = Math.atan2(c.y - a.y, c.x - a.x);
    if (p2 < p1) [p1, p2] = [p2, p1];
    if (p2 - p1 > Math.PI) [p1, p2] = [p2, p1 + Math.PI * 2];
    const out: Shape[] = [
      { k: "line", a, b: { x: a.x + Math.cos(p1) * r, y: a.y + Math.sin(p1) * r } },
      { k: "line", a, b: { x: a.x + Math.cos(p2) * r, y: a.y + Math.sin(p2) * r } },
    ];
    for (const l of levelsOn(g)) out.push({ k: "ellipse", cx: a.x, cy: a.y, rx: r * l.v, ry: r * l.v, start: p1, end: p2, color: l.color });
    return out;
  },
  pitchfan: (g) => {
    const [a, b, c] = g.P;
    if (!c) return [{ k: "line", a, b }];
    const out: Shape[] = [{ k: "line", a: b, b: c }];
    for (const l of levelsOn(g)) {
      const q = lerp(b, c, l.v);
      out.push({ k: "line", a, b: rayEnd(a, q, g.w, g.h), color: l.color });
    }
    return out;
  },
  gannbox: (g) => {
    const [a, b] = g.P;
    const out: Shape[] = [{ k: "rect", x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), w: Math.abs(b.x - a.x), h: Math.abs(b.y - a.y), fill: g.s.fill ? rgba(g.col, g.s.fillOpacity / 2) : undefined }];
    for (const l of levelsOn(g)) {
      const x = a.x + (b.x - a.x) * l.v;
      const y = a.y + (b.y - a.y) * l.v;
      out.push({ k: "line", a: { x, y: a.y }, b: { x, y: b.y }, color: l.color });
      out.push({ k: "line", a: { x: a.x, y }, b: { x: b.x, y }, color: l.color });
      if (g.s.labels) {
        out.push({ k: "text", x, y: Math.min(a.y, b.y) - 3, text: String(l.v), size: 9, color: l.color, align: "center", base: "bottom" });
        out.push({ k: "text", x: Math.min(a.x, b.x) - 3, y, text: String(l.v), size: 9, color: l.color, align: "right", base: "middle" });
      }
    }
    return out;
  },
  gannsquare: (g) => gannSquareGeo(g, g.P[0], g.P[1]),
  gannsqfixed: (g) => {
    const [a, b] = g.P;
    const side = Math.abs(b.x - a.x);
    return gannSquareGeo(g, a, { x: b.x, y: a.y + side * (b.y >= a.y ? 1 : -1) });
  },
  gannfan: (g) => {
    const [a, b] = g.P;
    const v = sub(b, a);
    const ratios: [number, number, string][] = [[1, 8, "1/8"], [1, 4, "1/4"], [1, 3, "1/3"], [1, 2, "1/2"], [1, 1, "1/1"], [2, 1, "2/1"], [3, 1, "3/1"], [4, 1, "4/1"], [8, 1, "8/1"]];
    const colors = ["#FF9800", "#4CAF50", "#089981", "#00BCD4", "#787B86", "#2962FF", "#9C27B0", "#E91E63", "#F23645"];
    const out: Shape[] = [];
    ratios.forEach(([t, p, txt], i) => {
      const q = { x: a.x + v.x * t, y: a.y + v.y * p };
      const e = rayEnd(a, q, g.w, g.h);
      out.push({ k: "line", a, b: e, color: colors[i] });
      if (g.s.labels) out.push({ k: "text", x: a.x + (q.x - a.x) * 0.9, y: a.y + (q.y - a.y) * 0.9, text: txt, size: 9, color: colors[i], base: "bottom" });
    });
    return out;
  },
  xabcd: (g) => harmonicGeo(g),
  cypher: (g) => harmonicGeo(g),
  abcd: (g) => {
    const out = polyWithLabels(g, PATTERN_LABELS.abcd);
    const [A, B, C, D] = g.P;
    const pr = g.d.points.map((p) => p.price);
    if (C) out.push({ k: "line", a: A, b: C, dash: DASH[2], alpha: 0.6 }, { k: "text", x: mid(A, C).x, y: mid(A, C).y, text: ratio(pr[2] - pr[1], pr[1] - pr[0]), size: 10, color: g.tcol, align: "center" });
    if (D) out.push({ k: "line", a: B, b: D, dash: DASH[2], alpha: 0.6 }, { k: "text", x: mid(B, D).x, y: mid(B, D).y, text: ratio(pr[3] - pr[2], pr[2] - pr[1]), size: 10, color: g.tcol, align: "center" });
    return out;
  },
  hs: (g) => {
    const out: Shape[] = [];
    if (g.P.length === 7 && g.s.fill) out.push({ k: "poly", pts: g.P, closed: true, fill: g.fill, noStroke: true });
    out.push(...polyWithLabels(g, PATTERN_LABELS.hs));
    if (g.P[4]) {
      const [n0, n1] = extendSeg(g.P[2], g.P[4], false, true, g.w, g.h);
      out.push({ k: "line", a: n0, b: n1, dash: DASH[2] });
    }
    return out;
  },
  trianglepat: (g) => {
    const out: Shape[] = [];
    if (g.P.length === 4 && g.s.fill) out.push({ k: "poly", pts: g.P, closed: true, fill: g.fill, noStroke: true });
    out.push(...polyWithLabels(g, PATTERN_LABELS.trianglepat));
    if (g.P[2]) out.push({ k: "line", a: g.P[0], b: g.P[2], dash: DASH[2], alpha: 0.6 });
    if (g.P[3]) out.push({ k: "line", a: g.P[1], b: g.P[3], dash: DASH[2], alpha: 0.6 });
    return out;
  },
  threedrives: (g) => polyWithLabels(g, PATTERN_LABELS.threedrives),
  ew_impulse: (g) => polyWithLabels(g, PATTERN_LABELS.ew_impulse),
  ew_correction: (g) => polyWithLabels(g, PATTERN_LABELS.ew_correction),
  ew_triangle: (g) => polyWithLabels(g, PATTERN_LABELS.ew_triangle),
  ew_double: (g) => polyWithLabels(g, PATTERN_LABELS.ew_double),
  ew_triple: (g) => polyWithLabels(g, PATTERN_LABELS.ew_triple),
  cyclic: (g) => {
    const [a, b] = g.P;
    const dx = b.x - a.x;
    const out: Shape[] = [];
    if (Math.abs(dx) < 3) return [{ k: "line", a: { x: a.x, y: 0 }, b: { x: a.x, y: g.h } }];
    for (let k = 0, x = a.x; k < 500 && x > -20 && x < g.w + 20; k++, x = a.x + k * dx) out.push({ k: "line", a: { x, y: 0 }, b: { x, y: g.h } });
    return out;
  },
  timecycles: (g) => {
    const [a, b] = g.P;
    const dx = b.x - a.x;
    if (Math.abs(dx) < 3) return [];
    const r = Math.abs(dx) / 2;
    const out: Shape[] = [];
    for (let k = 0; k < 300; k++) {
      const cx = a.x + (k + 0.5) * dx;
      if (cx - r > g.w + 10 || cx + r < -10) break;
      out.push({ k: "ellipse", cx, cy: a.y, rx: r, ry: r, start: Math.PI, end: Math.PI * 2 });
    }
    return out;
  },
  sine: (g) => {
    const [a, b] = g.P;
    const dx = b.x - a.x;
    if (Math.abs(dx) < 2) return [{ k: "line", a, b }];
    const m = (a.y + b.y) / 2;
    const amp = (a.y - b.y) / 2;
    const pts: XY[] = [];
    for (let x = 0; x <= g.w; x += 3) pts.push({ x, y: m + amp * Math.cos((Math.PI * (x - a.x)) / dx) });
    return [{ k: "poly", pts }];
  },
  long: (g) => posGeometry(g, true),
  short: (g) => posGeometry(g, false),
  forecast: (g) => {
    const [a, b] = g.P;
    const [p0, p1] = g.d.points;
    const dp = p1.price - p0.price;
    const l0 = g.deps.timeToLogical(p0.time), l1 = g.deps.timeToLogical(p1.time);
    const bars = l0 !== null && l1 !== null ? Math.round(l1 - l0) : 0;
    const col = dp >= 0 ? g.deps.upColor() : g.deps.downColor();
    return [
      { k: "line", a, b, arrow: "end", color: col, width: 2 },
      { k: "ellipse", cx: a.x, cy: a.y, rx: 3, ry: 3, fill: col, color: col },
      label(g, b.x, b.y + (dp >= 0 ? -16 : 16), `${dp >= 0 ? "+" : ""}${g.deps.priceFormatter(dp)} (${((dp / p0.price) * 100).toFixed(2)}%) · ${bars} bars`, { bg: rgba(col, 0.9) }),
    ];
  },
  daterange: (g) => rangeGeo(g, true, false, false),
  pricerange: (g) => rangeGeo(g, false, true, false),
  dprange: (g) => rangeGeo(g, true, true, false),
  measure: (g) => rangeGeo(g, true, true, true),
  barspattern: (g) => {
    const [a, b] = g.P;
    const c3 = g.P[2];
    const r = barRange(g.deps, g.d.points[0].time, g.d.points[1].time);
    const out: Shape[] = [
      { k: "line", a: { x: a.x, y: a.y - 20 }, b: { x: a.x, y: a.y + 20 }, dash: DASH[1] },
      { k: "line", a: { x: b.x, y: b.y - 20 }, b: { x: b.x, y: b.y + 20 }, dash: DASH[1] },
    ];
    if (!r || !c3) return out;
    const c = g.deps.candles();
    const src = c.slice(r[0], r[1] + 1);
    const shift = g.d.points[2].price - src[0].open;
    const bs = g.deps.barSpacing();
    const wdt = Math.max(1, bs * 0.6);
    src.forEach((k, i) => {
      const Y = (p: number) => g.deps.priceToY(p + shift);
      const o = Y(k.open), hh = Y(k.high), ll = Y(k.low), cc = Y(k.close);
      if (o === null || hh === null || ll === null || cc === null) return;
      out.push({ k: "candle", x: c3.x + i * bs, w: wdt, o, h: hh, l: ll, c: cc, up: k.close >= k.open, color: g.col, alpha: k.close >= k.open ? 0.35 : 0.7 });
    });
    return out;
  },
  ghostfeed: (g) => {
    const out: Shape[] = [];
    const pts = g.d.points;
    if (g.P.length < 2) return g.P.length ? [{ k: "ellipse", cx: g.P[0].x, cy: g.P[0].y, rx: 3, ry: 3, fill: g.col }] : [];
    const bs = Math.max(g.deps.barSpacing(), 2);
    const rnd = seeded(hashStr(g.d.id));
    const wdt = Math.max(1, bs * 0.6);
    let prevClose = pts[0].price;
    for (let i = 1; i < g.P.length; i++) {
      const a = g.P[i - 1], b = g.P[i];
      const n = Math.max(1, Math.round(Math.abs(b.x - a.x) / bs));
      const dp = (pts[i].price - pts[i - 1].price) / n;
      for (let k = 1; k <= n; k++) {
        const target = pts[i - 1].price + dp * k;
        const noise = (rnd() - 0.5) * Math.abs(dp) * 1.6;
        const open = prevClose;
        const close = k === n ? pts[i].price : target + noise;
        const wick = Math.abs(dp) * 0.6 + Math.abs(close - open) * 0.3;
        const hi = Math.max(open, close) + rnd() * wick;
        const lo = Math.min(open, close) - rnd() * wick;
        const Y = g.deps.priceToY;
        const o = Y(open), h = Y(hi), l = Y(lo), c = Y(close);
        prevClose = close;
        if (o === null || h === null || l === null || c === null) continue;
        const up = close >= open;
        out.push({ k: "candle", x: a.x + ((b.x - a.x) * k) / n, w: wdt, o, h, l, c, up, color: up ? g.deps.upColor() : g.deps.downColor(), alpha: 0.55 });
      }
    }
    if (g.sel) out.push({ k: "poly", pts: g.P, dash: DASH[1], alpha: 0.5 });
    return out;
  },
  projection: (g) => {
    const [a, b, c] = g.P;
    if (!c) return [{ k: "line", a, b }];
    const [p0, p1, p2] = g.d.points;
    const d = add(c, sub(b, a));
    const target = p2.price + (p1.price - p0.price);
    const pct = ((target - p2.price) / p2.price) * 100;
    return [
      { k: "poly", pts: [a, b, c], closed: true, fill: g.s.fill ? g.fill : undefined },
      { k: "line", a: c, b: d, dash: DASH[2], arrow: "end" },
      label(g, d.x, d.y + (target >= p2.price ? -14 : 14), `${g.deps.priceFormatter(target)} (${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%)`),
    ];
  },
  avwap: (g) => {
    const c = g.deps.candles();
    const l0 = g.deps.timeToLogical(g.d.points[0].time);
    if (l0 === null || c.length === 0) return [];
    const i0 = Math.max(0, Math.min(c.length - 1, Math.round(l0)));
    let pv = 0, v = 0;
    const pts: XY[] = [];
    for (let i = i0; i < c.length; i++) {
      const k = c[i];
      const tp = (k.high + k.low + k.close) / 3;
      pv += tp * (k.volume || 0);
      v += k.volume || 0;
      const val = v > 0 ? pv / v : tp;
      const x = g.deps.timeToX(k.time), y = g.deps.priceToY(val);
      if (x !== null && y !== null) pts.push({ x, y });
    }
    const out: Shape[] = [{ k: "poly", pts, width: Math.max(2, g.s.width) }];
    if (pts.length && g.s.labels) out.push({ k: "text", x: pts[pts.length - 1].x + 4, y: pts[pts.length - 1].y, text: "VWAP", size: 10, color: g.col, base: "middle" });
    if (g.P[0]) out.push({ k: "line", a: { x: g.P[0].x, y: g.P[0].y - 12 }, b: { x: g.P[0].x, y: g.P[0].y + 12 }, dash: DASH[1] });
    return out;
  },
  frvp: (g) => {
    const [a, b] = g.P;
    const r = barRange(g.deps, g.d.points[0].time, g.d.points[1].time);
    const x0 = Math.min(a.x, b.x), x1 = Math.max(a.x, b.x);
    const out: Shape[] = [
      { k: "line", a: { x: x0, y: 0 }, b: { x: x0, y: g.h }, dash: DASH[1], alpha: 0.5 },
      { k: "line", a: { x: x1, y: 0 }, b: { x: x1, y: g.h }, dash: DASH[1], alpha: 0.5 },
    ];
    if (!r) return out;
    const c = g.deps.candles().slice(r[0], r[1] + 1);
    let lo = Infinity, hi = -Infinity;
    for (const k of c) { if (k.low < lo) lo = k.low; if (k.high > hi) hi = k.high; }
    if (!(hi > lo)) return out;
    const rows = 24;
    const step = (hi - lo) / rows;
    const vol = new Array<number>(rows).fill(0);
    for (const k of c) {
      const r0 = Math.max(0, Math.min(rows - 1, Math.floor((k.low - lo) / step)));
      const r1 = Math.max(0, Math.min(rows - 1, Math.floor((k.high - lo) / step)));
      const share = (k.volume || 0) / (r1 - r0 + 1);
      for (let i = r0; i <= r1; i++) vol[i] += share;
    }
    const total = vol.reduce((x, y) => x + y, 0);
    let poc = 0;
    vol.forEach((v, i) => { if (v > vol[poc]) poc = i; });
    const inVA = new Set<number>([poc]);
    let acc = vol[poc], lo2 = poc, hi2 = poc;
    while (acc < total * 0.7 && (lo2 > 0 || hi2 < rows - 1)) {
      const dn = lo2 > 0 ? vol[lo2 - 1] : -1, upv = hi2 < rows - 1 ? vol[hi2 + 1] : -1;
      if (upv >= dn) { hi2++; acc += vol[hi2]; inVA.add(hi2); } else { lo2--; acc += vol[lo2]; inVA.add(lo2); }
    }
    const maxV = Math.max(...vol, 1);
    const maxW = Math.max(40, (x1 - x0) * 0.4);
    for (let i = 0; i < rows; i++) {
      const yTop = g.deps.priceToY(lo + step * (i + 1)), yBot = g.deps.priceToY(lo + step * i);
      if (yTop === null || yBot === null) continue;
      out.push({ k: "rect", x: x0, y: yTop + 0.5, w: (vol[i] / maxV) * maxW, h: Math.max(1, yBot - yTop - 1), fill: rgba(inVA.has(i) ? "#2962FF" : "#787B86", inVA.has(i) ? 0.45 : 0.25), noStroke: true });
    }
    const yp = g.deps.priceToY(lo + step * (poc + 0.5));
    if (yp !== null) out.push({ k: "line", a: { x: x0, y: yp }, b: { x: x1, y: yp }, color: "#F23645", width: 2 });
    return out;
  },
  brush: (g) => [{ k: "poly", pts: g.P }],
  highlighter: (g) => [{ k: "poly", pts: g.P, alpha: 0.35, width: Math.max(6, g.s.width) }],
  rect: (g) => {
    const [a, b] = g.P;
    const out: Shape[] = [{ k: "rect", x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), w: Math.abs(b.x - a.x), h: Math.abs(b.y - a.y), fill: g.s.fill ? g.fill : undefined }];
    if (g.d.text) out.push(textShape(g, mid(a, b).x, mid(a, b).y, { align: "center" }));
    return out;
  },
  rotrect: (g) => {
    const [a, b, c] = g.P;
    if (!c) return [{ k: "line", a, b }];
    const u = sub(b, a);
    const len = Math.hypot(u.x, u.y) || 1;
    const n = { x: -u.y / len, y: u.x / len };
    const hgt = (c.x - b.x) * n.x + (c.y - b.y) * n.y;
    return [{ k: "poly", pts: [a, b, add(b, mul(n, hgt)), add(a, mul(n, hgt))], closed: true, fill: g.s.fill ? g.fill : undefined }];
  },
  path: (g) => {
    const out: Shape[] = [{ k: "poly", pts: g.P }];
    const n = g.P.length;
    if (n >= 2) out.push({ k: "line", a: g.P[n - 2], b: g.P[n - 1], arrow: "end" });
    return out;
  },
  circle: (g) => {
    const [a, b] = g.P;
    const r = Math.hypot(b.x - a.x, b.y - a.y);
    return [{ k: "ellipse", cx: a.x, cy: a.y, rx: r, ry: r, fill: g.s.fill ? g.fill : undefined }];
  },
  ellipse: (g) => {
    const [a, b] = g.P;
    return [{ k: "ellipse", cx: (a.x + b.x) / 2, cy: (a.y + b.y) / 2, rx: Math.abs(b.x - a.x) / 2, ry: Math.abs(b.y - a.y) / 2, fill: g.s.fill ? g.fill : undefined }];
  },
  triangle: (g) => [{ k: "poly", pts: g.P, closed: g.P.length === 3, fill: g.s.fill && g.P.length === 3 ? g.fill : undefined }],
  polyline: (g) => {
    const n = g.P.length;
    const closed = n >= 3 && Math.hypot(g.P[0].x - g.P[n - 1].x, g.P[0].y - g.P[n - 1].y) < 8;
    return [{ k: "poly", pts: closed ? g.P.slice(0, -1) : g.P, closed, fill: closed && g.s.fill ? g.fill : undefined }];
  },
  arc: (g) => {
    const [a, b, c] = g.P;
    if (!c) return [{ k: "line", a, b }];
    const q = sub(mul(c, 2), mid(a, b));
    return [{ k: "bezier", a, c1: lerp(a, q, 2 / 3), c2: lerp(b, q, 2 / 3), b, fill: g.s.fill ? g.fill : undefined }];
  },
  curve: (g) => {
    const [a, b, c] = g.P;
    if (!c) return [{ k: "line", a, b }];
    return [{ k: "bezier", a, c1: lerp(a, c, 2 / 3), c2: lerp(b, c, 2 / 3), b }];
  },
  dcurve: (g) => {
    const [a, b, c] = g.P;
    if (!c) return [{ k: "line", a, b }];
    return [{ k: "bezier", a, c1: c, c2: sub(mul(mid(a, b), 2), c), b }];
  },
  text: (g) => [textShape(g, g.P[0].x, g.P[0].y, { base: "middle" })],
  atext: (g) => [textShape(g, g.P[0].x, g.P[0].y, { base: "middle" })],
  note: (g) => noteGeo(g),
  pin: (g) => {
    const a = g.P[0];
    const head = { x: a.x, y: a.y - 22 };
    const out: Shape[] = [
      { k: "line", a, b: { x: a.x, y: a.y - 14 }, width: 2, color: "#9E9E9E" },
      { k: "ellipse", cx: head.x, cy: head.y, rx: 8, ry: 8, fill: g.col },
      { k: "ellipse", cx: head.x - 2, cy: head.y - 2, rx: 2.5, ry: 2.5, fill: "rgba(255,255,255,0.75)", color: "rgba(255,255,255,0.75)" },
    ];
    // the note opens on hover / selection, like TradingView's pins
    if (g.d.text && (g.sel || g.deps.hoverId() === g.d.id)) out.push(textShape(g, head.x + 14, head.y, { base: "middle", bg: rgba(g.deps.bgColor(), 0.95), border: g.col, pad: 6 }));
    return out;
  },
  anote: (g) => noteGeo(g),
  callout: (g) => {
    const [a, b] = g.P;
    const t = textShape(g, b.x, b.y, { align: "center", base: "middle", bg: rgba(g.col, 0.85), color: contrastText(g.col), pad: 6 });
    const box = textBox(t as Extract<Shape, { k: "text" }>);
    const cx = Math.max(box.x + 8, Math.min(box.x + box.w - 8, a.x));
    const edgeY = a.y > box.y + box.h ? box.y + box.h : box.y;
    return [{ k: "poly", pts: [{ x: cx - 6, y: edgeY }, a, { x: cx + 6, y: edgeY }], closed: true, fill: rgba(g.col, 0.85), noStroke: true }, t];
  },
  comment: (g) => {
    const a = g.P[0];
    const t = textShape(g, a.x + 4, a.y - 14, { base: "bottom", bg: rgba(g.col, 0.85), color: contrastText(g.col), pad: 6 });
    return [{ k: "poly", pts: [{ x: a.x + 4, y: a.y - 16 }, a, { x: a.x + 16, y: a.y - 16 }], closed: true, fill: rgba(g.col, 0.85), noStroke: true }, t];
  },
  signpost: (g) => {
    const a = g.P[0];
    const top = a.y - 44;
    return [
      { k: "line", a, b: { x: a.x, y: top } },
      { k: "ellipse", cx: a.x, cy: a.y, rx: 3, ry: 3, fill: g.col },
      textShape(g, a.x, top, { align: "center", base: "bottom", bg: rgba(g.col, 0.9), color: contrastText(g.col), pad: 5 }),
    ];
  },
  pricelabel: (g) => {
    const a = g.P[0];
    return [
      { k: "poly", pts: [a, { x: a.x + 8, y: a.y - 10 }, { x: a.x + 14, y: a.y - 10 }], closed: true, fill: rgba(g.col, 0.9), noStroke: true },
      label(g, a.x + 8, a.y - 10, g.deps.priceFormatter(g.d.points[0].price), { align: "left", base: "bottom" }),
    ];
  },
  pricenote: (g) => {
    const [a, b] = g.P;
    return [
      { k: "line", a, b },
      { k: "ellipse", cx: a.x, cy: a.y, rx: 3, ry: 3, fill: g.col },
      label(g, b.x, b.y, g.deps.priceFormatter(g.d.points[0].price), { align: b.x >= a.x ? "left" : "right" }),
    ];
  },
  arrowmarker: (g) => {
    const [a, b] = g.P;
    const out: Shape[] = [{ k: "line", a, b, arrow: "end", width: Math.max(3, g.s.width + 2) }];
    if (g.d.text) out.push(textShape(g, a.x, a.y + (a.y >= b.y ? 6 : -6), { align: "center", base: a.y >= b.y ? "top" : "bottom" }));
    return out;
  },
  arrowup: (g) => {
    const a = g.P[0];
    const col = g.s.color ? g.col : g.deps.upColor();
    return [
      { k: "poly", pts: [{ x: a.x, y: a.y + 4 }, { x: a.x - 9, y: a.y + 16 }, { x: a.x - 4, y: a.y + 16 }, { x: a.x - 4, y: a.y + 30 }, { x: a.x + 4, y: a.y + 30 }, { x: a.x + 4, y: a.y + 16 }, { x: a.x + 9, y: a.y + 16 }], closed: true, fill: col, color: col },
      ...(g.d.text ? [textShape(g, a.x, a.y + 34, { align: "center", base: "top", color: col })] : []),
    ];
  },
  arrowdown: (g) => {
    const a = g.P[0];
    const col = g.s.color ? g.col : g.deps.downColor();
    return [
      { k: "poly", pts: [{ x: a.x, y: a.y - 4 }, { x: a.x - 9, y: a.y - 16 }, { x: a.x - 4, y: a.y - 16 }, { x: a.x - 4, y: a.y - 30 }, { x: a.x + 4, y: a.y - 30 }, { x: a.x + 4, y: a.y - 16 }, { x: a.x + 9, y: a.y - 16 }], closed: true, fill: col, color: col },
      ...(g.d.text ? [textShape(g, a.x, a.y - 34, { align: "center", base: "bottom", color: col })] : []),
    ];
  },
  // Left / right marks: the up arrow's outline with x and y swapped (tip at the point).
  arrowleft: (g) => {
    const a = g.P[0];
    return [{ k: "poly", pts: [[4, 0], [16, -9], [16, -4], [30, -4], [30, 4], [16, 4], [16, 9]].map(([dx, dy]) => ({ x: a.x + dx, y: a.y + dy })), closed: true, fill: g.col, color: g.col }];
  },
  arrowright: (g) => {
    const a = g.P[0];
    return [{ k: "poly", pts: [[-4, 0], [-16, -9], [-16, -4], [-30, -4], [-30, 4], [-16, 4], [-16, 9]].map(([dx, dy]) => ({ x: a.x + dx, y: a.y + dy })), closed: true, fill: g.col, color: g.col }];
  },
  flag: (g) => {
    const a = g.P[0];
    return [
      { k: "line", a, b: { x: a.x, y: a.y - 28 }, width: 2 },
      { k: "poly", pts: [{ x: a.x, y: a.y - 28 }, { x: a.x + 18, y: a.y - 23 }, { x: a.x, y: a.y - 17 }], closed: true, fill: g.col },
    ];
  },
  image: (g) => {
    const a = g.P[0];
    const w = g.d.data?.w ?? 160, h = g.d.data?.h ?? 100;
    if (!g.d.data?.src) return [{ k: "rect", x: a.x, y: a.y, w, h, dash: DASH[2] }];
    return [{ k: "image", x: a.x, y: a.y, w, h, src: g.d.data.src }, ...(g.sel ? [{ k: "rect" as const, x: a.x, y: a.y, w, h, dash: DASH[1] }] : [])];
  },
  table: (g) => {
    const a = g.P[0];
    const rows = (g.d.text ?? "").split(/\n|;/).map((r) => r.split(/[,|]/).map((c) => c.trim()));
    const cols = Math.max(1, ...rows.map((r) => r.length));
    const size = g.s.fontSize;
    const cw = Array.from({ length: cols }, (_, j) => Math.max(...rows.map((r) => (r[j] ?? "").length), 2) * size * 0.62 + 12);
    const rh = size * 1.7;
    const out: Shape[] = [{ k: "rect", x: a.x, y: a.y, w: cw.reduce((x, y) => x + y, 0), h: rh * rows.length, fill: rgba(g.deps.bgColor(), 0.9) }];
    let y = a.y;
    rows.forEach((r, i) => {
      let x = a.x;
      r.forEach((cell, j) => {
        out.push({ k: "text", x: x + 6, y: y + rh / 2, text: cell, size, bold: i === 0, color: g.tcol, base: "middle" });
        x += cw[j];
        if (j < cols - 1 && i === 0) out.push({ k: "line", a: { x, y: a.y }, b: { x, y: a.y + rh * rows.length }, alpha: 0.5 });
      });
      y += rh;
      if (i < rows.length - 1) out.push({ k: "line", a: { x: a.x, y }, b: { x: a.x + cw.reduce((p, q) => p + q, 0), y }, alpha: 0.5 });
    });
    return out;
  },
  icon: (g) => {
    const a = g.P[0];
    const st = g.d.data?.sticker;
    if (st) {
      const size = Math.max(10, g.s.fontSize * 0.5);
      return [{ k: "text", x: a.x, y: a.y, text: `${g.d.data?.emoji ?? ""} ${st.text}`.trim(), size, align: "center", base: "middle", bold: true, color: "#FFFFFF", bg: st.color, pad: 7, border: "#FFFFFF" }];
    }
    return [{ k: "text", x: a.x, y: a.y, text: g.d.data?.emoji ?? "⭐", size: g.s.fontSize, align: "center", base: "middle", ...(g.d.data?.glyph ? { color: g.col } : {}) }];
  },
};

function noteGeo(g: G): Shape[] {
  const a = g.P[0];
  const out: Shape[] = [
    { k: "rect", x: a.x - 9, y: a.y - 9, w: 18, h: 18, fill: g.col },
    { k: "text", x: a.x, y: a.y, text: "✎", size: 11, color: "#FFFFFF", align: "center", base: "middle" },
  ];
  if (g.sel || g.deps.hoverId() === g.d.id) out.push(textShape(g, a.x + 14, a.y, { base: "middle", bg: rgba(g.deps.bgColor(), 0.95), border: g.col, pad: 6 }));
  return out;
}

function forkGeo(g: G, kind: "andrews" | "schiff" | "mschiff" | "inside"): Shape[] {
  const [a, b, c] = g.P;
  if (!c) return [{ k: "line", a, b }];
  const o = kind === "schiff" ? { x: a.x, y: (a.y + b.y) / 2 } : kind === "mschiff" ? mid(a, b) : a;
  const m = mid(b, c);
  const v = sub(m, o);
  const out: Shape[] = [
    { k: "line", a: b, b: c },
    { k: "line", a: o, b: rayEnd(o, m, g.w, g.h) },
  ];
  const tine = (p: XY, extra?: Partial<Extract<Shape, { k: "line" }>>): Shape => ({ k: "line", a: p, b: rayEnd(p, add(p, v), g.w, g.h), ...extra });
  const bEnd = rayEnd(b, add(b, v), g.w, g.h);
  const cEnd = rayEnd(c, add(c, v), g.w, g.h);
  if (g.s.fill) out.push({ k: "poly", pts: [b, bEnd, cEnd, c], closed: true, fill: g.fill, noStroke: true });
  out.push(tine(b), tine(c));
  if (kind === "inside") {
    out.push(tine(lerp(b, c, 0.25), { dash: DASH[2], alpha: 0.7 }), tine(lerp(b, c, 0.75), { dash: DASH[2], alpha: 0.7 }));
  }
  if (kind !== "andrews") out.push({ k: "line", a, b: o, dash: DASH[1], alpha: 0.6 });
  return out;
}

function gannSquareGeo(g: G, a: XY, b: XY): Shape[] {
  const out: Shape[] = [{ k: "rect", x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), w: Math.abs(b.x - a.x), h: Math.abs(b.y - a.y), fill: g.s.fill ? rgba(g.col, g.s.fillOpacity / 2) : undefined }];
  for (const t of [0.25, 0.5, 0.75]) {
    const x = a.x + (b.x - a.x) * t, y = a.y + (b.y - a.y) * t;
    out.push({ k: "line", a: { x, y: a.y }, b: { x, y: b.y }, dash: DASH[1], alpha: 0.6 });
    out.push({ k: "line", a: { x: a.x, y }, b: { x: b.x, y }, dash: DASH[1], alpha: 0.6 });
    out.push({ k: "line", a, b: { x: b.x, y }, alpha: 0.7 });
    out.push({ k: "line", a, b: { x, y: b.y }, alpha: 0.7 });
  }
  out.push({ k: "line", a, b }, { k: "line", a: { x: a.x, y: b.y }, b: { x: b.x, y: a.y } });
  const r = Math.abs(b.x - a.x);
  out.push({ k: "ellipse", cx: a.x, cy: a.y, rx: r, ry: Math.abs(b.y - a.y), start: 0, end: Math.PI * 2, alpha: 0.3 });
  return out;
}

function harmonicGeo(g: G): Shape[] {
  const out: Shape[] = [];
  const [X, A, B, C, D] = g.P;
  const pr = g.d.points.map((p) => p.price);
  if (B && g.s.fill) out.push({ k: "poly", pts: [X, A, B], closed: true, fill: g.fill, noStroke: true });
  if (D && g.s.fill) out.push({ k: "poly", pts: [B, C, D], closed: true, fill: g.fill, noStroke: true });
  out.push(...polyWithLabels(g, PATTERN_LABELS.xabcd));
  const rl = (p: XY, q: XY, txt: string) => {
    out.push({ k: "line", a: p, b: q, dash: DASH[2], alpha: 0.6 });
    out.push({ k: "text", x: mid(p, q).x, y: mid(p, q).y, text: txt, size: 10, color: g.tcol, align: "center", bg: rgba(g.deps.bgColor(), 0.8), pad: 2 });
  };
  if (B) rl(X, B, ratio(pr[2] - pr[1], pr[1] - pr[0]));
  if (C) rl(A, C, ratio(pr[3] - pr[2], pr[2] - pr[1]));
  if (D) {
    rl(B, D, ratio(pr[4] - pr[3], pr[3] - pr[2]));
    rl(X, D, ratio(pr[4] - pr[1], pr[1] - pr[0]));
  }
  return out;
}

function rangeGeo(g: G, date: boolean, price: boolean, measure: boolean): Shape[] {
  const [a, b] = g.P;
  const [p0, p1] = g.d.points;
  const dp = p1.price - p0.price;
  const l0 = g.deps.timeToLogical(p0.time), l1 = g.deps.timeToLogical(p1.time);
  const bars = l0 !== null && l1 !== null ? Math.round(l1 - l0) : 0;
  const col = measure ? (dp >= 0 ? "#2962FF" : g.deps.downColor()) : g.col;
  const x0 = Math.min(a.x, b.x), y0 = Math.min(a.y, b.y);
  const out: Shape[] = [{ k: "rect", x: x0, y: y0, w: Math.abs(b.x - a.x), h: Math.abs(b.y - a.y), fill: rgba(col, 0.15), noStroke: !measure, color: col }];
  const m = mid(a, b);
  if (date) out.push({ k: "line", a: { x: a.x, y: m.y }, b: { x: b.x, y: m.y }, arrow: "end", color: col });
  if (price) out.push({ k: "line", a: { x: m.x, y: a.y }, b: { x: m.x, y: b.y }, arrow: "end", color: col });
  const parts: string[] = [];
  if (price) parts.push(`${dp >= 0 ? "+" : ""}${g.deps.priceFormatter(dp)} (${((dp / p0.price) * 100).toFixed(2)}%)`);
  if (date) {
    let vol = 0;
    const r = barRange(g.deps, p0.time, p1.time);
    if (r) for (const k of g.deps.candles().slice(r[0], r[1] + 1)) vol += k.volume || 0;
    parts.push(`${bars} bars, ${formatSpan(p1.time - p0.time)}`);
    if (vol > 0) parts.push(`Vol ${fmtVol(vol)}`);
  }
  const below = b.y >= a.y;
  out.push({ k: "text", x: m.x, y: (below ? Math.max(a.y, b.y) + 6 : Math.min(a.y, b.y) - 6), text: parts.join("\n"), align: "center", base: below ? "top" : "bottom", size: 11, color: "#FFFFFF", bg: rgba(col, 0.9), pad: 5 });
  return out;
}

// ---------------------------------------------------------------------------
// Public geometry / hit testing
// ---------------------------------------------------------------------------

function project(d: Drawing, deps: DrawingDeps, w: number, h: number): XY[] | null {
  if (d.screen && TOOL_BY_ID.get(d.type)?.screen) return [{ x: d.screen.x * w, y: d.screen.y * h }];
  const out: XY[] = [];
  for (const p of d.points) {
    const x = deps.timeToX(p.time);
    const y = deps.priceToY(p.price);
    if (x === null || y === null) return null;
    out.push({ x, y });
  }
  return out;
}

export function geometry(d: Drawing, deps: DrawingDeps, w: number, h: number, sel = false): Shape[] {
  const P = project(d, deps, w, h);
  if (!P || P.length === 0) return [];
  const s = styleOf(d, deps);
  const col = s.color || deps.lineColor();
  const g: G = {
    d, s, deps, w, h, P, sel,
    col,
    fill: rgba(s.fillColor || col, s.fillOpacity),
    tcol: s.textColor || col,
  };
  const fn = GEO[d.type];
  if (!fn) return [];
  const need = TOOL_BY_ID.get(d.type)?.points;
  if (typeof need === "number" && P.length < need && need > 1) {
    // In-progress multi-point tool: show what is placed so far.
    if (P.length < 2) return [];
    if (need === 3 || PATTERN_LABELS[d.type]) return fn(g);
    return [{ k: "poly", pts: P }];
  }
  try {
    return fn(g);
  } catch {
    return [];
  }
}

/** Index of the anchor handle at (x, y), or null. Handles are only
 *  rendered for the selected drawing, so callers should gate on that. */
export function hitHandle(d: Drawing, x: number, y: number, deps: DrawingDeps, w = 0, h = 0): number | null {
  const P = project(d, deps, w, h);
  if (!P) return null;
  for (let i = 0; i < P.length; i++) {
    if (Math.abs(x - P[i].x) <= HANDLE_PX && Math.abs(y - P[i].y) <= HANDLE_PX) return i;
  }
  return null;
}

/** Does (x, y) hit the drawing? Uses the same geometry as painting. */
export function hitTest(d: Drawing, x: number, y: number, deps: DrawingDeps, width: number, height: number): boolean {
  if (d.hidden) return false;
  for (const s of geometry(d, deps, width, height, deps.selectedId() === d.id)) if (hitShape(s, x, y)) return true;
  return false;
}

/** Visible on the current interval group? */
export function visibleNow(d: Drawing, deps: Pick<DrawingDeps, "intervalGroup" | "intervalCount">): boolean {
  if (d.hidden) return false;
  const v = d.vis?.[deps.intervalGroup()];
  if (v === false) return false;
  if (v && typeof v === "object") {
    if (!v.on) return false;
    const n = deps.intervalCount?.();
    if (n != null && (n < v.min || n > v.max)) return false;
  }
  return true;
}

/** Bounding box of a drawing's anchors in pixels (floating toolbar placement). */
export function anchorBox(d: Drawing, deps: DrawingDeps, w: number, h: number): { x: number; y: number; w: number; h: number } | null {
  const P = project(d, deps, w, h);
  if (!P || P.length === 0) return null;
  const xs = P.map((p) => p.x), ys = P.map((p) => p.y);
  const x0 = Math.min(...xs), y0 = Math.min(...ys);
  return { x: x0, y: y0, w: Math.max(...xs) - x0, h: Math.max(...ys) - y0 };
}

/**
 * Fill in derived anchors for a freshly placed drawing: long / short
 * positions get a target and stop, bars-pattern its copy anchor.
 */
export function finalizeDrawing(d: Drawing, deps: DrawingDeps, w: number, h: number): Drawing {
  if (d.type === "long" || d.type === "short") {
    const e = d.points[0];
    const y = deps.priceToY(e.price);
    const x = deps.timeToX(e.time);
    if (y === null || x === null) return d;
    const right = deps.xToTime(x + Math.max(80, Math.min(240, w * 0.2))) ?? e.time;
    const up = deps.yToPrice(y - Math.max(40, h * 0.1)) ?? e.price * 1.02;
    const dn = deps.yToPrice(y + Math.max(25, h * 0.06)) ?? e.price * 0.99;
    const long = d.type === "long";
    const tp = long ? up : dn;
    const sl = long ? dn : up;
    return {
      ...d,
      points: [e, { time: right, price: tp }, { time: right, price: sl }],
      data: { account: 100000, risk: 1, qty: 0, lot: 1, ...(d.data ?? {}) },
    };
  }
  if (d.type === "barspattern" && d.points.length === 2) {
    const r = barRange(deps, d.points[0].time, d.points[1].time);
    const c = deps.candles();
    const x1 = deps.timeToX(Math.max(d.points[0].time, d.points[1].time));
    const t = x1 !== null ? deps.xToTime(x1 + deps.barSpacing() * 2) : null;
    return {
      ...d,
      points: [...d.points, { time: t ?? d.points[1].time, price: r ? c[r[0]].open : d.points[1].price }],
    };
  }
  return normalizeDrawing(d, deps, -1);
}

/** Keep derived anchors consistent after a drag (index = handle moved,
 *  -1 = whole drawing / creation). */
export function normalizeDrawing(d: Drawing, deps: DrawingDeps, index: number): Drawing {
  if ((d.type === "long" || d.type === "short") && d.points.length === 3) {
    const pts = d.points.map((p) => ({ ...p }));
    if (index === 1) pts[2].time = pts[1].time;
    else pts[1].time = pts[2].time;
    return { ...d, points: pts };
  }
  if (d.type === "avwap") {
    const c = deps.candles();
    const l = deps.timeToLogical(d.points[0].time);
    if (l === null || c.length === 0) return d;
    const k = c[Math.max(0, Math.min(c.length - 1, Math.round(l)))];
    return { ...d, points: [{ time: k.time, price: (k.high + k.low + k.close) / 3 }] };
  }
  return d;
}

/** The drawing's price at chart time `t` — for alerts on lines. */
export function drawingValueAt(d: Drawing, t: number, deps: Pick<DrawingDeps, "timeToLogical" | "toolDefaults">): number | null {
  const [p0, p1] = d.points;
  if (!p0) return null;
  if (d.type === "hline") return p0.price;
  if (d.type === "hray") return t >= p0.time ? p0.price : null;
  if (!p1 || !["trend", "ray", "extended", "info", "angle", "arrowline"].includes(d.type)) return null;
  const l0 = deps.timeToLogical(p0.time), l1 = deps.timeToLogical(p1.time), lt = deps.timeToLogical(t);
  if (l0 === null || l1 === null || lt === null || l1 === l0) return null;
  const s = styleOf(d, deps);
  const frac = (lt - l0) / (l1 - l0);
  const extRight = d.type === "ray" || d.type === "extended" || d.type === "angle" || s.extendRight;
  const extLeft = d.type === "extended" || s.extendLeft;
  if ((frac > 1 && !extRight) || (frac < 0 && !extLeft)) return null;
  return p0.price + (p1.price - p0.price) * frac;
}

/** Snap (x, y) relative to an anchor to the nearest 45° (Shift). */
export function snapAngle(anchor: XY, p: XY): XY {
  const dx = p.x - anchor.x, dy = p.y - anchor.y;
  const len = Math.hypot(dx, dy);
  const step = Math.PI / 4;
  const ang = Math.round(Math.atan2(dy, dx) / step) * step;
  return { x: anchor.x + Math.cos(ang) * len, y: anchor.y + Math.sin(ang) * len };
}

// ---------------------------------------------------------------------------
// Primitive
// ---------------------------------------------------------------------------

interface MediaScope {
  context: CanvasRenderingContext2D;
  mediaSize: { width: number; height: number };
}

export class DrawingsPrimitive implements ISeriesPrimitive<Time> {
  private _deps: DrawingDeps;
  private _requestUpdate: (() => void) | null = null;
  private _paneView: IPrimitivePaneView;

  constructor(deps: DrawingDeps) {
    this._deps = deps;
    this._paneView = {
      zOrder: () => "top",
      renderer: () => ({
        draw: (target) => {
          target.useMediaCoordinateSpace((scope: MediaScope) => {
            const { context: ctx, mediaSize } = scope;
            const deps = this._deps;
            const { width: w, height: h } = mediaSize;
            deps.extras?.(ctx, w, h);
            const selected = deps.selectedId();
            const repaint = () => this.requestUpdate();
            if (!deps.hidden()) {
              for (const d of deps.drawings()) {
                if (!visibleNow(d, deps)) continue;
                const sel = d.id === selected;
                const s = styleOf(d, deps);
                const base = { color: s.color || deps.lineColor(), width: s.width, dash: DASH[s.dash] ?? [] };
                for (const shape of geometry(d, deps, w, h, sel)) paintShape(ctx, shape, base, repaint, { w, h });
                if (sel) {
                  const P = project(d, deps, w, h) ?? [];
                  for (const p of P) {
                    ctx.save();
                    ctx.fillStyle = deps.bgColor();
                    ctx.strokeStyle = d.locked ? "#9E9E9E" : deps.accent();
                    ctx.lineWidth = 1.5;
                    ctx.beginPath();
                    ctx.arc(p.x, p.y, 4.5, 0, Math.PI * 2);
                    ctx.fill();
                    ctx.stroke();
                    ctx.restore();
                  }
                }
              }
            }
            const pending = deps.pending();
            if (pending && (pending.cursor || pending.points.length > 1)) {
              const pts = pending.cursor ? [...pending.points, pending.cursor] : pending.points;
              const ghost: Drawing = { id: "__pending__", type: pending.type, points: pts, data: pending.data };
              const s = styleOf(ghost, deps);
              const base = { color: s.color || deps.lineColor(), width: s.width, dash: DASH[s.dash] ?? [] };
              ctx.save();
              ctx.globalAlpha = 0.75;
              for (const shape of geometry(ghost, deps, w, h, false)) paintShape(ctx, shape, base, repaint);
              ctx.restore();
            }
          });
        },
      }),
    };
  }

  attached(param: SeriesAttachedParameter<Time>): void {
    this._requestUpdate = param.requestUpdate;
  }

  detached(): void {
    this._requestUpdate = null;
  }

  updateAllViews(): void {
    /* state is read lazily at paint time */
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return [this._paneView];
  }

  /** Ask the chart to repaint (call after mutating the drawing store). */
  requestUpdate(): void {
    this._requestUpdate?.();
  }
}
