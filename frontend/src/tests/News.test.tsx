// News page: ships off with an "enable" switch, highlights watchlist words safely, filters by source,
// and the Settings dialog saves the operator's choices to /api/news/settings.
import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import News from "../pages/News";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const now = new Date().toISOString();
const SETTINGS = {
  enabled: false, interval_s: 20, sources: { Moneycontrol: true, BSE: true }, custom_feeds: [],
  watchlist: ["Reliance"], muted: ["horoscope"], alerts: false, retention_days: 7,
};
const ITEMS = [
  { id: 1, url: "https://mc.example/1", source: "Moneycontrol", title: "Reliance wins <b>order</b>", summary: null, published_at: now, seen_at: now, tone: 1 },
  { id: 2, url: "https://bse.example/2", source: "BSE", title: "TCS board meeting", summary: "Results", published_at: now, seen_at: now },
  { id: 3, url: "https://mc.example/3", source: "Moneycontrol", title: "Daily horoscope", summary: null, published_at: now, seen_at: now },
];
let puts: unknown[] = [];

/** Open the Filters panel (types, sources, extra switches, shortcuts) once. */
async function openFilters(): Promise<void> {
  const b = await screen.findByTestId("news-filters-btn");
  if (b.getAttribute("aria-expanded") !== "true") fireEvent.click(b);
}

beforeEach(() => {
  puts = [];
  try { localStorage.removeItem("news:lastSeen"); localStorage.removeItem("news:prefs"); } catch { /* storage off */ }
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes("/api/news/settings") && init?.method === "PUT") {
      const body = JSON.parse(String(init.body));
      puts.push(body);
      return json(body);
    }
    if (url.includes("/api/news/items")) return json({ items: url.includes("since_id") ? [] : ITEMS, max_id: 3 });
    if (url.includes("/api/news/status")) return json({
      settings: SETTINGS, known_sources: ["BSE", "Moneycontrol"], collector: { polls: 0, last_poll_at: null, last_poll_ms: null },
      sources: [{ source: "Moneycontrol", url: "u1", custom: false, enabled: true, health: "off", last_ok: null, error: null, paused_until: null, items_in_feed: 0, hour: 2, day: 2, total: 2, last_item: now }],
    });
    return json({}, 404);
  }) as unknown as typeof fetch;
});

describe("News page", () => {
  it("shows headlines with watchlist highlight, hides muted words, never renders HTML from a title", async () => {
    render(<News />);
    expect(await screen.findByText("TCS board meeting")).toBeTruthy();
    const mark = screen.getByText("Reliance");
    expect(mark.tagName).toBe("MARK");                                 // watchlist word highlighted
    expect(screen.getByText(/wins <b>order<\/b>/)).toBeTruthy();       // markup stays text
    expect(screen.queryByText("Daily horoscope")).toBeNull();           // muted
    fireEvent.click(screen.getByTestId("news-section-insights"));     // the poll status line lives under Insights
    expect(screen.getByText(/never sent to AI analysis/)).toBeTruthy();
  });

  it("filters by source chip", async () => {
    render(<News />);
    await screen.findByText("TCS board meeting");
    await openFilters();   // types, sources and the extra switches sit in the Filters panel
    fireEvent.click(screen.getByRole("button", { name: /^BSE/ }));
    expect(screen.queryByText(/wins/)).toBeNull();
    expect(screen.getByText("TCS board meeting")).toBeTruthy();
  });

  it("filters by headline tone", async () => {
    render(<News />);
    await screen.findByText("TCS board meeting");
    await openFilters();
    fireEvent.click(screen.getByTestId("news-tone-1"));
    expect(screen.queryByText("TCS board meeting")).toBeNull();       // tone 0: not positive
    expect(screen.getByText(/wins <b>order<\/b>/)).toBeTruthy();
  });

  it("is off until enabled, and Settings saves the interval and watchlist", async () => {
    render(<News />);
    fireEvent.click(await screen.findByTestId("news-enable"));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]).toMatchObject({ enabled: true });

    fireEvent.click(screen.getByTestId("news-settings-btn"));
    const dlg = await screen.findByTestId("news-settings");
    const interval = within(dlg).getByLabelText("Poll interval") as HTMLInputElement;
    fireEvent.change(interval, { target: { value: "45" } });
    fireEvent.click(within(dlg).getByRole("tab", { name: "Watchlist & alerts" }));
    const add = within(dlg).getByPlaceholderText(/Add a word or company/);
    fireEvent.change(add, { target: { value: "RBI" } });
    fireEvent.keyDown(add, { key: "Enter" });
    fireEvent.click(within(dlg).getByTestId("news-save"));
    await waitFor(() => expect(puts).toHaveLength(2));
    expect(puts[1]).toMatchObject({ interval_s: 45, watchlist: ["Reliance", "RBI"] });
  });
});

