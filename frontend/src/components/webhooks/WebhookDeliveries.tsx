// WebhookDeliveries — recent delivery log for a webhook.
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";

interface Delivery {
  id: number;
  direction: string;
  event_type: string | null;
  status_code: number | null;
  error: string | null;
  attempted_at: string;
}

interface Props {
  webhookId: number | null;
}

function fmtTime(iso: string): string {
  return new Date(iso).toLocaleString();
}

export function WebhookDeliveries({ webhookId }: Props) {
  const { data, isLoading } = useQuery<{ deliveries: Delivery[]; count: number }>({
    queryKey: ["webhook-deliveries", webhookId],
    queryFn: () => api.get(`/api/webhooks/${webhookId}/deliveries`),
    enabled: webhookId !== null,
    refetchInterval: 10000,
  });

  if (!webhookId) {
    return (
      <div className="widget" data-testid="webhook-deliveries">
        <h3>Delivery Log</h3>
        <p className="empty">Select a webhook to see its delivery log.</p>
      </div>
    );
  }

  return (
    <div className="widget" data-testid="webhook-deliveries">
      <h3>Delivery Log</h3>
      {isLoading ? (
        <p className="empty">Loading…</p>
      ) : !data || data.deliveries.length === 0 ? (
        <p className="empty">No deliveries yet.</p>
      ) : (
        <div className="list">
          {data.deliveries.slice(0, 20).map((d: Delivery) => (
            <div key={d.id} className="item">
              <div className="head">
                <span className={`badge ${d.status_code && d.status_code < 300 ? "info" : "danger"}`}>
                  {d.status_code ?? "ERR"}
                </span>
                <span className="symbol">{d.event_type ?? "—"}</span>
              </div>
              {d.error && <div className="body pnl-neg" style={{ fontSize: 12 }}>{d.error}</div>}
              <div className="body reason">{fmtTime(d.attempted_at)}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
