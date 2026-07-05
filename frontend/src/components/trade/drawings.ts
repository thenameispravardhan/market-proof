// drawings — the chart drawing layer for ChartPanel.
//
// All drawings (trend lines, rays, rectangles, fib retracements,
// horizontal/vertical lines, text notes) live in a plain array owned by
// ChartPanel and are painted by DrawingsPrimitive, a lightweight-charts
// series primitive attached to the main series. The primitive re-reads
// state through a DrawingDeps accessor on every paint, so pan/zoom and
// store mutations both repaint correctly; ChartPanel calls
// `requestUpdate()` after mutating the store.
//
// Coordinates: points are stored as {time, price} where `time` is the
// chart time (IST-shifted epoch seconds — the same convention as the
// candle data), so drawings survive timeframe switches and reloads.
// ChartPanel supplies timeToX (fractional-logical interpolation, so a
// 5m anchor still lands correctly on a 1D chart) and priceToY.

import type {
  IPrimitivePaneView,
  ISeriesPrimitive,
  SeriesAttachedParameter,
  Time,
} from "lightweight-charts";

export type DrawingType =
  | "trend"
  | "ray"
  | "hline"
  | "vline"
  | "rect"
  | "fib"
  | "text";

export interface DrawingPoint {
  time: number; // chart time (IST-shifted epoch seconds)
  price: number;
}

export interface Drawing {
  id: string;
  type: DrawingType;
  points: DrawingPoint[]; // 1 point (hline/vline/text) or 2 (others)
  text?: string; // only for "text"
}

/** How many anchor points a tool needs before the drawing is complete. */
export function pointsNeeded(type: DrawingType): 1 | 2 {
  return type === "hline" || type === "vline" || type === "text" ? 1 : 2;
}

export const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1] as const;

// TradingView-ish per-level colors (0 → 1).
export const FIB_COLORS = [
  "#787B86",
  "#F23645",
  "#FF9800",
  "#4CAF50",
  "#089981",
  "#00BCD4",
  "#787B86",
];

export interface DrawingDeps {
  drawings: () => Drawing[];
  /** In-progress drawing preview: first anchor + current cursor. */
  pending: () => { type: DrawingType; from: DrawingPoint; cursor: DrawingPoint | null } | null;
  selectedId: () => string | null;
  timeToX: (t: number) => number | null;
  priceToY: (p: number) => number | null;
  lineColor: () => string;
  selectedColor: () => string;
  fillAlpha: () => string; // rgba() used for rect/fib fills
  priceFormatter: (p: number) => string;
}

interface MediaScope {
  context: CanvasRenderingContext2D;
  mediaSize: { width: number; height: number };
}

/** Resolve a drawing's anchor points to pixel coords (null if offscale). */
function toXY(
  d: DrawingPoint,
  deps: DrawingDeps,
): { x: number | null; y: number | null } {
  return { x: deps.timeToX(d.time), y: deps.priceToY(d.price) };
}

function drawSegment(
  ctx: CanvasRenderingContext2D,
  x1: number,
  y1: number,
  x2: number,
  y2: number,
): void {
  ctx.beginPath();
  ctx.moveTo(x1, y1);
  ctx.lineTo(x2, y2);
  ctx.stroke();
}

function drawHandle(ctx: CanvasRenderingContext2D, x: number, y: number, color: string): void {
  ctx.save();
  ctx.fillStyle = color;
  ctx.strokeStyle = "#0A0A0A";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.arc(x, y, 4, 0, Math.PI * 2);
  ctx.fill();
  ctx.stroke();
  ctx.restore();
}

/** Extend the (x1,y1)→(x2,y2) direction from (x2,y2) to the pane edge. */
function rayEnd(
  x1: number,
  y1: number,
  x2: number,
  y2: number,
  width: number,
  height: number,
): { x: number; y: number } {
  const dx = x2 - x1;
  const dy = y2 - y1;
  if (dx === 0 && dy === 0) return { x: x2, y: y2 };
  // Scale the direction vector until it leaves the pane bounds.
  let t = Infinity;
  if (dx > 0) t = Math.min(t, (width + 10 - x2) / dx);
  else if (dx < 0) t = Math.min(t, (-10 - x2) / dx);
  if (dy > 0) t = Math.min(t, (height + 10 - y2) / dy);
  else if (dy < 0) t = Math.min(t, (-10 - y2) / dy);
  if (!Number.isFinite(t) || t < 0) t = 0;
  return { x: x2 + dx * t, y: y2 + dy * t };
}

