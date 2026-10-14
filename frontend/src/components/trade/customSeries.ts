// customSeries — chart styles lightweight-charts doesn't ship, as one
// custom series: volume candles (body width ∝ volume), HLC bars, High-low
// bars, HLC area, Kagi lines, Point & figure columns, and the order-flow
// footprint (sell | buy volume per price beside a thin candle, Fyers-style).

import type {
  CustomData,
  CustomSeriesOptions,
  CustomSeriesPricePlotValues,
  CustomSeriesWhitespaceData,
  ICustomSeriesPaneRenderer,
  ICustomSeriesPaneView,
  PaneRendererCustomData,
  PriceToCoordinateConverter,
  Time,
} from "lightweight-charts";

export type ShapeMode = "volcandles" | "hlc" | "highlow" | "hlcarea" | "kagi" | "pnf" | "footprint";

export interface OhlcItem extends CustomData<Time> {
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  flag?: number;
  /** Footprint: [price, buy, sell] per traded price, from recorded ticks. */
  fp?: [number, number, number][];
  /** Session-cumulative delta up to and including this bar. */
  cd?: number;
}

/** 1 / 2 / 2.5 / 5 × 10^k at or above `x`: a readable footprint row height in price. */
export function niceStep(x: number): number {
  if (!(x > 0)) return 0.05;
  const e = Math.pow(10, Math.floor(Math.log10(x)));
  return ([1, 2, 2.5, 5, 10].find((m) => m * e >= x - 1e-12) ?? 10) * e;
}

/** Group a bar's per-price buy / sell into rows of `step`, lowest first. */
export function footprintRows(fp: [number, number, number][], step: number): { p: number; buy: number; sell: number }[] {
  const rows = new Map<number, { p: number; buy: number; sell: number }>();
  for (const [price, buy, sell] of fp) {
    const p = Math.round(Math.floor(price / step + 1e-9) * step * 1e6) / 1e6;
    const r = rows.get(p) ?? { p, buy: 0, sell: 0 };
    r.buy += buy;
    r.sell += sell;
    rows.set(p, r);
  }
  return [...rows.values()].sort((a, b) => a.p - b.p);
}

export const fmtK = (v: number): string => {
  const a = Math.abs(v);
  return a >= 1e7 ? `${(v / 1e7).toFixed(2)}Cr` : a >= 1e5 ? `${(v / 1e5).toFixed(2)}L` : a >= 1e3 ? `${(v / 1e3).toFixed(a >= 1e4 ? 1 : 2)}K` : String(Math.round(v));
};

export interface ShapeSeriesOptions extends CustomSeriesOptions {
  mode: ShapeMode;
  upColor: string;
  downColor: string;
  /** Point & figure box size. */
  box: number;
}

interface MediaScope {
  context: CanvasRenderingContext2D;
  mediaSize: { width: number; height: number };
}

function alpha(hex: string, a: number): string {
  const m = /^#([0-9a-f]{6})$/i.exec((hex || "").trim());
  if (!m) return hex;
  const h = m[1];
  return `rgba(${parseInt(h.slice(0, 2), 16)},${parseInt(h.slice(2, 4), 16)},${parseInt(h.slice(4, 6), 16)},${a})`;
}

class ShapeRenderer implements ICustomSeriesPaneRenderer {
  data: PaneRendererCustomData<Time, OhlcItem> | null = null;
  opts: ShapeSeriesOptions | null = null;

