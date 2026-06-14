// Tests for the "Connect Fyers" banner.
//
// Background: the Fyers OAuth flow opens a popup. Fyers redirects
// back to /api/fyers/callback on success; the callback writes the
// access_token and the banner polls /api/fyers/status. When the
// popup closes WITHOUT the callback having run (the most common
// case: the configured FYERS_APP_ID was deleted on Fyers'
// dashboard, so Fyers' login page shows a "deleted app" error
// and the popup never reaches the callback), the banner used to
// silently reset to "Connect Fyers" with no operator feedback.
// These tests pin down the new behaviour: an explicit error
// message covering the most likely causes.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ConnectFyers } from "../components/accounts/ConnectFyers";

function makeJsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function wrapper(qc: QueryClient) {
  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

const NOT_CONNECTED = {
  connected: false,
  credentials_set: true,
  account_present: true,
  app_id: "DELETED-APP-100",
  live_mode: true,
  reason: "Click 'Connect Fyers' to run the OAuth flow.",
};

const CONNECTED = {
  connected: true,
  credentials_set: true,
  account_present: true,
  app_id: "WORKING-APP-100",
  live_mode: true,
  reason: null,
};

describe("ConnectFyers", () => {
  let originalFetch: typeof fetch;
  let originalOpen: typeof window.open;

  beforeEach(() => {
    originalFetch = globalThis.fetch;
    originalOpen = window.open;
    // We don't actually want a popup; the test stubs `window.open`
    // to return a fake popup object whose `closed` flag the
    // component's interval will read.
  });
  afterEach(() => {
    globalThis.fetch = originalFetch;
    window.open = originalOpen;
    vi.restoreAllMocks();
  });

  function makeQc() {
    return new QueryClient({
      defaultOptions: {
        queries: { retry: false, refetchInterval: false },
        mutations: { retry: false },
      },
    });
  }

  it("renders the not-connected state and the Connect button when creds are set", async () => {
    globalThis.fetch = vi.fn(async (input) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/api/fyers/status")) {
        return makeJsonResponse(NOT_CONNECTED);
      }
      return makeJsonResponse({}, 404);
    }) as unknown as typeof fetch;
    const qc = makeQc();
    render(<ConnectFyers />, { wrapper: wrapper(qc) });
    // Wait for the fetched status to drive the banner past the
    // initial "Fyers Not Configured" placeholder (the query is
    // undefined on first render) into the real "Fyers Token
    // Expired" state. We key off the heading text, not the
    // button, because the button is rendered in both states.
    await waitFor(() => {
      expect(screen.getByText(/Fyers Token Expired/i)).toBeInTheDocument();
    });
    // Button label flips from "Set creds in .env first" (initial
    // placeholder) to "Connect Fyers" once status.credentials_set
    // is true.
    expect(screen.getByTestId("fyers-connect")).toBeInTheDocument();
    expect(screen.getByTestId("fyers-connect")).toHaveTextContent(
      /Connect Fyers/i,
    );
    // The banner shows the FYERS_APP_ID the running server has,
    // so the operator can sanity-check it matches the keys they
    // just put in .env. Without this, a stale server (started
    // before the operator edited .env) is invisible — they see
    // "OAuth did not complete" but have no way to know whether
    // the running app_id matches what's on disk.
    const appIdRow = screen.getByTestId("fyers-app-id");
    expect(appIdRow).toHaveTextContent(/DELETED-APP-100/);
  });

  it("renders the connected state when status.connected is true", async () => {
    globalThis.fetch = vi.fn(async (input) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/api/fyers/status")) {
        return makeJsonResponse(CONNECTED);
      }
      return makeJsonResponse({}, 404);
    }) as unknown as typeof fetch;
    const qc = makeQc();
    render(<ConnectFyers />, { wrapper: wrapper(qc) });
    await waitFor(() => {
      expect(screen.getByText(/Fyers Connected/i)).toBeInTheDocument();
    });
    expect(screen.queryByTestId("fyers-connect")).not.toBeInTheDocument();
  });

  it("surfaces an error when the OAuth popup closes without completing (deleted app)", async () => {
    // The OAuth popup closes because the operator (1) saw Fyers'
    // "deleted app" error page in the popup, (2) closed it manually.
    // The banner used to silently reset; now it must show a
    // message that covers the most common causes.
    let popupCloseResolve: () => void = () => {};
    const popupClosed = new Promise<void>((resolve) => {
      popupCloseResolve = resolve;
    });
    const fakePopup = {
      closed: false,
      // We control `closed` from the test; the component's
      // `setInterval` will read this on each tick.
    } as Window;
    window.open = vi.fn(() => fakePopup) as unknown as typeof window.open;

    // First call: /api/fyers/status returns NOT_CONNECTED so the
    // banner shows the "Connect" CTA. Subsequent polls continue
    // to return NOT_CONNECTED — the OAuth did not complete.
    globalThis.fetch = vi.fn(async (input) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/api/fyers/status")) {
        return makeJsonResponse(NOT_CONNECTED);
      }
      if (url.includes("/api/fyers/authorize-url")) {
        return makeJsonResponse({
          configured: true,
          url: "https://api-t1.fyers.in/api/v3/generate-authcode?...",
          state: "test-state",
          reason: null,
        });
      }
      return makeJsonResponse({}, 404);
    }) as unknown as typeof fetch;

    // Cast to a mutable record so we can flip `closed` from the
    // test. Window.closed is a getter in the real DOM, but a
    // plain object literal works for our purposes here.
    const mutablePopup = fakePopup as unknown as { closed: boolean };

    const user = userEvent.setup();
    const qc = makeQc();
    render(<ConnectFyers />, { wrapper: wrapper(qc) });

    const connectBtn = await screen.findByTestId("fyers-connect");
    await user.click(connectBtn);

    // window.open was called and a popup is "open".
    expect(window.open).toHaveBeenCalled();
    // The button flips to "Waiting for Fyers…" while the popup is open.
    await waitFor(() => {
      expect(screen.getByText(/Waiting for Fyers/i)).toBeInTheDocument();
    });

    // Operator closes the popup. The component's 1-second poll
    // will read `fakePopup.closed === true` on the next tick.
    await act(async () => {
      mutablePopup.closed = true;
      popupCloseResolve();
      // Wait for the 1-second poll + 1.5-second grace period
      // to elapse inside the component.
      await new Promise((r) => setTimeout(r, 2700));
    });

    // The banner now shows the explicit error message. This is
    // the contract operators depended on — silently resetting
    // would leave them stuck on "Fyers Token Expired" with no
    // way to diagnose the cause.
    const err = await screen.findByTestId("fyers-error");
    expect(err.textContent).toMatch(/OAuth did not complete/i);
    expect(err.textContent).toMatch(/FYERS_APP_ID/);
    // The most common production cause is that the operator
    // updated .env with new keys but forgot to restart the
    // backend — the running process is still using the old
    // (deleted) keys it loaded at startup. Make sure the
    // message names that explicitly.
    expect(err.textContent).toMatch(/restart the backend/i);
    expect(err.textContent).toMatch(/deleted on the Fyers dashboard/i);
  });
});
