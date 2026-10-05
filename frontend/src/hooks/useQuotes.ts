// useQuotes — live price store fed by the `/ws` "quotes" channel.
//
// The backend bridges every MarketDataBus tick (Fyers WebSocket in real
// time, REST/paper as backstop) onto the event bus. `ingestQuote` is
// called once per pushed tick from the App-level WebSocket; components
// read a symbol's latest price with `useQuote(symbol)`.
//
// Why an external store instead of React state: ticks can arrive many
// times per second across many symbols. Pushing each through component
// state would thrash React. Instead we keep a plain Map and notify
// subscribers at most once per animation frame (~60fps) — still real
// time, but one repaint per frame no matter the tick rate.

import { useCallback, useSyncExternalStore } from "react";

export interface QuoteTick {
  symbol: string;
  last_price: number | null;
  bid: number | null;
  ask: number | null;
  volume: number | null;
  change: number | null;
  change_pct: number | null;
  prev_close: number | null;
  source: string | null;
  simulated: boolean;
  ts: string;
}

const store = new Map<string, QuoteTick>();
const listeners = new Set<() => void>();
let flushScheduled = false;

// Coalesce notifications to one repaint per frame. Falls back to a 16ms
// timer where requestAnimationFrame is unavailable (e.g. jsdom in tests).
function schedule(cb: () => void): void {
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(() => cb());
  else setTimeout(cb, 16);
}

/** Record a pushed tick. Safe to call with an unknown/malformed payload. */
export function ingestQuote(raw: unknown): void {
  if (!raw || typeof raw !== "object") return;
  const t = raw as QuoteTick;
  if (!t.symbol) return;
  store.set(t.symbol.toUpperCase(), t);
  if (!flushScheduled) {
    flushScheduled = true;
    schedule(() => {
      flushScheduled = false;
      // Snapshot so a listener that (un)subscribes mid-flush is safe.
      [...listeners].forEach((l) => l());
    });
  }
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

let version = 0;
listeners.add(() => { version += 1; });

/** Re-render on every tick flush (one per frame) — for small components that
 *  read several symbols at once via peekQuote (e.g. a multi-leg live P&L). */
export function useQuoteTick(): number {
  return useSyncExternalStore(subscribe, () => version);
}

export function peekQuote(symbol: string): QuoteTick | undefined {
  return store.get(symbol.toUpperCase());
}

/** Imperative per-symbol tick callback (no React re-render) — for the chart,
 *  which applies ticks straight to its series. Returns the unsubscribe. */
export function onQuote(symbol: string, cb: (q: QuoteTick) => void): () => void {
  const key = symbol.toUpperCase();
  let last = store.get(key);
  if (last) cb(last);
  const l = () => {
    const q = store.get(key);
    if (q && q !== last) {
      last = q;
      cb(q);
    }
  };
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
}

/**
 * Subscribe to the latest live quote for one symbol. Returns undefined
 * until the first tick for that symbol arrives. The store hands back the
 * same object reference between flushes, so useSyncExternalStore is stable.
 */
export function useLiveQuote(symbol?: string | null): QuoteTick | undefined {
  const key = symbol ? symbol.toUpperCase() : "";
  const getSnapshot = useCallback(() => (key ? store.get(key) : undefined), [key]);
  return useSyncExternalStore(subscribe, getSnapshot);
}
