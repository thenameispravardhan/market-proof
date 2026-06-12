// ChannelList — notification channels list.
import {
  useNotificationChannels,
  useDeleteNotificationChannel,
  useTestNotificationChannel,
} from "../../hooks/useApi";
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

  const handleDelete = async (c: NotificationChannel) => {
    if (!confirm(`Delete channel "${c.name}"?`)) return;
    await deleteChannel.mutateAsync(c.id);
  };

  const handleTest = async (e: React.MouseEvent, id: number) => {
    e.stopPropagation();
    try {
      const r = await testChannel.mutateAsync(id);
      alert(r.ok ? "✓ Test message sent!" : `✗ ${r.error}`);
    } catch (err) {
      alert(`✗ ${(err as Error).message}`);
    }
  };

  return (
    <div className="widget" data-testid="channel-list">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
        <h3 style={{ margin: 0 }}>Channels</h3>
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
                Events: {c.events_filter ?? "*"}
              </div>
              <div className="body reason" style={{ display: "flex", gap: 8, marginTop: 6 }}>
                <button
                  className="btn-sm"
                  onClick={(e) => handleTest(e, c.id)}
                  disabled={testChannel.isPending}
                >
                  Test
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