  draw(target: { useMediaCoordinateSpace: (f: (s: MediaScope) => void) => void }, toY: PriceToCoordinateConverter): void {
    const data = this.data;
    const o = this.opts;
    if (!data || !o || !data.visibleRange) return;
    target.useMediaCoordinateSpace(({ context: ctx }: MediaScope) => {
      const { from, to } = data.visibleRange!;
      const bars = data.bars;
      const bs = data.barSpacing * (data.conflationFactor || 1);
      const Y = (p: number) => toY(p) as number | null;
      const lo = Math.max(0, from - 1);
      const hi = Math.min(bars.length, to + 1);
      const colorOf = (d: OhlcItem) => (d.close >= d.open ? o.upColor : o.downColor);
      if (o.mode === "hlcarea") {
        const pts = bars.slice(lo, hi).map((b) => ({ x: b.x, h: Y(b.originalData.high), l: Y(b.originalData.low), c: Y(b.originalData.close) }))
          .filter((p) => p.h !== null && p.l !== null && p.c !== null) as { x: number; h: number; l: number; c: number }[];
        if (pts.length < 2) return;
        const band = (top: "h" | "c", bot: "c" | "l", color: string) => {
          ctx.beginPath();
          pts.forEach((p, i) => (i ? ctx.lineTo(p.x, p[top]) : ctx.moveTo(p.x, p[top])));
          for (let i = pts.length - 1; i >= 0; i--) ctx.lineTo(pts[i].x, pts[i][bot]);
          ctx.closePath();
          ctx.fillStyle = color;
          ctx.fill();
        };
        band("h", "c", alpha(o.upColor, 0.18));
        band("c", "l", alpha(o.downColor, 0.18));
        const line = (k: "h" | "l" | "c", color: string, w: number) => {
          ctx.beginPath();
          pts.forEach((p, i) => (i ? ctx.lineTo(p.x, p[k]) : ctx.moveTo(p.x, p[k])));
          ctx.strokeStyle = color;
          ctx.lineWidth = w;
          ctx.stroke();
        };
        line("h", alpha(o.upColor, 0.7), 1);
        line("l", alpha(o.downColor, 0.7), 1);
        line("c", "#9E9E9E", 2);
        return;
      }
      if (o.mode === "kagi") {
        let prev: { x: number; y: number } | null = null;
        for (let i = lo; i < hi; i++) {
          const b = bars[i];
          const d = b.originalData;
          const y0 = Y(d.open), y1 = Y(d.close);
          if (y0 === null || y1 === null) continue;
          const thick = d.flag === 1;
          ctx.strokeStyle = thick ? o.upColor : o.downColor;
          ctx.lineWidth = thick ? 3 : 1;
          ctx.beginPath();
          if (prev) {
            ctx.moveTo(prev.x, y0);
            ctx.lineTo(b.x, y0);
          }
          ctx.moveTo(b.x, y0);
          ctx.lineTo(b.x, y1);
          ctx.stroke();
          prev = { x: b.x, y: y1 };
        }
        return;
      }
      if (o.mode === "footprint") {
        this.footprint(ctx, bars, lo, hi, bs, Y);
        return;
      }
      let maxVol = 1;
      if (o.mode === "volcandles") for (let i = lo; i < hi; i++) maxVol = Math.max(maxVol, bars[i].originalData.volume || 0);
      for (let i = lo; i < hi; i++) {
        const b = bars[i];
        const d = b.originalData;
        const yh = Y(d.high), yl = Y(d.low), yo = Y(d.open), yc = Y(d.close);
        if (yh === null || yl === null || yo === null || yc === null) continue;
        const color = colorOf(d);
        ctx.strokeStyle = color;
        ctx.fillStyle = color;
        ctx.lineWidth = 1;
        if (o.mode === "volcandles") {
          const w = Math.max(1, bs * 0.9 * (0.15 + 0.85 * Math.sqrt((d.volume || 0) / maxVol)));
          ctx.beginPath();
          ctx.moveTo(b.x, yh);
          ctx.lineTo(b.x, yl);
          ctx.stroke();
          ctx.fillRect(b.x - w / 2, Math.min(yo, yc), w, Math.max(1, Math.abs(yc - yo)));
        } else if (o.mode === "hlc") {
          const w = Math.max(1, Math.min(4, bs * 0.15));
          ctx.lineWidth = Math.max(1, Math.min(2, bs * 0.1));
          ctx.beginPath();
          ctx.moveTo(b.x, yh);
          ctx.lineTo(b.x, yl);
          ctx.moveTo(b.x, yc);
          ctx.lineTo(b.x + w + bs * 0.25, yc);
          ctx.stroke();
        } else if (o.mode === "highlow") {
          const w = Math.max(1, bs * 0.35);
          ctx.fillRect(b.x - w / 2, yh, w, Math.max(1, yl - yh));
        } else if (o.mode === "pnf") {
          const box = o.box > 0 ? o.box : 1;
          const n = Math.max(1, Math.round((d.high - d.low) / box));
          const w = Math.max(3, bs * 0.8);
          ctx.lineWidth = Math.max(1, Math.min(2, w / 8));
          for (let k = 0; k < n; k++) {
            const t = Y(d.low + (k + 1) * box), btm = Y(d.low + k * box);
            if (t === null || btm === null) continue;
            const hgt = Math.abs(btm - t);
            const pad = Math.min(hgt, w) * 0.12;
            if (d.flag === 1) {
              ctx.strokeStyle = o.upColor;
              ctx.beginPath();
              ctx.moveTo(b.x - w / 2 + pad, t + pad);
              ctx.lineTo(b.x + w / 2 - pad, btm - pad);
              ctx.moveTo(b.x + w / 2 - pad, t + pad);
              ctx.lineTo(b.x - w / 2 + pad, btm - pad);
              ctx.stroke();
            } else {
              ctx.strokeStyle = o.downColor;
              ctx.beginPath();
              ctx.ellipse(b.x, (t + btm) / 2, Math.max(1, w / 2 - pad), Math.max(1, hgt / 2 - pad), 0, 0, Math.PI * 2);
              ctx.stroke();
            }
          }
        }
      }
    });
  }

