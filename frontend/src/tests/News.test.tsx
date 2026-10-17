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
