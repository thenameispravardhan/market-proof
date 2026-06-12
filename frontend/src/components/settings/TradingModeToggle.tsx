// TradingModeToggle — switches between paper and live mode.
// Switching to LIVE requires the user to type "LIVE" as confirmation.
import { useState } from "react";
import { useGlobalSettings, useSetTradingMode } from "../../hooks/useApi";

export function TradingModeToggle() {
  const { data: settings } = useGlobalSettings();
  const setMode = useSetTradingMode();
  const [confirmText, setConfirmText] = useState("");
  const [showConfirm, setShowConfirm] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const current = settings?.global?.TRADING_MODE ?? "paper";

  const handleLiveRequest = () => {
    if (current === "live") return;
    setShowConfirm(true);
    setConfirmText("");
    setError(null);
  };

  const handleConfirm = async () => {
    if (confirmText !== "LIVE") {
      setError('Type "LIVE" exactly to confirm.');
      return;
    }
    try {
      await setMode.mutateAsync({ mode: "live", confirm: true });
      setShowConfirm(false);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const handlePaper = async () => {
    if (current === "paper") return;
    try {
      await setMode.mutateAsync({ mode: "paper", confirm: true });
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="widget" data-testid="trading-mode-toggle">
      <h3>Trading Mode</h3>
      <div style={{ display: "flex", gap: 12, alignItems: "center", marginBottom: 16 }}>
        <button
          className={current === "paper" ? "primary active" : ""}
          onClick={handlePaper}
          disabled={current === "paper" || setMode.isPending}
          data-testid="paper-mode-btn"
        >
          📄 Paper
        </button>
        <button
          className={current === "live" ? "primary active live" : "danger"}
          onClick={handleLiveRequest}
          disabled={current === "live" || setMode.isPending}
          data-testid="live-mode-btn"
        >
          ⚡ LIVE
        </button>
        <span style={{ color: "var(--text-dim)", fontSize: 12 }}>
          Current: <strong className={current === "live" ? "pnl-neg" : "pnl-pos"}>{current.toUpperCase()}</strong>
        </span>
      </div>

      {showConfirm && current !== "live" && (
        <div style={{ background: "#2d1c1c", border: "1px solid #f85149", padding: 16, borderRadius: 8 }}>
          <p style={{ color: "#f85149", marginBottom: 8, fontWeight: 600 }}>
            ⚠ Switching to LIVE mode will place REAL orders with REAL money.
          </p>
          <p style={{ fontSize: 13, marginBottom: 8 }}>
            Type <code className="mono">LIVE</code> to confirm:
          </p>
          <div style={{ display: "flex", gap: 8 }}>
            <input
              autoFocus
              value={confirmText}
              onChange={(e) => setConfirmText(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleConfirm()}
              placeholder="LIVE"
              style={{ width: 120 }}
              data-testid="live-confirm-input"
            />
            <button className="danger" onClick={handleConfirm} disabled={setMode.isPending}>
              Confirm LIVE
            </button>
            <button onClick={() => setShowConfirm(false)}>Cancel</button>
          </div>
          {error && <p className="pnl-neg" style={{ marginTop: 8 }}>{error}</p>}
        </div>
      )}
      {!showConfirm && error && <p className="pnl-neg">{error}</p>}
    </div>
  );
}
