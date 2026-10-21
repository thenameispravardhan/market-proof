// ReplayTrader — Bar Replay's simulated trading. The chart's own symbol fills
// at the replayed bar's close; an option is priced by /api/replay/contract
// (mocked) and labelled as a model estimate. Nothing reaches a broker.
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import ReplayTrader from "../components/trade/ReplayTrader";
import { loadJournal, type RBar, type ReplayMark } from "../components/trade/replayBook";

const IST = 19800;
const T0 = 1740972600 + IST;          // Mon 3 Mar 2025 09:00 IST, chart time
const bars: RBar[] = Array.from({ length: 12 }, (_, i) => {
  const o = 22500 + i * 10;
  return { time: T0 + i * 300, open: o, high: o + 12, low: o - 3, close: o + 10 };
});
const fmt = (v: number) => v.toFixed(2);

const CONTRACT = {
  ok: true, source: "model", note: "model: Black-Scholes on NIFTY with India VIX (IV 14%)", more: false, iv: 14,
  contract: { kind: "CE", name: "NIFTY", strike: 22550, expiry: 1741255200, label: "NIFTY 06MAR25 22550 CE", lot: 65, symbol: null },
  // one premium bar per chart bar: 100, 104, 108, ... (epoch times)
  candles: bars.map((b, i) => [b.time - IST, 100 + i * 4, 103 + i * 4, 99 + i * 4, 104 + i * 4]),
};

let calls: string[] = [];

function mount(idx: number, marks = vi.fn<(m: ReplayMark[]) => void>()) {
  const props = { symbol: "NSE:NIFTY50-INDEX", shortName: "NIFTY50", lot: 1, candles: () => bars, barSeconds: 300, resolution: "5", fno: true, playing: false, onMarks: marks, fmt };
  const r = render(<ReplayTrader {...props} idx={idx} />);
  return { ...r, rerenderAt: (i: number) => r.rerender(<ReplayTrader {...props} idx={i} />), marks };
}

describe("ReplayTrader", () => {
  let originalFetch: typeof fetch;
  beforeEach(() => {
    calls = [];
    localStorage.clear();
    originalFetch = globalThis.fetch;
    globalThis.fetch = vi.fn((u: RequestInfo | URL) => {
      calls.push(String(u));
      return Promise.resolve(new Response(JSON.stringify(CONTRACT), { status: 200, headers: { "Content-Type": "application/json" } }));
    }) as typeof fetch;
  });
  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("buys the chart symbol at the replayed close, takes the target on a later bar and journals it", async () => {
    const { rerenderAt, marks, unmount } = mount(2);
    fireEvent.change(screen.getByTestId("rt-lots"), { target: { value: "50" } });
    fireEvent.change(screen.getByTestId("rt-tp"), { target: { value: "25" } });
    expect(screen.getByTestId("rt-buy").textContent).toContain("22530.00");     // bar 2 closes at 22530
    fireEvent.click(screen.getByTestId("rt-buy"));
    expect(screen.getByTestId("rt-pos-chart").textContent).toContain("+50");
    rerenderAt(3);                                                               // high 22542: target 22555 not yet
    expect(screen.getByTestId("rt-pos-chart")).toBeTruthy();
    rerenderAt(4);                                                               // high 22552 … still not
    rerenderAt(5);                                                               // high 22562: target hit
    await waitFor(() => expect(screen.queryByTestId("rt-pos-chart")).toBeNull());
    expect(screen.getByTestId("rt-session-net").textContent).toBe("+₹1,250");    // 25 pts x 50
    const last = marks.mock.calls[marks.mock.calls.length - 1][0];
    expect(last.map((m) => m.exit ?? false)).toEqual([false, true]);
    expect(loadJournal()[0]).toMatchObject({ reason: "TARGET", pnl: 1250, chart: "NSE:NIFTY50-INDEX" });
    unmount();
    expect(marks).toHaveBeenLastCalledWith([]);
  });

  it("applies a position's stop only once it's entered — a half-typed stop never fires", async () => {
    const { rerenderAt } = mount(2);
    fireEvent.change(screen.getByTestId("rt-lots"), { target: { value: "10" } });
    fireEvent.click(screen.getByTestId("rt-sell"));                              // short 10 @ 22530
    const sl = screen.getByTestId("rt-pos-sl-chart");
    fireEvent.change(sl, { target: { value: "2" } });                           // still typing
    rerenderAt(3);                                                               // high 22542 would hit a stop at 2
    expect(screen.getByTestId("rt-pos-chart")).toBeTruthy();
    fireEvent.change(sl, { target: { value: "22545" } });
    fireEvent.keyDown(sl, { key: "Enter" });
    rerenderAt(4);                                                               // high 22552: stopped at 22545
    await waitFor(() => expect(screen.queryByTestId("rt-pos-chart")).toBeNull());
    expect(loadJournal()[0]).toMatchObject({ side: "SELL", reason: "SL", exit: 22545, pnl: -150 });
  });

  it("prices an option from the replay endpoint, trades whole lots and closes it when the replay ends", async () => {
    const { rerenderAt, unmount } = mount(2);
    fireEvent.change(screen.getByTestId("rt-kind"), { target: { value: "CE" } });
    expect(await screen.findByText("NIFTY 06MAR25 22550 CE")).toBeTruthy();
    expect(screen.getByTestId("rt-preview").textContent).toContain("model price (estimate)");
    const q = new URL(calls[0], "http://x").searchParams;
    expect(q.get("kind")).toBe("CE");
    expect(q.get("strike_mode")).toBe("ATM");
    expect(Number(q.get("t"))).toBe(bars[2].time + 300 - IST);                    // the replayed bar's close, epoch
    expect(Number(q.get("from"))).toBe(bars[2].time - IST);
    expect(screen.getByTestId("rt-buy").textContent).toContain("112.00");         // premium bar 2 closes at 112
    fireEvent.click(screen.getByTestId("rt-buy"));
    const row = await screen.findByTestId("rt-pos-CE:22550:1741255200");
    expect(within(row).getByText("+65")).toBeTruthy();                             // 1 lot of 65
    rerenderAt(4);                                                                 // premium 120
    await waitFor(() => expect(within(screen.getByTestId("rt-pos-CE:22550:1741255200")).getByText("+₹520")).toBeTruthy());
    unmount();                                                                     // leaving the replay
    expect(loadJournal()[0]).toMatchObject({ label: "NIFTY 06MAR25 22550 CE", reason: "REPLAY END", entry: 112, exit: 120, pnl: 520, source: "model" });
  });

  it("says why when the endpoint can't price a contract", async () => {
    globalThis.fetch = vi.fn(() => Promise.resolve(new Response(JSON.stringify({ ok: false, reason: "SBIN has no futures & options" }),
      { status: 200, headers: { "Content-Type": "application/json" } }))) as typeof fetch;
    mount(2);
    fireEvent.change(screen.getByTestId("rt-kind"), { target: { value: "FUT" } });
    expect(await screen.findByText("SBIN has no futures & options")).toBeTruthy();
    fireEvent.click(screen.getByTestId("rt-sell"));
    await waitFor(() => expect(screen.getByTestId("rt-msg").textContent).toContain("no futures & options"));
    expect(screen.queryByTestId("rt-positions")?.querySelector("tbody")).toBeFalsy();
  });
});