function paintOne(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
  d: Drawing,
  deps: DrawingDeps,
  selected: boolean,
): void {
  const color = selected ? deps.selectedColor() : deps.lineColor();
  ctx.save();
  ctx.strokeStyle = color;
  ctx.fillStyle = color;
  ctx.lineWidth = selected ? 2 : 1;
  ctx.font = "10px monospace";

  const p0 = toXY(d.points[0], deps);
  const p1 = d.points.length > 1 ? toXY(d.points[1], deps) : null;

  switch (d.type) {
    case "hline": {
      if (p0.y === null) break;
      ctx.setLineDash([5, 4]);
      drawSegment(ctx, 0, p0.y, width, p0.y);
      ctx.setLineDash([]);
      const label = deps.priceFormatter(d.points[0].price);
      ctx.textAlign = "right";
      ctx.fillText(label, width - 4, p0.y - 4);
      if (selected && p0.x !== null) drawHandle(ctx, p0.x, p0.y, color);
      break;
    }
    case "vline": {
      if (p0.x === null) break;
      ctx.setLineDash([5, 4]);
      drawSegment(ctx, p0.x, 0, p0.x, height);
      ctx.setLineDash([]);
      if (selected && p0.y !== null) drawHandle(ctx, p0.x, p0.y, color);
      break;
    }
    case "trend":
    case "ray": {
      if (!p1 || p0.x === null || p0.y === null || p1.x === null || p1.y === null) break;
      let ex = p1.x;
      let ey = p1.y;
      if (d.type === "ray") {
        const e = rayEnd(p0.x, p0.y, p1.x, p1.y, width, height);
        ex = e.x;
        ey = e.y;
      }
      drawSegment(ctx, p0.x, p0.y, ex, ey);
      if (selected) {
        drawHandle(ctx, p0.x, p0.y, color);
        drawHandle(ctx, p1.x, p1.y, color);
      }
      break;
    }
    case "rect": {
      if (!p1 || p0.x === null || p0.y === null || p1.x === null || p1.y === null) break;
      const x = Math.min(p0.x, p1.x);
      const y = Math.min(p0.y, p1.y);
      const w = Math.abs(p1.x - p0.x);
      const h = Math.abs(p1.y - p0.y);
      ctx.save();
      ctx.fillStyle = deps.fillAlpha();
      ctx.fillRect(x, y, w, h);
      ctx.restore();
      ctx.strokeRect(x, y, w, h);
      if (selected) {
        drawHandle(ctx, p0.x, p0.y, color);
        drawHandle(ctx, p1.x, p1.y, color);
      }
      break;
    }
    case "fib": {
      if (!p1 || p0.x === null || p1.x === null) break;
      const price0 = d.points[0].price;
      const price1 = d.points[1].price;
      const xL = Math.min(p0.x, p1.x);
      const xR = Math.max(p0.x, p1.x);
      let prevY: number | null = null;
      for (let li = 0; li < FIB_LEVELS.length; li++) {
        const level = FIB_LEVELS[li];
        // 0 anchors at the SECOND click's price, 1 at the first — the
        // retracement runs back from the end of the move.
        const price = price1 + (price0 - price1) * level;
        const y = deps.priceToY(price);
        if (y === null) {
          prevY = null;
          continue;
        }
        ctx.save();
        ctx.strokeStyle = selected ? color : FIB_COLORS[li];
        ctx.fillStyle = selected ? color : FIB_COLORS[li];
        drawSegment(ctx, xL, y, xR, y);
        ctx.textAlign = "left";
        ctx.fillText(
          `${level} ${deps.priceFormatter(price)}`,
          xL + 2,
          y - 3,
        );
        ctx.restore();
        if (prevY !== null) {
          ctx.save();
          ctx.globalAlpha = 0.07;
          ctx.fillStyle = FIB_COLORS[li];
          ctx.fillRect(xL, Math.min(prevY, y), xR - xL, Math.abs(y - prevY));
          ctx.restore();
        }
        prevY = y;
      }
      if (selected && p0.y !== null && p1.y !== null) {
        drawHandle(ctx, p0.x, p0.y, color);
        drawHandle(ctx, p1.x, p1.y, color);
      }
      break;
    }
    case "text": {
      if (p0.x === null || p0.y === null || !d.text) break;
      ctx.font = "11px monospace";
      const metrics = ctx.measureText(d.text);
      const pad = 4;
      ctx.save();
      ctx.globalAlpha = 0.85;
      ctx.fillStyle = "#1F1F1F";
      ctx.fillRect(p0.x - pad, p0.y - 14, metrics.width + pad * 2, 18);
      ctx.restore();
      ctx.strokeRect(p0.x - pad, p0.y - 14, metrics.width + pad * 2, 18);
      ctx.textAlign = "left";
      ctx.fillText(d.text, p0.x, p0.y);
      if (selected) drawHandle(ctx, p0.x, p0.y, color);
      break;
    }
  }
  ctx.restore();
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
            const { context, mediaSize } = scope;
            const deps = this._deps;
            const selected = deps.selectedId();
            for (const d of deps.drawings()) {
              paintOne(context, mediaSize.width, mediaSize.height, d, deps, d.id === selected);
            }
            const pending = deps.pending();
            if (pending && pending.cursor) {
              // Ghost preview of the in-progress two-click drawing.
              context.save();
              context.globalAlpha = 0.6;
              paintOne(
                context,
                mediaSize.width,
                mediaSize.height,
                {
                  id: "__pending__",
                  type: pending.type,
                  points: [pending.from, pending.cursor],
                },
                deps,
                false,
              );
              context.restore();
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

// ---- hit testing ----------------------------------------------------------

function distToSegment(
  px: number,
  py: number,
  x1: number,
  y1: number,
  x2: number,
  y2: number,
): number {
  const dx = x2 - x1;
  const dy = y2 - y1;
  const lenSq = dx * dx + dy * dy;
  let t = lenSq > 0 ? ((px - x1) * dx + (py - y1) * dy) / lenSq : 0;
  t = Math.max(0, Math.min(1, t));
  const cx = x1 + t * dx;
  const cy = y1 + t * dy;
  return Math.hypot(px - cx, py - cy);
}

const HIT_PX = 7;
const HANDLE_PX = 9;

/** Index of the anchor handle at (x, y), or null. Handles are only
 *  rendered for the selected drawing, so callers should gate on that. */
export function hitHandle(
  d: Drawing,
  x: number,
  y: number,
  deps: DrawingDeps,
): number | null {
  for (let i = 0; i < d.points.length; i++) {
    const px = deps.timeToX(d.points[i].time);
    const py = deps.priceToY(d.points[i].price);
    if (
      px !== null &&
      py !== null &&
      Math.abs(x - px) <= HANDLE_PX &&
      Math.abs(y - py) <= HANDLE_PX
    ) {
      return i;
    }
  }
  return null;
}

/** Does (x, y) hit the drawing? Uses the same projection as painting. */
export function hitTest(
  d: Drawing,
  x: number,
  y: number,
  deps: DrawingDeps,
  width: number,
  height: number,
): boolean {
  const p0 = toXY(d.points[0], deps);
  const p1 = d.points.length > 1 ? toXY(d.points[1], deps) : null;
  switch (d.type) {
    case "hline":
      return p0.y !== null && Math.abs(y - p0.y) <= HIT_PX;
    case "vline":
      return p0.x !== null && Math.abs(x - p0.x) <= HIT_PX;
    case "trend": {
      if (!p1 || p0.x === null || p0.y === null || p1.x === null || p1.y === null) return false;
      return distToSegment(x, y, p0.x, p0.y, p1.x, p1.y) <= HIT_PX;
    }
    case "ray": {
      if (!p1 || p0.x === null || p0.y === null || p1.x === null || p1.y === null) return false;
      const e = rayEnd(p0.x, p0.y, p1.x, p1.y, width, height);
      return distToSegment(x, y, p0.x, p0.y, e.x, e.y) <= HIT_PX;
    }
    case "rect": {
      if (!p1 || p0.x === null || p0.y === null || p1.x === null || p1.y === null) return false;
      const xMin = Math.min(p0.x, p1.x);
      const xMax = Math.max(p0.x, p1.x);
      const yMin = Math.min(p0.y, p1.y);
      const yMax = Math.max(p0.y, p1.y);
      const nearX = x >= xMin - HIT_PX && x <= xMax + HIT_PX;
      const nearY = y >= yMin - HIT_PX && y <= yMax + HIT_PX;
      if (!nearX || !nearY) return false;
      // border proximity OR inside
      const onBorder =
        Math.abs(x - xMin) <= HIT_PX ||
        Math.abs(x - xMax) <= HIT_PX ||
        Math.abs(y - yMin) <= HIT_PX ||
        Math.abs(y - yMax) <= HIT_PX;
      const inside = x > xMin && x < xMax && y > yMin && y < yMax;
      return onBorder || inside;
    }
    case "fib": {
      if (!p1 || p0.x === null || p1.x === null) return false;
      const xL = Math.min(p0.x, p1.x) - HIT_PX;
      const xR = Math.max(p0.x, p1.x) + HIT_PX;
      if (x < xL || x > xR) return false;
      const price0 = d.points[0].price;
      const price1 = d.points[1].price;
      for (const level of FIB_LEVELS) {
        const yl = deps.priceToY(price1 + (price0 - price1) * level);
        if (yl !== null && Math.abs(y - yl) <= HIT_PX) return true;
      }
      return false;
    }
    case "text": {
      if (p0.x === null || p0.y === null) return false;
      const w = (d.text?.length ?? 1) * 7 + 8;
      return x >= p0.x - 6 && x <= p0.x + w && y >= p0.y - 16 && y <= p0.y + 6;
    }
  }
}

let idCounter = 0;

/** Cheap unique id for a new drawing. */
export function newDrawingId(): string {
  idCounter += 1;
  return `d${Date.now().toString(36)}_${idCounter}`;
}
