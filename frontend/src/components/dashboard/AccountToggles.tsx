// Dashboard account switches — turn paper / Fyers trading on or off in
// one click. Each switch flips the matching broker account's `enabled`
// flag; the execution manager blocks any signal routed to a disabled
// account, so this is a hard kill-switch per account.
//
// The FYERS switch is the LIVE-trading master switch: the backend flips
// TRADING_MODE to `live` when the real account is enabled and back to
// `paper` when disabled. Turning it ON therefore starts real-money
// trading — we demand a typed confirmation ("LIVE") before sending it.
// Turning it OFF is risk-reducing and needs no confirmation.

import { useBrokerAccounts, useToggleBrokerAccount } from "../../hooks/useApi";
import type { BrokerAccount } from "../../types";

function Switch({
  label,
  account,
  onToggle,
  pending,
  title,
}: {
  label: string;
  account: BrokerAccount | undefined;
  onToggle: (a: BrokerAccount) => void;
  pending: boolean;
  title?: string;
}) {
  const enabled = account?.enabled ?? false;
  return (
    <button
      className={`acct-switch ${enabled ? "on" : "off"}`}
      onClick={() => account && onToggle(account)}
      disabled={!account || pending}
      title={
        account
          ? title ??
            `${label} trading is ${enabled ? "ON — click to disable" : "OFF — click to enable"}`
          : `${label} account not found`
      }
      data-testid={`toggle-${label.toLowerCase()}`}
    >
      <span className="acct-switch-name">{label}</span>
      <span className="acct-switch-track">
        <span className="acct-switch-knob" />
      </span>
      <span className="acct-switch-state">{enabled ? "ON" : "OFF"}</span>
    </button>
  );
}

export function AccountToggles() {
  const { data: accounts } = useBrokerAccounts();
  const toggle = useToggleBrokerAccount();

  const paper = (accounts ?? []).find((a) => a.paper_mode);
  // Prefer a real Fyers account (has app_id); fall back to any non-paper.
  const fyers =
    (accounts ?? []).find((a) => !a.paper_mode && a.app_id) ??
    (accounts ?? []).find((a) => !a.paper_mode);

  const onTogglePaper = (a: BrokerAccount) =>
    toggle.mutate({ id: a.id, enabled: !a.enabled });

  const onToggleFyers = (a: BrokerAccount) => {
    if (!a.enabled) {
      // Enabling = LIVE trading with real money. Typed confirmation.
      const answer = window.prompt(
        "⚠ This starts LIVE trading with REAL MONEY from your Fyers " +
          "account.\n\nOrders will be placed automatically on news " +
          "signals, sized from your actual account funds.\n\n" +
          'Type "LIVE" to confirm:'
      );
      if (answer === null || answer.trim().toUpperCase() !== "LIVE") return;
    }
    toggle.mutate({ id: a.id, enabled: !a.enabled });
  };

  const fyersTitle = fyers
    ? fyers.enabled
      ? "LIVE trading is ON (real money) — click to stop and return to paper mode"
      : "Start LIVE trading with real money — asks for confirmation, flips the bot to live mode"
    : "Fyers account not found";

  return (
    <div className="acct-toggles" data-testid="account-toggles">
      <Switch label="Paper" account={paper} onToggle={onTogglePaper} pending={toggle.isPending} />
      <Switch
        label="Fyers"
        account={fyers}
        onToggle={onToggleFyers}
        pending={toggle.isPending}
        title={fyersTitle}
      />
    </div>
  );
}
