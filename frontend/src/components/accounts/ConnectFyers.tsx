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

// `token_expires_at` is epoch seconds (from the token's JWT `exp`).
function formatExpiry(epochS: number | null | undefined): string | null {
  if (!epochS) return null;
  const d = new Date(epochS * 1000);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleString(undefined, {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

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
  // Popup-close watcher; cleared on unmount so leaving the page mid-OAuth
  // doesn't leave an interval running.
  const watchRef = useRef<ReturnType<typeof setInterval> | null>(null);
  useEffect(() => () => {
    if (watchRef.current) clearInterval(watchRef.current);
  }, []);

  // While the popup is open, poll status every 2s so the banner
  // flips to "Connected" the instant the OAuth callback finishes.
  useEffect(() => {
    if (mode !== "opening" && mode !== "waiting") return;
    const t = setInterval(() => refetch(), 2000);
    return () => clearInterval(t);
  }, [mode, refetch]);

  // "Authorized" = OAuth done + a valid token held, regardless of the trade
  // on/off switch. This — NOT `connected` (which also requires the account
  // to be switched ON) — is what tells a successful OAuth apart from a
  // failure. A switched-OFF-but-authorised account is connected, just not
  // trading. Fall back to `connected` for older status payloads that don't
  // carry `has_token`/`authorized`.
  const isAuthorized = (s: typeof status): boolean =>
    !!(s?.connected || s?.authorized || (s?.account_present && s?.has_token));
  const authorized = isAuthorized(status);
  // Trade switch is OFF but the connection is fine.
  const tradingOff = authorized && status?.enabled === false;

  // Clear stale error messages whenever the underlying status changes
  // (e.g. user fixed the .env, or token got refreshed). Prevents the
  // banner from showing "Fyers creds not configured" on top of a
  // "Token Expired" message — they are different states.
  useEffect(() => {
    setErrorMsg(null);
  }, [status?.connected, status?.has_token, status?.credentials_set, status?.account_present]);

  // Watch for the authorised state — when it flips on (token minted), close
  // the popup and reset, even if the account is currently switched OFF.
  useEffect(() => {
    if (authorized && (mode === "opening" || mode === "waiting")) {
      setMode("idle");
      setErrorMsg(null);
    }
  }, [authorized, mode]);

  const onConnect = async () => {
    setMode("opening");
    setErrorMsg(null);
    try {
      const resp = await authorize.mutateAsync();
      if (!resp.configured) {
        setMode("error");
        setErrorMsg(
          resp.reason ??
            "Fyers App ID / Secret Key are not set. Enter them under API Credentials below."
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
          `Popup was blocked. Allow popups for ${window.location.host} in your browser, then click Connect again.`
        );
        return;
      }
      setMode("waiting");
      // Fyers redirects the popup to /api/fyers/callback on success. If it
      // closes without a token appearing, say so plainly with the usual
      // causes instead of silently resetting the banner.
      if (watchRef.current) clearInterval(watchRef.current);
      watchRef.current = setInterval(() => {
        if (popup.closed) {
          if (watchRef.current) clearInterval(watchRef.current);
          watchRef.current = null;
          // The callback's token write can race the close, so give the
          // status poll one more tick and read the latest value via ref.
          setTimeout(() => {
            if (!isAuthorized(statusRef.current)) {
              setMode("error");
              setErrorMsg(
                "OAuth did not complete. Usual causes:\n" +
                  "• The login window was closed, or a captcha / 2FA step wasn't finished.\n" +
                  "• The FYERS_APP_ID above was deleted on the Fyers dashboard. Create a new app there and enter its keys under API Credentials.\n" +
                  "• The Redirect URL registered on Fyers doesn't exactly match the one under Fyers App Activation."
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

  // Authorised (token held) — show the connected card. If the trade switch
  // is OFF we say so plainly (amber) instead of the alarming "Token Expired"
  // / "OAuth failed" state: the OAuth is fine, only trading is paused, and
  // live prices keep flowing.
  if (authorized) {
    const accent = tradingOff ? "var(--amber)" : "var(--green)";
    return (
      <div
        className="widget"
        data-testid="connect-fyers"
        style={{ borderColor: accent, borderWidth: 1, borderStyle: "solid" }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <span style={{ color: accent, fontSize: 20 }}>●</span>
          <div style={{ flex: 1 }}>
            <div style={{ fontWeight: 700, color: "var(--text)" }}>
              {tradingOff ? "Fyers Connected — Trading Off" : "Fyers Connected"}
            </div>
            <div style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 2 }}>
              {tradingOff
                ? status?.reason ??
                  "Authorised — the Fyers account is switched OFF, so no orders route to it. Live prices keep flowing. Turn it back on with the Fyers toggle on the Dashboard."
                : [
                    `App ID ${status?.app_id || "—"}`,
                    formatExpiry(status?.token_expires_at) &&
                      `session valid until ${formatExpiry(status?.token_expires_at)}`,
                  ]
                    .filter(Boolean)
                    .join(" · ")}
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

  // First load: don't flash "Not Configured" before the status arrives.
  if (!status) {
    return (
      <div className="widget" data-testid="connect-fyers">
        <div style={{ fontSize: 12, color: "var(--text-dim)" }}>Checking Fyers connection…</div>
      </div>
    );
  }

  // Not connected — show the CTA.
  const notConfigured = !status.credentials_set;

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
              : status?.token_expired
              ? "Fyers Session Expired"
              : status?.account_present
              ? "Fyers Login Needed"
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
            ? "Add keys first"
            : "Connect Fyers"}
        </button>
      </div>
    </div>
  );
}
