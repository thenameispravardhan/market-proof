// Same-story grouping: different outlets' headlines for one story join; different stocks, or headlines that only
// share a couple of words, never do.
import { describe, it, expect } from "vitest";
import { clusterStories, storyTokens, tickerName } from "./newsStories";

const REL = "NSE:RELIANCE-EQ";
const min = 60_000;

describe("storyTokens", () => {
  it("drops stop words, stems lightly, keeps numbers and codes, adds the stocks", () => {
    const t = storyTokens("Reliance's Q2 net profit rises 10% to ₹19,323 crore", [REL]);
    expect([...t].sort()).toEqual(["$NSE:RELIANCE-EQ", "10", "19323", "net", "profit", "q2", "reliance", "rise"].sort());
  });
});

describe("clusterStories", () => {
  it("joins one story's reports and points them all at the first", () => {
    const items = [
      { id: 2, title: "RIL Q2 net profit rises 10% to ₹19,323 crore; revenue up 5%", t: 4 * min, tickers: [REL] },
      { id: 1, title: "Reliance Industries Q2 results: net profit rises 10%", t: 0, tickers: [REL] },
      { id: 3, title: "Reliance Q2 profit jumps 10% on retail, Jio growth", t: 9 * min, tickers: [REL] },
      { id: 4, title: "Reliance Retail to acquire stake in a beauty brand", t: 10 * min, tickers: [REL] },
    ];
    const lead = clusterStories(items);
    expect([lead.get(1), lead.get(2), lead.get(3)]).toEqual([1, 1, 1]);
    expect(lead.get(4)).toBe(4);                                   // same stock, another story
  });

  it("keeps different stocks apart however alike the words", () => {
    const lead = clusterStories([
      { id: 1, title: "TCS Q2 profit rises 5% to ₹12,000 crore", t: 0, tickers: ["NSE:TCS-EQ"] },
      { id: 2, title: "Infosys Q2 profit rises 5% to ₹12,000 crore", t: min, tickers: ["NSE:INFY-EQ"] },
    ]);
    expect(lead.get(2)).toBe(2);
  });

  it("needs more in common than the stock itself", () => {
    const lead = clusterStories([
      { id: 1, title: "Reliance shares rise 2%", t: 0, tickers: [REL] },
      { id: 2, title: "Reliance shares fall 2%", t: 30 * min, tickers: [REL] },
    ]);
    expect(lead.get(2)).toBe(2);
  });

  it("joins market wraps from two outlets, not headlines sharing two words or six hours apart", () => {
    const lead = clusterStories([
      { id: 1, title: "Stock market today: Sensex, Nifty open higher", t: 0 },
      { id: 2, title: "Sensex, Nifty open higher amid positive global cues", t: 2 * min },
      { id: 3, title: "Sensex, Nifty end lower as banks drag", t: 3 * min },
      { id: 4, title: "Sensex, Nifty open higher amid positive global cues", t: 7 * 60 * min },
    ]);
    expect(lead.get(2)).toBe(1);
    expect(lead.get(3)).toBe(3);
    expect(lead.get(4)).toBe(4);                                   // the next day's open is another story
  });
});

describe("tickerName", () => {
  it("strips the exchange and the series", () => {
    expect(["NSE:BAJAJ-AUTO-EQ", "BSE:ACME-A", "NSE:M&M-EQ", "NSE:MCDOWELL-N-EQ"].map(tickerName)).toEqual(["BAJAJ-AUTO", "ACME", "M&M", "MCDOWELL-N"]);
  });
});
