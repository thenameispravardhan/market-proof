// The Fyers dashboard switch is the LIVE-trading master switch — turning
// it ON must demand the typed "LIVE" confirmation before any request is
// sent, because the backend flips TRADING_MODE=live (real money) on it.
// Turning it OFF (risk-reducing) must fire immediately, no prompt.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AccountToggles } from "../components/dashboard/AccountToggles";

function makeJsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function accounts(fyersEnabled: boolean) {
  return [
    { id: 2, name: "Paper Account", broker: "fyers", app_id: null, paper_mode: true, enabled: true },
    { id: 1, name: "Fyers", broker: "fyers", app_id: "LUVWQUOVWJ-200", paper_mode: false, enabled: fyersEnabled },
  ];
}

function setup(fyersEnabled: boolean) {
  const puts: Array<{ url: string; body: unknown }> = [];
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.includes("/api/broker-accounts") && init?.method === "PUT") {
      puts.push({ url, body: JSON.parse(String(init.body)) });
      return makeJsonResponse({});
    }
    if (url.includes("/api/broker-accounts")) {
      return makeJsonResponse({ accounts: accounts(fyersEnabled) });
    }
    return makeJsonResponse([], 404);
  }) as unknown as typeof fetch;

  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  render(
    <QueryClientProvider client={qc}>
      <AccountToggles />
    </QueryClientProvider>
  );
  return puts;
}

describe("AccountToggles — Fyers LIVE confirmation", () => {
  let originalFetch: typeof fetch;

  beforeEach(() => {
    originalFetch = globalThis.fetch;
  });
  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("enabling Fyers requires typing LIVE — wrong answer sends nothing", async () => {
    const puts = setup(false);
    vi.spyOn(window, "prompt").mockReturnValue("nope");

    const toggle = await screen.findByTestId("toggle-fyers");
    await waitFor(() => expect(toggle).not.toBeDisabled());
    toggle.click();

    expect(window.prompt).toHaveBeenCalled();
    // Give any (wrong) mutation a chance to fire, then assert none did.
    await new Promise((r) => setTimeout(r, 50));
    expect(puts.length).toBe(0);
  });

  it("enabling Fyers with the typed LIVE confirmation sends enabled:true", async () => {
    const puts = setup(false);
    vi.spyOn(window, "prompt").mockReturnValue("live"); // case-insensitive

    const toggle = await screen.findByTestId("toggle-fyers");
    await waitFor(() => expect(toggle).not.toBeDisabled());
    toggle.click();

    await waitFor(() => expect(puts.length).toBe(1));
    expect(puts[0].url).toContain("/api/broker-accounts/1");
    expect(puts[0].body).toEqual({ enabled: true });
  });

  it("disabling Fyers needs no confirmation (risk-reducing)", async () => {
    const puts = setup(true);
    const promptSpy = vi.spyOn(window, "prompt");

    const toggle = await screen.findByTestId("toggle-fyers");
    await waitFor(() => expect(toggle).not.toBeDisabled());
    toggle.click();

    await waitFor(() => expect(puts.length).toBe(1));
    expect(promptSpy).not.toHaveBeenCalled();
    expect(puts[0].body).toEqual({ enabled: false });
  });
});
