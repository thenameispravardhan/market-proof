// Prompt history: past versions of the selected template, newest
// first. Each row is a snapshot — system_prompt + user_template +
// parameters. Each row also has a Restore action that rolls the active
// template back to that version (the backend bumps to a new version and
// keeps the full history, so a restore is itself reversible).

import { usePromptHistory, useRestorePrompt } from "../../hooks/useApi";
import { ApiClientError } from "../../api/client";
import { SkeletonList } from "../dashboard/Skeleton";

export function PromptHistory({ eventType }: { eventType: string | null }) {
  const { data, isLoading, error } = usePromptHistory(eventType);
  const restore = useRestorePrompt(eventType);

  // The highest version in the list is the currently-active one — there's
  // nothing to roll back to, so its Restore button is disabled.
  const currentVersion = data && data.length > 0 ? data[0]!.version : null;

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
        <>
          <table className="table">
            <thead>
              <tr>
                <th className="mono">Ver</th>
                <th>When</th>
                <th>Note</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {data.map((h) => {
                const isCurrent = h.version === currentVersion;
                const pending = restore.isPending && restore.variables === h.version;
                return (
                  <tr key={h.id}>
                    <td className="mono">v{h.version}</td>
                    <td className="mono" style={{ fontSize: 12 }}>
                      {new Date(h.changed_at).toLocaleString()}
                    </td>
                    <td>{h.change_note ?? "—"}</td>
                    <td style={{ textAlign: "right" }}>
                      <button
                        className="btn-sm"
                        onClick={() => restore.mutate(h.version)}
                        disabled={isCurrent || restore.isPending}
                        title={
                          isCurrent
                            ? "This is the current version"
                            : `Make v${h.version} the active prompt`
                        }
                        data-testid={`restore-prompt-${h.version}`}
                      >
                        {pending ? "Restoring…" : isCurrent ? "Current" : "Restore"}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {restore.isError && (
            <p className="pnl-neg" style={{ fontSize: 12, marginTop: 8 }}>
              {(restore.error as Error).message}
            </p>
          )}
        </>
      )}
    </div>
  );
}
