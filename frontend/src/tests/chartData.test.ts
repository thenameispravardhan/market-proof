import { describe, expect, it } from "vitest";
import {
  aggregate,
  calendarBucket,
  fetchPlan,
  intervalLabel,
  intervalLongLabel,
  kagi,
  lineBreak,
  liveBucket,
  mergeOlder,
  normalizeInterval,
  parseInterval,
  pointFigure,
  rangeBars,
  renko,
  wallClock,
  formatDate,
  formatClock,
  type Bar,
} from "../components/trade/chartData";

const DAY = 86400;
const bar = (time: number, o: number, h: number, l: number, c: number, v = 10): Bar => ({ time, open: o, high: h, low: l, close: c, volume: v });

describe("intervals", () => {
  it("parses and normalises interval keys", () => {
    expect(parseInterval("5")).toEqual({ unit: "m", n: 5 });
    expect(parseInterval("D")).toEqual({ unit: "D", n: 1 });
    expect(parseInterval("15S")).toEqual({ unit: "S", n: 15 });
    expect(normalizeInterval("1h")).toBe("60");
    expect(normalizeInterval("4H")).toBe("240");
    expect(normalizeInterval("1d")).toBe("D");
    expect(normalizeInterval("w")).toBe("1W");
    expect(normalizeInterval("3m")).toBe("3"); // a bare "m" suffix is minutes
    expect(normalizeInterval("garbage")).toBeNull();
  });

  it("labels like TradingView", () => {
    expect(intervalLabel("1")).toBe("1m");
    expect(intervalLabel("60")).toBe("1h");
    expect(intervalLabel("75")).toBe("75m");
    expect(intervalLabel("D")).toBe("D");
    expect(intervalLabel("1W")).toBe("W");
    expect(intervalLabel("3M")).toBe("3M");
    expect(intervalLongLabel("240")).toBe("4 hours");
    expect(intervalLongLabel("30S")).toBe("30 seconds");
  });

  it("fetches natively when Fyers serves the interval, else aggregates a divisor", () => {
    expect(fetchPlan("15")).toMatchObject({ res: "15", aggregate: false });
    expect(fetchPlan("75")).toMatchObject({ res: "15", aggregate: true });
    expect(fetchPlan("D")).toMatchObject({ res: "D", aggregate: false });
    expect(fetchPlan("1W")).toMatchObject({ res: "D", aggregate: true });
    expect(fetchPlan("10S")).toMatchObject({ res: "10S", aggregate: false });
    expect(fetchPlan("20S")).toMatchObject({ res: "10S", aggregate: true });
    for (const k of ["1", "5", "75", "240"]) expect(fetchPlan(k).initialDays).toBeLessThanOrEqual(100);
  });
});

describe("aggregation", () => {
  it("builds 75m bars anchored to each session's first bar", () => {
    const open = 10 * DAY + 555 * 60; // 09:15 chart time
    const bars = Array.from({ length: 10 }, (_, i) => bar(open + i * 900, 100 + i, 101 + i, 99 + i, 100.5 + i, 1));
    const out = aggregate(bars, "75");
    expect(out).toHaveLength(2);
    expect(out[0]).toMatchObject({ time: open, open: 100, high: 105, low: 99, close: 104.5, volume: 5 });
    expect(out[1].time).toBe(open + 75 * 60);
  });

  it("builds weekly bars Monday-anchored and merges a split week on paging", () => {
    // 1970-01-05 was a Monday: day 4.
    const mon = 4 * DAY;
    const days = [0, 1, 2, 3, 4, 7, 8].map((d, i) => bar(mon + d * DAY, 10 + i, 12 + i, 9 + i, 11 + i, 1));
    const weekly = aggregate(days, "1W");
    expect(weekly).toHaveLength(2);
    expect(weekly[0]).toMatchObject({ open: 10, close: 15, volume: 5 });
    const older = aggregate(days.slice(0, 2), "1W");
    const cur = aggregate(days.slice(2), "1W");
    const merged = mergeOlder(older, cur, "1W");
    expect(merged).toHaveLength(2);
    expect(merged[0]).toMatchObject({ open: 10, close: 15, high: 16, low: 9, volume: 5 });
  });

  it("groups months into quarters", () => {
    const jan = Date.UTC(2026, 0, 2) / 1000;
    const apr = Date.UTC(2026, 3, 1) / 1000;
    expect(calendarBucket(jan, { unit: "M", n: 3 })).toBe(calendarBucket(Date.UTC(2026, 2, 31) / 1000, { unit: "M", n: 3 }));
    expect(calendarBucket(jan, { unit: "M", n: 3 })).not.toBe(calendarBucket(apr, { unit: "M", n: 3 }));
  });

  it("places live ticks in the right bucket", () => {
    const last = bar(10 * DAY + 555 * 60, 1, 1, 1, 1);
    expect(liveBucket(last, last.time + 100, "5")).toEqual({ kind: "same" });
    expect(liveBucket(last, last.time + 301, "5")).toEqual({ kind: "new", time: last.time + 300 });
    expect(liveBucket(last, last.time - 1, "5")).toEqual({ kind: "stale" });
    // next session re-anchors to 09:15
    const next = 11 * DAY + 555 * 60 + 130;
    expect(liveBucket(last, next, "75")).toEqual({ kind: "new", time: 11 * DAY + 555 * 60 });
  });
});

describe("non-time chart types", () => {
  const closes = [100, 101, 103, 104, 102, 99, 98, 101, 105];
  const bars = closes.map((c, i) => bar(1000 + i * 60, c, c + 0.5, c - 0.5, c, 5));

  it("renko: one box to continue, two to reverse; strictly rising times", () => {
    const out = renko(bars, 2);
    expect(out.length).toBeGreaterThan(2);
    for (let i = 1; i < out.length; i++) expect(out[i].time).toBeGreaterThan(out[i - 1].time);
    for (const b of out) expect(Math.abs(b.close - b.open)).toBeCloseTo(2);
  });

  it("line break reverses only past the last three lines", () => {
    const out = lineBreak(bars, 3);
    expect(out.length).toBeGreaterThan(1);
    expect(out[0].close).toBeGreaterThan(out[0].open);
  });

  it("kagi segments alternate direction", () => {
    const out = kagi(bars, 3);
    expect(out.length).toBeGreaterThanOrEqual(2);
    for (let i = 1; i < out.length; i++) expect(Math.sign(out[i].close - out[i].open)).not.toBe(Math.sign(out[i - 1].close - out[i - 1].open));
  });

  it("point & figure alternates X and O columns", () => {
    const out = pointFigure(bars, 1, 3);
    expect(out.length).toBeGreaterThanOrEqual(2);
    for (let i = 1; i < out.length; i++) expect(out[i].flag).not.toBe(out[i - 1].flag);
  });

  it("range bars never exceed the range", () => {
    const out = rangeBars(bars, 2);
    for (const b of out.slice(0, -1)) expect(b.high - b.low).toBeCloseTo(2);
  });
});

describe("time display", () => {
  it("formats exchange time and other zones", () => {
    const t = Date.UTC(2026, 9, 5, 9, 15) / 1000; // chart time = IST wall clock
    const ist = wallClock(t, "exchange");
    expect(formatDate(ist, "yyyy-MM-dd")).toBe("2026-10-05");
    expect(formatClock(ist, false)).toBe("09:15");
    expect(formatClock(ist, true)).toBe("9:15 AM");
    const utc = wallClock(t, "UTC");
    expect(formatClock(utc, false)).toBe("03:45");
  });
});
