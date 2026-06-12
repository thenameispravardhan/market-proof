// AuditLog — filterable audit log table.
import { useState } from "react";
import { useAuditLog } from "../../hooks/useApi";

function fmtTime(iso: string): string {
  return new Date(iso).toLocaleString();
}

export function AuditLog() {
  const [action, setAction] = useState("");
  const [target, setTarget] = useState("");
  const { data, isLoading } = useAuditLog({
    action: action || undefined,
    target: target || undefined,
    limit: 100,
  });

  return (
    <div className="widget" data-testid="audit-log">
      <h3>Audit Log</h3>
      <div className="field-row" style={{ marginBottom: 12 }}>
        <input
          value={action}
          onChange={(e) => setAction(e.target.value)}
          placeholder="Filter by action (e.g. settings.)"
          style={{ flex: 1 }}
        />
        <input
          value={target}
          onChange={(e) => setTarget(e.target.value)}
          placeholder="Filter by target"
          style={{ flex: 1 }}
        />
      </div>
      {isLoading ? (
        <p className="empty">Loading…</p>
      ) : !data || data.entries.length === 0 ? (
        <p className="empty">No audit log entries matching the filter.</p>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
            <thead>
              <tr style={{ color: "var(--text-dim)", borderBottom: "1px solid #30363d" }}>
                <th style={{ textAlign: "left", padding: "4px 8px" }}>Time</th>
                <th style={{ textAlign: "left", padding: "4px 8px" }}>Actor</th>
                <th style={{ textAlign: "left", padding: "4px 8px" }}>Action</th>
                <th style={{ textAlign: "left", padding: "4px 8px" }}>Target</th>
              </tr>
            </thead>
            <tbody>
              {data.entries.map((e) => (
                <tr key={e.id} style={{ borderBottom: "1px solid #21262d" }}>
                  <td style={{ padding: "4px 8px", color: "var(--text-dim)" }}>{fmtTime(e.created_at)}</td>
                  <td style={{ padding: "4px 8px" }}>
                    <span className="badge info">{e.actor}</span>
                  </td>
                  <td style={{ padding: "4px 8px" }} className="mono">{e.action}</td>
                  <td style={{ padding: "4px 8px" }} className="mono">{e.target ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
