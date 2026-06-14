// ConnectFyers — a persistent banner at the top of the Accounts page
// that walks the operator through the Fyers OAuth flow in 1 click.
//
// States:
//   - not configured  : Fyers App ID / Secret not in .env → prompt to add
//   - creds set, no OAuth : Show big "Connect Fyers" button
//   - connected      : Show "Connected to <account>" + Disconnect
//
// The button opens Fyers' OAuth URL in a popup window. The popup
// redirects to /api/fyers/callback on success, which mints the
// access token. We poll /api/fyers/status every 2s while the
// popup is open so the banner flips to "Connected" the moment
// OAuth completes — no manual refresh.

import { useEffect, useRef, useState } from "react";
import {
  useFyersAuthorizeUrl,
  useFyersDisconnect,
  useFyersStatus,
} from "../../hooks/useApi";

type Mode = "idle" | "opening" | "waiting" | "error";

export function ConnectFyers() {
  const { data: status, refetch } = useFyersStatus();
  const authorize = useFyersAuthorizeUrl();
  const disconnect = useFyersDisconnect();
  const [mode, setMode] = useState<Mode>("idle");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  // Mirror the latest status into a ref so the popup-close handler
  // below can read the most recent value instead of the one
  // captured at click-time. Without this, the callback's token
  // write + status refetch can race the close handler and we'd
  // see a stale "not connected" even after OAuth succeeded.
  const statusRef = useRef(status);
  useEffect(() => {
    statusRef.current = status;
  }, [status]);

  // While the popup is open, poll status every 2s so the banner
  // flips to "Connected" the instant the OAuth callback finishes.
  useEffect(() => {
    if (mode !== "opening" && mode !== "waiting") return;
    const t = setInterval(() => refetch(), 2000);
    return () => clearInterval(t);
  }, [mode, refetch]);

  // Clear stale error messages whenever the underlying status changes
  // (e.g. user fixed the .env, or token got refreshed). Prevents the
  // banner from showing "Fyers creds not configured" on top of a
  // "Token Expired" message — they are different states.
  useEffect(() => {
    setErrorMsg(null);
  }, [status?.connected, status?.credentials_set, status?.account_present]);

  // Watch for the connected state — when it flips on, close the popup
  // and reset.
  useEffect(() => {
    if (status?.connected && (mode === "opening" || mode === "waiting")) {
      setMode("idle");
      setErrorMsg(null);
    }
  }, [status?.connected, mode]);

  const onConnect = async () => {
    setMode("opening");
    setErrorMsg(null);
    try {
      const resp = await authorize.mutateAsync();
      if (!resp.configured) {
        setMode("error");
        setErrorMsg(
          resp.reason ??
            "Fyers creds not configured in .env. Add FYERS_APP_ID and FYERS_SECRET_KEY, then restart the backend."
        );
        return;
      }
      // Open the Fyers auth URL in a popup. ~600x700 is plenty.
      const popup = window.open(
        resp.url,
        "fyers-oauth",
        "width=600,height=700,left=200,top=100"
      );
      if (!popup) {
        setMode("error");
        setErrorMsg(
          "Popup was blocked. Allow popups for 127.0.0.1:5173 in your browser, then click Connect again."
        );
        return;
      }
      setMode("waiting");
      // Watch for the popup to close (user finished or cancelled).
      // Fyers redirects back to /api/fyers/callback on success; if
      // the popup closes without reaching the callback, the most
      // likely causes are:
      //   - The configured FYERS_APP_ID was deleted on Fyers'
      //     dashboard (Fyers shows "deleted app" in the popup)
      //   - The operator updated .env with new keys but DID NOT
      //     restart the backend. The server is still using the
      //     values it loaded at startup, so the OAuth URL is
      //     built with the OLD (deleted) app_id.
      //   - The redirect URI in .env / broker_accounts doesn't
      //     match what Fyers has on file
      //   - The user closed the popup manually
      //   - The popup is showing a captcha / 2FA challenge that
      //     the user never completed
      // We surface a single message covering all five instead of
      // silently resetting — operators were getting stuck on
      // "Fyers Token Expired" with no way to diagnose the cause.
      const watch = setInterval(() => {
        if (popup.closed) {
          clearInterval(watch);
          // Give the status poll one more tick to detect a fresh
          // token (the callback's token write + status update can
          // race the popup-close handler). Read from statusRef so
          // we see the most recent value, not the one captured at
          // click-time.
          setTimeout(() => {
            if (!statusRef.current?.connected) {
              setMode("error");
              setErrorMsg(
                "OAuth did not complete. Most common causes: " +
                  "(1) you updated FYERS_APP_ID / FYERS_SECRET_KEY in .env " +
                  "but did NOT restart the backend — the running process is " +
                  "still using the old (deleted) keys it loaded at startup. " +
                  "Stop the uvicorn process and start it again, then retry. " +
                  "(2) the FYERS_APP_ID in .env was deleted on the Fyers dashboard " +
                  "(re-create it on https://myapi.fyers.in/dashboard/, update .env, " +
                  "and restart the backend). " +
                  "(3) FYERS_REDIRECT_URI in .env doesn't match what's registered on " +
                  "https://myapi.fyers.in/dashboard/. " +
                  "(4) the popup was closed before login finished. " +
                  "(5) a captcha or 2FA challenge was shown and not completed."
              );
            } else {
              setMode("idle");
            }
          }, 1500);
        }
      }, 1000);
    } catch (e: any) {
      setMode("error");
      setErrorMsg(`Could not start OAuth: ${e?.message ?? "unknown error"}`);
    }
  };

  const onDisconnect = async () => {
    if (!confirm("Disconnect Fyers? The bot will stop using live data and you will need to re-authorise.")) return;
    try {
      const r = await disconnect.mutateAsync();
      // The endpoint returns 200 with { ok: false, reason } when it
      // can't find a connected account to clear — surface that instead
      // of letting the click look like it did nothing.
      if (r && r.ok === false) {
        setErrorMsg(
          `Disconnect did nothing: ${r.reason ?? "no connected Fyers account found."}`
        );
      } else {
        setErrorMsg(null);
      }
    } catch (e: any) {
      setErrorMsg(`Disconnect failed: ${e?.message ?? "unknown error"}`);
    }
  };

  // ---- Render ----

  if (status?.connected) {
    return (
      <div
        className="widget"
        data-testid="connect-fyers"
        style={{ borderColor: "var(--green)", borderWidth: 1, borderStyle: "solid" }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <span style={{ color: "var(--green)", fontSize: 20 }}>●</span>
          <div style={{ flex: 1 }}>
            <div style={{ fontWeight: 700, color: "var(--text)" }}>
              Fyers Connected
            </div>
            <div style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 2 }}>
              App ID: {status.app_id || "—"} · Status bar will show live NIFTY / SENSEX / BANKNIFTY
            </div>
          </div>
          <button
            className="btn-sm danger"
            onClick={onDisconnect}
            disabled={disconnect.isPending}
            data-testid="fyers-disconnect"
          >
            {disconnect.isPending ? "Disconnecting…" : "Disconnect"}
          </button>
        </div>
      </div>
    );
  }

  // Not connected — show the CTA.
  const notConfigured = !status?.credentials_set;

  return (
    <div
      className="widget"
      data-testid="connect-fyers"
      style={{ borderColor: "var(--amber)", borderWidth: 1, borderStyle: "solid" }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <span style={{ color: "var(--amber)", fontSize: 20 }}>●</span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontWeight: 700, color: "var(--text)" }}>
            {notConfigured
              ? "Fyers Not Configured"
              : status?.account_present
              ? "Fyers Token Expired"
              : "Connect Fyers"}
          </div>
          <div style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 2 }}>
            {status?.reason ??
              "Click the button to authorize the bot with your Fyers account."}
          </div>
          {/* Show the running server's FYERS_APP_ID so the operator
              can verify it matches the new keys they just pasted in
              .env. The most common cause of "I changed .env but the
              OAuth still fails" is forgetting to restart the
              backend, so we make the in-use app_id visible here. */}
          {status?.app_id && (
            <div
              style={{
                fontSize: 11,
                color: "var(--text-dim)",
                marginTop: 4,
                fontFamily: "var(--mono)",
              }}
              data-testid="fyers-app-id"
            >
              FYERS_APP_ID (running): {status.app_id}
            </div>
          )}
          {errorMsg && (
            <div
              style={{
                fontSize: 12,
                color: "var(--red)",
                marginTop: 8,
                padding: "8px 10px",
                border: "1px solid var(--red)",
                borderRadius: 4,
                background: "rgba(255, 80, 80, 0.08)",
                fontFamily: "inherit",
                whiteSpace: "pre-wrap",
                lineHeight: 1.45,
              }}
              data-testid="fyers-error"
            >
              {errorMsg}
            </div>
          )}
        </div>
        <button
          className="btn-sm primary"
          onClick={onConnect}
          disabled={mode === "opening" || mode === "waiting" || notConfigured}
          data-testid="fyers-connect"
        >
          {mode === "opening"
            ? "Opening…"
            : mode === "waiting"
            ? "Waiting for Fyers…"
            : notConfigured
            ? "Set creds in .env first"
            : "Connect Fyers"}
        </button>
      </div>
    </div>
  );
}
