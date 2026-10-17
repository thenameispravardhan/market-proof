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
