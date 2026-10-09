// The App ID / Redirect fields are pre-filled from the server. Saving only
// a DeepSeek key must not resend them (that rewrote the Fyers keys and told
// the operator to re-authorise for nothing).

import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { FyersCredentialsCard } from "../components/accounts/FyersCredentialsCard";

const CRED = {
  fyers_app_id: "APP-100",
  fyers_redirect_uri: "https://bot.example/api/fyers/callback",
  fyers_secret_set: true,
  fyers_secret_masked: "ab••••yz",
  deepseek_key_set: false,
  deepseek_key_masked: "",
};

describe("FyersCredentialsCard", () => {
  const originalFetch = globalThis.fetch;
  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("sends only changed fields", async () => {
    const puts: unknown[] = [];
    globalThis.fetch = vi.fn(async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/api/settings/credentials") && init?.method === "PUT") {
        puts.push(JSON.parse(String(init.body)));
      }
      return new Response(JSON.stringify(CRED), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }) as unknown as typeof fetch;
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <FyersCredentialsCard />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("cred-app-id")).toHaveValue("APP-100"));
    const user = userEvent.setup();

    await user.click(screen.getByTestId("cred-save"));
    expect(screen.getByTestId("cred-msg")).toHaveTextContent(/Nothing changed/i);
    expect(puts).toHaveLength(0);

    await user.type(screen.getByTestId("cred-deepseek"), "sk-new");
    await user.click(screen.getByTestId("cred-save"));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]).toEqual({ deepseek_api_key: "sk-new" });
    expect(screen.getByTestId("cred-msg")).not.toHaveTextContent(/Connect Fyers/i);
  });
});
