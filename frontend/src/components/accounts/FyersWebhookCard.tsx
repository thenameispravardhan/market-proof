// Fyers order-postback (webhook) configuration — fully frontend-driven.
//
// The operator sets their public base URL (e.g. a Cloudflare Tunnel /
// ngrok host), and the card shows the exact URL + secret + preference
// to paste into the Fyers API dashboard's "Webhooks/Postbacks" section.
// The secret is carried in the URL as a ?token=, so auth doesn't depend
// on Fyers' request signing. Postbacks reconcile the matching order —
// they never create a new trade.

import { useEffect, useState } from "react";
import {
  useFyersPostbackConfig,
  useUpdateFyersPostbackConfig,
} from "../../hooks/useApi";

function CopyField({ label, value }: { label: string; value: string | null }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      /* clipboard may be unavailable; ignore */
    }
  };
  return (
    <div className="field">
      <label>{label}</label>
      <div style={{ display: "flex", gap: 8 }}>
        <input className="mono" readOnly value={value ?? "—"} style={{ flex: 1 }} />
        <button className="btn-sm" onClick={copy} disabled={!value} title="Copy">
          {copied ? "✓" : "Copy"}
        </button>
      </div>
    </div>
  );
}

export function FyersWebhookCard() {
  const { data: cfg } = useFyersPostbackConfig();
  const update = useUpdateFyersPostbackConfig();
  const [base, setBase] = useState("");

  useEffect(() => {
    if (cfg) setBase(cfg.public_base_url || "");
  }, [cfg?.public_base_url]);

  const save = () => update.mutate({ public_base_url: base.trim() });
  const regenerate = () =>
    update.mutate({ public_base_url: base.trim(), regenerate_secret: true });

  return (
    <div className="widget" data-testid="fyers-webhook">
      <h3>
        Fyers Order Postback (Webhook)
        <span className={`badge ${cfg?.configured ? "green" : "warn"}`}>
          {cfg?.configured ? "Ready" : "Setup"}
        </span>
      </h3>

      <p className="text-dim" style={{ fontSize: 12, marginBottom: 12 }}>
        Fyers POSTs order fills / rejects here so the bot reconciles them
        instantly (no polling). Set your public tunnel URL, then paste the
        generated URL + secret into the Fyers dashboard.
      </p>

      <div className="field">
        <label>Public base URL (your tunnel / reverse proxy)</label>
        <div style={{ display: "flex", gap: 8 }}>
          <input
            className="mono"
            placeholder="https://your-tunnel.trycloudflare.com"
            value={base}
            onChange={(e) => setBase(e.target.value)}
            style={{ flex: 1 }}
          />
          <button className="btn-sm primary" onClick={save} disabled={update.isPending}>
            {update.isPending ? "…" : "Save"}
          </button>
        </div>
      </div>

      <CopyField label="Webhook URL (paste in Fyers)" value={cfg?.url ?? null} />
      <CopyField label="Webhook Secret" value={cfg?.secret ?? null} />

      <div className="field">
        <label>Webhook Preference (select in Fyers)</label>
        <input className="mono" readOnly value={cfg?.preference ?? "—"} />
      </div>

      <button className="btn-sm" onClick={regenerate} disabled={update.isPending}>
        Regenerate secret
      </button>
      {!cfg?.url && (
        <p className="text-dim" style={{ fontSize: 11, marginTop: 8 }}>
          Set a public base URL above to generate the full webhook URL.
        </p>
      )}
    </div>
  );
}
