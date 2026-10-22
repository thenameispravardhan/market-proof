// tvIcons — thin line icons (28 × 28, currentColor) for the chart's left
// drawing strip and the Trade page's right panel bar, in the TradingView
// manner. Any tool without its own icon falls back to its group's, and a
// group without one to the tool's text glyph.

import type { ReactNode } from "react";

const S = { fill: "none", stroke: "currentColor", strokeWidth: 1.1, strokeLinecap: "round", strokeLinejoin: "round" } as const;
const dot = (x: number, y: number, r = 1.8) => <circle cx={x} cy={y} r={r} {...S} />;

const P: Record<string, ReactNode> = {
  // cursors
  cross: <><path {...S} d="M14 5v7M14 16v7M5 14h7M16 14h7" /></>,
  dot: <circle cx="14" cy="14" r="2.6" fill="currentColor" />,
  arrow: <path {...S} d="M9 6l12 9.5-5.6.6 3.3 6.4-2.2 1.1-3.3-6.4L9 21z" />,
  demo: <><circle {...S} cx="14" cy="14" r="3.2" /><path {...S} d="M14 5v3M14 20v3M5 14h3M20 14h3M7.6 7.6l2 2M18.4 18.4l2 2M7.6 20.4l2-2M18.4 9.6l2-2" /></>,
  eraser: <path {...S} d="M11 22h12M6.5 16.5l9-9a1.5 1.5 0 0 1 2.1 0l4 4a1.5 1.5 0 0 1 0 2.1L13 22H10.6l-4.1-4.1a1 1 0 0 1 0-1.4zM10.5 12.5l6 6" />,
  // lines
  trend: <><path {...S} d="M8.2 19.8L19.8 8.2" />{dot(7, 21)}{dot(21, 7)}</>,
  ray: <><path {...S} d="M8.3 19.7L23 5M19 5h4v4" />{dot(7, 21)}</>,
  info: <><path {...S} d="M8.2 19.8L19.8 8.2" />{dot(7, 21)}{dot(21, 7)}<rect {...S} x="15" y="16" width="8" height="6" rx="1" /></>,
  extended: <><path {...S} d="M4 24L24 4" />{dot(10, 18)}{dot(18, 10)}</>,
  angle: <><path {...S} d="M6 21h16M6 21L19 8M12 21a6 6 0 0 0-1.8-4.3" />{dot(19, 8)}</>,
  hline: <><path {...S} d="M4 14h8.2M15.8 14H24" />{dot(14, 14)}</>,
  hray: <><path {...S} d="M8.8 14H24" />{dot(7, 14)}</>,
  vline: <><path {...S} d="M14 4v8.2M14 15.8V24" />{dot(14, 14)}</>,
  crossline: <><path {...S} d="M4 14h8.2M15.8 14H24M14 4v8.2M14 15.8V24" />{dot(14, 14)}</>,
  channel: <><path {...S} d="M5 17L17 5M11 23L23 11" />{dot(5, 17)}{dot(17, 5)}{dot(11, 23)}</>,
  pitchfork: <><path {...S} d="M5 23l9-9M14 14l9-6M14 14l9 0M14 14l6 9" />{dot(5, 23)}{dot(14, 14)}</>,
  // fibonacci / gann
  fib: <><path {...S} d="M4 6h20M4 11h20M4 16h20M4 22h20" />{dot(6, 22)}{dot(22, 6)}</>,
  gann: <><rect {...S} x="5" y="5" width="18" height="18" /><path {...S} d="M5 23L23 5M5 14h18M14 5v18" /></>,
  // patterns
  pattern: <><path {...S} d="M4 20l5-11 5 8 5-12 5 13" />{dot(9, 9, 1.4)}{dot(14, 17, 1.4)}{dot(19, 5, 1.4)}</>,
  wave: <path {...S} d="M3 16c3-8 5-8 8 0s5 8 8 0 4-6 6-4" />,
  // forecast / measure
  long: <><rect {...S} x="6" y="5" width="16" height="18" rx="1" /><path {...S} d="M6 14h16" /><path d="M7 6h14v7.5H7z" fill="currentColor" opacity=".18" /></>,
  short: <><rect {...S} x="6" y="5" width="16" height="18" rx="1" /><path {...S} d="M6 14h16" /><path d="M7 14.5h14V22H7z" fill="currentColor" opacity=".18" /></>,
  forecast: <><path {...S} d="M5 20l6-6" />{dot(5, 20)}<path {...S} strokeDasharray="2 2" d="M11 14l11-7" /><path {...S} d="M18 6.5l4 .5-1.5 3.8" /></>,
  daterange: <><path {...S} d="M5 6v16M23 6v16M8 14h12M11 11l-3 3 3 3M17 11l3 3-3 3" /></>,
  pricerange: <><path {...S} d="M6 5h16M6 23h16M14 8v12M11 11l3-3 3 3M11 17l3 3 3-3" /></>,
  dprange: <><rect {...S} x="5" y="5" width="18" height="18" /><path {...S} d="M9 19l10-10M15 9h4v4" /></>,
  anchor: <><circle {...S} cx="14" cy="7" r="2" /><path {...S} d="M14 9v14M9 13h10M6 17a8 6 0 0 0 16 0" /></>,
  profile: <><path {...S} d="M5 5v18" /><path {...S} d="M5 7h7M5 10h11M5 13h16M5 16h12M5 19h8M5 22h4" /></>,
  bars: <><path {...S} d="M8 8v12M6 11h2M8 16h2M14 5v16M12 9h2M14 14h2M20 9v12M18 12h2M20 18h2" /></>,
  // shapes
  brush: <><path {...S} d="M20.5 4.5l3 3-9.8 9.8-3-3zM10.7 14.3c-2.2-.3-4.2 1.2-4.2 3.5 0 1.6-.8 3.3-2.5 4.2 3.6.9 7.8-.2 9.2-3.7" /></>,
  highlighter: <><path {...S} d="M17 5l6 6-8 8-6-6zM9 13l-3 3v3h3l3-3M5 24h18" /></>,
  arrowshape: <><path {...S} d="M6 22L21 7M13 7h8v8" /></>,
  rect: <><rect {...S} x="6" y="8" width="16" height="12" />{dot(6, 8)}{dot(22, 20)}</>,
  circle: <><circle {...S} cx="14" cy="14" r="8.5" />{dot(14, 14)}</>,
  ellipse: <ellipse {...S} cx="14" cy="14" rx="10" ry="6.5" />,
  triangle: <path {...S} d="M14 5l9.5 17h-19z" />,
  path: <><path {...S} d="M5 21l6-9 6 5 6-10" />{dot(5, 21)}{dot(23, 7)}</>,
  arc: <><path {...S} d="M5 21C8 9 20 9 23 21" />{dot(5, 21)}{dot(23, 21)}</>,
  // annotation
  text: <path {...S} d="M7 7h14M14 7v15M11 22h6" />,
  note: <><path {...S} d="M6 6h16v12l-4 4H6z" /><path {...S} d="M18 22v-4h4M10 11h8M10 15h5" /></>,
  callout: <><path {...S} d="M5 6h18v11H13l-5 5v-5H5z" /></>,
  smile: <><circle {...S} cx="14" cy="14" r="9" /><path {...S} d="M10 16.5c2.2 2.6 5.8 2.6 8 0" /><circle cx="11" cy="11.5" r="1" fill="currentColor" /><circle cx="17" cy="11.5" r="1" fill="currentColor" /></>,
  image: <><rect {...S} x="4.5" y="6.5" width="19" height="15" rx="1.5" /><path {...S} d="M4.5 18l5-5 4 4 3-3 7 7" /><circle {...S} cx="18.5" cy="11" r="1.6" /></>,
  // utilities
  measure: <><path {...S} d="M4 18L18 4l6 6-14 14z" /><path {...S} d="M8 14l2 2M11 11l3 3M14 8l2 2M17 5l2 2" /></>,
  zoom: <><circle {...S} cx="12" cy="12" r="7" /><path {...S} d="M17 17l6 6M12 9v6M9 12h6" /></>,
  magnet: <><path {...S} d="M7 5h4v9a3 3 0 0 0 6 0V5h4v9a7 7 0 0 1-14 0z" /><path {...S} d="M7 9h4M17 9h4" /></>,
  stay: <><path {...S} d="M18 4l4 4-11 11H7v-4zM15 7l4 4" /><rect {...S} x="16" y="18" width="8" height="6" rx="1" /><path {...S} d="M18 18v-2a2 2 0 0 1 4 0v2" /></>,
  lock: <><rect {...S} x="7" y="12" width="14" height="11" rx="1.5" /><path {...S} d="M10 12V9a4 4 0 0 1 8 0v3M14 16.5v2.5" /></>,
  unlock: <><rect {...S} x="7" y="12" width="14" height="11" rx="1.5" /><path {...S} d="M10 12V9a4 4 0 0 1 7.8-1.2M14 16.5v2.5" /></>,
  eye: <><path {...S} d="M3.5 14S7.5 7 14 7s10.5 7 10.5 7-4 7-10.5 7S3.5 14 3.5 14z" /><circle {...S} cx="14" cy="14" r="3.2" /></>,
  eyeoff: <><path {...S} d="M3.5 14S7.5 7 14 7s10.5 7 10.5 7-4 7-10.5 7S3.5 14 3.5 14z" /><path {...S} d="M5 23L23 5" /></>,
  link: <><path {...S} d="M12 16l4-4M10.5 13.5l-2.8 2.8a3.5 3.5 0 0 0 5 5l2.8-2.8M17.5 14.5l2.8-2.8a3.5 3.5 0 0 0-5-5l-2.8 2.8" /></>,
  backspace: <><path {...S} d="M10 7h13v14H10l-6-7z" /><path {...S} d="M13 11l6 6M19 11l-6 6" /></>,
  trash: <><path {...S} d="M6 8h16M11 8V5.5h6V8M8 8l1 15h10l1-15M12 12v7M16 12v7" /></>,
  bell: <><path {...S} d="M8 19V13a6 6 0 0 1 12 0v6l2 2H6zM12 23h4" /></>,
  pick: <><circle {...S} cx="12" cy="12" r="6" /><path {...S} d="M12 3v4M12 17v4M3 12h4M17 12h4M18 18l5 5" /></>,
  star: <path {...S} d="M14 4.5l2.8 5.9 6.4.8-4.7 4.4 1.2 6.4L14 18.9 8.3 22l1.2-6.4-4.7-4.4 6.4-.8z" />,
  collapse: <path {...S} d="M16 8l-6 6 6 6M21 8l-6 6 6 6" />,
  // the right panel bar
  watch: <><path {...S} d="M5 7h18M5 12h18M5 17h12M5 22h8" /><path {...S} d="M20 17v6M17 20h6" /></>,
  details: <><circle {...S} cx="14" cy="14" r="9.5" /><path {...S} d="M14 12.5V20M14 8.5v.5" /></>,
  trade: <><path {...S} d="M9 23V6M5 10l4-4 4 4M19 5v17M15 18l4 4 4-4" /></>,
  dom: <><rect {...S} x="7" y="4" width="14" height="20" rx="1" /><path {...S} d="M7 9h14M7 14h14M7 19h14M14 4v20" /></>,
  timeline: <><circle {...S} cx="14" cy="14" r="9.5" /><path {...S} d="M14 8.5V14l4 2.5" /></>,
  chain: <><rect {...S} x="4.5" y="5.5" width="19" height="17" rx="1" /><path {...S} d="M4.5 11h19M4.5 16.5h19M14 5.5v17" /></>,
  flow: <><path {...S} d="M4 22h20" /><path {...S} d="M7 22v-6M11 22V9M15 22v-9M19 22V6M23 22v-4" /></>,
  book: <><path {...S} d="M4 7h11M4 11h15M4 15h9M4 19h13M17 7h7M21 11h3M15 15h9M19 19h5" /></>,
  fno: <><path {...S} d="M14 5v18M8 23h12M5 9h18M8 9l-3 7h6zM20 9l-3 7h6zM5 16a3 3 0 0 0 6 0M17 16a3 3 0 0 0 6 0" /></>,
  data: <><rect {...S} x="4.5" y="5.5" width="19" height="17" rx="1" /><path {...S} d="M4.5 10.5h19M11 10.5v12" /></>,
  tree: <><rect {...S} x="4" y="4" width="7" height="5" rx="1" /><rect {...S} x="15" y="12" width="9" height="5" rx="1" /><rect {...S} x="15" y="20" width="9" height="5" rx="1" /><path {...S} d="M7.5 9v13.5H15M7.5 14.5H15" /></>,
  alerts: <><circle {...S} cx="14" cy="15" r="8" /><path {...S} d="M14 10.5V15l3 2M5.5 7.5l3-3M22.5 7.5l-3-3" /></>,
};

