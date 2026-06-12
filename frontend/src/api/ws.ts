// WebSocket hook type — minimal; actual impl is in hooks/useWebSocket.ts
export type WsStatus = "connecting" | "open" | "closed";

export interface WsMessage {
  channel: string;
  payload: unknown;
  event_id: string;
  ts: string;
}

export interface WsHook {
  status: WsStatus;
  lastMessage: WsMessage | null;
  subscribe: (channels: string[]) => void;
  unsubscribe: (channels: string[]) => void;
}
