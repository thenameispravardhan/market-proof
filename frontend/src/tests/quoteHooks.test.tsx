import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ingestQuote, useLiveQuoteThrottled, useQuotesTick } from "../hooks/useQuotes";

const tick = (symbol: string, last_price: number) =>
  ingestQuote({ symbol, last_price, bid: null, ask: null, volume: null, change: null, change_pct: null, prev_close: null, source: "fyers_ws", simulated: false, ts: "" });

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("useLiveQuoteThrottled", () => {
  it("re-renders at most once per window and still lands the last tick of a burst", () => {
    let renders = 0;
    let seen: number | null | undefined;
    function Page() {
      renders += 1;
      seen = useLiveQuoteThrottled("NSE:THR-EQ", 1000)?.last_price;
      return null;
    }
    render(<Page />);
    act(() => { tick("NSE:THR-EQ", 100); vi.advanceTimersByTime(20); });
    expect(seen).toBe(100);
    const before = renders;
    // 30 ticks over ~0.5 s: per-frame they were 30 re-renders of the page
    for (let i = 1; i <= 30; i++) act(() => { tick("NSE:THR-EQ", 100 + i); vi.advanceTimersByTime(17); });
    expect(renders - before).toBeLessThanOrEqual(1);
    act(() => { vi.advanceTimersByTime(1000); });
    expect(seen).toBe(130);
    expect(renders - before).toBeLessThanOrEqual(2);
  });
});

describe("useQuotesTick", () => {
  it("re-renders only for its own symbols", () => {
    let renders = 0;
    function Row() {
      renders += 1;
      useQuotesTick(["NSE:MINE-EQ"]);
      return null;
    }
    render(<Row />);
    const before = renders;
    act(() => { tick("NSE:OTHER-EQ", 1); vi.advanceTimersByTime(20); });
    act(() => { tick("NSE:OTHER-EQ", 2); vi.advanceTimersByTime(20); });
    expect(renders).toBe(before);
    act(() => { tick("NSE:MINE-EQ", 5); vi.advanceTimersByTime(20); });
    expect(renders).toBe(before + 1);
  });
});
