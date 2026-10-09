import { describe, expect, it } from "vitest";
import {
  INDICATORS,
  INDICATOR_BY_TYPE,
  cloneInstances,
  defaultInputs,
  instanceDefaults,
  newInstance,
  sanitizeInputs,
  sanitizeInstance,
  visibleOnInterval,
  type IndicatorInstance,
} from "../components/trade/indicatorCatalog";
import { atr, type OhlcvCandle } from "../lib/indicators";

function bars(n: number): OhlcvCandle[] {
  let s = 7;
  const r = () => (s = (s * 16807) % 2147483647) / 2147483647;
  let p = 100;
  return Array.from({ length: n }, (_, i) => {
    const o = p;
    p = Math.max(1, p + (r() - 0.5) * 2);
    return { time: 1_700_000_000 + i * 300, open: o, high: Math.max(o, p) + r(), low: Math.min(o, p) - r(), close: p, volume: Math.floor(r() * 1000) };
  });
}

const ctx = { interval: "5", up: "#0f0", down: "#f00", other: () => undefined };

describe("indicator settings: saved values are repaired on load", () => {
  it("clamps, rounds and parses inputs; drops unknown keys; bad options fall back", () => {
    const inst = sanitizeInstance({
      uid: "a",
      type: "bb",
      inputs: { length: 0, mult: "2.5", source: "vwap", offset: 3.6, junk: 1 },
    })!;
    expect(inst.inputs).toEqual({ length: 1, mult: 2.5, source: "close", offset: 4 });
  });

  it("non-numeric numbers fall back to the default", () => {
    expect(sanitizeInstance({ type: "rsi", inputs: { length: "abc" } })!.inputs.length).toBe(defaultInputs(INDICATOR_BY_TYPE.get("rsi")!).length);
    expect(sanitizeInstance({ type: "rsi", inputs: { length: null } })!.inputs.length).toBe(defaultInputs(INDICATOR_BY_TYPE.get("rsi")!).length);
  });

  it("repairs plot styles, precision, visibility ranges and placement", () => {
    const inst = sanitizeInstance({
      type: "macd",
      plots: [{ width: 9, dash: 5, kind: "bogus", color: "" }, null, { visible: false }],
      precision: 42,
      vis: { minutes: { on: true, min: 30, max: 5 }, hours: { on: true, min: -3, max: 99 } },
      pane: "",
      scale: "middle",
    })!;
    expect(inst.plots[0]).toMatchObject({ width: 4, dash: 0 });
    expect(inst.plots[0].kind).toBeUndefined();
    expect(inst.plots[0].color).toMatch(/^#/);
    expect(inst.plots[2].visible).toBe(false);
    expect(inst.precision).toBeNull();
    expect(inst.vis?.minutes).toEqual({ on: true, min: 5, max: 30 });
    expect(inst.vis?.hours).toEqual({ on: true, min: 1, max: 24 });
    expect(inst.pane).toBeUndefined();
    expect(inst.scale).toBeUndefined();
  });

  it("saved defaults with out-of-range inputs can't create a broken indicator", () => {
    expect(newInstance("ema", { length: -5 })!.inputs.length).toBe(1);
  });

  it("every catalog default is itself valid", () => {
    for (const d of INDICATORS) expect(sanitizeInputs(d, defaultInputs(d)), d.type).toEqual(defaultInputs(d));
  });

  it("every indicator computes at the edges of its input ranges", () => {
    const c = bars(300);
    for (const d of INDICATORS) {
      for (const edge of ["min", "max"] as const) {
        const raw = Object.fromEntries(d.inputs.map((i) => [i.key, i[edge] ?? i.def]));
        const res = d.compute(c, sanitizeInputs(d, raw), ctx);
        expect(res.plots.length, d.type).toBe(d.plots.length);
        for (const p of res.plots) expect(p.length, d.type).toBe(c.length);
      }
    }
  });
});

describe("indicator settings: applying", () => {
  it("a visibility range typed backwards still shows the indicator inside it", () => {
    const inst = { ...newInstance("ema")!, vis: { minutes: { on: true, min: 30, max: 5 } } } as IndicatorInstance;
    expect(visibleOnInterval(inst, "15")).toBe(true);
    expect(visibleOnInterval(inst, "1")).toBe(false);
  });

  it("a visibility range that runs to the group's end includes longer minute charts", () => {
    const inst = { ...newInstance("ema")!, vis: { minutes: { on: true, min: 15, max: 59 } } } as IndicatorInstance;
    expect(visibleOnInterval(inst, "90")).toBe(true);
    expect(visibleOnInterval(inst, "5")).toBe(false);
    const capped = { ...inst, vis: { minutes: { on: true, min: 1, max: 30 } } } as IndicatorInstance;
    expect(visibleOnInterval(capped, "45")).toBe(false);
  });

  it("save as default from a hidden, pinned copy keeps neither flag", () => {
    const inst = { ...newInstance("ema")!, visible: false, scale: "left" as const, pane: "own" };
    const d = instanceDefaults(inst) as Record<string, unknown>;
    expect(d.visible).toBeUndefined();
    expect(d.scale).toBeUndefined();
    expect(d.pane).toBeUndefined();
    expect(d.inputs).toEqual(inst.inputs);
  });

  it("templates keep merged panes attached after uids are renewed", () => {
    const host = newInstance("rsi")!;
    const guest = { ...newInstance("ema")!, pane: host.uid };
    const solo = { ...newInstance("macd")!, pane: "own" };
    const [h2, g2, s2] = cloneInstances([host, guest, solo]);
    expect(h2.uid).not.toBe(host.uid);
    expect(g2.pane).toBe(h2.uid);
    expect(s2.pane).toBe("own");
  });
});

describe("indicator maths", () => {
  it("atr matches TradingView's ta.atr (RMA of TR, first TR = high - low)", () => {
    const c = bars(60);
    const tr = c.map((k, i) => (i === 0 ? k.high - k.low : Math.max(k.high - k.low, Math.abs(k.high - c[i - 1].close), Math.abs(k.low - c[i - 1].close))));
    let prev = tr.slice(0, 14).reduce((a, b) => a + b, 0) / 14;
    const ref: (number | null)[] = tr.map((t, i) => {
      if (i < 13) return null;
      if (i > 13) prev = (prev * 13 + t) / 14;
      return prev;
    });
    const got = atr(c, 14);
    got.forEach((v, i) => (ref[i] === null ? expect(v).toBeNull() : expect(v).toBeCloseTo(ref[i] as number, 9)));
  });

  it("ichimoku cloud leads and the lagging span trails by displacement - 1 bars", () => {
    const d = INDICATOR_BY_TYPE.get("ichimoku")!;
    const res = d.compute(bars(120), defaultInputs(d), ctx);
    expect(res.shifts).toEqual([0, 0, -25, 25, 25]);
  });
});