describe("News page: full text from the scraper", () => {
  it("shows a TEXT badge and opens the extracted article in a reader", async () => {
    const withText = [{ ...ITEMS[1], extract: { status: "done", chars: 2400, error: null } },
      { ...ITEMS[0], extract: { status: "blocked", chars: 0, error: "robots.txt" } }];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/news/article")) return json({
        status: "done", text: "Full article body about TCS results.", words: 6, date: now,
        meta: { author: "Asha Rao", tags: ["Earnings"] }, tables: [[["Quarter", "Revenue"], ["Q2", "1234"]]], images: [],
      });
      if (url.includes("/api/news/items")) return json({ items: url.includes("since_id") ? [] : withText, max_id: 3 });
      if (url.includes("/api/news/status")) return json({
        settings: { ...SETTINGS, enabled: true, extract: true, extract_sources: { BSE: true, Moneycontrol: true } },
        known_sources: ["BSE", "Moneycontrol"], collector: { polls: 1, last_poll_at: null, last_poll_ms: null }, sources: [],
        extract: { handed: 2, by_status: { done: 1, blocked: 1 }, with_text: 1, paused: [] },
      });
      return json({}, 404);
    }) as unknown as typeof fetch;
    render(<News />);
    expect((await screen.findByTitle("robots.txt")).textContent).toBe("blocked");   // why there is no text, on hover
    fireEvent.click(screen.getByRole("button", { name: /TEXT/ }));
    const reader = await screen.findByTestId("news-reader");
    expect(within(reader).getByText(/Full article body/)).toBeTruthy();
    expect(within(reader).getByText(/Asha Rao/)).toBeTruthy();
    expect(within(reader).getByText("1234")).toBeTruthy();                // table rendered
    fireEvent.click(await screen.findByTestId("news-section-insights"));   // stats / panels live under Insights
    expect(screen.getByTestId("news-extract")).toBeTruthy();              // full-text stats panel
  });
});

describe("News page: detection lag and trends", () => {
  it("shows each headline's lag and the stats strip, and no trending-words row", async () => {
    const seen = new Date(Date.now() - 5 * 60_000);
    const mk = (id: number, title: string, lagS: number) => ({ id, url: `https://x.example/${id}`, source: "Mint", title, summary: null,
      published_at: new Date(seen.getTime() - lagS * 1000).toISOString(), seen_at: seen.toISOString() });
    const first = { ...mk(0, "Old backlog story", 7200), seen_at: new Date(Date.now() - 3600_000).toISOString() };   // Mint's first poll
    const list = [mk(1, "Infosys buyback approved by board", 45), mk(2, "Infosys shares rally after buyback", 900), mk(3, "RBI policy today", 30), first];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/news/items")) return json({ items: url.includes("since_id") ? [] : list, max_id: 3 });
      if (url.includes("/api/news/status")) return json({
        settings: { ...SETTINGS, watchlist: [], muted: [], enabled: true, extract: false, extract_sources: {} },
        known_sources: ["Mint"], collector: { polls: 3, last_poll_at: null, last_poll_ms: null },
        sources: [{ source: "Mint", url: "u", custom: false, enabled: true, health: "ok", last_ok: null, error: null, paused_until: null,
          items_in_feed: 3, hour: 3, day: 3, total: 3, last_item: null, lag: { p50: 45, p90: 900, n: 3 } }],
        lag: { p50: 45, p90: 900, n: 3, fastest: "Mint" },
      });
      return json({}, 404);
    }) as unknown as typeof fetch;
    render(<News />);
    fireEvent.click(await screen.findByTestId("news-section-insights"));   // stats / panels live under Insights
    const strip = await screen.findByTestId("news-stats");
    expect(within(strip).getByText("45s")).toBeTruthy();                  // median detection lag
    expect(within(strip).getAllByText("Mint").length).toBe(1);            // fastest source
    expect(screen.queryByTestId("news-trending")).toBeNull();            // removed: common words, not news
    fireEvent.click(screen.getByTestId("news-section-news"));   // the list: per-headline lag chips
    expect(screen.getByText("+45s").className).toContain("fast");        // per-headline lag, colour by speed
    expect(screen.getByText("+15m").className).toContain("slow");
    expect(screen.queryByText("+2.0h")).toBeNull();                       // first-poll backlog: no lag chip
  });

  it("updates live from the server's push, without polling", async () => {
    render(<News />);
    await screen.findByText("TCS board meeting");
    const calls = () => (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls.filter((c) => String(c[0]).includes("since_id")).length;
    const before = calls();
    window.dispatchEvent(new CustomEvent("news:event", { detail: { new: 0, polls: 5, last_poll_at: Date.now() / 1000 } }));
    expect(calls()).toBe(before);                                       // nothing new: no request
    window.dispatchEvent(new CustomEvent("news:event", { detail: { new: 2 } }));
    await waitFor(() => expect(calls()).toBe(before + 1));              // new headlines: fetched at once
  });
});

