// Accounts page — intentionally minimal. The bot runs on exactly two
// accounts:
//   1. Paper Trading — built in, always available, simulated fills.
//   2. Fyers — connect in one click via OAuth (keys live in .env).
// No broker-account CRUD; there is nothing for the operator to manage
// beyond connecting Fyers.

import { ConnectFyers } from "../components/accounts/ConnectFyers";
import { FyersCredentialsCard } from "../components/accounts/FyersCredentialsCard";
import { FyersSetupCard } from "../components/accounts/FyersSetupCard";
import { useGlobalSettings } from "../hooks/useApi";

function PaperAccountCard() {
  const { data } = useGlobalSettings();
  const isPaper = (data?.global?.TRADING_MODE ?? "paper") === "paper";
  return (
    <div
      className="widget"
      data-testid="paper-account"
      style={{ borderColor: "var(--green)", borderWidth: 1, borderStyle: "solid" }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <span style={{ color: "var(--green)", fontSize: 20 }}>●</span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontWeight: 700, color: "var(--text)" }}>Paper Trading</div>
          <div style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 2 }}>
            Built-in simulated account — fills at live quotes, no real money.
            Always available.
          </div>
        </div>
        <span className={`badge ${isPaper ? "green" : "neutral"}`}>
          {isPaper ? "Active" : "Standby"}
        </span>
      </div>
    </div>
  );
}

export default function Accounts() {
  return (
    <div>
      <h1 className="page-title">Accounts</h1>
      <p className="text-dim" style={{ marginBottom: 18, maxWidth: 640 }}>
        The bot trades through two accounts: a built-in <strong>paper</strong>{" "}
        account for simulation, and your <strong>Fyers</strong> account for live
        orders. Connect Fyers in one click — the API keys are read from{" "}
        <code className="mono">.env</code>.
      </p>
      <div className="layout-2">
        <PaperAccountCard />
        <ConnectFyers />
      </div>
      <div style={{ marginTop: 16 }}>
        <FyersCredentialsCard />
      </div>
      <div style={{ marginTop: 16 }}>
        <FyersSetupCard />
      </div>
      <div style={{ marginTop: 16 }}>
      </div>
    </div>
  );
}
