// WebhookList — inbound and outbound webhook registrations.
import { useWebhooks, useDeleteWebhook } from "../../hooks/useApi";
import type { Webhook } from "../../types";

interface Props {
  selectedId: number | null;
  onSelect: (id: number) => void;
  onNew: () => void;
}

export function WebhookList({ selectedId, onSelect, onNew }: Props) {
  const { data: webhooks, isLoading } = useWebhooks();
  const deleteWebhook = useDeleteWebhook();

  const handleDelete = async (w: Webhook) => {
    if (!confirm(`Delete webhook "${w.name}"?`)) return;
    await deleteWebhook.mutateAsync(w.id);
  };

  return (
    <div className="widget" data-testid="webhook-list">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
        <h3 style={{ margin: 0 }}>Webhooks</h3>
        <button className="btn-sm primary" onClick={onNew}>+ New</button>
      </div>
      {isLoading ? (
        <p className="empty">Loading…</p>
      ) : !webhooks || webhooks.length === 0 ? (
        <p className="empty">No webhooks. Create inbound or outbound webhooks.</p>
      ) : (
        <div className="list">
          {webhooks.map((w: Webhook) => (
            <div
              key={w.id}
              className={`item${selectedId === w.id ? " selected" : ""}`}
              onClick={() => onSelect(w.id)}
            >
              <div className="head">
                <span className="symbol">{w.name}</span>
                <span className={`badge ${w.direction === "in" ? "warn" : "info"}`}>
                  {w.direction === "in" ? "inbound" : "outbound"}
                </span>
                {!w.enabled && <span className="badge warn" style={{ marginLeft: 4 }}>disabled</span>}
              </div>
              <div className="body mono" style={{ fontSize: 12, color: "var(--text-dim)" }}>
                {w.url.length > 50 ? w.url.slice(0, 50) + "…" : w.url}
              </div>
              <div className="body reason">
                Events: {w.event_filter ?? "*"} · Secret: {w.secret ? "●●●●" : "none"}
              </div>
              <div className="body reason" style={{ marginTop: 6 }}>
                <button
                  className="btn-sm danger"
                  onClick={(e) => { e.stopPropagation(); handleDelete(w); }}
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