/** The icon for each drawing tool id (anything missing: its group's). */
const TOOL_ICON: Record<string, string> = {
  trend: "trend", ray: "ray", info: "info", extended: "extended", angle: "angle", hline: "hline", hray: "hray",
  vline: "vline", cross: "crossline", channel: "channel", regression: "channel", flattop: "channel", disjoint: "channel",
  pitchfork: "pitchfork", schiff: "pitchfork", mschiff: "pitchfork", ipitchfork: "pitchfork",
  gannbox: "gann", gannsqfixed: "gann", gannsquare: "gann", gannfan: "gann",
  ew_impulse: "wave", ew_correction: "wave", ew_triangle: "wave", ew_double: "wave", ew_triple: "wave",
  cyclic: "wave", timecycles: "wave", sine: "wave",
  long: "long", short: "short", forecast: "forecast", barspattern: "bars", ghostfeed: "forecast", projection: "forecast",
  avwap: "anchor", frvp: "profile", daterange: "daterange", pricerange: "pricerange", dprange: "dprange",
  brush: "brush", highlighter: "highlighter", arrowmarker: "arrowshape", arrowline: "arrowshape", arrowup: "arrowshape",
  arrowdown: "arrowshape", arrowleft: "arrowshape", arrowright: "arrowshape", rect: "rect", rotrect: "rect", path: "path",
  circle: "circle", ellipse: "ellipse", polyline: "path", triangle: "triangle", arc: "arc", curve: "arc", dcurve: "arc",
  text: "text", atext: "text", note: "note", anote: "note", callout: "callout", comment: "callout",
  measure: "measure",
};
const GROUP_ICON: Record<string, string> = {
  lines: "trend", fib: "fib", patterns: "pattern", forecast: "long", shapes: "brush", annotate: "text", icons: "smile",
};

/** A named icon (see `P`), or `fallback` text when there is none. */
export function TvIcon({ name, fallback, size = 28 }: { name: string; fallback?: ReactNode; size?: number }) {
  const body = P[name];
  if (!body) return <>{fallback ?? null}</>;
  return (
    <svg className="tv-ico" width={size} height={size} viewBox="0 0 28 28" aria-hidden="true">
      {body}
    </svg>
  );
}

/** A drawing tool's icon: its own, its group's, else its glyph. */
export function ToolIcon({ id, group, glyph, size }: { id: string; group?: string; glyph: ReactNode; size?: number }) {
  const name = TOOL_ICON[id] ?? (group ? GROUP_ICON[group] : undefined) ?? "";
  return <TvIcon name={name} fallback={glyph} size={size} />;
}
