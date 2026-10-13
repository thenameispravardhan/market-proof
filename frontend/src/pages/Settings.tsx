// Settings page — organised by PURPOSE, not by whatever was built last.
//
// Left column  : the trading parameters you actually tune (one grouped form).
// Right column : in decreasing edit frequency — news sources (what the bot
//                watches), breaker history and audit log (read-only), and
//                panel layout.
// The Rule Book moved to the Rules page, where a rules reference belongs.

import { resetPanelSizes } from "../hooks/usePanelSizes";
import { AllSettings } from "../components/settings/AllSettings";
import { AuditLog } from "../components/settings/AuditLog";
import { BreakerHistory } from "../components/settings/BreakerHistory";
import { NewsSources } from "../components/settings/NewsSources";
import { CHART_LINK_IV_KEY, chartLinkIv } from "../lib/chartLink";

export default function Settings() {
  return (
    <div>
      <h1 className="page-title">Settings</h1>
      <div className="page-cols">
        <div className="page-col">
          <AllSettings />
        </div>
        <div className="page-col">
          <NewsSources />
          <BreakerHistory />
          <AuditLog />
          <div className="widget" data-testid="layout-settings">
            <h3>Layout</h3>
            <p className="text-dim" style={{ marginBottom: 10 }}>
              Drag the bottom-right corner of any panel to resize it. Sizes are
              remembered per page in this browser.
            </p>
            <button
              className="btn-sm"
              style={{ alignSelf: "flex-start" }}
              onClick={() => {
                resetPanelSizes();
                window.location.reload();
              }}
            >
              Reset panel sizes
            </button>
            <label style={{ marginTop: 14 }}>
              Symbol-link chart timeframe{" "}
              <select
                defaultValue={chartLinkIv()}
                onChange={(e) => {
                  try { localStorage.setItem(CHART_LINK_IV_KEY, e.target.value); } catch { /* storage off */ }
                }}
                data-testid="chart-link-iv"
              >
                {[["1", "1 min"], ["3", "3 min"], ["5", "5 min"], ["15", "15 min"], ["30", "30 min"], ["60", "1 hour"], ["1D", "1 day"]].map(([v, l]) => (
                  <option key={v} value={v}>{l}</option>
                ))}
              </select>
            </label>
            <p className="text-dim">Clicking a symbol (Trade History, open positions, news pipeline) opens the Trade page on it, at that time, in this timeframe.</p>
          </div>
        </div>
      </div>
    </div>
  );
}
