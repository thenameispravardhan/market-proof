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
}

interface UseWebSocketResult {
  status: WsStatus;
  lastMessage: WsMessage | null;
  subscribe: (channels: string[]) => void;
  unsubscribe: (channels: string[]) => void;
}

export function useWebSocket(options: UseWebSocketOptions = {}): UseWebSocketResult {
  const { channels = [], backoffMinMs = 1000, backoffMaxMs = 30_000 } = options;

  const [status, setStatus] = useState<WsStatus>("connecting");
  const [lastMessage, setLastMessage] = useState<WsMessage | null>(null);

  // Keep refs for the WS instance and mutable config so callbacks don't
  // close over stale values.
  const wsRef = useRef<WebSocket | null>(null);
  const backoffRef = useRef(backoffMinMs);
  const channelsRef = useRef(channels);
  channelsRef.current = channels;

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
          setLastMessage({
            channel: frame.channel,
            payload: frame.payload,
            event_id: frame.event_id ?? "",
            ts: frame.ts ?? "",
          });
        }
      } catch {
        // ignore malformed frames
      }
    };

    ws.onclose = () => {
      setStatus("closed");
      wsRef.current = null;
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

  useEffect(() => {
    const ws = connect();
    return () => {
      // On unmount: close without reconnecting by nulling the ref first.
      wsRef.current = null;
      if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
        ws.close();
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const send = useCallback((msg: unknown) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
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
