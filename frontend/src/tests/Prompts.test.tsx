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
    originalFetch = globalThis.fetch;
  });
  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("loads the DEFAULT template into the editor with a model selector", async () => {
    // Single-prompt mode: the page renders the DEFAULT template editor
    // directly (no list to click).
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/history")) {
        return makeJsonResponse({ event_type: "DEFAULT", history: [] });
      }
      if (url.includes("/api/prompts") && !url.includes("/preview")) {
        return makeJsonResponse({
          prompts: [
            {
              id: 1,
              event_type: "DEFAULT",
              system_prompt: "You are a financial analyst.",
              user_template: "Analyse the filing at {{pdf_url}}.",
              model: "deepseek-v4-flash",
              temperature: 0.2,
              max_tokens: 2000,
              reasoning_effort: "medium",
              thinking_enabled: false,
              stream: false,
              version: 3,
              updated_at: "2026-06-10T10:00:00Z",
              updated_by: "ui",
            },
          ],
        });
      }
      return makeJsonResponse({ detail: "not found" }, 404);
    }) as unknown as typeof fetch;

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchInterval: false } },
    });
    render(<Prompts />, { wrapper: wrapper(qc) });

    // The editor auto-loads the single DEFAULT template — no row click.
    await waitFor(() => {
      expect(
        (screen.getByLabelText("System prompt") as HTMLTextAreaElement).value
      ).toBe("You are a financial analyst.");
    });
    // The model selector is present and reflects the stored model.
    const modelSel = screen.getByTestId("prompt-model") as HTMLSelectElement;
    expect(modelSel.value).toBe("deepseek-v4-flash");
    const optionValues = [...modelSel.options].map((o) => o.value);
    expect(optionValues).toEqual(["deepseek-v4-flash", "deepseek-v4-pro"]);
    // The user template hint is visible (help line below the textarea).
    expect(screen.getAllByText(/pdf_url/i).length).toBeGreaterThan(0);
    // The version chip shows v3.
    expect(screen.getAllByText(/v3/).length).toBeGreaterThan(0);

    // The v4 inference controls reflect the stored values.
    const thinkingSel = screen.getByTestId("prompt-thinking") as HTMLSelectElement;
    expect(thinkingSel.value).toBe("disabled");
    const reasoningSel = screen.getByTestId("prompt-reasoning") as HTMLSelectElement;
    expect(reasoningSel.value).toBe("medium");
    // Reasoning effort is an independent control — always selectable,
    // even with thinking off.
    expect(reasoningSel.disabled).toBe(false);
    const streamSel = screen.getByTestId("prompt-stream") as HTMLSelectElement;
    expect(streamSel.value).toBe("off");
  });

  it("changing the model and saving POSTs the new model + bumps the version", async () => {
    const postCalls: { url: string; body: any }[] = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method || "GET").toUpperCase();
      if (url.includes("/history")) {
        return makeJsonResponse({ event_type: "DEFAULT", history: [] });
      }
      if (url.includes("/api/prompts/DEFAULT") && method === "POST") {
        const body = init?.body ? JSON.parse(init.body as string) : null;
        postCalls.push({ url, body });
        return makeJsonResponse({
          id: 1,
          event_type: "DEFAULT",
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
      if (url.includes("/api/prompts") && method === "GET" && !url.includes("/preview")) {
        return makeJsonResponse({
          prompts: [
            {
              id: 1,
              event_type: "DEFAULT",
              system_prompt: "OLD system prompt long enough to pass validation.",
              user_template: "OLD template {{pdf_url}}",
              model: "deepseek-v4-flash",
              temperature: 0.2,
              max_tokens: 2000,
              reasoning_effort: "medium",
              thinking_enabled: false,
              stream: false,
              version: 3,
              updated_at: "2026-06-10T10:00:00Z",
              updated_by: "ui",
            },
          ],
        });
      }
      return makeJsonResponse({ detail: "not found" }, 404);
    }) as unknown as typeof fetch;

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchInterval: false } },
    });
    render(<Prompts />, { wrapper: wrapper(qc) });

    const user = userEvent.setup();
    // Editor auto-loads DEFAULT; change the model via the new selector.
    const modelSel = (await screen.findByTestId("prompt-model")) as HTMLSelectElement;
    fireEvent.change(modelSel, { target: { value: "deepseek-v4-pro" } });
    // Flip the v4 inference controls too: enable thinking, bump reasoning
    // effort to high, and turn streaming on.
    fireEvent.change(screen.getByTestId("prompt-thinking"), {
      target: { value: "enabled" },
    });
    fireEvent.change(screen.getByTestId("prompt-reasoning"), {
      target: { value: "high" },
    });
    fireEvent.change(screen.getByTestId("prompt-stream"), {
      target: { value: "on" },
    });
    // Save (enabled now that the form is dirty).
    await user.click(screen.getByTestId("save-prompt"));

    await waitFor(() => {
      expect(postCalls).toHaveLength(1);
    });
    expect(postCalls[0]!.url).toContain("/api/prompts/DEFAULT");
    expect(postCalls[0]!.body).toMatchObject({
      model: "deepseek-v4-pro",
      temperature: 0.2,
      max_tokens: 2000,
      reasoning_effort: "high",
      thinking_enabled: true,
      stream: true,
    });
    // Optimistic UI shows the success state.
    expect(await screen.findByText(/Saved/)).toBeInTheDocument();
  });

  it("reset-to-default fills the form without saving; Save then persists it", async () => {
    const postCalls: { url: string; body: any }[] = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method || "GET").toUpperCase();
      if (url.includes("/history")) {
        return makeJsonResponse({ event_type: "DEFAULT", history: [] });
      }
      // Factory-default endpoint — returns seed values, never persists.
      if (url.includes("/api/prompts/DEFAULT/default") && method === "GET") {
        return makeJsonResponse({
          event_type: "DEFAULT",
          system_prompt: "FACTORY system prompt long enough to validate.",
          user_template: "FACTORY {{pdf_url}}",
          model: "deepseek-v4-flash",
          temperature: 0.2,
          max_tokens: 2000,
          reasoning_effort: "medium",
          thinking_enabled: false,
          stream: false,
        });
      }
      if (url.includes("/api/prompts/DEFAULT") && method === "POST") {
        const body = init?.body ? JSON.parse(init.body as string) : null;
        postCalls.push({ url, body });
        return makeJsonResponse({
          id: 1,
          event_type: "DEFAULT",
          ...body,
          version: 8,
          updated_at: "2026-06-12T00:00:00Z",
          updated_by: "ui",
        });
      }
      if (url.includes("/api/prompts") && method === "GET" && !url.includes("/preview")) {
        // The currently-saved version — deliberately NOT the factory default.
        return makeJsonResponse({
          prompts: [
            {
              id: 1,
              event_type: "DEFAULT",
              system_prompt: "SAVED prompt the operator edited earlier, long enough.",
              user_template: "SAVED {{pdf_url}}",
              model: "deepseek-v4-pro",
              temperature: 0.5,
              max_tokens: 1500,
              reasoning_effort: "high",
              thinking_enabled: true,
              stream: true,
              version: 7,
              updated_at: "2026-06-10T10:00:00Z",
              updated_by: "ui",
            },
          ],
        });
      }
      return makeJsonResponse({ detail: "not found" }, 404);
    }) as unknown as typeof fetch;

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchInterval: false } },
    });
    render(<Prompts />, { wrapper: wrapper(qc) });

    const user = userEvent.setup();
    // Editor auto-loads the SAVED version → form == saved → Save disabled.
    const sysPrompt = (await screen.findByLabelText("System prompt")) as HTMLTextAreaElement;
    await waitFor(() => expect(sysPrompt.value).toContain("SAVED prompt"));
    expect(screen.getByTestId("save-prompt")).toBeDisabled();

    // Click Reset → form swaps to factory default, but nothing is POSTed.
    await user.click(screen.getByTestId("reset-prompt"));
    await waitFor(() =>
      expect(sysPrompt.value).toBe("FACTORY system prompt long enough to validate.")
    );
    expect((screen.getByTestId("prompt-model") as HTMLSelectElement).value).toBe("deepseek-v4-flash");
    expect(postCalls).toHaveLength(0);

    // The form is now dirty, so Save lights up. Saving persists the default.
    const saveBtn = screen.getByTestId("save-prompt") as HTMLButtonElement;
    expect(saveBtn.disabled).toBe(false);
    await user.click(saveBtn);

    await waitFor(() => expect(postCalls).toHaveLength(1));
    expect(postCalls[0]!.body).toMatchObject({
      system_prompt: "FACTORY system prompt long enough to validate.",
      user_template: "FACTORY {{pdf_url}}",
      model: "deepseek-v4-flash",
      reasoning_effort: "medium",
      thinking_enabled: false,
      stream: false,
      change_note: "Reset to default",
    });
  });

  it("restore button rolls a past version back via /restore and disables the current one", async () => {
    const restoreCalls: string[] = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method || "GET").toUpperCase();
      if (url.includes("/restore/") && method === "POST") {
        restoreCalls.push(url);
        return makeJsonResponse({
          id: 1,
          event_type: "DEFAULT",
          system_prompt: "RESTORED prompt long enough to validate properly.",
          user_template: "base {{pdf_url}}",
          model: "deepseek-v4-flash",
          temperature: 0.2,
          max_tokens: 2000,
          reasoning_effort: "medium",
          thinking_enabled: false,
          stream: false,
          version: 3,
          updated_at: "2026-06-12T00:00:00Z",
          updated_by: "ui",
        });
      }
      if (url.includes("/history")) {
        return makeJsonResponse({
          event_type: "DEFAULT",
          history: [
            {
              id: 22,
              template_id: 1,
              version: 2,
              system_prompt: "current default prompt, long enough.",
              user_template: "default {{pdf_url}}",
              model: "deepseek-v4-flash",
              temperature: 0.2,
              max_tokens: 2000,
              reasoning_effort: "medium",
              thinking_enabled: false,
              stream: false,
              changed_at: "2026-06-11T00:00:00Z",
              change_note: "seed_default_prompts",
            },
            {
              id: 21,
              template_id: 1,
              version: 1,
              system_prompt: "base prompt the operator wrote, long enough.",
              user_template: "base {{pdf_url}}",
              model: "deepseek-v4-flash",
              temperature: 0.2,
              max_tokens: 2000,
              reasoning_effort: "medium",
              thinking_enabled: false,
              stream: false,
              changed_at: "2026-06-10T00:00:00Z",
              change_note: "base",
            },
          ],
        });
      }
      if (url.includes("/api/prompts") && method === "GET" && !url.includes("/preview")) {
        return makeJsonResponse({
          prompts: [
            {
              id: 1,
              event_type: "DEFAULT",
              system_prompt: "current default prompt, long enough.",
              user_template: "default {{pdf_url}}",
              model: "deepseek-v4-flash",
              temperature: 0.2,
              max_tokens: 2000,
              reasoning_effort: "medium",
              thinking_enabled: false,
              stream: false,
              version: 2,
              updated_at: "2026-06-11T00:00:00Z",
              updated_by: "ui",
            },
          ],
        });
      }
      return makeJsonResponse({ detail: "not found" }, 404);
    }) as unknown as typeof fetch;

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchInterval: false } },
    });
    render(<Prompts />, { wrapper: wrapper(qc) });

    const user = userEvent.setup();
    // The current version (v2) can't be restored onto itself.
    const currentBtn = (await screen.findByTestId("restore-prompt-2")) as HTMLButtonElement;
    expect(currentBtn.disabled).toBe(true);
    expect(currentBtn.textContent).toContain("Current");

    // Rolling back to v1 POSTs to the restore endpoint.
    const restoreV1 = screen.getByTestId("restore-prompt-1") as HTMLButtonElement;
    expect(restoreV1.disabled).toBe(false);
    await user.click(restoreV1);

    await waitFor(() => expect(restoreCalls).toHaveLength(1));
    expect(restoreCalls[0]).toContain("/api/prompts/DEFAULT/restore/1");
  });

  it("shows the empty state when /api/prompts returns 404", async () => {
    globalThis.fetch = vi.fn(async () => makeJsonResponse({ detail: "not found" }, 404)) as unknown as typeof fetch;

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchInterval: false } },
    });
    render(<Prompts />, { wrapper: wrapper(qc) });

    await waitFor(() => {
      expect(screen.getByText(/Endpoint not available yet/i)).toBeInTheDocument();
    });
  });
});