describe("News page: fixes and trader shortcuts", () => {
  it("source chip counts leave out muted headlines, like the list does", async () => {
    render(<News />);
    await screen.findByText("TCS board meeting");
    await openFilters();   // types, sources and the extra switches sit in the Filters panel
    expect(screen.getByRole("button", { name: /^Moneycontrol/ }).textContent).toBe("Moneycontrol 1");   // the horoscope is muted
  });

  it("an update that repeats headlines the page already has adds no NEW badge and no duplicate", async () => {
    const f = globalThis.fetch as unknown as (u: RequestInfo | URL, i?: RequestInit) => Promise<Response>;
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
      (String(input).includes("since_id") ? json({ items: ITEMS, max_id: 3 }) : f(input, init))) as unknown as typeof fetch;
    render(<News />);
    await screen.findByText("TCS board meeting");
    window.dispatchEvent(new CustomEvent("news:event", { detail: { new: 1 } }));
    await waitFor(() => expect((globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls.some((c) => String(c[0]).includes("since_id"))).toBe(true));
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByText("NEW")).toBeNull();
    expect(screen.getAllByText("TCS board meeting")).toHaveLength(1);
  });

  it("a feed time in the future (wrong time zone) does not pin the story to the top", async () => {
    const seen = new Date(Date.now() - 10 * 60_000).toISOString();
    const list = [
      { id: 1, url: "https://x.example/1", source: "Mint", title: "Story with a future time", summary: null, published_at: new Date(Date.now() + 5.5 * 3600_000).toISOString(), seen_at: seen },
      { id: 2, url: "https://x.example/2", source: "Mint", title: "Story from five minutes ago", summary: null, published_at: new Date(Date.now() - 5 * 60_000).toISOString(), seen_at: new Date().toISOString() },
    ];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/news/items")) return json({ items: url.includes("since_id") ? [] : list, max_id: 2 });
      if (url.includes("/api/news/status")) return json({ settings: { ...SETTINGS, watchlist: [], muted: [] }, known_sources: ["Mint"], collector: { polls: 0, last_poll_at: null, last_poll_ms: null }, sources: [] });
      return json({}, 404);
    }) as unknown as typeof fetch;
    render(<News />);
    await screen.findByText("Story with a future time");
    const titles = [...document.querySelectorAll(".news-title")].map((e) => e.textContent);
    expect(titles).toEqual(["Story from five minutes ago", "Story with a future time"]);
    expect(screen.getByText("10m")).toBeTruthy();                         // aged from when it was first seen
  });

  it("chart asks the server to look for the stocks again and links each to the Trade page at the news time", async () => {
    const f = globalThis.fetch as unknown as (u: RequestInfo | URL, i?: RequestInit) => Promise<Response>;
    const asked: string[] = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (!url.includes("/api/news/stocks")) return f(input, init);
      asked.push(url);
      return json({ tickers: ["NSE:RELIANCE-EQ"] });
    }) as unknown as typeof fetch;
    render(<News />);
    await screen.findByText(/wins/);
    fireEvent.click(screen.getAllByRole("button", { name: /chart/ })[0]);
    const box = await screen.findByTestId("news-stocks");
    const links = within(box).getAllByRole("link");
    expect(asked).toEqual(["/api/news/stocks?id=1"]);
    expect(links.map((a) => a.textContent)).toEqual(["RELIANCE ↗"]);
    expect(links[0].getAttribute("href")).toContain("#/trade?symbol=NSE%3ARELIANCE-EQ");
    expect(links[0].getAttribute("href")).toMatch(/&at=\d+/);
  });
});

