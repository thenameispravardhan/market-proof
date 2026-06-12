// Prompt history: past versions of the selected template, newest
// first. Each row is a snapshot — system_prompt + user_template +
// parameters. We keep the layout terse: a compact table of version
// / changed_at / change_note. Clicking a row could be wired up to
// preview the historical prompt, but that's outside this minimal
// scope.

import { usePromptHistory } from "../../hooks/useApi";
import { ApiClientError } from "../../api/client";
import { SkeletonList } from "../dashboard/Skeleton";

export function PromptHistory({ eventType }: { eventType: string | null }) {
  const { data, isLoading, error } = usePromptHistory(eventType);

  return (
    <div className="widget" data-testid="prompt-history">
      <h3>History {eventType && <span className="meta mono" style={{ color: "var(--text-dim)" }}>{eventType}</span>}</h3>
      {!eventType ? (
        <p className="empty">Select a prompt to see its history.</p>
      ) : isLoading ? (
        <SkeletonList rows={3} />
      ) : error ? (
        <p className="empty">
          {error instanceof ApiClientError && error.status === 404
            ? "Endpoint not available yet — backend doesn't expose prompt history."
            : `Failed to load: ${error.message}`}
        </p>
      ) : !data || data.length === 0 ? (
        <p className="empty">No prior versions yet.</p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th className="mono">Ver</th>
              <th>When</th>
              <th>Note</th>
            </tr>
          </thead>
          <tbody>
            {data.map((h) => (
              <tr key={h.id}>
                <td className="mono">v{h.version}</td>
                <td className="mono" style={{ fontSize: 12 }}>
                  {new Date(h.changed_at).toLocaleString()}
                </td>
                <td>{h.change_note ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
