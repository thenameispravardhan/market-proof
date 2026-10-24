// customSeries — chart styles lightweight-charts doesn't ship, as one
// custom series: volume candles (body width ∝ volume), HLC bars, High-low
// bars, HLC area, Kagi lines, Point & figure columns, and the order-flow
// footprint (sell | buy volume per price beside a thin candle, Fyers-style).

import { analyze, barSignals, cellLayout, developing, fpSettings, groupRows, heatColor, nakedPocs, rowStep, STAT_LABEL, STAT_ROW_PX, tpoRows, type FootprintSettings, type FpBarStats, type FpRow, type StatRow } from "./footprint";
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
  /** The bar's order-flow statistics (the table at the pane's foot). */
  st?: FpBarStats;
  /** Big prints: [price, qty, side]. */
  prints?: [number, number, number][];
}

/** 1 / 2 / 2.5 / 5 × 10^k at or above `x`: a readable footprint row height in price. */
export function niceStep(x: number): number {
  if (!(x > 0)) return 0.05;
  const e = Math.pow(10, Math.floor(Math.log10(x)));
  return ([1, 2, 2.5, 5, 10].find((m) => m * e >= x - 1e-12) ?? 10) * e;
}

/** Group a bar's per-price buy / sell into rows of `step`, lowest first. */
export function footprintRows(fp: [number, number, number][], step: number): { p: number; buy: number; sell: number }[] {
  return groupRows(fp, step);
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
  /** Footprint: its settings, the instrument's tick and colours. */
  fp?: Partial<FootprintSettings>;
  tick?: number;
  textColor?: string;
  pocColor?: string;
}

interface MediaScope {
  context: CanvasRenderingContext2D;
  mediaSize: { width: number; height: number };
}

function alpha(color: string, a: number): string {
  const c = (color || "").trim();
  const m = /^#([0-9a-f]{6})$/i.exec(c);
  if (m) {
    const h = m[1];
    return `rgba(${parseInt(h.slice(0, 2), 16)},${parseInt(h.slice(2, 4), 16)},${parseInt(h.slice(4, 6), 16)},${a})`;
  }
  const r = /^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)/i.exec(c);
  return r ? `rgba(${r[1]},${r[2]},${r[3]},${a})` : c;
}

class ShapeRenderer implements ICustomSeriesPaneRenderer {
  data: PaneRendererCustomData<Time, OhlcItem> | null = null;
  opts: ShapeSeriesOptions | null = null;

