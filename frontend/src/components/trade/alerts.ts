// alerts — price alerts for the chart: the model, migration from the old
// `{id, price}` alerts, and the pure condition check run on every tick.
//
// An alert compares the symbol's price with a TARGET: a fixed value (or a
// value range for channel conditions), an indicator plot, or a drawn line.

export type AlertCond = "cross" | "crossUp" | "crossDown" | "gt" | "lt" | "enter" | "exit";
export type AlertTrigger = "once" | "oncePerBar" | "oncePerBarClose" | "everyTime";

export const COND_LABEL: Record<AlertCond, string> = {
  cross: "Crossing",
  crossUp: "Crossing Up",
  crossDown: "Crossing Down",
  gt: "Greater Than",
  lt: "Less Than",
  enter: "Entering Channel",
  exit: "Exiting Channel",
};

export const TRIGGER_LABEL: Record<AlertTrigger, string> = {
  once: "Only once",
  oncePerBar: "Once per bar",
  oncePerBarClose: "Once per bar close",
  everyTime: "Every time",
};

export interface AlertItem {
  id: string;
  cond: AlertCond;
  /** "value" | "ind:<uid>:<plot>" | "draw:<id>" */
  target: string;
  targetLabel?: string;
  value: number;
  /** Upper bound for channel conditions on a value target. */
  value2?: number;
  trigger: AlertTrigger;
  /** Epoch ms; null = open-ended. */
  expires: number | null;
  message: string;
  popup: boolean;
  sound: boolean;
  notify: boolean;
  active: boolean;
  created: number;
  /** Bar time it last fired on (once-per-bar triggers). */
  lastBar?: number;
  lastFired?: number;
  fired?: number;
}

export function newAlert(value: number, patch: Partial<AlertItem> = {}): AlertItem {
  return {
    id: `a${Date.now().toString(36)}${Math.floor(Math.random() * 1e4)}`,
    cond: "cross",
    target: "value",
    value,
    trigger: "once",
    expires: null,
    message: "",
    popup: true,
    sound: true,
    notify: true,
    active: true,
    created: Date.now(),
    ...patch,
  };
}

/** Accept current alerts and the old `{id, price}` shape. */
export function migrateAlert(raw: unknown): AlertItem | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Partial<AlertItem> & { price?: number };
  if (typeof r.cond === "string" && typeof r.value === "number") return { ...newAlert(r.value), ...r } as AlertItem;
  if (typeof r.price === "number") return newAlert(r.price, { id: typeof r.id === "string" ? r.id : undefined });
  return null;
}

/** Does moving from `prev` to `cur` satisfy the condition against [lo, hi]
 *  (lo === hi for single-value targets)? */
export function conditionMet(cond: AlertCond, prev: number, cur: number, lo: number, hi = lo): boolean {
  const t = lo;
  switch (cond) {
    case "cross": return (prev < t && cur >= t) || (prev > t && cur <= t);
    case "crossUp": return prev < t && cur >= t;
    case "crossDown": return prev > t && cur <= t;
    case "gt": return cur > t;
    case "lt": return cur < t;
    case "enter": {
      const a = Math.min(lo, hi), b = Math.max(lo, hi);
      return !(prev >= a && prev <= b) && cur >= a && cur <= b;
    }
    case "exit": {
      const a = Math.min(lo, hi), b = Math.max(lo, hi);
      return prev >= a && prev <= b && !(cur >= a && cur <= b);
    }
  }
}

/**
 * Decide whether an alert fires on this tick. `barTime` is the live bar's
 * time; `barClosed` is true on the tick that opens a new bar (then `prev`
 * / `cur` are the last two CLOSES). Returns the updated alert when it
 * fires, null otherwise.
 */
export function evaluate(
  a: AlertItem,
  prev: number,
  cur: number,
  lo: number,
  hi: number,
  barTime: number,
  barClosed: boolean,
  now = Date.now(),
): AlertItem | null {
  if (!a.active) return null;
  if (a.expires != null && now > a.expires) return null;
  if (a.trigger === "oncePerBarClose" && !barClosed) return null;
  if (a.trigger !== "oncePerBarClose" && barClosed) return null;
  if (!conditionMet(a.cond, prev, cur, lo, hi)) return null;
  if (a.trigger === "oncePerBar" && a.lastBar === barTime) return null;
  // level conditions ("greater than") re-fire every tick otherwise
  if (a.trigger === "everyTime" && (a.cond === "gt" || a.cond === "lt") && a.lastFired && now - a.lastFired < 5000) return null;
  return {
    ...a,
    active: a.trigger !== "once",
    lastBar: barTime,
    lastFired: now,
    fired: (a.fired ?? 0) + 1,
  };
}

/** Short one-line description, e.g. "Crossing 2,450.00 · Only once". */
export function describe(a: AlertItem, fmt: (n: number) => string): string {
  const tgt = a.target === "value"
    ? a.cond === "enter" || a.cond === "exit"
      ? `${fmt(Math.min(a.value, a.value2 ?? a.value))} – ${fmt(Math.max(a.value, a.value2 ?? a.value))}`
      : fmt(a.value)
    : a.targetLabel ?? a.target;
  return `${COND_LABEL[a.cond]} ${tgt} · ${TRIGGER_LABEL[a.trigger]}`;
}

/** A short two-tone beep (WebAudio) — the alert / execution sound. */
export function beep(): void {
  try {
    const AC = (window as unknown as { AudioContext?: typeof AudioContext; webkitAudioContext?: typeof AudioContext }).AudioContext
      ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!AC) return;
    const ctx = new AC();
    const tone = (f: number, at: number) => {
      const o = ctx.createOscillator();
      const g = ctx.createGain();
      o.frequency.value = f;
      g.gain.setValueAtTime(0.08, ctx.currentTime + at);
      g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + at + 0.18);
      o.connect(g).connect(ctx.destination);
      o.start(ctx.currentTime + at);
      o.stop(ctx.currentTime + at + 0.2);
    };
    tone(880, 0);
    tone(1320, 0.12);
    setTimeout(() => void ctx.close(), 600);
  } catch {
    /* audio unavailable */
  }
}
