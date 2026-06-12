// BrokerAccountList — shows accounts with secrets shown as ●●●●.
import { useBrokerAccounts, useDeleteBrokerAccount, useTestBrokerAccount } from "../../hooks/useApi";
import type { BrokerAccount } from "../../types";

interface Props {
  selectedId: number | null;
  onSelect: (id: number) => void;
  onNew: () => void;
}

export function BrokerAccountList({ selectedId, onSelect, onNew }: Props) {
  const { data: accounts, isLoading } = useBrokerAccounts();
  const deleteAccount = useDeleteBrokerAccount();
  const testAccount = useTestBrokerAccount();

  const handleDelete = async (a: BrokerAccount) => {
    if (!confirm(`Delete account "${a.name}"?`)) return;
    await deleteAccount.mutateAsync(a.id);
  };

  const handleTest = async (e: React.MouseEvent, id: number) => {
    e.stopPropagation();
    const result = await testAccount.mutateAsync(id);
    alert(result.ok ? `✓ ${result.message}` : `✗ ${result.message}`);
  };

  return (
    <div className="widget" data-testid="broker-account-list">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
        <h3 style={{ margin: 0 }}>Broker Accounts</h3>
        <button className="btn-sm primary" onClick={onNew}>+ New</button>
      </div>
      {isLoading ? (
        <p className="empty">Loading…</p>
      ) : !accounts || accounts.length === 0 ? (
        <p className="empty">No broker accounts yet. Add one to start trading.</p>
      ) : (
        <div className="list">
          {accounts.map((a: BrokerAccount) => (
            <div
              key={a.id}
              className={`item${selectedId === a.id ? " selected" : ""}`}
              onClick={() => onSelect(a.id)}
            >
              <div className="head">
                <span className="symbol">{a.name}</span>
                <span className={`badge ${a.paper_mode ? "warn" : "info"}`}>
                  {a.paper_mode ? "paper" : "live"}
                </span>
              </div>
              <div className="body">
                {a.broker.toUpperCase()} · App ID: {a.app_id ?? "—"}
              </div>
              <div className="body mono" style={{ color: "var(--text-dim)", fontSize: 12 }}>
                secret: ●●●● &nbsp; token: {a.access_token ? "●●●●" : "not set"}
              </div>
              <div className="body reason" style={{ display: "flex", gap: 8, marginTop: 6 }}>
                <button
                  className="btn-sm"
                  onClick={(e) => handleTest(e, a.id)}
                  disabled={testAccount.isPending}
                >
                  Test
                </button>
                <button
                  className="btn-sm danger"
                  onClick={(e) => { e.stopPropagation(); handleDelete(a); }}
                >
                  Delete
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
