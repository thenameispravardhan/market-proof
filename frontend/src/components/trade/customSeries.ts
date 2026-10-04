// customSeries — chart styles lightweight-charts doesn't ship, as one
// custom series: volume candles (body width ∝ volume), HLC bars, High-low
// bars, HLC area, Kagi lines and Point & figure columns.

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

export type ShapeMode = "volcandles" | "hlc" | "highlow" | "hlcarea" | "kagi" | "pnf";

export interface OhlcItem extends CustomData<Time> {
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  flag?: number;
}

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