  draw(target: { useMediaCoordinateSpace: (f: (s: MediaScope) => void) => void }, toY: PriceToCoordinateConverter): void {
    const data = this.data;
    const o = this.opts;
    if (!data || !o || !data.visibleRange) return;
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }: MediaScope) => {
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
        this.footprint(ctx, bars, lo, hi, bs, Y, mediaSize.width, mediaSize.height);
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

  /** Order-flow footprint, the ATAS / TradingView way: a thin candle on the left
   *  of each bar, its traded volume per price row beside it (bid × ask, delta or
   *  volume) filling the candle's high-low exactly, diagonal imbalances in bold,
   *  the POC boxed, the value area as a bar at the cells' left edge,
   *  stacked-imbalance zones extended to the right until price trades back into
   *  them, absorption / delta-divergence markers, big prints as bubbles, the
   *  visible bars' volume profile on the right and a statistics table (volume,
   *  delta, delta %, CVD …) along the foot of the pane. */
  private footprint(ctx: CanvasRenderingContext2D, bars: PaneRendererCustomData<Time, OhlcItem>["bars"], lo: number, hi: number, bs: number, Y: (p: number) => number | null, paneW: number, paneH: number): void {
    const o = this.opts!;
    const fs = fpSettings(o.fp);
    const tick = o.tick && o.tick > 0 ? o.tick : 0.05;
    const up = o.upColor, down = o.downColor, text = o.textColor || "#D1D4DC", poc = o.pocColor || "#FFB300";
    let pxPer = 0;
    for (let i = lo; i < hi && !pxPer; i++) {
      const d = bars[i].originalData;
      const a = Y(d.high), b = Y(d.low);
      if (a !== null && b !== null && d.high > d.low) pxPer = Math.abs(b - a) / (d.high - d.low);
    }
    const step = rowStep(tick, pxPer, fs.rowTicks);
    const half = Math.max(1, bs * 0.47);
    const statH = STAT_ROW_PX;
    const statsTop = fs.stats.length && bs >= 2 ? paneH - fs.stats.length * statH - 2 : paneH;
    const fits = (t: string, w: number) => ctx.measureText(t).width <= w - 3;
    ctx.textBaseline = "middle";
    // statistics: one pass for the scale of each row (cell shade ∝ the visible max)
    const maxVis: Record<string, number> = {};
    for (let i = lo; i < hi; i++) {
      const st = bars[i].originalData.st;
      if (!st) continue;
      for (const k of fs.stats) maxVis[k] = Math.max(maxVis[k] ?? 0, Math.abs(statValue(k, st)));
    }
    const clamp = (d: OhlcItem) => (d.fp ?? []).map(([p, b2, s2]) => [Math.min(d.high, Math.max(d.low, p)), b2, s2] as [number, number, number]);
    // a profile's rows: the footprint's levels, or time at price from the candles (tpo)
    const rowsOf = (idx: number[]) => (fs.profileType === "tpo"
      ? tpoRows(idx.map((i) => bars[i].originalData), step)
      : groupRows(idx.flatMap((i) => clamp(bars[i].originalData)), step));
    const range = (a: number, b: number) => Array.from({ length: Math.max(0, b - a) }, (_, k) => a + k);
    if (fs.profile && fs.profileScope === "session") this.sessionProfiles(ctx, bars, lo, hi, step, tick, Y, half, up, down, poc, fs, rowsOf);
    else if (fs.profile && fs.profileScope === "composite") {
      this.profile(ctx, rowsOf(lastSessions(bars, hi, fs.compositeDays)), step, tick, Y, paneW, up, down, poc, fs.vaPct, fs.profileType,
        `${fs.compositeDays}-day POC`);
    } else if (fs.profile) this.profile(ctx, rowsOf(range(lo, hi)), step, tick, Y, paneW, up, down, poc, fs.vaPct, fs.profileType,
      fs.profileType === "tpo" ? "TPO POC" : "VPOC");
    if (fs.devLines) this.developingLines(ctx, bars, lo, hi, step, tick, Y, half, poc, fs, rowsOf);
    // heatmap: every cell against the busiest cell on screen, so heavy prices stand out ACROSS bars
    let heatMax = 0;
    if (fs.mode === "heatmap") {
      for (let i = lo; i < hi; i++) {
        for (const r of groupRows(clamp(bars[i].originalData), step)) heatMax = Math.max(heatMax, r.buy + r.sell);
      }
    }
    const pocs: [number, number, number | null][] = [];      // [high, low, POC row] per bar from `first`, for naked POCs
    const pocAt: { x: number; y: number; i: number }[] = [];
    const zones: { y0: number; y1: number; lo: number; hi: number; side: 1 | -1; x: number; i: number }[] = [];
    const bubbles: { x: number; y: number; q: number; side: number }[] = [];
    const marks: { x: number; y: number; side: 1 | -1; label: string }[] = [];
    let maxPrint = 0;
    const flatPx = Math.max(6, Math.min(16, step * pxPer || 12));
    const first = Math.max(0, lo - 150);              // zones that started off-screen still reach in
    for (let i = first; i < hi; i++) {
      const b = bars[i];
      const d = b.originalData;
      const visible = i >= lo;
      const yh = Y(d.high), yl = Y(d.low), yo = Y(d.open), yc = Y(d.close);
      if (yh === null || yl === null || yo === null || yc === null) continue;
      const left = b.x - half, right = b.x + half;
      const candleX = bs >= 24 ? left + 3 : b.x;
      const color = d.close >= d.open ? up : down;
      if (visible) {
        ctx.fillStyle = color;
        ctx.fillRect(candleX - 0.5, yh, 1, Math.max(1, yl - yh));
        const bodyW = bs >= 24 ? 3 : Math.max(1, Math.min(6, bs * 0.6));
        ctx.fillRect(candleX - bodyW / 2, Math.min(yo, yc), bodyW, Math.max(1, Math.abs(yc - yo)));
      }
      if (!d.fp?.length) {
        pocs.push([d.high, d.low, null]);
        pocAt.push({ x: right, y: 0, i });
        if (visible) this.statsCells(ctx, d, b.x, half, statsTop, statH, fs, maxVis, up, down, text);
        continue;
      }
      const a = analyze(groupRows(clamp(d), step), step, fs);
      const g = cellLayout(d.low, d.high, step, yh, yl, flatPx);
      for (const z of a.zones) zones.push({ ...z, y0: g.box(round6(z.hi - step))[0], y1: g.box(z.lo)[1], x: right, i });
      pocs.push([d.high, d.low, a.poc]);
      if (a.poc !== null) {
        const [pt, pb] = g.box(a.poc);
        pocAt.push({ x: right, y: (pt + pb) / 2, i });
      } else pocAt.push({ x: right, y: 0, i });
      if (!visible) continue;
      const cx0 = bs >= 24 ? left + 8 : left, cx1 = right - 1;
      const cw = cx1 - cx0;
      if (cw >= 6) {
        const font = g.rowPx >= 13 ? 11 : g.rowPx >= 10 ? 10 : 9;
        ctx.font = `${font}px sans-serif`;
        const showText = fs.numbers && g.rowPx >= 9;
        for (const r of a.rows) {
          const [yt, yb] = g.box(r.p);
          const y = yt + 0.5, h = Math.max(1, yb - yt - 1);
          const delta = r.buy - r.sell, vol = r.buy + r.sell;
          const buyImb = a.buyImb.has(r.p), sellImb = a.sellImb.has(r.p);
          if (fs.mode === "bidask") {
            const w2 = cw / 2;
            ctx.fillStyle = alpha(down, 0.1 + 0.55 * (r.sell / Math.max(1, a.maxSide)));
            ctx.fillRect(cx0, y, w2 - 0.5, h);
            ctx.fillStyle = alpha(up, 0.1 + 0.55 * (r.buy / Math.max(1, a.maxSide)));
            ctx.fillRect(cx0 + w2 + 0.5, y, w2 - 0.5, h);
            if (sellImb) this.edge(ctx, cx0, y, w2 - 0.5, h, down);
            if (buyImb) this.edge(ctx, cx0 + w2 + 0.5, y, w2 - 0.5, h, up);
            if (showText) {
              const ts = fmtCell(r.sell), tb = fmtCell(r.buy);
              if (fits(ts, w2) && fits(tb, w2)) {
                ctx.textAlign = "right";
                ctx.font = `${sellImb ? "bold " : ""}${font}px sans-serif`;
                ctx.fillStyle = sellImb ? brighten(down) : text;
                ctx.fillText(ts, cx0 + w2 - 3, y + h / 2);
                ctx.textAlign = "left";
                ctx.font = `${buyImb ? "bold " : ""}${font}px sans-serif`;
                ctx.fillStyle = buyImb ? brighten(up) : text;
                ctx.fillText(tb, cx0 + w2 + 3, y + h / 2);
              }
            }
          } else if (fs.mode === "delta") {
            ctx.fillStyle = alpha(delta >= 0 ? up : down, 0.12 + 0.7 * (Math.abs(delta) / Math.max(1, a.maxAbsDelta)));
            ctx.fillRect(cx0, y, cw, h);
            if (buyImb || sellImb) this.edge(ctx, cx0, y, cw, h, buyImb ? up : down);
            const t = fmtCell(delta);
            if (showText && fits(t, cw)) {
              ctx.textAlign = "center";
              ctx.fillStyle = text;
              ctx.fillText(t, cx0 + cw / 2, y + h / 2);
            }
          } else if (fs.mode === "heatmap") {
            const t = vol / Math.max(1, heatMax);
            ctx.fillStyle = heatColor(t, 0.18 + 0.72 * Math.sqrt(t));
            ctx.fillRect(cx0, y, cw, h);
            if (buyImb || sellImb) this.edge(ctx, cx0, y, cw, h, buyImb ? up : down);
            const tv = fmtCell(vol);
            if (showText && fits(tv, cw)) {
              ctx.textAlign = "center";
              ctx.fillStyle = t > 0.55 ? "#111" : text;
              ctx.fillText(tv, cx0 + cw / 2, y + h / 2);
            }
          } else {
            const w = Math.max(1, cw * (vol / Math.max(1, a.maxVol)));
            ctx.fillStyle = alpha(delta >= 0 ? up : down, 0.35);
            ctx.fillRect(cx0, y, w, h);
            const t = fmtCell(vol);
            if (showText && fits(t, cw)) {
              ctx.textAlign = "left";
              ctx.fillStyle = text;
              ctx.fillText(t, cx0 + 3, y + h / 2);
            }
          }
          if (fs.poc && r.p === a.poc) {
            ctx.strokeStyle = poc;
            ctx.lineWidth = 1;
            ctx.strokeRect(cx0 + 0.5, y, cw - 1, h);
          }
        }
        if (fs.va && a.vah !== null && a.val !== null) {
          const ya = g.box(a.vah)[0], yb = g.box(a.val)[1];
          ctx.fillStyle = alpha(poc, 0.8);
          ctx.fillRect(cx0 - 3, ya, 2, yb - ya);
        }
        if (fs.unfinished) {                          // inside the extreme row, at its right edge
          ctx.fillStyle = text;
          if (a.unfinishedHigh) this.dot(ctx, cx1 - 3, g.box(a.rows[a.rows.length - 1].p)[0] + 3);
          if (a.unfinishedLow) this.dot(ctx, cx1 - 3, g.box(a.rows[0].p)[1] - 3);
        }
      }
      if (fs.signals) {
        const prev = i > 0 ? bars[i - 1].originalData : null;
        const sg = barSignals(a, d, prev && prev.high > 0 ? prev : null);
        const top = Math.min(yh, yl), bottom = Math.max(yh, yl);
        const bear = [sg.absorbHigh && "A", sg.divBear && "D"].filter(Boolean).join("");
        const bull = [sg.absorbLow && "A", sg.divBull && "D"].filter(Boolean).join("");
        if (bear) marks.push({ x: b.x, y: top - 8, side: -1, label: bear });
        if (bull) marks.push({ x: b.x, y: bottom + 8, side: 1, label: bull });
      }
      if (fs.prints) {
        for (const [p, q, side] of d.prints ?? []) {
          const [yt, yb] = g.box(g.rowOf(p));
          bubbles.push({ x: b.x, y: (yt + yb) / 2, q, side });
          maxPrint = Math.max(maxPrint, q);
        }
      }
      this.statsCells(ctx, d, b.x, half, statsTop, statH, fs, maxVis, up, down, text);
    }
    // naked POCs: a bar's POC no later bar traded through, drawn on to the right edge with its price
    if (fs.npoc && pocs.length) {
      ctx.save();
      ctx.strokeStyle = alpha(poc, 0.75);
      ctx.fillStyle = poc;
      ctx.lineWidth = 1;
      ctx.setLineDash([2, 3]);
      ctx.font = "9px sans-serif";
      ctx.textAlign = "right";
      ctx.textBaseline = "bottom";
      for (const [k, p] of nakedPocs(pocs, step)) {
        const at = pocAt[k];
        if (!at || at.i >= hi - 1) continue;              // the forming bar's POC still moves
        const y = Math.round(at.y) + 0.5;
        ctx.beginPath();
        ctx.moveTo(at.x, y);
        ctx.lineTo(paneW, y);
        ctx.stroke();
        ctx.fillText(`nPOC ${p.toFixed(2)}`, paneW - 4, y - 1);
      }
      ctx.restore();
      ctx.textBaseline = "middle";
    }
    // stacked imbalance zones: from their bar to the first later bar that trades back into them
    for (const z of zones) {
      let x1 = bars[hi - 1] ? bars[hi - 1].x + half : z.x;
      for (let j = z.i + 1; j < hi; j++) {
        const d = bars[j].originalData;
        if (d.low < z.hi && d.high > z.lo) {
          x1 = bars[j].x - half;
          break;
        }
      }
      if (x1 <= z.x) continue;
      ctx.fillStyle = alpha(z.side > 0 ? up : down, 0.16);
      ctx.fillRect(z.x, z.y0, x1 - z.x, Math.max(1, z.y1 - z.y0));
      ctx.fillStyle = alpha(z.side > 0 ? up : down, 0.7);
      ctx.fillRect(z.x, z.side > 0 ? z.y1 - 1 : z.y0, x1 - z.x, 1);
    }
    for (const bb of bubbles) {
      const r = 3 + 11 * Math.sqrt(bb.q / Math.max(1, maxPrint));
      ctx.beginPath();
      ctx.arc(bb.x, bb.y, r, 0, Math.PI * 2);
      ctx.fillStyle = alpha(bb.side >= 0 ? up : down, 0.35);
      ctx.fill();
      ctx.strokeStyle = alpha(bb.side >= 0 ? up : down, 0.9);
      ctx.lineWidth = 1;
      ctx.stroke();
    }
    // absorption (A) / divergence (D): bearish above the bar, bullish below
    ctx.font = "bold 9px sans-serif";
    ctx.textAlign = "center";
    for (const m of marks) {
      const c = m.side > 0 ? up : down;
      const w = 7 + 6 * m.label.length;
      ctx.fillStyle = alpha(c, 0.9);
      ctx.beginPath();
      if (m.side < 0) {                            // a tag pointing down at the high
        ctx.moveTo(m.x - w / 2, m.y - 6); ctx.lineTo(m.x + w / 2, m.y - 6); ctx.lineTo(m.x + w / 2, m.y + 2);
        ctx.lineTo(m.x + 3, m.y + 2); ctx.lineTo(m.x, m.y + 5); ctx.lineTo(m.x - 3, m.y + 2); ctx.lineTo(m.x - w / 2, m.y + 2);
      } else {
        ctx.moveTo(m.x - w / 2, m.y + 6); ctx.lineTo(m.x + w / 2, m.y + 6); ctx.lineTo(m.x + w / 2, m.y - 2);
        ctx.lineTo(m.x + 3, m.y - 2); ctx.lineTo(m.x, m.y - 5); ctx.lineTo(m.x - 3, m.y - 2); ctx.lineTo(m.x - w / 2, m.y - 2);
      }
      ctx.closePath();
      ctx.fill();
      ctx.fillStyle = "#fff";
      ctx.fillText(m.label, m.x, m.side < 0 ? m.y - 2 : m.y + 2);
    }
    // the statistics table's row names, on the pane's left edge
    if (statsTop < paneH && fs.stats.length) {
      ctx.font = "10px sans-serif";
      ctx.textAlign = "left";
      fs.stats.forEach((k, n) => {
        const y = statsTop + n * statH;
        const label = STAT_LABEL[k];
        const w = ctx.measureText(label).width + 8;
        ctx.fillStyle = "rgba(0,0,0,0.55)";
        ctx.fillRect(0, y, w, statH - 1);
        ctx.fillStyle = text;
        ctx.fillText(label, 4, y + statH / 2);
      });
      ctx.fillStyle = "rgba(128,128,128,0.25)";
      ctx.fillRect(0, statsTop - 1, paneW, 1);
    }
  }

  /** The visible bars' volume profile against the pane's right edge (buy part
   *  on the inside, sell on the outside), its value area a shade stronger and
   *  a dashed POC line across the pane, labelled with its price. */
  private profile(ctx: CanvasRenderingContext2D, rows: FpRow[], step: number, tick: number, Y: (p: number) => number | null,
    paneW: number, up: string, down: string, poc: string, vaPct: number, type: FootprintSettings["profileType"], label: string): void {
    if (!rows.length) return;
    const a = analyze(rows, step, { imbalance: 0, imbalanceMin: 0, stacked: 0, vaPct });
    if (!a.maxVol) return;
    const maxW = Math.min(170, paneW * 0.16);
    const xr = paneW - 1;
    this.histogram(ctx, a, step, tick, Y, xr, -1, maxW, up, down, type);
    if (a.poc === null) return;
    const yp = Y(a.poc + (step - tick) / 2);
    if (yp === null) return;
    ctx.save();
    ctx.strokeStyle = alpha(poc, 0.65);
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(0, Math.round(yp) + 0.5);
    ctx.lineTo(xr, Math.round(yp) + 0.5);
    ctx.stroke();
    ctx.restore();
    ctx.font = "10px sans-serif";
    ctx.textAlign = "right";
    ctx.textBaseline = "bottom";
    ctx.fillStyle = poc;
    ctx.fillText(`${label} ${a.poc.toFixed(2)}`, xr - maxW - 4, yp - 2);
    ctx.textBaseline = "middle";
  }

  /** One profile's rows as horizontal bars growing from `x0` (dir −1 = leftwards from the right edge,
   *  +1 = rightwards). Volume: the buy part nearer `x0`, the sell part beyond it, the value area a shade
   *  stronger. Delta: buy − sell per row, coloured by its side. */
  private histogram(ctx: CanvasRenderingContext2D, a: ReturnType<typeof analyze>, step: number, tick: number,
    Y: (p: number) => number | null, x0: number, dir: 1 | -1, maxW: number, up: string, down: string,
    type: FootprintSettings["profileType"], fade = 1): void {
    const maxD = Math.max(1e-9, ...a.rows.map((r) => Math.abs(r.buy - r.sell)));
    for (const r of a.rows) {
      const ya = Y(r.p + step - tick / 2), yb = Y(r.p - tick / 2);
      if (ya === null || yb === null) continue;
      const y = Math.min(ya, yb) + 0.5, h = Math.max(1, Math.abs(yb - ya) - 1);
      const inVa = a.val !== null && a.vah !== null && r.p >= a.val && r.p <= a.vah;
      const k = (r.p === a.poc ? 0.75 : inVa ? 0.42 : 0.22) * fade;
      if (type === "delta") {
        const d = r.buy - r.sell, w = maxW * (Math.abs(d) / maxD);
        ctx.fillStyle = alpha(d >= 0 ? up : down, Math.max(k, 0.3 * fade));
        ctx.fillRect(dir < 0 ? x0 - w : x0, y, w, h);
        continue;
      }
      const vol = r.buy + r.sell;
      const w = maxW * (vol / a.maxVol), wb = w * (r.buy / Math.max(1e-9, vol));
      if (type === "tpo") {                         // time at price: one colour, no side
        ctx.fillStyle = alpha(TPO_COLOR, Math.max(k, 0.3 * fade));
        ctx.fillRect(dir < 0 ? x0 - w : x0, y, w, h);
        continue;
      }
      ctx.fillStyle = alpha(up, k);
      ctx.fillRect(dir < 0 ? x0 - wb : x0, y, wb, h);
      ctx.fillStyle = alpha(down, k);
      ctx.fillRect(dir < 0 ? x0 - w : x0 + wb, y, w - wb, h);
    }
  }

  /** A profile per session (IST day), drawn from the session's first bar rightwards over its own
   *  bars, faint enough to read the cells through, with its POC as a solid line across the session. */
  private sessionProfiles(ctx: CanvasRenderingContext2D, bars: PaneRendererCustomData<Time, OhlcItem>["bars"], lo: number, hi: number,
    step: number, tick: number, Y: (p: number) => number | null, half: number, up: string, down: string, poc: string,
    fs: FootprintSettings, rowsOf: (idx: number[]) => FpRow[]): void {
    const days = new Map<number, number[]>();
    for (let i = lo; i < hi; i++) {
      const t = Number(bars[i].originalData.time);
      if (!Number.isFinite(t)) continue;
      const d = Math.floor(t / 86400);
      const list = days.get(d) ?? [];
      list.push(i);
      days.set(d, list);
    }
    for (const idx of days.values()) {
      const rows = rowsOf(idx);
      if (!rows.length) continue;
      const a = analyze(rows, step, { imbalance: 0, imbalanceMin: 0, stacked: 0, vaPct: fs.vaPct });
      if (!a.maxVol) continue;
      const x0 = bars[idx[0]].x - half, x1 = bars[idx[idx.length - 1]].x + half;
      this.histogram(ctx, a, step, tick, Y, x0, 1, Math.max(8, (x1 - x0) * 0.45), up, down, fs.profileType, 0.6);
      if (a.poc === null) continue;
      const yp = Y(a.poc + (step - tick) / 2);
      if (yp === null) continue;
      ctx.fillStyle = alpha(poc, 0.8);
      ctx.fillRect(x0, Math.round(yp), x1 - x0, 1);
    }
  }

  /** Developing POC (solid) and value area (dashed): where each session's POC / VAH / VAL stood after
   *  every bar, as step lines over the visible bars — counted from the session's first loaded bar. */
  private developingLines(ctx: CanvasRenderingContext2D, bars: PaneRendererCustomData<Time, OhlcItem>["bars"], lo: number, hi: number,
    step: number, tick: number, Y: (p: number) => number | null, half: number, poc: string, fs: FootprintSettings,
    rowsOf: (idx: number[]) => FpRow[]): void {
    const dayOf = (i: number) => Math.floor(Number(bars[i].originalData.time) / 86400);
    let i = lo;
    while (i < hi) {
      const d = dayOf(i);
      let s0 = i;
      while (s0 > 0 && dayOf(s0 - 1) === d) s0--;                // the session's start, maybe off-screen
      let e = i;
      while (e < hi && dayOf(e) === d) e++;
      const idx = Array.from({ length: e - s0 }, (_, k) => s0 + k);
      const dev = developing(idx.map((j) => rowsOf([j])), step, fs.vaPct);
      const mid = (p: number | null) => (p == null ? null : Y(p + (step - tick) / 2));
      ctx.save();
      for (const [key, dash, a] of [["poc", [], 0.95], ["vah", [3, 3], 0.6], ["val", [3, 3], 0.6]] as const) {
        ctx.strokeStyle = alpha(poc, a);
        ctx.lineWidth = key === "poc" ? 1.5 : 1;
        ctx.setLineDash(dash as unknown as number[]);
        ctx.beginPath();
        let prevY: number | null = null;
        for (let k = Math.max(0, i - s0); k < idx.length; k++) {
          const y = mid(dev[k][key]);
          if (y == null) { prevY = null; continue; }
          const x0 = bars[idx[k]].x - half, x1 = bars[idx[k]].x + half;
          if (prevY == null) ctx.moveTo(x0, y);
          else ctx.lineTo(x0, y);                                 // a step: up / down at the bar's left edge
          ctx.lineTo(x1, y);
          prevY = y;
        }
        ctx.stroke();
      }
      ctx.restore();
      i = e;
    }
  }

  private statsCells(ctx: CanvasRenderingContext2D, d: OhlcItem, x: number, half: number, top: number, h: number,
    fs: FootprintSettings, maxVis: Record<string, number>, up: string, down: string, text: string): void {
    const st = d.st;
    if (!st || !fs.stats.length) return;
    ctx.font = "10px sans-serif";
    ctx.textAlign = "center";
    fs.stats.forEach((k, n) => {
      const v = statValue(k, st);
      const y = top + n * h;
      const signed = k !== "vol" && k !== "trades";
      const base = !signed ? "#5C6BC0" : v >= 0 ? up : down;
      ctx.fillStyle = alpha(base, 0.12 + 0.5 * (Math.abs(v) / Math.max(1e-9, maxVis[k] ?? 1)));
      ctx.fillRect(x - half + 0.5, y, half * 2 - 1, h - 1);
      const t = k === "dpct" ? `${v.toFixed(0)}%` : k === "oi" && st.oiChg == null ? "" : fmtCell(v);
      if (t && ctx.measureText(t).width <= half * 2 - 4) {
        ctx.fillStyle = text;
        ctx.fillText(t, x, y + h / 2);
      }
    });
  }

  private edge(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, c: string): void {
    ctx.strokeStyle = c;
    ctx.lineWidth = 1;
    ctx.strokeRect(x + 0.5, y + 0.5, Math.max(0, w - 1), Math.max(0, h - 1));
  }

  private dot(ctx: CanvasRenderingContext2D, x: number, y: number): void {
    ctx.beginPath();
    ctx.arc(x, y, 2, 0, Math.PI * 2);
    ctx.fill();
  }
}

