// RuleDryRun — test a rule against a synthetic analysis dict.
import { useState } from "react";
import { useDryRunRule } from "../../hooks/useApi";

const DEFAULT_ANALYSIS = {
  sentiment: "positive",
  sentiment_score: 75,
  confidence: 0.8,
  recommendation: "BUY",
};

interface Props {
  strategyId: number | null;
}

export function RuleDryRun({ strategyId }: Props) {
  const [json, setJson] = useState(JSON.stringify(DEFAULT_ANALYSIS, null, 2));
  const [parseError, setParseError] = useState<string | null>(null);
  const dryRun = useDryRunRule();

  const handleRun = async () => {
    setParseError(null);
    let analysis: Record<string, unknown>;
    try {
      analysis = JSON.parse(json);
    } catch (e) {
      setParseError(`Invalid JSON: ${(e as Error).message}`);
      return;
    }
    await dryRun.mutateAsync({ analysis, strategy_id: strategyId ?? undefined });
  };

  return (
    <div className="widget" data-testid="rule-dry-run">
      <div className="widget-header">
        <h3>Dry run</h3>
      </div>
      <div className="widget-body">
        <p className="field-hint" style={{ marginBottom: 8 }}>
          Paste a sample analysis to see which rule would fire. Supported
          fields: sentiment, sentiment_score, confidence, recommendation.
        </p>
        <div className="field">
          <label htmlFor="dry-run-json">Analysis JSON</label>
          <textarea
            id="dry-run-json"
            rows={7}
            className="mono"
            value={json}
            onChange={(e) => setJson(e.target.value)}
          />
        </div>
        {parseError && <p className="pnl-neg">{parseError}</p>}
        <button
          className="primary save-rule-btn"
          onClick={handleRun}
          disabled={dryRun.isPending}
          data-testid="dry-run-btn"
        >
          {dryRun.isPending ? "Running…" : "Run dry-run"}
        </button>

        {dryRun.data && (
          <div style={{ marginTop: 14 }}>
            {dryRun.data.matched ? (
              <div>
                <p className="pnl-pos">
                  ✓ Matched rule #{dryRun.data.matched_rule?.id} — "{dryRun.data.matched_rule?.name}"
                </p>
                <p>
                  <strong>Action:</strong>{" "}
                  <span className={`badge ${String(dryRun.data.action).toLowerCase()}`}>
                    {dryRun.data.action}
                  </span>
                </p>
                {dryRun.data.action_params && (
                  <pre className="mono" style={{ fontSize: 11 }}>
                    {JSON.stringify(dryRun.data.action_params, null, 2)}
                  </pre>
                )}
              </div>
            ) : (
              <p className="pnl-neg">✗ No rule matched → default HOLD</p>
            )}
          </div>
        )}
        {dryRun.isError && (
          <p className="pnl-neg">{(dryRun.error as Error).message}</p>
        )}
      </div>
    </div>
  );
}
