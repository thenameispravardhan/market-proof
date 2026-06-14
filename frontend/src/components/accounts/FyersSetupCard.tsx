// Fyers App Activation card — the copy-paste cheat-sheet for the
// one-time Fyers dashboard setup.
//
// Under SEBI's 2026 retail-algo framework, order placement is allowed
// ONLY from a single app created + activated on the NEW Fyers API
// dashboard (fyers.in/web/api-dashboard), and only from a whitelisted
// static IP. That activation is a Fyers-side step with no API, so the
// operator has to do it by hand once. This card removes the guesswork:
// it surfaces the three exact values they must paste (Redirect URL,
// Primary Static IP, App ID), each with a Copy button, plus the step
// order. Everything after activation (Connect Fyers, trading) is in the
// bot UI.

import { useState } from "react";
import { useFyersStatus, useServerInfo } from "../../hooks/useApi";

const FYERS_DASHBOARD_URL = "https://fyers.in/web/api-dashboard/user-apps";

function CopyField({
  label,
  value,
  hint,
}: {
  label: string;
  value: string | null;
  hint?: string;
}) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      /* clipboard may be unavailable; the value is selectable */
    }
  };
  return (
    <div className="field" style={{ marginBottom: 10 }}>
      <label>
        {label}
        {hint && (
          <span style={{ color: "var(--text-dim)", fontWeight: 400 }}> — {hint}</span>
        )}
      </label>
      <div style={{ display: "flex", gap: 8 }}>
        <input
          className="mono"
          readOnly
          value={value ?? "—"}
          style={{ flex: 1, userSelect: "all" }}
          onFocus={(e) => e.currentTarget.select()}
        />
        <button className="btn-sm" onClick={copy} disabled={!value} title="Copy">
          {copied ? "✓" : "Copy"}
        </button>
      </div>
    </div>
  );
}

export function FyersSetupCard() {
  const { data: status } = useFyersStatus();
  const { data: serverInfo } = useServerInfo();

  const appId = status?.app_id ?? null;
  const redirect = status?.redirect_uri ?? null;
  const ip = serverInfo?.public_ip ?? null;

  return (
    <div className="widget" data-testid="fyers-setup">
      <h3>
        Fyers App Activation
        <span className="badge neutral">One-time</span>
      </h3>

      <p className="text-dim" style={{ fontSize: 12, marginBottom: 14 }}>
        SEBI's 2026 algo rules require <strong>one</strong> app, created and{" "}
        <strong>activated</strong> on the new{" "}
        <a href={FYERS_DASHBOARD_URL} target="_blank" rel="noreferrer">
          Fyers API Dashboard
        </a>
        , with order placement locked to a whitelisted static IP. This is a
        one-time manual step on Fyers' site — copy the three values below into
        the activation form.
      </p>

      <ol
        style={{
          fontSize: 13,
          lineHeight: 1.6,
          margin: "0 0 14px",
          paddingLeft: 18,
          color: "var(--text)",
        }}
      >
        <li>
          Open the{" "}
          <a href={FYERS_DASHBOARD_URL} target="_blank" rel="noreferrer">
            Fyers API Dashboard
          </a>{" "}
          → your app → <strong>Activate / Edit App Details</strong>.
        </li>
        <li>Paste the three values below into the matching fields.</li>
        <li>
          Leave <em>Secondary IP</em> blank · tick <strong>all Permissions</strong>{" "}
          (incl. <strong>Order Placement</strong>) · accept the API terms.
        </li>
        <li>
          Click <strong>Activate</strong> on Fyers, then{" "}
          <strong>Connect Fyers</strong> below.
        </li>
      </ol>

      <CopyField
        label="Redirect URL"
        value={redirect}
        hint="must match exactly"
      />
      <CopyField
        label="Primary Static IP"
        value={ip}
        hint="this server's public IP"
      />
      <CopyField
        label="App ID"
        value={appId}
        hint="the dashboard app must show this"
      />

      <p className="text-dim" style={{ fontSize: 11, marginTop: 4 }}>
        The App ID above is what the bot is running with (from{" "}
        <code className="mono">.env</code>). If the app on the Fyers dashboard
        shows a <em>different</em> ID, you're editing the wrong app — order
        placement only works from the one whose ID matches here. Static IPs are
        editable once per week on Fyers.
      </p>
    </div>
  );
}
