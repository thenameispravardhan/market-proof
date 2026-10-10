// Readiness banner: silent when every pre-market check passes, loud and
// specific when one fails.

import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ReadinessBanner, backupProblem } from "../components/dashboard/ReadinessBanner";

function json(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function renderWith(selftest: unknown, backups: unknown) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.includes("fyers-selftest")) return json(selftest);
    if (url.includes("/api/system/backups")) return json(backups);
    return json({});
  }) as unknown as typeof fetch;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ReadinessBanner />
    </QueryClientProvider>,
  );
}

const original = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = original;
});

describe("ReadinessBanner", () => {
  it("renders nothing when all checks pass", async () => {
    const { container } = renderWith(
      { enabled: true, result: { ran_at: "2026-10-12T03:15:00Z", ok: true, problems: [], checks: [{ name: "token", status: "ok", detail: "" }] } },
      { max_age_hours: 72, status: { newest_age_hours: 10, offsite: "ok", local_copies: 7 } },
    );
    await new Promise((r) => setTimeout(r, 50));
    expect(container.textContent).toBe("");
  });

  it("names the failing check", async () => {
    renderWith(
      { enabled: true, result: { ran_at: "2026-10-12T03:15:00Z", ok: false, problems: [], checks: [{ name: "egress_ip", status: "error", detail: "server egress IP 1.2.3.4 is not in the Fyers whitelist" }] } },
      { max_age_hours: 72, status: null },
    );
    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    expect(screen.getByText(/not in the Fyers whitelist/)).toBeTruthy();
  });

  it("flags stale, missing and failed-offsite backups", () => {
    expect(backupProblem({ max_age_hours: 72, status: { newest_age_hours: 100, offsite: "ok", local_copies: 7 } })).toMatch(/100h old/);
    expect(backupProblem({ max_age_hours: 72, status: { newest_age_hours: null, offsite: "unknown", local_copies: 0 } })).toMatch(/No database backup/);
    expect(backupProblem({ max_age_hours: 72, status: { newest_age_hours: 5, offsite: "failed", local_copies: 7 } })).toMatch(/offsite/);
    expect(backupProblem({ max_age_hours: 0, status: { newest_age_hours: 500, offsite: "failed", local_copies: 7 } })).toBeNull();
  });
});
