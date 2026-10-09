// Fyers / DeepSeek API credentials — editable entirely from the UI.
//
// The recurring pain was that Fyers keys lived in .env and every change
// (new app, rotated secret) meant editing the file by hand and restarting
// the backend. This card lets the operator manage App ID, Secret Key and
// Redirect URI from the browser: the backend writes them to .env (still
// the durable source of truth) AND hot-applies them to the running
// process, so OAuth / order placement pick up the new values immediately.
//
// Secrets are write-only: the current secret is never sent to the browser
// (only a masked preview + a "set" flag), and an empty secret field means
// "leave the existing secret unchanged".

import { useEffect, useState } from "react";
import { useFyersCredentials, useUpdateCredentials } from "../../hooks/useApi";

export function FyersCredentialsCard() {
  const { data: cred } = useFyersCredentials();
  const update = useUpdateCredentials();

  const [appId, setAppId] = useState("");
  const [secret, setSecret] = useState("");
  const [redirect, setRedirect] = useState("");
  const [deepseek, setDeepseek] = useState("");
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  // Seed the non-secret fields from the server once loaded.
  useEffect(() => {
    if (cred) {
      setAppId(cred.fyers_app_id || "");
      setRedirect(cred.fyers_redirect_uri || "");
    }
  }, [cred?.fyers_app_id, cred?.fyers_redirect_uri]);

  const save = async () => {
    setMsg(null);
    // Send only what actually changed. The App ID / Redirect fields are
    // pre-filled from the server, so sending them unconditionally made a
    // DeepSeek-only save rewrite the Fyers keys, drop the cached Fyers
    // session and tell the operator to re-authorise for nothing.
    const body: Record<string, string> = {};
    if (appId.trim() && appId.trim() !== (cred?.fyers_app_id ?? "")) body.fyers_app_id = appId.trim();
    if (secret.trim()) body.fyers_secret_key = secret.trim();
    if (redirect.trim() && redirect.trim() !== (cred?.fyers_redirect_uri ?? ""))
      body.fyers_redirect_uri = redirect.trim();
    if (deepseek.trim()) body.deepseek_api_key = deepseek.trim();
    if (Object.keys(body).length === 0) {
      setMsg({ kind: "err", text: "Nothing changed — edit a field first." });
      return;
    }
    const fyersChanged = "fyers_app_id" in body || "fyers_secret_key" in body;
    try {
      await update.mutateAsync(body);
      setSecret("");
      setDeepseek("");
      setMsg({
        kind: "ok",
        text: fyersChanged
          ? "Saved and applied. The Fyers keys changed, so click Connect Fyers at the top to log in with them."
          : "Saved and applied.",
      });
    } catch (e: any) {
      setMsg({ kind: "err", text: `Save failed: ${e?.message ?? "unknown error"}` });
    }
  };

  return (
    <div className="widget" data-testid="fyers-credentials">
      <h3>
        API Credentials
        <span className="badge neutral">.env</span>
      </h3>
      <p className="text-dim" style={{ fontSize: 12, marginBottom: 12 }}>
        Manage your Fyers (and DeepSeek) keys here — saved to{" "}
        <code className="mono">.env</code> and applied live, no restart. Secrets
        are write-only; leave a secret blank to keep the current one.
      </p>

      <div className="field">
        <label>Fyers App ID</label>
        <input
          className="mono"
          placeholder="e.g. LUVWQUOVWJ-200"
          value={appId}
          onChange={(e) => setAppId(e.target.value)}
          data-testid="cred-app-id"
        />
      </div>

      <div className="field">
        <label>
          Fyers Secret Key{" "}
          {cred?.fyers_secret_set && (
            <span className="text-dim" style={{ fontWeight: 400 }}>
              — current: {cred.fyers_secret_masked}
            </span>
          )}
        </label>
        <input
          className="mono"
          type="password"
          placeholder={cred?.fyers_secret_set ? "•••• (leave blank to keep)" : "paste secret"}
          value={secret}
          onChange={(e) => setSecret(e.target.value)}
          data-testid="cred-secret"
          autoComplete="off"
        />
      </div>

      <div className="field">
        <label>Fyers Redirect URI</label>
        <input
          className="mono"
          placeholder="http://localhost:8000/api/fyers/callback"
          value={redirect}
          onChange={(e) => setRedirect(e.target.value)}
          data-testid="cred-redirect"
        />
      </div>

      <div className="field">
        <label>
          DeepSeek API Key{" "}
          {cred?.deepseek_key_set && (
            <span className="text-dim" style={{ fontWeight: 400 }}>
              — current: {cred.deepseek_key_masked}
            </span>
          )}
        </label>
        <input
          className="mono"
          type="password"
          placeholder={cred?.deepseek_key_set ? "•••• (leave blank to keep)" : "sk-…"}
          value={deepseek}
          onChange={(e) => setDeepseek(e.target.value)}
          data-testid="cred-deepseek"
          autoComplete="off"
        />
      </div>

      <button
        className="btn-sm primary"
        onClick={save}
        disabled={update.isPending}
        data-testid="cred-save"
      >
        {update.isPending ? "Saving…" : "Save credentials"}
      </button>

      {msg && (
        <p
          style={{
            fontSize: 12,
            marginTop: 10,
            color: msg.kind === "ok" ? "var(--green)" : "var(--red)",
            lineHeight: 1.45,
          }}
          data-testid="cred-msg"
        >
          {msg.text}
        </p>
      )}
    </div>
  );
}