  /** Order-flow footprint: a thin candle, sell volume per price row on its
   *  left (red), buy on its right (green), shade ∝ the row's share of the
   *  bar's busiest row; Δ and cumulative Δ above, sell / buy totals below.
   *  A bar without recorded ticks is just the thin candle. */
  private footprint(ctx: CanvasRenderingContext2D, bars: PaneRendererCustomData<Time, OhlcItem>["bars"], lo: number, hi: number, bs: number, Y: (p: number) => number | null): void {
    const o = this.opts!;
    // Row height: ≥ 14 px of price, never finer than the data's own levels.
    let pxPer = 0;
    for (let i = lo; i < hi && !pxPer; i++) {
      const d = bars[i].originalData;
      const a = Y(d.high), b = Y(d.low);
      if (a !== null && b !== null && d.high > d.low) pxPer = Math.abs(b - a) / (d.high - d.low);
    }
    const step = niceStep(pxPer > 0 ? 14 / pxPer : 0.05);
    const bodyW = Math.max(2, Math.min(6, bs * 0.08));
    const cellW = Math.max(0, (bs * 0.94 - bodyW) / 2 - 2);
    const text = cellW >= 24;
    ctx.font = "10px sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    for (let i = lo; i < hi; i++) {
      const b = bars[i];
      const d = b.originalData;
      const yh = Y(d.high), yl = Y(d.low), yo = Y(d.open), yc = Y(d.close);
      if (yh === null || yl === null || yo === null || yc === null) continue;
      const color = d.close >= d.open ? o.upColor : o.downColor;
      ctx.fillStyle = color;
      ctx.fillRect(b.x - 0.5, yh, 1, Math.max(1, yl - yh));
      ctx.fillRect(b.x - bodyW / 2, Math.min(yo, yc), bodyW, Math.max(1, Math.abs(yc - yo)));
      if (!d.fp?.length || cellW < 4) continue;
      const rows = footprintRows(d.fp, step);
      const max = Math.max(1, ...rows.map((r) => Math.max(r.buy, r.sell)));
      let buy = 0, sell = 0;
      for (const r of rows) {
        buy += r.buy;
        sell += r.sell;
        const yt = Y(r.p + step), yb = Y(r.p);
        if (yt === null || yb === null) continue;
        const y = Math.min(yt, yb) + 0.5, h = Math.max(1, Math.abs(yb - yt) - 1);
        const cell = (x: number, v: number, c: string) => {
          ctx.fillStyle = alpha(c, 0.18 + 0.72 * (v / max));
          ctx.fillRect(x, y, cellW, h);
          if (text && h >= 9 && v > 0) {
            ctx.fillStyle = "#FFFFFF";
            ctx.fillText(fmtK(v), x + cellW / 2, y + h / 2);
          }
        };
        cell(b.x - bodyW / 2 - 1 - cellW, r.sell, o.downColor);
        cell(b.x + bodyW / 2 + 1, r.buy, o.upColor);
      }
      if (!text) continue;
      const delta = buy - sell;
      ctx.fillStyle = delta >= 0 ? o.upColor : o.downColor;
      ctx.fillText(fmtK(delta), b.x, yh - 20);
      if (d.cd !== undefined) {
        ctx.fillStyle = d.cd >= 0 ? o.upColor : o.downColor;
        ctx.fillText(fmtK(d.cd), b.x, yh - 8);
      }
      ctx.fillStyle = o.downColor;
      ctx.fillText(fmtK(sell), b.x, yl + 9);
      ctx.fillStyle = o.upColor;
      ctx.fillText(fmtK(buy), b.x, yl + 21);
    }
  }
}

export class ShapeSeries implements ICustomSeriesPaneView<Time, OhlcItem, ShapeSeriesOptions> {
  private _r = new ShapeRenderer();

  renderer(): ICustomSeriesPaneRenderer {
    return this._r as unknown as ICustomSeriesPaneRenderer;
  }

  update(data: PaneRendererCustomData<Time, OhlcItem>, opts: ShapeSeriesOptions): void {
    this._r.data = data;
    this._r.opts = opts;
  }

  priceValueBuilder(d: OhlcItem): CustomSeriesPricePlotValues {
    return [d.low, d.high, d.close];
  }

  isWhitespace(d: OhlcItem | CustomSeriesWhitespaceData<Time>): d is CustomSeriesWhitespaceData<Time> {
    return (d as OhlcItem).close === undefined;
  }

  defaultOptions(): ShapeSeriesOptions {
    return {
      color: "#26A69A",
      lastValueVisible: true,
      title: "",
      visible: true,
      priceLineVisible: true,
      priceLineSource: 0,
      priceLineWidth: 1,
      priceLineColor: "",
      priceLineStyle: 2,
      priceFormat: { type: "price", precision: 2, minMove: 0.01 },
      baseLineVisible: false,
      baseLineColor: "#B2B5BE",
      baseLineWidth: 1,
      baseLineStyle: 0,
      mode: "volcandles",
      upColor: "#26A69A",
      downColor: "#EF5350",
      box: 1,
    } as unknown as ShapeSeriesOptions;
  }
}
