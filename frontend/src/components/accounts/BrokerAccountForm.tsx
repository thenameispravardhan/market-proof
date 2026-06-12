// BrokerAccountForm — create or edit a broker account.
// Secrets are write-only: existing values shown as ●●●●,
// leave blank to keep the current value.
import { useEffect, useState } from "react";
import { useBrokerAccounts, useCreateBrokerAccount, useUpdateBrokerAccount } from "../../hooks/useApi";

interface Props {
  accountId: number | null;
  onSaved?: () => void;
}

export function BrokerAccountForm({ accountId, onSaved }: Props) {
  const { data: accounts } = useBrokerAccounts();
  const account = accounts?.find((a) => a.id === accountId) ?? null;
  const create = useCreateBrokerAccount();
  const update = useUpdateBrokerAccount(accountId);

  const [name, setName] = useState("");
  const [broker, setBroker] = useState("fyers");
  const [appId, setAppId] = useState("");
  const [secretKey, setSecretKey] = useState("");
  const [accessToken, setAccessToken] = useState("");
  const [redirectUri, setRedirectUri] = useState("http://localhost:8000/api/fyers/callback");
  const [paperMode, setPaperMode] = useState(true);
  const [enabled, setEnabled] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (account) {
      setName(account.name);
      setBroker(account.broker);
      setAppId(account.app_id ?? "");
      setSecretKey(""); // never pre-fill
      setAccessToken(""); // never pre-fill
      setRedirectUri(account.redirect_uri ?? "http://localhost:8000/api/fyers/callback");
      setPaperMode(account.paper_mode);
      setEnabled(account.enabled);
    } else {
      setName("");
      setBroker("fyers");
      setAppId("");
      setSecretKey("");
      setAccessToken("");
      setRedirectUri("http://localhost:8000/api/fyers/callback");
      setPaperMode(true);
      setEnabled(true);
    }
    setError(null);
  }, [account]);

  const handleSave = async () => {
    setError(null);
    const body: Record<string, unknown> = {
      name,
      broker,
      app_id: appId || null,
      redirect_uri: redirectUri || null,
      paper_mode: paperMode,
      enabled,
    };
    // Only include secrets if the operator actually typed something.
    if (secretKey) body.secret_key = secretKey;
    if (accessToken) body.access_token = accessToken;
    try {
      if (account) {
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
    <div className="widget" data-testid="broker-account-form">
      <h3>{account ? `Edit — ${account.name}` : "New Broker Account"}</h3>
      <div className="field">
        <label htmlFor="ba-name">Account name</label>
        <input id="ba-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Primary" />
      </div>
      <div className="field">
        <label htmlFor="ba-broker">Broker</label>
        <input id="ba-broker" value={broker} onChange={(e) => setBroker(e.target.value)} />
      </div>
      <div className="field">
        <label htmlFor="ba-app-id">App ID</label>
        <input id="ba-app-id" value={appId} onChange={(e) => setAppId(e.target.value)} />
      </div>
      <div className="field">
        <label htmlFor="ba-secret">
          Secret key {account ? "(leave blank to keep current)" : ""}
        </label>
        <input
          id="ba-secret"
          type="password"
          value={secretKey}
          onChange={(e) => setSecretKey(e.target.value)}
          placeholder={account ? "●●●●" : "enter secret key"}
          autoComplete="new-password"
        />
      </div>
      <div className="field">
        <label htmlFor="ba-token">
          Access token {account ? "(leave blank to keep current)" : ""}
        </label>
        <input
          id="ba-token"
          type="password"
          value={accessToken}
          onChange={(e) => setAccessToken(e.target.value)}
          placeholder={account ? "●●●●" : "enter access token"}
          autoComplete="new-password"
        />
      </div>
      <div className="field">
        <label htmlFor="ba-redirect">Redirect URI</label>
        <input id="ba-redirect" value={redirectUri} onChange={(e) => setRedirectUri(e.target.value)} />
      </div>
      <div className="field-row">
        <label>
          <input type="checkbox" checked={paperMode} onChange={(e) => setPaperMode(e.target.checked)} />
          {" "}Paper mode
        </label>
        <label>
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          {" "}Enabled
        </label>
      </div>
      {error && <p className="pnl-neg">{error}</p>}
      <button
        className="primary"
        onClick={handleSave}
        disabled={create.isPending || update.isPending || !name}
        data-testid="save-account"
      >
        {create.isPending || update.isPending ? "Saving…" : account ? "Update" : "Create"}
      </button>
    </div>
  );
}