describe("News page: stocks, stories, kinds and keys", () => {
  const REL = "NSE:RELIANCE-EQ", TCS = "NSE:TCS-EQ";
  const at = (m: number) => new Date(Date.now() - m * 60_000).toISOString();
  const mk = (id: number, source: string, title: string, mins: number, tickers: string[] = [], tags: string[] = []) =>
    ({ id, url: `https://x.example/${id}`, source, title, summary: null, published_at: at(mins), seen_at: at(mins), tickers, tags });
  const LIST = [
    mk(1, "Moneycontrol", "Reliance Q2 net profit rises 10% to ₹19,323 crore", 30, [REL], ["results"]),
    mk(2, "Economic Times", "RIL Q2 net profit rises 10% to ₹19,323 crore; revenue up 5%", 26, [REL], ["results"]),
    mk(3, "Mint", "Reliance Retail to acquire a beauty brand", 20, [REL], ["deals"]),
    mk(4, "Zee Business", "TCS bags $1 billion order from UK insurer", 10, [TCS], ["orders"]),
    mk(5, "NDTV", "Sensex ends 300 points higher", 5, [], ["market"]),
  ];
  const KINDS = [["results", "Results"], ["orders", "Orders"], ["deals", "Stake / M&A"], ["market", "Markets"]];
  const opened: string[] = [];
  let watching: Record<string, string[]> = {};               // watchlist word -> the stocks it names
  let statusExtra: Record<string, unknown> = {}, settingsExtra: Record<string, unknown> = {};
  let reactions: Record<string, unknown> | null = null;     // /api/news/reaction: null = Fyers not connected

  beforeEach(() => {
    opened.length = 0;
    watching = {};
    statusExtra = {}; settingsExtra = {}; reactions = null;
    vi.spyOn(window, "open").mockImplementation((u?: string | URL) => { opened.push(String(u)); return null; });
    try { localStorage.removeItem("news:prefs"); } catch { /* storage off */ }
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/news/settings") && init?.method === "PUT") { const b = JSON.parse(String(init.body)); puts.push(b); return json(b); }
      if (url.includes("/api/market/quotes")) return json({ quotes: { [REL]: { ltp: 2950, change: 36, change_pct: 1.25 } } });
      if (url.includes("/api/news/reaction")) return json(reactions ? { ok: true, reactions } : { ok: false, reactions: {} });
      if (url.includes("/api/news/items")) return json({ items: url.includes("since_id") ? [] : LIST, max_id: 5 });
      if (url.includes("/api/news/status")) return json({
        settings: { ...SETTINGS, enabled: true, watchlist: Object.keys(watching), muted: [], ...settingsExtra }, known_sources: ["Mint"], kinds: KINDS,
        collector: { polls: 3, last_poll_at: null, last_poll_ms: null }, watch_stocks: watching,
        sources: [{ source: "Moneycontrol", url: "u", custom: false, enabled: true, health: "ok", last_ok: null, error: null, paused_until: null,
          items_in_feed: 3, hour: 1, day: 1, total: 1, last_item: null }],
        lag: { p50: 45, p90: 90, n: 3, fastest: "Mint" }, ...statusExtra,
      });
      return json({}, 404);
    }) as unknown as typeof fetch;
  });

  it("folds a stock's updates of one kind, and a re-titled headline, under the newest; nothing is dropped", async () => {
    const URB = "NSE:URBANCO-EQ";
    const THREAD = [
      mk(11, "Moneycontrol", "Urban Company Q2 results: revenue jumps 41%", 50, [URB], ["results"]),
      mk(12, "Mint", "InstaHelp drags Urban Company profit lower", 40, [URB], ["results"]),
      mk(13, "Inc42", "Urban Company net loss triples in September quarter", 30, [URB], ["results"]),
      mk(14, "CNBC-TV18", "Urban Company plans ₹2,500 crore QIP", 20, [URB], ["funding"]),        // another kind: its own row
      mk(15, "BusinessLine", "Midday movers: PNB, DLF and Urban Company", 25, [URB, "NSE:PNB-EQ"], ["market"]),   // a roundup
      mk(16, "NDTV", "Sensex flat ahead of results", 15, [], ["market"]),
      { ...mk(17, "NDTV", "Sensex ends 300 points lower", 5, [], ["market"]), url: "https://x.example/16#update-1" },   // re-titled
    ];
    const f = globalThis.fetch as unknown as (u: RequestInfo | URL, i?: RequestInit) => Promise<Response>;
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => (String(input).includes("/api/news/items")
      ? json({ items: String(input).includes("since_id") ? [] : THREAD, max_id: 17 }) : f(input, init))) as unknown as typeof fetch;
    render(<News />);
    await screen.findByText(/net loss triples/);
    expect([...document.querySelectorAll(".news-title")].map((e) => e.textContent)).toEqual(["Sensex ends 300 points lower",
      "Urban Company plans ₹2,500 crore QIP", "Midday movers: PNB, DLF and Urban Company", "Urban Company net loss triples in September quarter"]);
    const row = screen.getByText(/net loss triples/).closest("article")!;
    fireEvent.click(within(row).getByRole("button", { name: /2 earlier/ }));
    expect(within(within(row).getByTestId("news-story")).getAllByRole("link").map((a) => a.textContent))
      .toEqual(["Urban Company Q2 results: revenue jumps 41%", "InstaHelp drags Urban Company profit lower"]);   // oldest first
    expect(within(screen.getByText(/300 points lower/).closest("article")!).getByRole("button", { name: /1 earlier/ })).toBeTruthy();
  });

  it("Market news only hides headlines that name no stock and no news type, and is remembered", async () => {
    const MIX = [
      mk(21, "Moneycontrol", "India vs West Indies 5th T20I: live score", 12),
      mk(22, "Moneycontrol", "TCS bags $1 billion order from UK insurer", 8, [TCS], ["orders"]),
      mk(23, "Mint", "Sensex ends 300 points higher", 4, [], ["market"]),
    ];
    const f = globalThis.fetch as unknown as (u: RequestInfo | URL, i?: RequestInit) => Promise<Response>;
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => (String(input).includes("/api/news/items")
      ? json({ items: String(input).includes("since_id") ? [] : MIX, max_id: 23 }) : f(input, init))) as unknown as typeof fetch;
    render(<News />);
    await screen.findByText(/T20I/);
    await openFilters();   // types, sources and the extra switches sit in the Filters panel
    expect(screen.getByRole("button", { name: "Moneycontrol 2" })).toBeTruthy();
    fireEvent.click(screen.getByRole("switch", { name: /Market news only/ }));
    expect(screen.queryByText(/T20I/)).toBeNull();
    expect(screen.getByText(/TCS bags/)).toBeTruthy();
    expect(screen.getByText(/Sensex ends/)).toBeTruthy();                                 // a news type, no stock: kept
    expect(screen.getByRole("button", { name: "Moneycontrol 1" })).toBeTruthy();           // the chip counts what is listed
    expect(JSON.parse(localStorage.getItem("news:prefs") || "{}").marketOnly).toBe(true);
  });

  it("counts a stock an article names only in its text, and can leave those out", async () => {
    const EXTRA = [...LIST, { ...mk(6, "Mint", "IT stocks rally on big deal wins", 3), text_tickers: [TCS] }];
    const f = globalThis.fetch as unknown as (u: RequestInfo | URL, i?: RequestInit) => Promise<Response>;
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => (String(input).includes("/api/news/items")
      ? json({ items: String(input).includes("since_id") ? [] : EXTRA, max_id: 6 }) : f(input, init))) as unknown as typeof fetch;
    render(<News />);
    expect((await screen.findByText(/IT stocks rally/)).closest("article")!.querySelector(".news-tk.text")!.textContent).toContain("TCS");
    fireEvent.click(screen.getByTestId("news-section-insights"));
    const panel = await screen.findByTestId("news-stocks-panel");
    const tcs = () => within(panel).getByRole("button", { name: "TCS" }).closest("tr")!.querySelectorAll("td")[1];
    expect(tcs().textContent).toBe("21");                                 // 2 stories, 1 of them from the article text
    fireEvent.click(within(panel).getByTestId("news-text-stocks"));
    expect(tcs().textContent).toBe("1");                                  // headlines only
  });

  it("switches insights panels and full text on and off, and remembers the panels", async () => {
    render(<News />);
    fireEvent.click(await screen.findByTestId("news-section-insights"));
    expect(await screen.findByTestId("news-stocks-panel")).toBeTruthy();
    fireEvent.click(screen.getByTestId("news-panel-stocks"));
    expect(screen.queryByTestId("news-stocks-panel")).toBeNull();
    expect(JSON.parse(localStorage.getItem("news:prefs") || "{}").hidden).toEqual(["stocks"]);
    expect(screen.getByTestId("news-extract").textContent).toMatch(/Off: headlines only/);
    fireEvent.click(screen.getByTestId("news-extract-toggle"));
    await waitFor(() => expect(puts[puts.length - 1]).toMatchObject({ extract: true }));
  });

  it("ranks the stocks in the news by stories, with their day's move, and filters the list by one", async () => {
    render(<News />);
    fireEvent.click(await screen.findByTestId("news-section-insights"));   // stats / panels live under Insights
    const panel = await screen.findByTestId("news-stocks-panel");
    const cells = (r: Element) => [...r.querySelectorAll("td")].map((c) => c.textContent);
    const rows = [...panel.querySelectorAll("tbody tr")];
    expect(rows.map((r) => cells(r).slice(0, 3))).toEqual([["RELIANCE", "2", "3"], ["TCS", "1", "1"]]);   // the repost is one story
    await waitFor(() => expect(within(panel).getByText("+1.25%").className).toContain("pos"));
    expect(within(panel).getByText("Today")).toBeTruthy();                             // shown once Fyers answers
    expect(within(panel).getByLabelText("RELIANCE chart").getAttribute("href")).toContain("symbol=NSE%3ARELIANCE-EQ");

    fireEvent.click(within(panel).getByRole("button", { name: "RELIANCE" }));
    expect(screen.queryByText(/TCS bags/)).toBeNull();
    expect(screen.getByText(/Reliance Retail/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "RELIANCE ×" }));                // the toolbar chip clears it
    expect(screen.getByText(/TCS bags/)).toBeTruthy();
  });

  it("tags each headline with its stocks and kind, and filters by kind", async () => {
    render(<News />);
    const tcs = (await screen.findByText(/TCS bags/)).closest("article")!;
    expect(within(tcs).getByLabelText("TCS chart").getAttribute("href")).toMatch(/symbol=NSE%3ATCS-EQ.*&at=\d+/);
    expect(within(tcs).getByText("Orders")).toBeTruthy();
    expect(within(tcs).getByRole("button", { name: /chart/ })).toBeTruthy();          // tagged: can still look again
    await openFilters();   // types, sources and the extra switches sit in the Filters panel
    const kinds = screen.getByTestId("news-kinds");
    expect([...kinds.querySelectorAll("button")].map((b) => b.textContent)).toEqual(["Results 2", "Orders 1", "Stake / M&A 1", "Markets 1"]);
    fireEvent.click(within(kinds).getByRole("button", { name: /^Orders/ }));
    expect(screen.queryByText(/Sensex ends/)).toBeNull();
    expect(screen.getByText(/TCS bags/)).toBeTruthy();
  });

  it("shows one story's reports from several outlets as one row, with who had it first", async () => {
    render(<News />);
    const lead = (await screen.findByText(/Reliance Q2 net profit/)).closest("article")!;
    expect(screen.queryByText(/RIL Q2 net profit/)).toBeNull();                       // folded into the first report
    expect(within(lead).getByText("1st by 4m")).toBeTruthy();
    expect(screen.getByText("4 stories · 5")).toBeTruthy();
    fireEvent.click(within(lead).getByRole("button", { name: /2 sources/ }));
    const story = within(lead).getByTestId("news-story");
    expect(within(story).getByText(/RIL Q2 net profit/)).toBeTruthy();
    expect(within(story).getByText("+4m")).toBeTruthy();
    fireEvent.click(await screen.findByTestId("news-section-insights"));   // stats / panels live under Insights
    const strip = screen.getByTestId("news-stats");
    const first = within(strip).getByText("First to report").parentElement!;
    expect(first.textContent).toContain("Moneycontrol1 of 1 shared stories");
    expect(first.getAttribute("title")).toContain("Moneycontrol 1");
    fireEvent.click(screen.getByTestId("news-section-news"));   // back to the list

    await openFilters();   // the extra switches sit in the Filters panel
    fireEvent.click(screen.getByRole("switch", { name: /2\+ sources/ }));
    expect(screen.queryByText(/TCS bags/)).toBeNull();
    expect(screen.getByText(/Reliance Q2 net profit/)).toBeTruthy();
  });

  it("lists every headline again with grouping off", async () => {
    localStorage.setItem("news:prefs", JSON.stringify({ group: false }));
    render(<News />);
    expect(await screen.findByText(/RIL Q2 net profit/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /sources$/ })).toBeNull();
  });

  it("a watchlist word that names a stock hits the headlines that call it something else", async () => {
    watching = { Reliance: [REL] };
    localStorage.setItem("news:prefs", JSON.stringify({ group: false }));            // the RIL report on its own row
    render(<News />);
    const ril = (await screen.findByText(/RIL Q2 net profit/)).closest("article")!;   // no "Reliance" in it
    expect(ril.className).toContain("hit");
    const tcs = screen.getByText(/TCS bags/).closest("article")!;
    expect(tcs.className).not.toContain("hit");
    await openFilters();   // the extra switches sit in the Filters panel
    fireEvent.click(screen.getByRole("switch", { name: /Watchlist only/ }));
    expect(screen.queryByText(/TCS bags/)).toBeNull();
    fireEvent.click(screen.getByTestId("news-settings-btn"));
    const dlg = await screen.findByTestId("news-settings");
    fireEvent.click(within(dlg).getByRole("tab", { name: "Watchlist & alerts" }));
    expect(within(dlg).getByTestId("news-watch-stocks").textContent).toContain("Reliance → RELIANCE");
  });

  it("moves through the stories with j / k and acts on the one under the cursor", async () => {
    render(<News />);
    await screen.findByText(/Sensex ends/);
    const row = (id: number) => document.querySelector(`[data-row="${id}"]`)!;
    fireEvent.keyDown(document.body, { key: "j" });
    expect(row(5).className).toContain("cursor");
    fireEvent.keyDown(document.body, { key: "j" });
    expect(row(4).className).toContain("cursor");
    fireEvent.keyDown(document.body, { key: "k" });
    expect(row(5).className).toContain("cursor");
    fireEvent.keyDown(document.body, { key: "o" });
    expect(opened).toEqual(["https://x.example/5"]);
    fireEvent.keyDown(document.body, { key: "j" });
    fireEvent.keyDown(document.body, { key: "c" });                                      // TCS: its chart
    expect(opened[1]).toContain("symbol=NSE%3ATCS-EQ");
    fireEvent.keyDown(document.body, { key: "j" });
    fireEvent.keyDown(document.body, { key: "j" });
    fireEvent.keyDown(document.body, { key: "e" });                                      // the Reliance story's sources
    expect(within(row(1) as HTMLElement).getByTestId("news-story")).toBeTruthy();
    fireEvent.keyDown(document.body, { key: "?" });
    expect(screen.getByTestId("news-keys")).toBeTruthy();
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(screen.queryByTestId("news-keys")).toBeNull();
    expect(document.querySelector(".news-item.cursor")).toBeNull();
    const search = screen.getByLabelText("Search headlines");
    fireEvent.keyDown(search, { key: "j" });                                             // typing: the list stays put
    expect(document.querySelector(".news-item.cursor")).toBeNull();
  });

  const fetched = () => (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls.map((c) => String(c[0]));

  it("shows how each tagged story's stock moved since it broke, against NIFTY", async () => {
    reactions = { 4: { sym: TCS, t0: 0, px0: 4000, px: 4072, pct: 1.8, traded: true, hi_pct: 2.1, lo_pct: -0.3, nifty_pct: 0.2, excess_pct: 1.6 },
      3: { sym: REL, t0: 0, px0: 2900, px: 2900, pct: 0, traded: false } };
    render(<News />);
    const tcs = (await screen.findByText(/TCS bags/)).closest("article")!;
    const rx = await within(tcs).findByTestId("news-reaction");
    expect(rx.textContent).toBe("+1.80% since");
    expect(rx.className).toContain("pos");
    expect(rx.getAttribute("title")).toContain("NIFTY +0.20% over the same span: +1.60% against the market");
    expect(rx.getAttribute("title")).toContain("range since -0.30% to +2.10%");
    const ret = screen.getByText(/Retail to acquire/).closest("article")!;
    expect(within(ret).queryByTestId("news-reaction")).toBeNull();                       // not traded since: nothing to say
    expect(fetched().find((u) => u.includes("/api/news/reaction"))).toContain("ids=1,3,4");   // the tagged stories on screen
  });

  it("ranks stories by importance with Top, and says why", async () => {
    render(<News />);
    await screen.findByText(/TCS bags/);
    const titles = () => [...document.querySelectorAll("article .news-title")].map((e) => e.textContent ?? "");
    expect(titles()[0]).toMatch(/Sensex ends/);                                           // Latest: newest first
    fireEvent.click(screen.getByRole("button", { name: "Top" }));
    expect(titles()[0]).toMatch(/Reliance Q2 net profit/);                                // results, 2 outlets, a large cap
    expect(titles()[titles().length - 1]).toMatch(/Sensex ends/);                         // a market wrap weighs least
    expect(document.querySelector('[data-row="1"] .news-score')!.getAttribute("title")).toContain("2 outlets +2");
    expect(JSON.parse(localStorage.getItem("news:prefs")!).sort).toBe("top");             // remembered in this browser
  });

  it("marks what came in since this browser's last visit", async () => {
    localStorage.setItem("news:lastSeen", "3");
    render(<News />);
    expect((await screen.findByTestId("news-unread")).textContent).toBe("2 new since your last visit");
    expect(screen.getAllByText("NEW")).toHaveLength(2);                                   // TCS and the Sensex wrap
    fireEvent.click(screen.getByRole("button", { name: /Mark seen \(2\)/ }));
    expect(screen.queryByText("NEW")).toBeNull();
    expect(localStorage.getItem("news:lastSeen")).toBe("5");
  });

  it("marks a stock several outlets name within minutes as breaking", async () => {
    settingsExtra = { burst_minutes: 60 };
    render(<News />);
    fireEvent.click(await screen.findByTestId("news-section-insights"));   // stats / panels live under Insights
    const panel = await screen.findByTestId("news-stocks-panel");
    expect(within(panel).getByTitle(/Breaking: 3 outlets in the last 60 min/).textContent).toBe("🔥3");
    fireEvent.click(screen.getByTestId("news-section-news"));   // back to the list
    expect(screen.getByText(/Retail to acquire/).closest("article")!.querySelector(".news-tk.hot")).toBeTruthy();
    expect(screen.getByText(/TCS bags/).closest("article")!.querySelector(".news-tk.hot")).toBeNull();   // one outlet
  });

  it("groups the sources in Settings with all on / all off, and saves the breaking-stock alert", async () => {
    statusExtra = { known_sources: ["BSE", "CNBC", "Mint", "NSE"],
      source_groups: [["India · markets", ["Mint"]], ["Exchanges & regulators", ["BSE", "NSE"]], ["Global", ["CNBC"]]],
      sources: [{ source: "Mint", url: "u1", custom: false, enabled: true, health: "ok", last_ok: null, error: null, paused_until: null, items_in_feed: 1, hour: 1, day: 1, total: 1, last_item: null },
        { source: "CNBC", url: "u2", custom: false, enabled: false, health: "off", last_ok: null, error: null, paused_until: null, items_in_feed: 0, hour: 0, day: 0, total: 0, last_item: null }] };
    settingsExtra = { sources: { Mint: true, BSE: true, NSE: false, CNBC: false }, extract_sources: {} };
    render(<News />);
    fireEvent.click(await screen.findByTestId("news-section-insights"));   // stats / panels live under Insights
    expect(await screen.findByRole("button", { name: /1 more source off/ })).toBeTruthy();   // the sidebar lists what is polled
    fireEvent.click(screen.getByTestId("news-settings-btn"));
    const dlg = await screen.findByTestId("news-settings");
    fireEvent.click(within(dlg).getByRole("tab", { name: "Sources" }));
    const official = within(dlg).getByText("Exchanges & regulators · 1/2 on").parentElement!;
    fireEvent.click(within(official).getByRole("button", { name: "All on" }));
    expect(within(dlg).getByText("Exchanges & regulators · 2/2 on")).toBeTruthy();
    fireEvent.click(within(dlg).getByRole("tab", { name: "Watchlist & alerts" }));
    fireEvent.click(within(dlg).getByTestId("news-bursts-toggle"));
    fireEvent.change(within(dlg).getByLabelText("Burst outlets"), { target: { value: "4" } });
    fireEvent.click(within(dlg).getByTestId("news-save"));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]).toMatchObject({ bursts: true, burst_sources: 4, sources: { BSE: true, NSE: true, CNBC: false } });
  });
});
