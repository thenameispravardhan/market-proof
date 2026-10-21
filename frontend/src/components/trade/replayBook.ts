// replayBook — the simulated trades taken during a chart's Bar Replay.
//
// Nothing here touches a broker. A replay session keeps a book of positions,
// one per instrument (the chart's own symbol, or a future / option of it
// priced by /api/replay/contract), netted like a broker does: a BUY against
// a short closes it first, and only the rest opens a long.
//
// Times are CHART times (epoch + IST, like the chart's candles). A position
// is marked to the close of the newest bar of its instrument that started
// before the replay's "now"; each replay step walks the bars it hasn't seen
// yet, so a stop / target is caught on the bar that crossed it (both inside
// one bar: the stop is assumed first — the conservative reading).
//
// Closed trades are kept in a journal in this browser (localStorage), so a
// replay's results can be reviewed after it ends.

export type ReplayKind = "SPOT" | "FUT" | "CE" | "PE";

export interface RBar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
}

export interface ReplayInstrument {
  key: string;
  label: string;
  kind: ReplayKind;
  lot: number;
  /** chart = the chart's own candles; real / model = the contract endpoint's answer. */
  source: "chart" | "real" | "model";
  bars: RBar[];
  strike?: number | null;
  /** Chart time the contract expires (positions settle at their last price). */
  expiry?: number | null;
  symbol?: string | null;
  /** More of the series to fetch past its last bar. */
  more?: boolean;
  note?: string;
}

export interface ReplayPosition {
  key: string;
  label: string;
  qty: number;
  avg: number;
  openedAt: number;
  sl: number | null;
  tp: number | null;
  mark: number;
  /** Start time of the last bar the position has been checked against. */
  markAt: number;
}

export interface ReplayTrade {
  id: string;
  chart: string;
  label: string;
  side: "BUY" | "SELL";
  qty: number;
  entry: number;
  exit: number;
  entryAt: number;
  exitAt: number;
  reason: string;
  pnl: number;
  source: ReplayInstrument["source"];
}

export interface ReplayMark {
  time: number;
  side: "BUY" | "SELL";
  text: string;
  exit?: boolean;
  pnl?: number;
}

export interface ReplayBook {
  chart: string;
  insts: Record<string, ReplayInstrument>;
  positions: ReplayPosition[];
  trades: ReplayTrade[];
  marks: ReplayMark[];
  seq: number;
}

export function newBook(chart: string): ReplayBook {
  return { chart, insts: {}, positions: [], trades: [], marks: [], seq: 0 };
}

/** The newest bar that started before `t` (the price at `t`), or null. */
export function barBefore(bars: RBar[], t: number): RBar | null {
  let lo = 0, hi = bars.length - 1, hit = -1;
  while (lo <= hi) {
    const m = (lo + hi) >> 1;
    if (bars[m].time < t) { hit = m; lo = m + 1; } else hi = m - 1;
  }
  return hit >= 0 ? bars[hit] : null;
}

/** Merge a continuation into an instrument's bars (later answers win). */
export function mergeBars(a: RBar[], b: RBar[]): RBar[] {
  const by = new Map<number, RBar>();
  for (const x of a) by.set(x.time, x);
  for (const x of b) by.set(x.time, x);
  return [...by.values()].sort((x, y) => x.time - y.time);
}

const round2 = (x: number) => Math.round(x * 100) / 100;

/** Book `qty` of `p` closed at `price`. `at` is when (the trade's exit
 *  time); `markAt` the start of the chart bar it happened on. */
