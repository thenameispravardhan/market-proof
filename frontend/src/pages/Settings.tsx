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
          </div>
        </div>
      </div>
    </div>
  );
}
