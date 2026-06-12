// Top-bar pill showing the current WebSocket state. We don't try to
// surface every event — just the connection. The dot colour drives
// the visual.

import type { WsStatus } from "../../hooks/useWebSocket";

const LABEL: Record<WsStatus, string> = {
  connecting: "Connecting…",
  open: "Connected",
  closed: "Disconnected",
};

export function ConnectionStatus({ status }: { status: WsStatus }) {
  return (
    <span
      className={`connection-pill ${status}`}
      data-testid="connection-pill"
      title={`WebSocket: ${LABEL[status]}`}
    >
      <span className="dot" aria-hidden="true" />
      {LABEL[status]}
    </span>
  );
}
