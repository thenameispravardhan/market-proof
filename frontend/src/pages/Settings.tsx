// Settings page: global settings, breaker history, and the audit log.
import { GlobalSettings } from "../components/settings/GlobalSettings";
import { AuditLog } from "../components/settings/AuditLog";
import { BreakerHistory } from "../components/settings/BreakerHistory";
import { EdgeMemory } from "../components/settings/EdgeMemory";
import { NewsSources } from "../components/settings/NewsSources";
import { RuleBook } from "../components/settings/RuleBook";
import { ThemeSwitcher } from "../components/settings/ThemeSwitcher";

export default function Settings() {
  return (
    <div>
      <h1 className="page-title">Settings</h1>
      <div className="layout-2">
        <div>
          <GlobalSettings />
        </div>
        <div>
          <ThemeSwitcher />
          <div style={{ marginTop: 16 }}>
            <NewsSources />
          </div>
          <div style={{ marginTop: 16 }}>
            <EdgeMemory />
          </div>
          <div style={{ marginTop: 16 }}>
            <BreakerHistory />
          </div>
          <div style={{ marginTop: 16 }}>
            <RuleBook />
          </div>
          <div style={{ marginTop: 16 }}>
            <AuditLog />
          </div>
        </div>
      </div>
    </div>
  );
}
