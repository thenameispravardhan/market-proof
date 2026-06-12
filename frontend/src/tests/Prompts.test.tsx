// Prompts page + PromptEditor tests. The editor is the most
// behavior-heavy widget on this page: it has to render an editable
// form for the selected template, and Save has to PUT to the
// /api/prompts/{event_type} endpoint with the new payload.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import Prompts from "../pages/Prompts";

function wrapper(qc: QueryClient) {
  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

function makeJsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("Prompts", () => {
  let originalFetch: typeof fetch;

  beforeEach(() => {
    originalFetch = global.fetch;
  });
  afterEach(() => {
    global.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("renders the prompts list and selects a row to load it into the editor", async () => {
    global.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/api/prompts") && !url.includes("/history") && !url.includes("/preview")) {
        return makeJsonResponse([
          {
            id: 1,
            event_type: "earnings",
            system_prompt: "You are a financial analyst.",
            user_template: "Analyse the filing at {{pdf_url}}.",
            model: "deepseek-chat",
            temperature: 0.2,
            max_tokens: 2000,
            version: 3,
            updated_at: "2026-06-10T10:00:00Z",
            updated_by: "ui",
          },
        ]);
      }
      return makeJsonResponse({ detail: "not found" }, 404);
    }) as unknown as typeof fetch;

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchInterval: false } },
    });
    render(<Prompts />, { wrapper: wrapper(qc) });

    // The list is rendered with the testid and shows our event type.
    expect(screen.getByTestId("prompt-list")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByTestId("prompt-row-earnings")).toBeInTheDocument();
    });

    // Click the row → editor loads the system prompt text.
    const user = userEvent.setup();
    await user.click(screen.getByTestId("prompt-row-earnings"));
    await waitFor(() => {
      expect(
        (screen.getByLabelText("System prompt") as HTMLTextAreaElement).value
      ).toBe("You are a financial analyst.");
    });
    // The user template hint is visible.
    expect(screen.getByText(/pdf_url/i)).toBeInTheDocument();
    // The version chip shows v3.
    expect(screen.getByText(/v3/)).toBeInTheDocument();
  });

  it("Save PUTs the edited prompt and bumps the version on the server", async () => {
    const putCalls: { url: string; body: unknown }[] = [];
    global.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method || "GET").toUpperCase();
      if (url.endsWith("/api/prompts") || (url.includes("/api/prompts") && method === "GET" && !url.includes("/history") && !url.includes("/preview"))) {
        // GET list
        if (method === "GET") {
          return makeJsonResponse([
            {
              id: 1,
              event_type: "earnings",
              system_prompt: "OLD system",
              user_template: "OLD template {{pdf_url}}",
              model: "deepseek-chat",
              temperature: 0.2,
              max_tokens: 2000,
              version: 3,
              updated_at: "2026-06-10T10:00:00Z",
              updated_by: "ui",
            },
          ]);
        }
      }
      if (url.includes("/api/prompts/earnings") && method === "PUT") {
        const body = init?.body ? JSON.parse(init.body as string) : null;
        putCalls.push({ url, body });
        return makeJsonResponse({
          id: 1,
          event_type: "earnings",
          system_prompt: body?.system_prompt,
          user_template: body?.user_template,
          model: body?.model,
          temperature: body?.temperature,
          max_tokens: body?.max_tokens,
          version: 4,
          updated_at: "2026-06-12T00:00:00Z",
          updated_by: "ui",
        });
      }
      return makeJsonResponse({ detail: "not found" }, 404);
    }) as unknown as typeof fetch;

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchInterval: false } },
    });
    render(<Prompts />, { wrapper: wrapper(qc) });

    const user = userEvent.setup();
    await user.click(await screen.findByTestId("prompt-row-earnings"));
    const ta = (await screen.findByLabelText("System prompt")) as HTMLTextAreaElement;
    // Edit the system prompt and the temperature.
    fireEvent.change(ta, { target: { value: "NEW system prompt" } });
    const temp = screen.getByLabelText("Temperature") as HTMLInputElement;
    fireEvent.change(temp, { target: { value: "0.4" } });
    // Save.
    await user.click(screen.getByTestId("save-prompt"));

    await waitFor(() => {
      expect(putCalls).toHaveLength(1);
    });
    expect(putCalls[0]!.url).toBe("/api/prompts/earnings");
    expect(putCalls[0]!.body).toMatchObject({
      system_prompt: "NEW system prompt",
      temperature: 0.4,
      model: "deepseek-chat",
      max_tokens: 2000,
    });
    // Optimistic UI shows the success state.
    expect(await screen.findByText(/Saved/)).toBeInTheDocument();
  });

  it("shows the empty state when /api/prompts returns 404", async () => {
    global.fetch = vi.fn(async () => makeJsonResponse({ detail: "not found" }, 404)) as unknown as typeof fetch;

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchInterval: false } },
    });
    render(<Prompts />, { wrapper: wrapper(qc) });

    await waitFor(() => {
      expect(screen.getByText(/Endpoint not available yet/i)).toBeInTheDocument();
    });
  });
});
