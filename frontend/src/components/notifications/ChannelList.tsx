// ChannelList — notification channels list.
import {
  useNotificationChannels,
  useDeleteNotificationChannel,
  useTestNotificationChannel,
} from "../../hooks/useApi";
import { useState } from "react";
import type { NotificationChannel } from "../../types";

interface Props {
  selectedId: number | null;
  onSelect: (id: number) => void;
  onNew: () => void;
}

const KIND_BADGE: Record<string, string> = {
  telegram: "info",
  discord: "info",
  email: "warn",
  webhook: "warn",
};

export function ChannelList({ selectedId, onSelect, onNew }: Props) {
  const { data: channels, isLoading } = useNotificationChannels();
  const deleteChannel = useDeleteNotificationChannel();
  const testChannel = useTestNotificationChannel();
  // Result of the last Test / Delete, shown under that channel instead of a
  // blocking alert().
  const [result, setResult] = useState<{ id: number; ok: boolean; text: string } | null>(null);

  const handleDelete = async (c: NotificationChannel) => {
    if (!confirm(`Delete channel "${c.name}"?`)) return;
    try {
      await deleteChannel.mutateAsync(c.id);
      if (selectedId === c.id) onNew();
    } catch (err) {
      setResult({ id: c.id, ok: false, text: `Delete failed: ${(err as Error).message}` });
    }
  };

  const handleTest = async (e: React.MouseEvent, id: number) => {
    e.stopPropagation();
    setResult(null);
    try {
      const r = await testChannel.mutateAsync(id);
      setResult({ id, ok: r.ok, text: r.ok ? "Test message sent." : `Test failed: ${r.error}` });
    } catch (err) {
      setResult({ id, ok: false, text: `Test failed: ${(err as Error).message}` });
    }
  };

  return (
    <div className="widget" data-testid="channel-list">
      <div className="widget-header">
        <h3>Channels</h3>
        <button className="btn-sm primary" onClick={onNew}>+ New</button>
      </div>
      {isLoading ? (
        <p className="empty">Loading…</p>
      ) : !channels || channels.length === 0 ? (
        <p className="empty">No notification channels. Add Telegram, Discord, email, or webhook.</p>
      ) : (
        <div className="list">
          {channels.map((c: NotificationChannel) => (
            <div
              key={c.id}
              className={`item${selectedId === c.id ? " selected" : ""}`}
              onClick={() => onSelect(c.id)}
            >
              <div className="head">
                <span className="symbol">{c.name}</span>
                <span className={`badge ${KIND_BADGE[c.kind] ?? "info"}`}>{c.kind}</span>
                {!c.enabled && <span className="badge warn" style={{ marginLeft: 4 }}>disabled</span>}
              </div>
              <div className="body reason">
                Sends: {!c.events_filter || c.events_filter.trim() === "*" ? "everything" : c.events_filter.split(",").join(", ")}
              </div>
              {result?.id === c.id && (
                <div className={`body reason ${result.ok ? "pnl-pos" : "pnl-neg"}`}>{result.text}</div>
              )}
              <div className="body reason" style={{ display: "flex", gap: 8, marginTop: 6 }}>
                <button
                  className="btn-sm"
                  onClick={(e) => handleTest(e, c.id)}
                  disabled={testChannel.isPending}
                  title="Send a test message now"
                >
                  {testChannel.isPending && testChannel.variables === c.id ? "Sending…" : "Test"}
                </button>
                <button
                  className="btn-sm danger"
                  onClick={(e) => { e.stopPropagation(); handleDelete(c); }}
                >
                  Delete
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
