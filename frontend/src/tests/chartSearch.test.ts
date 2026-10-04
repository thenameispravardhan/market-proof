import { describe, expect, it } from "vitest";
import { isValidElement, type ReactElement } from "react";
import { exchTicker, highlight, hitMatches } from "../components/trade/ChartDialogs";
import { kagi, pointFigure, pricePath, renko, type Bar } from "../components/trade/chartData";
import { DEFAULT_SETTINGS, brickInputs } from "../components/trade/chartSettings";
import { templateScope } from "../components/trade/IndicatorDialogs";
import type { InstrumentHit } from "../types";

const hit = (p: Partial<InstrumentHit>): InstrumentHit => ({
  symbol: "NSE:SBIN-EQ",
  short_name: "SBIN",
  exchange: "NSE",
  segment: "EQ",
  instrument_type: "EQ",
  lot_size: 1,
  tick_size: 0.05,
  expiry: null,
  strike: null,
  underlying: null,
  display: "SBIN · NSE:EQ",
  ...p,
});

describe("symbol search", () => {
  it("shows EXCHANGE:TICKER without the -EQ / -INDEX suffix", () => {
    expect(exchTicker(hit({}))).toBe("NSE:SBIN");
    expect(exchTicker(hit({ symbol: "NSE:NIFTY50-INDEX" }))).toBe("NSE:NIFTY50");
    expect(exchTicker(hit({ symbol: "NSE:NIFTY25JUN24500CE" }))).toBe("NSE:NIFTY25JUN24500CE");
  });

  it("wraps every case-insensitive match in a mark", () => {
    const out = highlight("NSE:SBIN", "sb") as unknown[];
    const marks = out.filter((x) => isValidElement(x)) as ReactElement<{ children: string }>[];
    expect(marks).toHaveLength(1);
    expect(marks[0].props.children).toBe("SB");
    expect(highlight("NSE:SBIN", "  ")).toBe("NSE:SBIN");
    // regex metacharacters in the query are literal
    expect(() => highlight("A+B (x)", "+ (")).not.toThrow();
  });

  it("filters by type chip and source like the API", () => {
    const fut = hit({ symbol: "NSE:NIFTY25JUNFUT", segment: "FO", instrument_type: "FUT" });
    const ce = hit({ symbol: "NSE:NIFTY25JUN24500CE", segment: "FO", instrument_type: "CE" });
    const etf = hit({ symbol: "NSE:NIFTYBEES-EQ", short_name: "NIFTYBEES" });
    const bse = hit({ symbol: "BSE:SBIN-A", exchange: "BSE" });
    expect(hitMatches(fut, "futures", "all")).toBe(true);
    expect(hitMatches(ce, "futures", "all")).toBe(false);
    expect(hitMatches(ce, "options", "NFO")).toBe(true);
    expect(hitMatches(ce, "all", "NSE")).toBe(false); // NSE = cash, NFO = F&O
    expect(hitMatches(etf, "etf", "all")).toBe(true);
    expect(hitMatches(etf, "stocks", "all")).toBe(false);
    expect(hitMatches(hit({}), "stocks", "NSE")).toBe(true);
    expect(hitMatches(bse, "all", "NSE")).toBe(false);
    expect(hitMatches(bse, "all", "BSE")).toBe(true);
  });
});

const bar = (time: number, o: number, h: number, l: number, c: number): Bar => ({ time, open: o, high: h, low: l, close: c, volume: 1 });

describe("brick chart sources", () => {
  it("feeds an up bar low → high and a down bar high → low", () => {
    expect(pricePath(bar(0, 10, 12, 9, 11), "hl")).toEqual([9, 12]);
    expect(pricePath(bar(0, 11, 12, 9, 10), "hl")).toEqual([12, 9]);
    expect(pricePath(bar(0, 11, 12, 9, 10))).toEqual([10]);
  });

  it("builds more bricks from highs / lows than from closes", () => {
    const bars = [bar(1, 100, 100, 100, 100), bar(2, 100, 104.5, 99.5, 101), bar(3, 101, 101.5, 95.5, 100)];
    expect(renko(bars, 1, "close").length).toBeLessThan(renko(bars, 1, "hl").length);
    expect(pointFigure(bars, 1, 3, "hl").length).toBeGreaterThanOrEqual(pointFigure(bars, 1, 3, "close").length);
    expect(kagi(bars, 2, "hl").length).toBeGreaterThan(0);
  });

  it("per-type inputs default to ATR and honour old shared settings", () => {
    expect(brickInputs(DEFAULT_SETTINGS, "renko").method).toBe("atr");
    const legacy = { ...DEFAULT_SETTINGS, boxSize: 2.5, reversal: 4, lineBreak: 2 };
    expect(brickInputs(legacy, "renko")).toMatchObject({ method: "traditional", box: 2.5 });
    expect(brickInputs(legacy, "pnf").reversal).toBe(4);
    expect(brickInputs(legacy, "linebreak").lines).toBe(2);
    const own = { ...legacy, bricks: { renko: { method: "atr" as const, atrLength: 20 } } };
    expect(brickInputs(own, "renko")).toMatchObject({ method: "atr", atrLength: 20 });
    expect(brickInputs(own, "kagi").method).toBe("traditional"); // still the legacy box
  });
});

describe("indicator templates", () => {
  it("describes what a template switches to", () => {
    expect(templateScope({ name: "a", items: [] })).toBe("");
    expect(templateScope({ name: "a", items: [], symbol: { symbol: "NSE:TCS-EQ", name: "TCS" }, interval: "15" }, (k) => `${k}m`)).toBe("TCS · 15m");
  });
});
