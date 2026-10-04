// chartSync — a tiny in-page bus between the charts of a multi-chart
// layout (crosshair, visible time range, interval, drawings) plus the
// notification log the Account Manager shows. Module-level on purpose:
// every ChartPanel on the page is a peer, whichever cell renders it.

export type SyncEvent =
  | { type: "crosshair"; src: string; time: number | null }
  | { type: "range"; src: string; from: number; to: number }
  | { type: "interval"; src: string; interval: string }
  | { type: "drawings"; src: string; symbol: string };

export interface SyncFlags {
  crosshair: boolean;
  time: boolean;
  interval: boolean;
  drawings: boolean;
}

export const NO_SYNC: SyncFlags = { crosshair: false, time: false, interval: false, drawings: true };

type Listener = (e: SyncEvent) => void;
const listeners = new Set<Listener>();

export function publishSync(e: SyncEvent): void {
  for (const l of [...listeners]) {
    try {
      l(e);
    } catch {
      /* one bad listener must not break the others */
    }
  }
}

export function subscribeSync(l: Listener): () => void {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
}

// ---- notification log (orders, alerts, connection messages) ----

export interface LogEntry {
  id: number;
  ts: number;
  kind: "order" | "alert" | "info" | "error";
  text: string;
}

const LOG_MAX = 200;
let log: LogEntry[] = [];
let seq = 0;
const logListeners = new Set<() => void>();

export function pushLog(kind: LogEntry["kind"], text: string): void {
  seq += 1;
  log = [{ id: seq, ts: Date.now(), kind, text }, ...log].slice(0, LOG_MAX);
  for (const l of [...logListeners]) l();
}

export function getLog(): LogEntry[] {
  return log;
}

export function clearLog(): void {
  log = [];
  for (const l of [...logListeners]) l();
}

export function subscribeLog(l: () => void): () => void {
  logListeners.add(l);
  return () => {
    logListeners.delete(l);
  };
}

/** Which chart the pointer is over (keyboard shortcuts go to it). */
export const chartFocus: { hover: string | null } = { hover: null };

/** The copied drawing, shared by every chart on the page (Ctrl+C in one
 *  chart, Ctrl+V in another or after switching symbol). */
export const drawingClipboard: { current: unknown } = { current: null };

/** The copied indicator (legend More → Copy), pasted onto any chart. */
export const indicatorClipboard: { current: unknown } = { current: null };
