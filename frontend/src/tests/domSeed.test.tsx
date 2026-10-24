import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

vi.mock("../hooks/useDepth", () => ({
  useDepth: () => ({ ok: true, bids: [[99.9, 100, 1]], asks: [[100.0, 120, 1]], total_buy: 100, total_sell: 120, ltp: 100 }),
}));
vi.mock("../hooks/useQuotes", () => ({ useLiveQuote: () => undefined }));

import { DomPanel } from "../components/trade/DomPanel";

const props = {
  position: null, orders: [], qty: 1, onQty: () => undefined, instant: false, onOrder: async () => "", onCancel: () => undefined,
  onModify: async () => "", onFlatten: async () => undefined, onClose: () => undefined, onMsg: () => undefined,
};

describe("DOM volume column", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("starts from today's recorded volume at price", async () => {
    const fetchMock = vi.fn(async (_u: string) => ({ ok: true, json: async () => ({ footprint: [[0, 100.0, 300, 200], [0, 99.95, 40, 60]] }) }));
    vi.stubGlobal("fetch", fetchMock);
    render(<DomPanel {...props} symbol={{ symbol: "NSE:NIFTY26OCTFUT", tick_size: 0.05, lot_size: 65 } as never} />);
    await waitFor(() => expect(screen.getByTestId("dom-vol-total").textContent).toMatch(/600/));
    expect(String(fetchMock.mock.calls[0][0])).toContain("resolution=D");
    expect(screen.getByTestId("dom-vol-total").getAttribute("title")).toMatch(/recorded footprint/);
  });

  it("doesn't seed an index (its recording is the future's prices)", async () => {
    const fetchMock = vi.fn(async (_u: string) => ({ ok: true, json: async () => ({ footprint: [] }) }));
    vi.stubGlobal("fetch", fetchMock);
    render(<DomPanel {...props} symbol={{ symbol: "NSE:NIFTY50-INDEX", tick_size: 0.05, lot_size: 1 } as never} />);
    await new Promise((r) => setTimeout(r, 20));
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