function close(book: ReplayBook, p: ReplayPosition, qty: number, price: number, at: number, reason: string,
  markAt = at): ReplayBook {
  const inst = book.insts[p.key];
  const side: "BUY" | "SELL" = p.qty > 0 ? "BUY" : "SELL";
  const pnl = round2((price - p.avg) * qty * (p.qty > 0 ? 1 : -1));
  const seq = book.seq + 1;
  const trade: ReplayTrade = {
    id: `${Date.now().toString(36)}-${seq}`, chart: book.chart, label: p.label, side, qty, entry: p.avg, exit: price,
    entryAt: p.openedAt, exitAt: at, reason, pnl, source: inst?.source ?? "chart",
  };
  const left = Math.abs(p.qty) - qty;
  const positions = book.positions
    .map((x) => (x === p ? (left > 0 ? { ...x, qty: Math.sign(x.qty) * left } : null) : x))
    .filter((x): x is ReplayPosition => x !== null);
  const mark: ReplayMark = { time: markAt, side: side === "BUY" ? "SELL" : "BUY", text: `${pnl >= 0 ? "+" : ""}${Math.round(pnl)}`, exit: true, pnl };
  return { ...book, positions, trades: [trade, ...book.trades], marks: [...book.marks, mark], seq };
}

/** A market fill of `qty` (> 0) at `price` at time `at` (the replayed
 *  bar's close; `barAt` = the start of the bar whose close it is): adds to,
 *  reduces, closes or reverses the instrument's position. SL / TP are price
 *  levels for a new position (null = none); adding to one keeps its levels. */
export function fill(
  book: ReplayBook, inst: ReplayInstrument, side: "BUY" | "SELL", qty: number, price: number, at: number, barAt: number,
  levels: { sl: number | null; tp: number | null } = { sl: null, tp: null },
): ReplayBook {
  if (!(qty > 0) || !(price > 0)) return book;
  let b: ReplayBook = { ...book, insts: { ...book.insts, [inst.key]: inst } };
  b = { ...b, marks: [...b.marks, { time: barAt, side, text: `${side === "BUY" ? "B" : "S"} ${qty} ${short(inst.label)} @ ${price}`.replace("  ", " ") }] };
  const sign = side === "BUY" ? 1 : -1;
  let left = qty;
  const p = b.positions.find((x) => x.key === inst.key);
  if (p && Math.sign(p.qty) !== sign) {
    const n = Math.min(Math.abs(p.qty), left);
    b = close(b, p, n, price, at, "MANUAL", barAt);
    left -= n;
  } else if (p) {
    const q = Math.abs(p.qty) + left;
    const avg = round2((p.avg * Math.abs(p.qty) + price * left) / q);
    return { ...b, positions: b.positions.map((x) => (x === p ? { ...x, qty: sign * q, avg, mark: price, markAt: barAt } : x)) };
  }
  if (left > 0) {
    b = {
      ...b,
      positions: [...b.positions, {
        key: inst.key, label: inst.label, qty: sign * left, avg: price, openedAt: at, sl: levels.sl, tp: levels.tp,
        mark: price, markAt: barAt,
      }],
    };
  }
  return b;
}

/** "NIFTY 06MAR25 22500 CE" → "22500CE" for chart marks. */
function short(label: string): string {
  const m = label.match(/(\d+(?:\.\d+)?) (CE|PE)$/);
  if (m) return `${m[1]}${m[2]}`;
  return /FUT/.test(label) ? "FUT" : "";
}

/** Walk every position over its instrument's bars that started after the
 *  last one it saw and before `now`: stops / targets on the bar that
 *  crosses them (a gap fills at the bar's open), else the mark moves on.
 *  A contract past its expiry settles at its last price. `chartBars` are
 *  the chart's own candles (the SPOT instrument's bars). */
