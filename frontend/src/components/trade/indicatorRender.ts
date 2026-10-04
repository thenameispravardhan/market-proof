// indicatorRender — the parts of indicators that aren't plain series:
//   FillPrimitive   shaded area between two plots / levels (Ichimoku cloud,
//                   band backgrounds, oscillator zones), drawn under the lines
//   paintProfile    horizontal volume-by-price bars (visible-range profile)
//   paintOiProfile  call / put open interest per strike from the option chain

import type { IPrimitivePaneView, ISeriesApi, ISeriesPrimitive, SeriesAttachedParameter, SeriesType, Time } from "lightweight-charts";
import type { Profile } from "./indicatorMore";

export interface FillBand {
  /** Values per bar index (may run past the last bar for shifted plots). */
  a: (number | null)[];
  b: (number | null)[];
  color: string;
  colorDown?: string;
}

interface Pt {
  x: number;
  ya: number;
  yb: number;
}

/** Fills attached to an indicator series: priced on that series' scale. */
export class FillPrimitive implements ISeriesPrimitive<Time> {
  private series: ISeriesApi<SeriesType> | null = null;
  private req: (() => void) | null = null;
  private readonly view: IPrimitivePaneView;

  constructor(
    private readonly bands: () => FillBand[],
    private readonly xOf: (j: number) => number | null,
    private readonly span: () => [number, number],
  ) {
    this.view = {
      zOrder: () => "bottom",
      renderer: () => ({
        draw: (target) => target.useMediaCoordinateSpace(({ context }) => this.paint(context)),
      }),
    };
  }

  attached(p: SeriesAttachedParameter<Time>): void {
    this.series = p.series as ISeriesApi<SeriesType>;
    this.req = p.requestUpdate;
  }

  detached(): void {
    this.series = null;
    this.req = null;
  }

  updateAllViews(): void {
    /* data is read at paint time */
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return [this.view];
  }

  requestUpdate(): void {
    this.req?.();
  }

  private paint(ctx: CanvasRenderingContext2D): void {
    const s = this.series;
    if (!s) return;
    const [from, to] = this.span();
    for (const band of this.bands()) {
      const last = Math.min(to, band.a.length - 1, band.b.length - 1);
      let run: Pt[] = [];
      let sign = 0;
      const flush = () => {
        if (run.length >= 2) {
          ctx.beginPath();
          ctx.moveTo(run[0].x, run[0].ya);
          for (const p of run) ctx.lineTo(p.x, p.ya);
          for (let k = run.length - 1; k >= 0; k--) ctx.lineTo(run[k].x, run[k].yb);
          ctx.closePath();
          ctx.fillStyle = sign < 0 && band.colorDown ? band.colorDown : band.color;
          ctx.fill();
        }
        run = [];
      };
      let prev: (Pt & { d: number }) | null = null;
      for (let j = Math.max(0, from); j <= last; j++) {
        const av = band.a[j];
        const bv = band.b[j];
        const x = av == null || bv == null ? null : this.xOf(j);
        const ya = x == null ? null : (s.priceToCoordinate(av as number) as number | null);
        const yb = x == null ? null : (s.priceToCoordinate(bv as number) as number | null);
        if (x == null || ya == null || yb == null) {
          flush();
          prev = null;
          continue;
        }
        const d = (av as number) - (bv as number);
        const sg = d > 0 ? 1 : d < 0 ? -1 : 0;
        if (prev && band.colorDown && sg !== 0 && sign !== 0 && sg !== sign) {
          // the lines cross between the two bars: close this colour at the crossing
          const d0 = prev.ya - prev.yb;
          const d1 = ya - yb;
          const t = d0 === d1 ? 0.5 : d0 / (d0 - d1);
          const xm = prev.x + (x - prev.x) * t;
          const ym = prev.ya + (ya - prev.ya) * t;
          run.push({ x: xm, ya: ym, yb: ym });
          flush();
          run.push({ x: xm, ya: ym, yb: ym });
        }
        if (sg !== 0) sign = sg;
        run.push({ x, ya, yb });
        prev = { x, ya, yb, d };
      }
      flush();
    }
  }
}

