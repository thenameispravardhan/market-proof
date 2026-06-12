// Prompt list: GET /api/prompts → table. Click a row to select it;
// the parent (Prompts page) drives the editor/history/preview panes.

import { usePrompts } from "../../hooks/useApi";
import { ApiClientError } from "../../api/client";
import { SkeletonList } from "../dashboard/Skeleton";
import type { PromptTemplate } from "../../types";

export function PromptList({
  selected,
  onSelect,
}: {
  selected: string | null;
  onSelect: (eventType: string) => void;
}) {
  const { data, isLoading, error } = usePrompts();

  return (
    <div className="widget" data-testid="prompt-list">
      <h3>Prompts</h3>
      {isLoading ? (
        <SkeletonList rows={4} />
      ) : error ? (
        <p className="empty">
          {error instanceof ApiClientError && error.status === 404
            ? "Endpoint not available yet — backend doesn't expose /api/prompts."
            : `Failed to load: ${error.message}`}
        </p>
      ) : !data || data.length === 0 ? (
        <p className="empty">No prompt templates yet.</p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Event type</th>
              <th className="mono">Ver</th>
              <th>Model</th>
            </tr>
          </thead>
          <tbody>
            {data.map((p: PromptTemplate) => (
              <tr
                key={p.id}
                className={`row-clickable ${selected === p.event_type ? "selected" : ""}`}
                onClick={() => onSelect(p.event_type)}
                data-testid={`prompt-row-${p.event_type}`}
              >
                <td>{p.event_type}</td>
                <td className="mono">v{p.version}</td>
                <td>{p.model}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