const round6 = (x: number) => Math.round(x * 1e6) / 1e6;
const TPO_COLOR = "#5C6BC0";

/** Bar indices of the last `n` sessions (IST days) ending with bar `hi − 1`. */
function lastSessions(bars: PaneRendererCustomData<Time, OhlcItem>["bars"], hi: number, n: number): number[] {
  const out: number[] = [];
  let days = 0, day = NaN;
  for (let i = hi - 1; i >= 0; i--) {
    const d = Math.floor(Number(bars[i].originalData.time) / 86400);
    if (d !== day) {
      if (++days > n) break;
      day = d;
    }
    out.push(i);
  }
  return out.reverse();
}

function statValue(k: StatRow, st: FpBarStats): number {
  switch (k) {
    case "vol": return st.vol;
    case "delta": return st.delta;
    case "dpct": return st.vol ? (st.delta / st.vol) * 100 : 0;
    case "cvd": return st.cvd;
    case "dmin": return st.dlo;
    case "dmax": return st.dhi;
    case "trades": return st.trades;
    case "oi": return st.oiChg ?? 0;
  }
}

/** Cell text: whole numbers below 10k, then K / L / Cr. */
function fmtCell(v: number): string {
  return Math.abs(v) < 10000 ? String(Math.round(v)) : fmtK(v);
}

/** A colour lifted for text on its own tint. */
function brighten(hex: string): string {
  const m = /^#([0-9a-f]{6})$/i.exec((hex || "").trim());
  if (!m) return hex;
  const c = [0, 2, 4].map((i) => Math.min(255, Math.round(parseInt(m[1].slice(i, i + 2), 16) * 1.25 + 30)));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
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
