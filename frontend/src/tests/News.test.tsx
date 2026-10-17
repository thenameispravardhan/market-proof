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
  { id: 1, url: "https://mc.example/1", source: "Moneycontrol", title: "Reliance wins <b>order</b>", summary: null, published_at: now, seen_at: now },
  { id: 2, url: "https://bse.example/2", source: "BSE", title: "TCS board meeting", summary: "Results", published_at: now, seen_at: now },
  { id: 3, url: "https://mc.example/3", source: "Moneycontrol", title: "Daily horoscope", summary: null, published_at: now, seen_at: now },
];
let puts: unknown[] = [];

beforeEach(() => {
  puts = [];
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
    expect(screen.getByText(/never sent to AI analysis/)).toBeTruthy();
  });

  it("filters by source chip", async () => {
    render(<News />);
    await screen.findByText("TCS board meeting");
    fireEvent.click(screen.getByRole("button", { name: /^BSE/ }));
    expect(screen.queryByText(/wins/)).toBeNull();
    expect(screen.getByText("TCS board meeting")).toBeTruthy();
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
    expect(screen.getByTestId("news-extract")).toBeTruthy();              // full-text stats panel
  });
});

describe("News page: detection lag and trends", () => {
  it("shows each headline's lag, the stats strip and trending words", async () => {
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
    const strip = await screen.findByTestId("news-stats");
    expect(within(strip).getByText("45s")).toBeTruthy();                  // median detection lag
    expect(within(strip).getAllByText("Mint").length).toBe(1);            // fastest source
    expect(screen.getByText("+45s").className).toContain("fast");        // per-headline lag, colour by speed
    expect(screen.getByText("+15m").className).toContain("slow");
    expect(screen.queryByText("+2.0h")).toBeNull();                       // first-poll backlog: no lag chip
    const trend = screen.getByTestId("news-trending");
    expect(within(trend).getByRole("button", { name: /infosys/ })).toBeTruthy();   // appears twice in the last hour
    expect(within(trend).queryByRole("button", { name: /^rbi/ })).toBeNull();      // once: not trending
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

  it("chart links the stock a headline names (exact tickers only) to the Trade page at the news time", async () => {
    const f = globalThis.fetch as unknown as (u: RequestInfo | URL, i?: RequestInit) => Promise<Response>;
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (!url.includes("/api/search/symbols")) return f(input, init);
      const q = new URL(url, "http://x").searchParams.get("q");
      const hit = (s: string, ex = "NSE") => ({ symbol: `${ex}:${s}-EQ`, short_name: s, exchange: ex, segment: "EQ", instrument_type: "EQ", lot_size: 1, tick_size: 0.05, expiry: null, strike: null, underlying: null, display: s });
      return json({ hits: q === "Reliance" ? [hit("RELIANCE", "BSE"), hit("RELIANCE")] : [hit("WINSOME")] });   // "wins" fuzzy-matches: ignored
    }) as unknown as typeof fetch;
    render(<News />);
    await screen.findByText(/wins/);
    fireEvent.click(screen.getAllByRole("button", { name: /chart/ })[0]);
    const box = await screen.findByTestId("news-stocks");
    const links = within(box).getAllByRole("link");
    expect(links.map((a) => a.textContent)).toEqual(["RELIANCE ↗"]);
    expect(links[0].getAttribute("href")).toContain("#/trade?symbol=NSE%3ARELIANCE-EQ");   // NSE listing preferred
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

  beforeEach(() => {
    opened.length = 0;
    watching = {};
    vi.spyOn(window, "open").mockImplementation((u?: string | URL) => { opened.push(String(u)); return null; });
    try { localStorage.removeItem("news:prefs"); } catch { /* storage off */ }
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/market/quotes")) return json({ quotes: { [REL]: { ltp: 2950, change: 36, change_pct: 1.25 } } });
      if (url.includes("/api/news/items")) return json({ items: url.includes("since_id") ? [] : LIST, max_id: 5 });
      if (url.includes("/api/news/status")) return json({
        settings: { ...SETTINGS, enabled: true, watchlist: Object.keys(watching), muted: [] }, known_sources: ["Mint"], kinds: KINDS,
        collector: { polls: 3, last_poll_at: null, last_poll_ms: null }, watch_stocks: watching,
        sources: [{ source: "Moneycontrol", url: "u", custom: false, enabled: true, health: "ok", last_ok: null, error: null, paused_until: null,
          items_in_feed: 3, hour: 1, day: 1, total: 1, last_item: null }],
        lag: { p50: 45, p90: 90, n: 3, fastest: "Mint" },
      });
      return json({}, 404);
    }) as unknown as typeof fetch;
  });

  it("ranks the stocks in the news by stories, with their day's move, and filters the list by one", async () => {
    render(<News />);
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
    expect(within(tcs).queryByRole("button", { name: /chart/ })).toBeNull();          // tagged: no lookup needed
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
    const strip = screen.getByTestId("news-stats");
    const first = within(strip).getByText("First to report").parentElement!;
    expect(first.textContent).toContain("Moneycontrol1 of 1 shared stories");
    expect(first.getAttribute("title")).toContain("Moneycontrol 1");

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
});
