// useWebSocket tests — the headline test is the auto-reconnect
// behaviour. We stub global.WebSocket with a minimal class that we
// can drive from the test (open, message, close, error). React
// Strict Mode is not used in these tests because its double-mount
// complicates timing.

import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { useWebSocket } from "../hooks/useWebSocket";

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static OPEN = 1;
  static CLOSED = 3;

  url: string;
  readyState = 0; // CONNECTING
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  sent: unknown[] = [];

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }
  send(data: string) {
    this.sent.push(JSON.parse(data));
  }
  close() {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.(new CloseEvent("close", { code: 1006, reason: "test" }));
  }
  // helpers used by tests
  fakeOpen() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.(new Event("open"));
  }
  fakeMessage(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) });
  }
  fakeClose() {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.(new CloseEvent("close", { code: 1006, reason: "test" }));
  }
}

beforeEach(() => {
  FakeWebSocket.instances = [];
  // NOTE: we intentionally do NOT use vi.useFakeTimers() here. The
  // combination of happy-dom + React 18's concurrent rendering +
  // fake timers triggers "Should not already be working." errors
  // because React 18 schedules state updates via microtasks and
  // fake timers don't flush them. Real timers + small waits are
  // simpler and more faithful to the production path.
  // @ts-expect-error -- assigning a class to the global WebSocket slot
  globalThis.WebSocket = FakeWebSocket;
});
afterEach(() => {
  delete (globalThis as { WebSocket?: unknown }).WebSocket;
});

const tick = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

describe("useWebSocket", () => {
  it("connects on mount, sends a subscribe for initial channels, and exposes status=open", async () => {
    const { result } = renderHook(() =>
      useWebSocket({ channels: ["signals"], backoffMinMs: 50, backoffMaxMs: 100 })
    );

    // The hook builds the WebSocket inside useEffect.
    expect(FakeWebSocket.instances.length).toBeGreaterThanOrEqual(1);
    const ws = FakeWebSocket.instances[FakeWebSocket.instances.length - 1]!;

    await act(async () => {
      ws.fakeOpen();
    });

    expect(result.current.status).toBe("open");
    expect(ws.sent).toEqual([{ type: "subscribe", channels: ["signals"] }]);
  });

  it("parses incoming 'event' frames and surfaces them as lastMessage", async () => {
    const { result } = renderHook(() => useWebSocket());
    const ws = FakeWebSocket.instances[FakeWebSocket.instances.length - 1]!;
    await act(async () => {
      ws.fakeOpen();
      ws.fakeMessage({
        type: "event",
        channel: "signals",
        payload: { symbol: "RELIANCE", action: "BUY" },
        event_id: "e-1",
        ts: "2026-06-12T00:00:00Z",
      });
    });
    expect(result.current.lastMessage).toEqual({
      channel: "signals",
      payload: { symbol: "RELIANCE", action: "BUY" },
      event_id: "e-1",
      ts: "2026-06-12T00:00:00Z",
    });
  });

  it("auto-reconnects after the server closes the socket, and re-subscribes to the original channels", async () => {
    const { result } = renderHook(() =>
      useWebSocket({ channels: ["signals", "trades"], backoffMinMs: 30, backoffMaxMs: 60 })
    );

    // First socket: open, then close.
    const first = FakeWebSocket.instances[FakeWebSocket.instances.length - 1]!;
    await act(async () => {
      first.fakeOpen();
    });
    expect(result.current.status).toBe("open");

    await act(async () => {
      first.fakeClose();
    });
    expect(result.current.status).toBe("closed");

    // Wait for the reconnect timer to fire and a new socket to be built.
    await act(async () => {
      await tick(80);
    });

    expect(FakeWebSocket.instances.length).toBeGreaterThanOrEqual(2);
    const second = FakeWebSocket.instances[FakeWebSocket.instances.length - 1]!;
    expect(second).not.toBe(first);

    act(() => {
      second.fakeOpen();
    });

    // The reconnect re-subscribes the original channels, regardless of
    // whether React has flushed the status update yet.
    expect(second.sent).toEqual([{ type: "subscribe", channels: ["signals", "trades"] }]);
    // And the new socket should eventually report as open.
    await waitFor(() => {
      expect(result.current.status).toBe("open");
    });
  });

  it("uses exponential backoff between successive reconnects", async () => {
    const { result } = renderHook(() =>
      useWebSocket({ backoffMinMs: 30, backoffMaxMs: 200 })
    );

    const first = FakeWebSocket.instances[FakeWebSocket.instances.length - 1]!;
    await act(async () => {
      first.fakeOpen();
    });
    expect(result.current.status).toBe("open");

    // Close → first reconnect at ~30ms. We wait long enough for it.
    await act(async () => {
      first.fakeClose();
      await tick(60);
    });
    expect(FakeWebSocket.instances.length).toBeGreaterThanOrEqual(2);
    const second = FakeWebSocket.instances[FakeWebSocket.instances.length - 1]!;

    // Close the second before opening → triggers another reconnect.
    await act(async () => {
      second.fakeClose();
    });

    // The backoff is now ~60ms (doubled). Wait 30ms — the third socket
    // should NOT have been created yet.
    await act(async () => {
      await tick(30);
    });
    expect(FakeWebSocket.instances.length).toBe(2);

    // Wait the remaining 60ms+ — the third socket should now exist.
    await act(async () => {
      await tick(80);
    });
    expect(FakeWebSocket.instances.length).toBeGreaterThanOrEqual(3);
  });

  it("exposes imperative subscribe/unsubscribe that round-trips over the wire", async () => {
    const { result } = renderHook(() => useWebSocket());
    const ws = FakeWebSocket.instances[FakeWebSocket.instances.length - 1]!;
    await act(async () => {
      ws.fakeOpen();
    });

    act(() => {
      result.current.subscribe(["signals", "trades"]);
      result.current.unsubscribe(["trades"]);
    });
    expect(ws.sent).toEqual([
      { type: "subscribe", channels: ["signals", "trades"] },
      { type: "unsubscribe", channels: ["trades"] },
    ]);
  });
});

