// useWebSocket — auto-reconnecting WebSocket hook with exponential backoff.
//
// Options:
//   channels    — subscribe to these channels on connect
//   backoffMinMs — initial backoff (default 1000ms)
//   backoffMaxMs — max backoff (default 30000ms)

import { useCallback, useEffect, useRef, useState } from "react";

export type WsStatus = "connecting" | "open" | "closed";

export interface WsMessage {
  channel: string;
  payload: unknown;
  event_id: string;
  ts: string;
}

interface UseWebSocketOptions {
  channels?: string[];
  backoffMinMs?: number;
  backoffMaxMs?: number;
  // Imperative per-event callback. Fired for every event frame BEFORE
  // `lastMessage` is set. High-frequency channels (e.g. "quotes") route
  // through here into an external store so they never churn React state.
  onEvent?: (msg: WsMessage) => void;
}

interface UseWebSocketResult {
  status: WsStatus;
  lastMessage: WsMessage | null;
  subscribe: (channels: string[]) => void;
  unsubscribe: (channels: string[]) => void;
}

// Capture the readyState constants at module load so the unmount
// cleanup is robust to a caller (or a test's afterEach) wiping
// `globalThis.WebSocket` between mount and unmount.
const WS_OPEN = WebSocket.OPEN;
const WS_CONNECTING = WebSocket.CONNECTING;

export function useWebSocket(options: UseWebSocketOptions = {}): UseWebSocketResult {
  const { channels = [], backoffMinMs = 1000, backoffMaxMs = 30_000, onEvent } = options;

  const [status, setStatus] = useState<WsStatus>("connecting");
  const [lastMessage, setLastMessage] = useState<WsMessage | null>(null);

  // Keep refs for the WS instance and mutable config so callbacks don't
  // close over stale values.
  const wsRef = useRef<WebSocket | null>(null);
  const backoffRef = useRef(backoffMinMs);
  const channelsRef = useRef(channels);
  channelsRef.current = channels;
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  const getUrl = () => {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${window.location.host}/ws`;
  };

  const connect = useCallback(() => {
    setStatus("connecting");
    const ws = new WebSocket(getUrl());
    wsRef.current = ws;

    ws.onopen = () => {
      setStatus("open");
      backoffRef.current = backoffMinMs; // reset on success
      if (channelsRef.current.length > 0) {
        ws.send(JSON.stringify({ type: "subscribe", channels: channelsRef.current }));
      }
    };

    ws.onmessage = (ev) => {
      try {
        const frame = JSON.parse(ev.data as string);
        if (frame.type === "event") {
          const msg: WsMessage = {
            channel: frame.channel,
            payload: frame.payload,
            event_id: frame.event_id ?? "",
            ts: frame.ts ?? "",
          };
          onEventRef.current?.(msg);
          // Quotes are high-frequency and consumed via the external store
          // through onEvent — don't re-render every lastMessage subscriber
          // on each tick.
          if (msg.channel !== "quotes") setLastMessage(msg);
        }
      } catch {
        // ignore malformed frames
      }
    };

    ws.onclose = () => {
      setStatus("closed");
      wsRef.current = null;
      // If the hook is being torn down, do not schedule a reconnect.
      if (disposedRef.current) return;
      // Schedule reconnect with exponential backoff.
      const delay = backoffRef.current;
      backoffRef.current = Math.min(delay * 2, backoffMaxMs);
      setTimeout(() => connect(), delay);
    };

    ws.onerror = () => {
      // onclose will fire next; nothing extra needed here.
    };

    return ws;
  }, [backoffMinMs, backoffMaxMs]);

  // `disposed` is set by the useEffect cleanup so that an `onclose`
  // fired by the cleanup's `ws.close()` does NOT schedule a reconnect
  // (otherwise we'd reconnect to a server that the caller is trying to
  // tear down, and the test harness's implicit act() cleanup would
  // double-schedule reconnects).
  const disposedRef = useRef(false);

  useEffect(() => {
    disposedRef.current = false;
    const ws = connect();
    return () => {
      disposedRef.current = true;
      // On unmount: close without reconnecting by nulling the ref first.
      wsRef.current = null;
      if (ws.readyState === WS_OPEN || ws.readyState === WS_CONNECTING) {
        ws.close();
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const send = useCallback((msg: unknown) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WS_OPEN) {
      ws.send(JSON.stringify(msg));
    }
  }, []);

  const subscribe = useCallback(
    (ch: string[]) => send({ type: "subscribe", channels: ch }),
    [send]
  );
  const unsubscribe = useCallback(
    (ch: string[]) => send({ type: "unsubscribe", channels: ch }),
    [send]
  );

  return { status, lastMessage, subscribe, unsubscribe };
}
