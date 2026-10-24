// flowAlerts — order-flow alerts for the chart, as pure functions.
//
// Price alerts watch the last price; these watch the recorded order flow the chart
// already loads (app/algo/ticks.py): a big print, a delta spike, a stacked imbalance,
// absorption, a delta divergence, a lopsided order book. Each is judged on the candle
// that just CLOSED (a forming bar's flow still moves, so its signal would flicker),
// except big prints, which fire as they arrive. Every event carries a key, so it
// fires once; nothing fires on history — the chart arms the alerts at the current
// candle / print when they are switched on.

import { analyze, barSignals, groupRows, inferTick } from "./footprint";

export interface FlowAlertConfig {
  on: boolean;
  /** A single print of at least this quantity (0 = off). */
  bigPrint: number;
  /** A candle's |delta| at least this many standard deviations from the last 20 candles' (0 = off). */
  deltaZ: number;
  /** A stacked-imbalance zone (the footprint settings' ratio and run length) in the candle. */
  stacked: boolean;
  /** Absorption: heavy aggression at the candle's extreme that price rejected. */
  absorption: boolean;
  /** A new high on negative delta, or a new low on positive delta. */
  divergence: boolean;
  /** The order book at least this lopsided: |bids − offers| / (bids + offers) ≥ X % (0 = off). */
  book: number;
  popup: boolean;
  sound: boolean;
  notify: boolean;
}

export const DEFAULT_FLOW_ALERTS: FlowAlertConfig = {
  on: false, bigPrint: 0, deltaZ: 3, stacked: true, absorption: true, divergence: false, book: 0,
  popup: true, sound: true, notify: false,
};

export function flowAlertsCfg(raw: Partial<FlowAlertConfig> | null | undefined): FlowAlertConfig {
  const s = { ...DEFAULT_FLOW_ALERTS, ...(raw ?? {}) };
  const n = (v: unknown, lo: number, hi: number) => (typeof v === "number" && Number.isFinite(v) ? Math.min(hi, Math.max(lo, v)) : lo);
  return { ...s, bigPrint: n(s.bigPrint, 0, 1e12), deltaZ: n(s.deltaZ, 0, 20), book: n(s.book, 0, 100) };
}

/** Do the alerts need the footprint and its prints loaded (the chart loads them only for the footprint otherwise)? */
export function needsFootprint(cfg: FlowAlertConfig): boolean {
  return cfg.on && (cfg.bigPrint > 0 || cfg.stacked || cfg.absorption || cfg.divergence);
}

export interface FlowEvent {
  key: string;
  text: string;
}

export interface Bar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
}

const hhmm = (chartTime: number) => new Date(chartTime * 1000).toISOString().slice(11, 16);
const qty = (v: number) => Math.round(v).toLocaleString("en-IN");

/** Big prints newer than `after` (epoch s): [ts, price, qty, side] as the flow API sends them. */
export function printEvents(cfg: FlowAlertConfig, prints: number[][], after: number, name: string,
  fmt: (p: number) => string): FlowEvent[] {
  if (!cfg.on || !(cfg.bigPrint > 0)) return [];
  return prints
    .filter(([ts, , q]) => ts > after && q >= cfg.bigPrint)
    .map(([ts, p, q, side]) => ({
      key: `print:${ts}:${p}:${q}`,
      text: `${name}: big ${side > 0 ? "BUY" : side < 0 ? "SELL" : ""} print ${qty(q)} @ ${fmt(p)}`.replace("  ", " "),
    }));
}

/** The events of the LAST bar in `bars` (closed candles, oldest first). `flow`: per chart time
 *  [buy, sell, delta, …, book bid (9), book ask (10)]; `fp`: per chart time the footprint levels
 *  [price, buy, sell] laid on these candles (an index's already shifted onto it). */
export function candleEvents(cfg: FlowAlertConfig, bars: Bar[], flow: Map<number, (number | null)[]>,
  fp: Map<number, { fp: [number, number, number][] }>,
  imb: { imbalance: number; imbalanceMin: number; stacked: number; rowTicks: number }, name: string): FlowEvent[] {
  const out: FlowEvent[] = [];
  const bar = bars[bars.length - 1];
  if (!cfg.on || !bar) return out;
  const at = hhmm(bar.time);
  const f = flow.get(bar.time);
  if (cfg.deltaZ > 0 && f) {
    const hist = bars.slice(-21, -1).map((b) => flow.get(b.time)?.[2]).filter((v): v is number => v != null);
    if (hist.length >= 10) {
      const mean = hist.reduce((a, v) => a + v, 0) / hist.length;
      const sd = Math.sqrt(hist.reduce((a, v) => a + (v - mean) ** 2, 0) / hist.length);
      const d = f[2] ?? 0;
      const z = sd > 0 ? (d - mean) / sd : 0;
      if (Math.abs(z) >= cfg.deltaZ) {
        out.push({ key: `delta:${bar.time}`, text: `${name} ${at}: delta spike ${d >= 0 ? "+" : ""}${qty(d)} (${z.toFixed(1)}σ ${d >= 0 ? "buying" : "selling"})` });
      }
    }
  }
  if (cfg.book > 0 && f && f[9] != null && f[10] != null && (f[9] as number) + (f[10] as number) > 0) {
    const bid = f[9] as number, ask = f[10] as number;
    const p = ((bid - ask) / (bid + ask)) * 100;
    if (Math.abs(p) >= cfg.book) {
      out.push({ key: `book:${bar.time}`, text: `${name} ${at}: book ${p > 0 ? "bid-heavy" : "offer-heavy"} ${p > 0 ? "+" : ""}${p.toFixed(0)}% (bids ${qty(bid)} vs offers ${qty(ask)})` });
    }
  }
  const levels = fp.get(bar.time)?.fp;
  if ((cfg.stacked || cfg.absorption || cfg.divergence) && levels?.length) {
    const tick = inferTick(levels.map((l) => l[0]));
    const step = imb.rowTicks > 0 ? Math.round(tick * imb.rowTicks * 1e6) / 1e6 : tick;
    const a = analyze(groupRows(levels, step), step, { imbalance: imb.imbalance, imbalanceMin: imb.imbalanceMin, stacked: imb.stacked, vaPct: 70 });
    if (cfg.stacked && imb.imbalance > 0 && imb.stacked > 1) {
      for (const z of a.zones) {
        out.push({ key: `stack:${bar.time}:${z.side}:${z.lo}`, text: `${name} ${at}: stacked ${z.side > 0 ? "BUY" : "SELL"} imbalance ${z.lo}–${z.hi}` });
      }
    }
    const prev = bars.length > 1 ? bars[bars.length - 2] : null;
    const sg = barSignals(a, bar, prev);
    if (cfg.absorption && sg.absorbHigh) out.push({ key: `absH:${bar.time}`, text: `${name} ${at}: absorption at the high — buying absorbed, closed low` });
    if (cfg.absorption && sg.absorbLow) out.push({ key: `absL:${bar.time}`, text: `${name} ${at}: absorption at the low — selling absorbed, closed high` });
    if (cfg.divergence && sg.divBear) out.push({ key: `divB:${bar.time}`, text: `${name} ${at}: bearish delta divergence — new high on negative delta` });
    if (cfg.divergence && sg.divBull) out.push({ key: `divU:${bar.time}`, text: `${name} ${at}: bullish delta divergence — new low on positive delta` });
  }
  return out;
}
