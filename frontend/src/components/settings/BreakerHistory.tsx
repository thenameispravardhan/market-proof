// Circuit-breaker history — the persistent answer to "WHY was the bot
// halted?". Every trip (daily loss, monthly drawdown, weekly throttle,
// consecutive-loser cooldown), every manual kill, and every resume is a
// risk_events row; this panel reads them back newest-first so the
// operator can decide "resume or let it cool down" with full context.

import { useBreakerHistory } from "../../hooks/useApi";

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso.endsWith("Z") ? iso : iso + "Z");
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "2-digit", month: "short",
    hour: "2-digit", minute: "2-digit",
  });
}

function shortType(t: string): string {
  return t.replace(/^BREAKER_/, "").replace(/_/g, " ");
}

export function BreakerHistory() {
  const { data, isLoading, error } = useBreakerHistory(50);

  return (
    <div className="widget" data-testid="breaker-history">
      <h3>Circuit Breaker History</h3>
      {isLoading ? (
        <p className="empty">Loading…</p>
      ) : error ? (
        <p className="empty">Failed to load: {error.message}</p>
      ) : !data || data.length === 0 ? (
        <p className="empty">No breaker trips recorded — a clean sheet.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>When (IST)</th>
              <th>Breaker</th>
              <th>Threshold</th>
              <th>Value</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            {data.map((e) => (
              <tr key={e.id} title={e.message}>
                <td className="mono">{fmtTime(e.created_at)}</td>
                <td className={`mono ${e.halted ? "pnl-neg" : ""}`}>
                  {shortType(e.event_type)}
                </td>
                <td className="mono">{e.threshold_value ?? "—"}</td>
                <td className="mono">{e.current_value ?? "—"}</td>
                <td className="mono">{e.action_taken ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