export function advance(book: ReplayBook, now: number, chartBars: RBar[]): ReplayBook {
  let b = book;
  for (const p0 of book.positions) {
    let p = b.positions.find((x) => x.key === p0.key);
    if (!p) continue;
    const inst = b.insts[p.key];
    const bars = inst?.source === "chart" ? chartBars : inst?.bars ?? [];
    let i = firstAfter(bars, p.markAt);
    let done = false;
    for (; i < bars.length && bars[i].time < now; i++) {
      const bar = bars[i];
      if (inst?.expiry != null && bar.time >= inst.expiry) break;
      const long = p.qty > 0;
      const hitSl = p.sl != null && (long ? bar.low <= p.sl : bar.high >= p.sl);
      const hitTp = p.tp != null && (long ? bar.high >= p.tp : bar.low <= p.tp);
      if (hitSl || hitTp) {
        const lvl = (hitSl ? p.sl : p.tp) as number;
        // A bar that opened beyond the level gapped through it: filled at the open.
        const gap = hitSl ? (long ? bar.open < lvl : bar.open > lvl) : (long ? bar.open > lvl : bar.open < lvl);
        b = close(b, p, Math.abs(p.qty), gap ? bar.open : lvl, bar.time, hitSl ? "SL" : "TARGET");
        done = true;
        break;
      }
      const moved: ReplayPosition = { ...p, mark: bar.close, markAt: bar.time };
      b = { ...b, positions: b.positions.map((x) => (x === p ? moved : x)) };
      p = moved;
    }
    if (!done && inst?.expiry != null && now > inst.expiry) b = close(b, p, Math.abs(p.qty), p.mark, inst.expiry, "EXPIRY");
  }
  return b;
}

function firstAfter(bars: RBar[], t: number): number {
  let lo = 0, hi = bars.length;
  while (lo < hi) {
    const m = (lo + hi) >> 1;
    if (bars[m].time <= t) lo = m + 1; else hi = m;
  }
  return lo;
}

/** Close everything at its mark (the replay ended / jumped). */
export function closeAll(book: ReplayBook, at: number, reason: string): ReplayBook {
  let b = book;
  for (const p of book.positions) {
    const cur = b.positions.find((x) => x.key === p.key);
    if (cur) b = close(b, cur, Math.abs(cur.qty), cur.mark, at, reason);
  }
  return b;
}

export function setLevels(book: ReplayBook, key: string, levels: { sl?: number | null; tp?: number | null }): ReplayBook {
  return { ...book, positions: book.positions.map((p) => (p.key === key ? { ...p, ...levels } : p)) };
}

export function openPnl(book: ReplayBook): number {
  return round2(book.positions.reduce((a, p) => a + (p.mark - p.avg) * p.qty, 0));
}

export interface Stats { n: number; wins: number; net: number; best: number; worst: number }

export function stats(trades: ReplayTrade[]): Stats {
  return {
    n: trades.length,
    wins: trades.filter((t) => t.pnl > 0).length,
    net: round2(trades.reduce((a, t) => a + t.pnl, 0)),
    best: trades.length ? Math.max(...trades.map((t) => t.pnl)) : 0,
    worst: trades.length ? Math.min(...trades.map((t) => t.pnl)) : 0,
  };
}

// ---- journal (this browser) ------------------------------------------------

const JOURNAL_KEY = "replay:journal:v1";
const JOURNAL_MAX = 1000;

export function loadJournal(): ReplayTrade[] {
  try {
    const v = JSON.parse(localStorage.getItem(JOURNAL_KEY) ?? "[]");
    return Array.isArray(v) ? (v as ReplayTrade[]) : [];
  } catch {
    return [];
  }
}

/** Newest first; keeps the last JOURNAL_MAX trades. */
export function saveJournal(trades: ReplayTrade[]): ReplayTrade[] {
  const seen = new Set<string>();
  const out = trades.filter((t) => (seen.has(t.id) ? false : (seen.add(t.id), true))).slice(0, JOURNAL_MAX);
  try { localStorage.setItem(JOURNAL_KEY, JSON.stringify(out)); } catch { /* private window / full: the session still shows them */ }
  return out;
}

export function journalCsv(trades: ReplayTrade[], fmtTime: (t: number) => string): string {
  const head = "chart,instrument,side,qty,entry,exit,entry_time,exit_time,reason,pnl,priced_by";
  const q = (s: string) => (/[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s);
  return [head, ...trades.map((t) => [t.chart, t.label, t.side, t.qty, t.entry, t.exit, fmtTime(t.entryAt), fmtTime(t.exitAt),
    t.reason, t.pnl, t.source].map((x) => q(String(x))).join(","))].join("\n");
}
