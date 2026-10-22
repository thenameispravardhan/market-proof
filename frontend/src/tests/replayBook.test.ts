import { describe, it, expect, beforeEach } from "vitest";
import {
  advance, closeAll, fill, journalCsv, loadJournal, newBook, openPnl, saveJournal, stats,
  type RBar, type ReplayInstrument,
} from "../components/trade/replayBook";

const bar = (time: number, open: number, high: number, low: number, close: number): RBar => ({ time, open, high, low, close });
const SPOT: ReplayInstrument = { key: "chart", label: "NIFTY50", kind: "SPOT", lot: 1, source: "chart", bars: [] };
// 5-minute chart bars from t=0: the replay's "now" after bar k is (k + 1) * 300.
const CHART = [
  bar(0, 100, 101, 99, 100),
  bar(300, 100, 104, 99.5, 103),
  bar(600, 103, 107, 102, 106),       // crosses 105
  bar(900, 106, 106, 96, 97),
];

describe("replay book", () => {
  beforeEach(() => localStorage.clear());

  it("fills at the bar's close and books the target on the bar that crosses it", () => {
    let b = fill(newBook("NSE:NIFTY50-INDEX"), SPOT, "BUY", 10, 100, 300, 0, { sl: 95, tp: 105 });
    expect(b.positions).toHaveLength(1);
    b = advance(b, 600, CHART);                       // bar 300: 99.5–104, nothing hit
    expect(b.positions[0].mark).toBe(103);
    expect(openPnl(b)).toBe(30);
    b = advance(b, 900, CHART);                       // bar 600 reaches 107: target 105
    expect(b.positions).toHaveLength(0);
    expect(b.trades[0]).toMatchObject({ side: "BUY", qty: 10, entry: 100, exit: 105, reason: "TARGET", pnl: 50, exitAt: 600 });
    expect(b.marks.map((m) => [m.time, m.side, m.exit ?? false])).toEqual([[0, "BUY", false], [600, "SELL", true]]);
  });

  it("a bar that gaps through the stop fills at its open; stop and target in one bar = the stop", () => {
    const gap = [bar(0, 100, 100, 100, 100), bar(300, 94, 95, 90, 92)];
    let b = fill(newBook("X"), SPOT, "BUY", 1, 100, 300, 0, { sl: 97, tp: null });
    b = advance(b, 600, gap);
    expect(b.trades[0]).toMatchObject({ exit: 94, reason: "SL", pnl: -6 });

    const wide = [bar(0, 100, 100, 100, 100), bar(300, 100, 112, 88, 100)];
    let s = fill(newBook("X"), SPOT, "SELL", 2, 100, 300, 0, { sl: 110, tp: 90 });
    s = advance(s, 600, wide);
    expect(s.trades[0]).toMatchObject({ side: "SELL", exit: 110, reason: "SL", pnl: -20 });
  });

  it("nets like a broker: adds average, an opposite fill closes first and reverses the rest", () => {
    let b = fill(newBook("X"), SPOT, "BUY", 10, 100, 300, 0);
    b = fill(b, SPOT, "BUY", 10, 110, 600, 300);
    expect(b.positions[0]).toMatchObject({ qty: 20, avg: 105 });
    b = fill(b, SPOT, "SELL", 25, 120, 900, 600, { sl: 125, tp: null });
    expect(b.trades[0]).toMatchObject({ side: "BUY", qty: 20, entry: 105, exit: 120, reason: "MANUAL", pnl: 300 });
    expect(b.positions[0]).toMatchObject({ qty: -5, avg: 120, sl: 125 });
  });

  it("settles a contract at its last price once the replay passes its expiry", () => {
    const opt: ReplayInstrument = {
      key: "CE:22500:1", label: "NIFTY 06MAR25 22500 CE", kind: "CE", lot: 65, source: "model", expiry: 1200,
      bars: [bar(0, 50, 50, 50, 50), bar(300, 50, 60, 49, 58), bar(600, 58, 70, 57, 66), bar(900, 66, 80, 65, 75), bar(1200, 1, 1, 1, 1)],
    };
    let b = fill(newBook("X"), opt, "BUY", 65, 50, 300, 0);
    b = advance(b, 1500, []);
    expect(b.trades[0]).toMatchObject({ exit: 75, reason: "EXPIRY", qty: 65, pnl: 1625, source: "model" });
    expect(b.marks[0].text).toBe("B 65 22500CE @ 50");
  });

  it("closes everything at its mark, and the journal keeps trades once each, newest first", () => {
    let b = fill(newBook("NSE:SBIN-EQ"), SPOT, "SELL", 5, 800, 300, 0);
    b = advance(b, 600, [bar(0, 800, 800, 800, 800), bar(300, 800, 801, 790, 795)]);
    b = closeAll(b, 600, "REPLAY END");
    expect(b.trades[0]).toMatchObject({ exit: 795, reason: "REPLAY END", pnl: 25 });
    saveJournal([...b.trades, ...loadJournal()]);
    saveJournal([...b.trades, ...loadJournal()]);
    expect(loadJournal()).toHaveLength(1);
    expect(stats(loadJournal())).toMatchObject({ n: 1, wins: 1, net: 25 });
    const csv = journalCsv(loadJournal(), (t) => `t${t}`).split("\n");
    expect(csv[0]).toContain("instrument");
    expect(csv[1]).toBe("NSE:SBIN-EQ,NIFTY50,SELL,5,800,795,t300,t600,REPLAY END,25,chart");
  });
});
