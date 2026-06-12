// useWebSocket tests — the headline test is the auto-reconnect
// behaviour. We stub global.WebSocket with a minimal class that we
// can drive from the test (open, message, close, error). React
// Strict Mode is not used in these tests because its double-mount
// complicates timing.

import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
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
  vi.useFakeTimers();
  // @ts-expect-error -- assigning a class to the global WebSocket slot
  globalThis.WebSocket = FakeWebSocket;
});
afterEach(() => {
  vi.useRealTimers();
  delete (globalThis as { WebSocket?: unknown }).WebSocket;
});

describe("useWebSocket", () => {
  it("connects on mount, sends a subscribe for initial channels, and exposes status=open", async () => {
    const { result } = renderHook(() =>
      useWebSocket({ channels: ["signals"], backoffMinMs: 100, backoffMaxMs: 200 })
    );

    // The hook builds the WebSocket synchronously inside useEffect.
    expect(FakeWebSocket.instances).toHaveLength(1);
    const ws = FakeWebSocket.instances[0]!;

    await act(async () => {
      ws.fakeOpen();
    });

    expect(result.current.status).toBe("open");
    expect(ws.sent).toEqual([{ type: "subscribe", channels: ["signals"] }]);
  });

  it("parses incoming 'event' frames and surfaces them as lastMessage", async () => {
    const { result } = renderHook(() => useWebSocket());
    const ws = FakeWebSocket.instances[0]!;
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
      useWebSocket({ channels: ["signals", "trades"], backoffMinMs: 100, backoffMaxMs: 200 })
    );

    // First socket: open, then close.
    const first = FakeWebSocket.instances[0]!;
    await act(async () => {
      first.fakeOpen();
    });
    expect(result.current.status).toBe("open");

    await act(async () => {
      first.fakeClose();
    });
    expect(result.current.status).toBe("closed");

    // The hook schedules a reconnect with backoff (initial 100ms). Advance
    // past the timer so the new socket is constructed.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(150);
    });

    // A second WebSocket should now exist.
    expect(FakeWebSocket.instances.length).toBeGreaterThanOrEqual(2);
    const second = FakeWebSocket.instances[FakeWebSocket.instances.length - 1]!;
    expect(second).not.toBe(first);

    await act(async () => {
      second.fakeOpen();
    });

    expect(result.current.status).toBe("open");
    // The re-subscribed channels match the original set.
    expect(second.sent).toEqual([{ type: "subscribe", channels: ["signals", "trades"] }]);
  });

  it("uses exponential backoff between successive reconnects", async () => {
    const { result } = renderHook(() =>
      useWebSocket({ backoffMinMs: 100, backoffMaxMs: 800 })
    );

    // First open.
    const first = FakeWebSocket.instances[0]!;
    await act(async () => {
      first.fakeOpen();
    });
    expect(result.current.status).toBe("open");

    // Close → first reconnect at ~100ms.
    await act(async () => {
      first.fakeClose();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120);
    });
    const second = FakeWebSocket.instances[1]!;
    await act(async () => {
      second.fakeClose(); // close before opening
    });
    // Second reconnect: should be ~200ms (doubled). If we only advance 120ms,
    // a third socket should NOT have been created yet.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120);
    });
    expect(FakeWebSocket.instances).toHaveLength(2);

    // Advance past the doubled backoff.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200);
    });
    expect(FakeWebSocket.instances.length).toBeGreaterThanOrEqual(3);
  });

  it("exposes imperative subscribe/unsubscribe that round-trips over the wire", async () => {
    const { result } = renderHook(() => useWebSocket());
    const ws = FakeWebSocket.instances[0]!;
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
