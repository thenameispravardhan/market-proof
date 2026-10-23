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

import { useCallback, useEffect, useState, useSyncExternalStore } from "react";

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
 *  read several symbols at once via peekQuote (e.g. a multi-leg live P&L).
 *  Any symbol's tick counts: prefer useQuotesTick when the symbols are known. */
export function useQuoteTick(): number {
  return useSyncExternalStore(subscribe, () => version);
}

/** Re-render only when one of `symbols` ticks (at most once per frame), then
 *  read them with peekQuote. A table row with useQuoteTick re-rendered on every
 *  tick of every symbol streaming to the page. */
export function useQuotesTick(symbols: readonly (string | null | undefined)[]): void {
  const keys = symbols.filter((s): s is string => !!s).map((s) => s.toUpperCase()).join(",");
  const getSnapshot = useCallback(
    // last prices joined: a string, so it is equal (no re-render) until one of them moves
    () => (keys ? keys.split(",").map((k) => { const q = store.get(k); return q ? `${q.last_price}|${q.bid}|${q.ask}` : ""; }).join(",") : ""),
    [keys],
  );
  useSyncExternalStore(subscribe, getSnapshot);
}

export function peekQuote(symbol: string): QuoteTick | undefined {
  return store.get(symbol.toUpperCase());
}

// Symbols on screen (refcounted). App sends the set to the server over /ws
// ("watch"), which keeps each one subscribed on the Fyers socket — so any
// stock or option a chart, row or ticket shows streams on its own.
const interest = new Map<string, number>();
let interestListener: ((syms: string[]) => void) | null = null;

export function watchedSymbols(): string[] {
  return [...interest.keys()];
}

export function onInterestChange(cb: ((syms: string[]) => void) | null): void {
  interestListener = cb;
}

/** Register interest in a symbol; returns the release. */
export function watchSymbol(symbol: string): () => void {
  const key = symbol.toUpperCase();
  if (!key.includes(":")) return () => {};
  const n = interest.get(key) ?? 0;
  interest.set(key, n + 1);
  if (!n) interestListener?.(watchedSymbols());
  return () => {
    const m = (interest.get(key) ?? 1) - 1;
    if (m > 0) { interest.set(key, m); return; }
    interest.delete(key);
    interestListener?.(watchedSymbols());
  };
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
  const release = watchSymbol(key);
  return () => {
    listeners.delete(l);
    release();
  };
}

/**
 * useLiveQuote for a big component (a whole page): re-renders at most once
 * per `ms` (the last tick of a burst still lands), not once per frame. Show
 * the price itself with a small useLiveQuote component, and read peekQuote
 * where the newest tick matters (sending an order).
 */
export function useLiveQuoteThrottled(symbol: string | null | undefined, ms = 1000): QuoteTick | undefined {
  const key = symbol ? symbol.toUpperCase() : "";
  const [held, setHeld] = useState<QuoteTick | undefined>(() => (key ? store.get(key) : undefined));
  useEffect(() => {
    if (!key) return undefined;
    let seen = store.get(key);
    let last = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const push = () => { timer = undefined; last = Date.now(); setHeld(store.get(key)); };
    setHeld(seen);     // not push(): the first tick after a symbol switch lands at once
    const l = () => {
      const q = store.get(key);
      if (q === seen) return;
      seen = q;
      const wait = ms - (Date.now() - last);
      if (wait <= 0) push();
      else if (!timer) timer = setTimeout(push, wait);
    };
    listeners.add(l);
    const release = watchSymbol(key);
    return () => {
      listeners.delete(l);
      clearTimeout(timer);
      release();
    };
  }, [key, ms]);
  // Right after a symbol switch `held` is still the old symbol's until the effect runs.
  if (!key) return undefined;
  return held && held.symbol.toUpperCase() === key ? held : store.get(key);
}

/**
 * Subscribe to the latest live quote for one symbol. Returns undefined
 * until the first tick for that symbol arrives. The store hands back the
 * same object reference between flushes, so useSyncExternalStore is stable.
 */
export function useLiveQuote(symbol?: string | null): QuoteTick | undefined {
  const key = symbol ? symbol.toUpperCase() : "";
  const getSnapshot = useCallback(() => (key ? store.get(key) : undefined), [key]);
  useEffect(() => (key ? watchSymbol(key) : undefined), [key]);
  return useSyncExternalStore(subscribe, getSnapshot);
}
