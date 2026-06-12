// WebhookForm — create or edit a webhook registration.
import { useEffect, useState } from "react";
import { useWebhooks, useCreateWebhook, useUpdateWebhook } from "../../hooks/useApi";

interface Props {
  webhookId: number | null;
  onSaved?: () => void;
}

export function WebhookForm({ webhookId, onSaved }: Props) {
  const { data: webhooks } = useWebhooks();
  const webhook = webhooks?.find((w) => w.id === webhookId) ?? null;
  const create = useCreateWebhook();
  const update = useUpdateWebhook(webhookId);

  const [name, setName] = useState("");
  const [direction, setDirection] = useState<"in" | "out">("out");
  const [url, setUrl] = useState("");
  const [eventFilter, setEventFilter] = useState("*");
  const [secret, setSecret] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (webhook) {
      setName(webhook.name);
      setDirection(webhook.direction);
      setUrl(webhook.url);
      setEventFilter(webhook.event_filter ?? "*");
      setSecret(""); // never pre-fill secrets
      setEnabled(webhook.enabled);
    } else {
      setName(""); setDirection("out"); setUrl(""); setEventFilter("*"); setSecret(""); setEnabled(true);
    }
    setError(null);
  }, [webhook]);

  const handleSave = async () => {
    setError(null);
    const body: Record<string, unknown> = {
      name, direction, url, event_filter: eventFilter, enabled,
    };
    if (secret) body.secret = secret;
    try {
      if (webhook) {
        await update.mutateAsync(body as never);
      } else {
        await create.mutateAsync(body as never);
      }
      onSaved?.();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="widget" data-testid="webhook-form">
      <h3>{webhook ? `Edit — ${webhook.name}` : "New Webhook"}</h3>
      <div className="field">
        <label htmlFor="wh-name">Name</label>
        <input id="wh-name" value={name} onChange={(e) => setName(e.target.value)} />
      </div>
      <div className="field">
        <label htmlFor="wh-direction">Direction</label>
        <select
          id="wh-direction"
          value={direction}
          onChange={(e) => setDirection(e.target.value as "in" | "out")}
          disabled={!!webhook}
        >
          <option value="out">Outbound (bot POSTs to this URL)</option>
          <option value="in">Inbound (external service POSTs to the bot)</option>
        </select>
      </div>
      <div className="field">
        <label htmlFor="wh-url">URL</label>
        <input id="wh-url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://…" />
      </div>
      <div className="field">
        <label htmlFor="wh-filter">Event filter (comma-separated or *)</label>
        <input id="wh-filter" value={eventFilter} onChange={(e) => setEventFilter(e.target.value)} />
      </div>
      <div className="field">
        <label htmlFor="wh-secret">
          {direction === "out" ? "HMAC secret (optional)" : "Bearer token / shared secret (optional)"}
          {webhook && " — leave blank to keep current"}
        </label>
        <input
          id="wh-secret"
          type="password"
          value={secret}
          onChange={(e) => setSecret(e.target.value)}
          placeholder={webhook?.secret ? "●●●●" : "optional"}
          autoComplete="new-password"
        />
      </div>
      {direction === "in" && webhookId && (
        <div className="field" style={{ background: "#0d1117", padding: 8, borderRadius: 4, fontSize: 12 }}>
          <p style={{ margin: 0, color: "var(--text-dim)" }}>
            Inbound URL:{" "}
            <code className="mono">/api/webhooks/in/{webhookId}</code>
          </p>
        </div>
      )}
      <div className="field">
        <label>
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          {" "}Enabled
        </label>
      </div>
      {error && <p className="pnl-neg">{error}</p>}
      <button
        className="primary"
        onClick={handleSave}
        disabled={create.isPending || update.isPending || !name || !url}
        data-testid="save-webhook"
      >
        {create.isPending || update.isPending ? "Saving…" : webhook ? "Update" : "Create"}
      </button>
    </div>
  );
}
