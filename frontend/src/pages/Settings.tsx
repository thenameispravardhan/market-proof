// Settings page: global settings, trading mode toggle (requires "LIVE" confirmation),
// and filterable audit log table.
import { GlobalSettings } from "../components/settings/GlobalSettings";
import { TradingModeToggle } from "../components/settings/TradingModeToggle";
import { AuditLog } from "../components/settings/AuditLog";
import { ThemeSwitcher } from "../components/settings/ThemeSwitcher";

export default function Settings() {
  return (
    <div>
      <h1 className="page-title">Settings</h1>
      <div className="layout-2">
        <div>
          <TradingModeToggle />
          <div style={{ marginTop: 16 }}>
            <GlobalSettings />
          </div>
        </div>
        <div>
          <ThemeSwitcher />
          <div style={{ marginTop: 16 }}>
            <AuditLog />
          </div>
        </div>
      </div>
    </div>
  );
}
