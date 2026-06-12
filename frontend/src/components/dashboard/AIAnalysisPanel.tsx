// AI Analysis panel: each row is a DeepSeek run on an announcement.
// Sentiment is colour-coded; recommendation gets a buy/sell/hold badge;
// we surface the rationale in a tooltip-style line under the row.

import { useAnalyses } from "../../hooks/useApi";
import { ApiClientError } from "../../api/client";
import { SkeletonList } from "./Skeleton";

function sentimentClass(s: string | null | undefined): string {
  if (!s) return "neutral";
  if (s === "positive") return "positive";
  if (s === "negative") return "negative";
  return "neutral";
}

function actionClass(a: string | null | undefined): string {
  if (!a) return "neutral";
  if (a === "BUY") return "buy";
  if (a === "SELL") return "sell";
  if (a === "BLOCK") return "block";
  return "hold";
}

export function AIAnalysisPanel() {
  const { data, isLoading, error } = useAnalyses(15);

  return (
    <div className="widget" data-testid="ai-analysis-panel">
      <h3>AI Analysis</h3>
      {isLoading ? (
        <SkeletonList rows={4} />
      ) : error ? (
        <p className="empty">
          {error instanceof ApiClientError && error.status === 404
            ? "Endpoint not available yet — backend doesn't expose analyses."
            : `Failed to load: ${error.message}`}
        </p>
      ) : !data || data.length === 0 ? (
        <p className="empty">No analyses yet.</p>
      ) : (
        <div className="list">
          {data.map((x) => (
            <div className="item" key={x.id}>
              <div className="head">
                <span className={`badge ${sentimentClass(x.sentiment)}`}>
                  {x.sentiment ?? "n/a"}
                </span>
                {x.recommendation && (
                  <span className={`badge ${actionClass(x.recommendation)}`}>
                    {x.recommendation}
                  </span>
                )}
                {x.confidence !== null && x.confidence !== undefined && (
                  <span className="meta mono">
                    conf {(x.confidence * 100).toFixed(0)}%
                  </span>
                )}
              </div>
              {x.rationale && <div className="body">{x.rationale}</div>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
