import { describe, expect, it } from "vitest";
import { conditionMet, evaluate, migrateAlert, newAlert } from "../components/trade/alerts";
import {
  TOOLS,
  drawingValueAt,
  finalizeDrawing,
  geometry,
  hitHandle,
  hitTest,
  snapAngle,
  type Drawing,
  type DrawingDeps,
} from "../components/trade/drawings";
import {
  INDICATORS,
  argsLabel,
  migrateActive,
  newInstance,
  sanitizeInstance,
  visibleOnInterval,
} from "../components/trade/indicatorCatalog";
import { candlePatterns, zigzag } from "../components/trade/indicatorMore";
import { barTime, logicalToX, timeToBar, xToLogical, type Bar } from "../components/trade/chartData";

// ---------------------------------------------------------------------------
// alerts
// ---------------------------------------------------------------------------

describe("alerts", () => {
  it("evaluates crossing conditions", () => {
    expect(conditionMet("cross", 99, 101, 100)).toBe(true);
    expect(conditionMet("cross", 101, 99, 100)).toBe(true);
    expect(conditionMet("crossUp", 101, 99, 100)).toBe(false);
    expect(conditionMet("crossDown", 101, 99, 100)).toBe(true);
    expect(conditionMet("gt", 90, 101, 100)).toBe(true);
    expect(conditionMet("enter", 90, 101, 100, 105)).toBe(true);
    expect(conditionMet("exit", 101, 106, 100, 105)).toBe(true);
  });

  it("fires once, once per bar, and only on bar close when asked", () => {
    const once = newAlert(100);
    const fired = evaluate(once, 99, 101, 100, 100, 1, false);
    expect(fired?.active).toBe(false);
    expect(evaluate(fired!, 99, 101, 100, 100, 1, false)).toBeNull();

    const perBar = newAlert(100, { trigger: "oncePerBar" });
    const a = evaluate(perBar, 99, 101, 100, 100, 5, false)!;
    expect(a.active).toBe(true);
    expect(evaluate(a, 99, 101, 100, 100, 5, false)).toBeNull(); // same bar
    expect(evaluate(a, 99, 101, 100, 100, 6, false)).not.toBeNull(); // next bar

    const onClose = newAlert(100, { trigger: "oncePerBarClose" });
    expect(evaluate(onClose, 99, 101, 100, 100, 1, false)).toBeNull();
    expect(evaluate(onClose, 99, 101, 100, 100, 1, true)).not.toBeNull();
  });

  it("expires and migrates old {id, price} alerts", () => {
    const exp = newAlert(100, { expires: 1000 });
    expect(evaluate(exp, 99, 101, 100, 100, 1, false, 2000)).toBeNull();
    const old = migrateAlert({ id: "x", price: 123 });
    expect(old).toMatchObject({ id: "x", value: 123, cond: "cross", trigger: "once", active: true });
    expect(migrateAlert(null)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// drawings — a linear fake chart: x = time, y = 1000 - price
// ---------------------------------------------------------------------------

function fakeDeps(drawings: Drawing[] = [], candles: Bar[] = []): DrawingDeps {
  return {
    drawings: () => drawings,
    pending: () => null,
    selectedId: () => null,
    hoverId: () => null,
    timeToX: (t) => t,
    xToTime: (x) => x,
    priceToY: (p) => 1000 - p,
    yToPrice: (y) => 1000 - y,
    timeToLogical: (t) => t / 10,
    barSpacing: () => 10,
    candles: () => candles,
    intervalGroup: () => "minutes",
    lineColor: () => "#FFD700",
    accent: () => "#FF8C00",
    upColor: () => "#26A69A",
    downColor: () => "#EF5350",
    bgColor: () => "#111111",
    priceFormatter: (p) => p.toFixed(2),
    lastPrice: () => 500,
    toolDefaults: () => undefined,
    hidden: () => false,
    repaint: () => undefined,
  };
}

describe("drawings", () => {
  it("every tool produces shapes for a full set of points", () => {
    const deps = fakeDeps([], Array.from({ length: 80 }, (_, i) => ({ time: i * 10, open: 500, high: 505, low: 495, close: 501, volume: 10 })));
    for (const t of TOOLS) {
      const need = typeof t.points === "number" ? t.points : 3;
      const pts = Array.from({ length: need }, (_, i) => ({ time: 100 + i * 60, price: 500 + (i % 2 ? 40 : -20) }));
      let d: Drawing = { id: t.id, type: t.id, points: pts, text: t.text, screen: t.screen ? { x: 0.3, y: 0.3 } : undefined, data: t.id === "icon" ? { emoji: "🚀" } : undefined };
      d = finalizeDrawing(d, deps, 800, 600);
      const shapes = geometry(d, deps, 800, 600);
      expect(shapes.length, `${t.id} draws nothing`).toBeGreaterThan(0);
    }
  });

  it("hit-tests a trend line and its handles", () => {
    const d: Drawing = { id: "a", type: "trend", points: [{ time: 100, price: 500 }, { time: 300, price: 700 }] };
    const deps = fakeDeps([d]);
    // midpoint: x=200, price 600 → y=400
    expect(hitTest(d, 200, 400, deps, 800, 600)).toBe(true);
    expect(hitTest(d, 200, 300, deps, 800, 600)).toBe(false);
    expect(hitHandle(d, 300, 300, deps)).toBe(1);
  });

  it("long position gets target / stop anchors and a risk-sized quantity", () => {
    const deps = fakeDeps();
    const d = finalizeDrawing({ id: "p", type: "long", points: [{ time: 100, price: 500 }] }, deps, 800, 600);
    expect(d.points).toHaveLength(3);
    expect(d.points[1].price).toBeGreaterThan(500); // target above for a long
    expect(d.points[2].price).toBeLessThan(500); // stop below
    expect(d.points[1].time).toBe(d.points[2].time);
  });

  it("reads a line's price at a time (alerts on drawings)", () => {
    const deps = fakeDeps();
    const ray: Drawing = { id: "r", type: "ray", points: [{ time: 100, price: 500 }, { time: 200, price: 600 }] };
    expect(drawingValueAt(ray, 300, deps)).toBeCloseTo(700);
    const trend: Drawing = { ...ray, type: "trend" };
    expect(drawingValueAt(trend, 300, deps)).toBeNull(); // not extended
    expect(drawingValueAt({ id: "h", type: "hline", points: [{ time: 0, price: 42 }] }, 999, deps)).toBe(42);
  });

  it("snaps to 45° steps", () => {
    const p = snapAngle({ x: 0, y: 0 }, { x: 100, y: 8 });
    expect(p.y).toBeCloseTo(0);
    const q = snapAngle({ x: 0, y: 0 }, { x: 100, y: 90 });
    expect(q.x).toBeCloseTo(q.y);
  });
});

// ---------------------------------------------------------------------------
// indicator catalog
// ---------------------------------------------------------------------------

describe("indicator catalog", () => {
  // 60 sessions of 5-minute bars (09:15 → 15:25), so daily / weekly / monthly
  // indicators (ADR, CPR W / M, previous-day levels) have history to use.
  const candles = Array.from({ length: 60 * 75 }, (_, i) => {
    const day = Math.floor(i / 75);
    const c = 100 + Math.sin(i / 7) * 5 + i * 0.01;
    return {
      time: (20000 + day) * 86400 + 33300 + (i % 75) * 300,
      open: i % 9 === 0 ? c : c - 0.3,
      high: c + 1,
      low: c - 1,
      close: c,
      volume: 1000 + (i % 13) * 50,
      oi: 50000 + (i % 40) * 100,
    };
  });
  const other = new Map(candles.map((k, i) => [k.time, k.close * 1.5 + Math.cos(i / 3)]));

  it("every indicator computes plots aligned to the candles", () => {
    for (const def of INDICATORS) {
      const inst = newInstance(def.type)!;
      const res = def.compute(candles, inst.inputs, { interval: "5", up: "#0f0", down: "#f00", flow: new Map(), other: () => other });
      expect(res.plots, def.type).toHaveLength(def.plots.length);
      for (const p of res.plots) expect(p.length).toBe(candles.length);
      for (const p of res.plots) expect(p.every((v) => v === null || Number.isFinite(v)), `${def.type} has NaN`).toBe(true);
      // flow, CVD, the options flow and book pressure read recorded ticks only: with none (an empty flow map) they're rightly empty
      if (!["flow", "cvd", "optflow", "bookpressure"].includes(def.type) && def.plots.length) expect(res.plots[0].some((v) => v !== null), `${def.type} is all null`).toBe(true);
      for (const m of res.marks ?? []) expect(m.i >= 0 && m.i < candles.length && m.plot < def.plots.length).toBe(true);
    }
  });

  it("covers the spec's indicator list", () => {
    const names = new Set(INDICATORS.map((d) => d.name.toLowerCase()));
    for (const n of [
      "Arnaud Legoux Moving Average", "Hull Moving Average", "McGinley Dynamic", "Guppy Multiple Moving Average", "EMA Cross", "MA with EMA Cross",
      "Bollinger Bands %B", "Bollinger Bands Width", "Chande Kroll Stop", "Price Channel", "Standard Error Bands",
      "Connors RSI", "Fisher Transform", "Know Sure Thing", "True Strength Index", "Ultimate Oscillator", "SMI Ergodic Indicator",
      "Vortex Indicator", "Williams Alligator", "Williams Fractal", "Zig Zag", "Accumulative Swing Index",
      "Accumulation/Distribution", "Klinger Oscillator", "Price Volume Trend", "Volume Profile Visible Range",
      "Relative Volatility Index", "Volatility O-H-L-C", "Correlation Coefficient", "Ratio", "Spread",
      "Jurik Moving Average", "Auto Fib Retracement", "Candlestick Patterns", "RSI Momentum Divergence", "Open Interest",
    ]) expect(names.has(n.toLowerCase()), n).toBe(true);
    expect(INDICATORS.length).toBeGreaterThanOrEqual(130);
    expect(INDICATORS.filter((d) => d.desk).length).toBeGreaterThanOrEqual(20);
  });

  it("finds classic candlestick patterns and zig zag swings", () => {
    const bar = (o: number, h: number, l: number, c: number, i: number) => ({ time: i * 60, open: o, high: h, low: l, close: c, volume: 1 });
    const engulf = [bar(10, 10.2, 9, 9.2, 0), bar(9.1, 10.6, 9, 10.5, 1)];
    expect(candlePatterns(engulf).map((h) => h.name)).toContain("Bullish Engulfing");
    const swings = [10, 12, 14, 16, 13, 10, 8, 11, 15].map((c, i) => bar(c, c + 0.1, c - 0.1, c, i));
    const zz = zigzag(swings, 10, 1);
    expect(zz.filter((p) => p.confirmed).map((p) => p.high)).toEqual([false, true, false]);
  });

  it("instances keep their own inputs; legend args read like TradingView", () => {
    const ema9 = newInstance("ema", { length: 9 })!;
    const ema21 = newInstance("ema", { length: 21 })!;
    expect(ema9.uid).not.toBe(ema21.uid);
    expect(argsLabel(ema21)).toBe("21 close 0");
  });

  it("migrates the old boolean toggles and repairs stored instances", () => {
    const list = migrateActive({ ema: true, rsi: true, sma: false }, true);
    expect(list.map((i) => i.type)).toEqual(["volume", "ema", "rsi"]);
    expect(list[1].inputs.length).toBe(50);
    expect(sanitizeInstance({ type: "nope" })).toBeNull();
    const fixed = sanitizeInstance({ type: "bb", inputs: { length: 30 } })!;
    expect(fixed.inputs).toMatchObject({ length: 30, mult: 2 });
    expect(fixed.plots).toHaveLength(3);
  });

  it("per-timeframe visibility", () => {
    const i = newInstance("rsi")!;
    i.vis = { minutes: { on: true, min: 1, max: 15 }, days: { on: false, min: 1, max: 366 } };
    expect(visibleOnInterval(i, "5")).toBe(true);
    expect(visibleOnInterval(i, "30")).toBe(false);
    expect(visibleOnInterval(i, "D")).toBe(false);
    expect(visibleOnInterval(i, "60")).toBe(true); // hours untouched
  });
});

describe("drawing anchors across timeframes (TradingView rule)", () => {
  const D = 86400, OPEN = 9 * 3600 + 15 * 60;
  const bar = (time: number): Bar => ({ time, open: 1, high: 1, low: 1, close: 1, volume: 0 });
  // two sessions of 5m bars (09:15-15:25), and a Fri / Mon pair of 1D bars
  const m5 = [0, 1].flatMap((d) => Array.from({ length: 75 }, (_, i) => bar(d * D + OPEN + i * 300)));
  const d1 = [bar(0), bar(3 * D)];

  it("a 1D anchor lands on that day's first 5m bar, not in the overnight gap", () => {
    expect(timeToBar(m5, D, 300)).toBe(75);
  });
  it("a 5m anchor sits inside its own day's 1D bar, never across the weekend", () => {
    const l = timeToBar(d1, OPEN + 10 * 300, D)!;
    expect(l).toBeGreaterThanOrEqual(0);
    expect(l).toBeLessThan(1);
  });
  it("bar times round-trip, and a gap time snaps to the next bar", () => {
    for (const l of [0, 37, 74, 75, 149, 152]) expect(timeToBar(m5, barTime(m5, l, 300)!, 300)).toBeCloseTo(l);
    expect(timeToBar(d1, 2 * D, D)).toBe(1); // Sunday → Monday
  });

  // The engine's time scale: whole indexes only — a fractional one is 0 (the pane's left edge).
  const engine = { logicalToCoordinate: (l: number) => (l % 1 === 0 ? 100 + l * 8 : 0) };
  const m15 = [0, 1].flatMap((d) => Array.from({ length: 25 }, (_, i) => bar(d * D + OPEN + i * 900)));

  it("a 5m anchor between 15m bars keeps its place instead of jumping to the left edge", () => {
    const l = timeToBar(m15, OPEN + 7 * 300, 900)!; // 09:50 → 2⅓ bars into the 15m session
    expect(l).toBeCloseTo(7 / 3);
    expect(logicalToX(engine, l)).toBeCloseTo(100 + (7 / 3) * 8);
    expect(logicalToX(engine, -1.5)).toBeCloseTo(88); // before the first bar
    expect(logicalToX(engine, 60.25)).toBeCloseTo(582); // in the future
  });
  it("the same trendline has the same slope in time on 5m and 15m", () => {
    const a = OPEN + 4 * 300, b = D + OPEN + 31 * 300; // both off the 15m grid
    const on = (c: Bar[], span: number) => [a, b].map((t) => logicalToX(engine, timeToBar(c, t, span)!)!);
    const [x5a, x5b] = on(m5, 300), [x15a, x15b] = on(m15, 900);
    // 5m is 3× as many bars as 15m: the span in pixels shrinks by exactly 3.
    expect((x5b - x5a) / (x15b - x15a)).toBeCloseTo(3);
  });
  it("x → logical round-trips, and bar centres are whole indexes", () => {
    for (const l of [0, 2.5, 74.2, -3, 90.75]) expect(xToLogical(engine, logicalToX(engine, l)!)).toBeCloseTo(l);
    expect(xToLogical(engine, 100 + 3 * 8)).toBe(3);
    expect(xToLogical({ logicalToCoordinate: () => null }, 50)).toBeNull();
  });
});
