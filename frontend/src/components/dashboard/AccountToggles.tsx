// Dashboard account switches — turn paper / Fyers trading on or off in
// one click. Each switch flips the matching broker account's `enabled`
// flag; the execution manager blocks any signal routed to a disabled
// account, so this is a hard kill-switch per account.

import { useBrokerAccounts, useToggleBrokerAccount } from "../../hooks/useApi";
import type { BrokerAccount } from "../../types";

function Switch({
  label,
  account,
  onToggle,
  pending,
}: {
  label: string;
  account: BrokerAccount | undefined;
  onToggle: (a: BrokerAccount) => void;
  pending: boolean;
}) {
  const enabled = account?.enabled ?? false;
  return (
    <button
      className={`acct-switch ${enabled ? "on" : "off"}`}
      onClick={() => account && onToggle(account)}
      disabled={!account || pending}
      title={
        account
          ? `${label} trading is ${enabled ? "ON — click to disable" : "OFF — click to enable"}`
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

  const onToggle = (a: BrokerAccount) =>
    toggle.mutate({ id: a.id, enabled: !a.enabled });

  return (
    <div className="acct-toggles" data-testid="account-toggles">
      <Switch label="Paper" account={paper} onToggle={onToggle} pending={toggle.isPending} />
      <Switch label="Fyers" account={fyers} onToggle={onToggle} pending={toggle.isPending} />
    </div>
  );
}
