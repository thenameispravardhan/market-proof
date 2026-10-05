// useDepth — live 5-level book for the DOM / Market depth panels.
//
// Pushed: the server streams Fyers DepthUpdate frames on the `depth` ws
// channel (App feeds them to ingestDepth). REST /api/market/depth is only the
// first snapshot and a slow keep-alive (it also keeps the server-side depth
// subscription alive). One poller per symbol however many panels show it.

import { useEffect, useSyncExternalStore } from "react";

export interface Depth {
  ok: boolean;
  reason?: string;
  symbol?: string;
  bids?: [number, number, number][];
  asks?: [number, number, number][];
  total_buy?: number | null;
  total_sell?: number | null;
  ltp?: number | null;
}

const store = new Map<string, Depth>();
const listeners = new Set<() => void>();
const pollers = new Map<string, { n: number; id: ReturnType<typeof setInterval> }>();
const KEEPALIVE_MS = 8000;

function emit(): void {
  [...listeners].forEach((l) => l());
}

export function ingestDepth(raw: unknown): void {
  const d = raw as Depth;
  if (!d || !d.symbol) return;
  const k = d.symbol.toUpperCase();
  store.set(k, { ...store.get(k), ...d });   // keep REST-only fields (ltp) the push lacks
  emit();
}

function load(sym: string): void {
  if (document.hidden) return;
  void fetch(`/api/market/depth?symbol=${encodeURIComponent(sym)}`)
    .then((r) => r.json())
    .then((j: Depth) => {
      store.set(sym.toUpperCase(), { ...j, symbol: j.symbol ?? sym });
      emit();
    })
    .catch(() => {
      store.set(sym.toUpperCase(), { ok: false, reason: "depth request failed", symbol: sym });
      emit();
    });
}

export function useDepth(symbol: string | null | undefined): Depth | null {
  const key = symbol ? symbol.toUpperCase() : "";
  useEffect(() => {
    if (!key) return;
    const p = pollers.get(key);
    if (p) p.n += 1;
    else {
      load(key);
      pollers.set(key, { n: 1, id: setInterval(() => load(key), KEEPALIVE_MS) });
    }
    return () => {
      const q = pollers.get(key);
      if (!q) return;
      q.n -= 1;
      if (q.n <= 0) {
        clearInterval(q.id);
        pollers.delete(key);
      }
    };
  }, [key]);
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => (key ? store.get(key) ?? null : null),
  );
}