/** Horizontal volume-by-price bars against the right (or left) edge. */
export function paintProfile(
  ctx: CanvasRenderingContext2D,
  p: Profile,
  opts: { w: number; widthPct: number; side: "Right" | "Left"; priceToY: (v: number) => number | null; up: string; down: string; poc: string },
): void {
  const tot = p.up.map((u, i) => u + p.down[i]);
  const max = Math.max(...tot, 1);
  const maxW = (opts.w * Math.max(5, Math.min(90, opts.widthPct))) / 100;
  ctx.save();
  for (let i = 0; i < tot.length; i++) {
    const yTop = opts.priceToY(p.lo + p.step * (i + 1));
    const yBot = opts.priceToY(p.lo + p.step * i);
    if (yTop == null || yBot == null) continue;
    const y = Math.min(yTop, yBot) + 0.5;
    const h = Math.max(1, Math.abs(yBot - yTop) - 1);
    const inVa = i >= p.vaLo && i <= p.vaHi;
    const wUp = (p.up[i] / max) * maxW;
    const wDn = (p.down[i] / max) * maxW;
    ctx.globalAlpha = inVa ? 0.55 : 0.28;
    if (opts.side === "Right") {
      ctx.fillStyle = opts.up;
      ctx.fillRect(opts.w - wUp - wDn, y, wUp, h);
      ctx.fillStyle = opts.down;
      ctx.fillRect(opts.w - wDn, y, wDn, h);
    } else {
      ctx.fillStyle = opts.up;
      ctx.fillRect(0, y, wUp, h);
      ctx.fillStyle = opts.down;
      ctx.fillRect(wUp, y, wDn, h);
    }
  }
  const yp = opts.priceToY(p.lo + p.step * (p.poc + 0.5));
  if (yp != null) {
    ctx.globalAlpha = 0.9;
    ctx.strokeStyle = opts.poc;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(opts.side === "Right" ? opts.w - maxW : 0, yp);
    ctx.lineTo(opts.side === "Right" ? opts.w : maxW, yp);
    ctx.stroke();
  }
  ctx.restore();
}

export interface OiStrike {
  strike: number;
  ce: number | null;
  pe: number | null;
}

const fmtOi = (v: number) => (v >= 1e7 ? `${(v / 1e7).toFixed(1)}Cr` : v >= 1e5 ? `${(v / 1e5).toFixed(1)}L` : v >= 1e3 ? `${(v / 1e3).toFixed(0)}K` : String(Math.round(v)));

/** Call (red) and put (green) open interest bars at each strike, from the right edge. */
export function paintOiProfile(
  ctx: CanvasRenderingContext2D,
  rows: OiStrike[],
  opts: { w: number; widthPct: number; priceToY: (v: number) => number | null; text: string },
): void {
  const max = Math.max(1, ...rows.map((r) => Math.max(r.ce ?? 0, r.pe ?? 0)));
  const maxW = (opts.w * Math.max(5, Math.min(80, opts.widthPct))) / 100;
  const ys = rows.map((r) => opts.priceToY(r.strike));
  const gaps = ys.slice(1).map((y, i) => (y != null && ys[i] != null ? Math.abs(y - (ys[i] as number)) : Infinity));
  const gap = Math.min(...gaps, 24);
  const h = Math.max(2, Math.min(9, gap * 0.35));
  ctx.save();
  ctx.font = "10px sans-serif";
  ctx.textBaseline = "middle";
  rows.forEach((r, i) => {
    const y = ys[i];
    if (y == null) return;
    const ce = r.ce ?? 0;
    const pe = r.pe ?? 0;
    const wc = (ce / max) * maxW;
    const wp = (pe / max) * maxW;
    ctx.globalAlpha = 0.55;
    ctx.fillStyle = "#EF5350";
    ctx.fillRect(opts.w - wc, y - h - 0.5, wc, h);
    ctx.fillStyle = "#26A69A";
    ctx.fillRect(opts.w - wp, y + 0.5, wp, h);
    if (gap >= 16) {
      ctx.globalAlpha = 0.85;
      ctx.fillStyle = opts.text;
      ctx.textAlign = "right";
      ctx.fillText(`${r.strike}  C ${fmtOi(ce)} · P ${fmtOi(pe)}`, opts.w - Math.max(wc, wp) - 4, y);
    }
  });
  ctx.restore();
}
