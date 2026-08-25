// Settings page — organised by PURPOSE, not by whatever was built last.
//
// Left column  : the trading parameters you actually tune (one grouped form).
// Right column : three clearly-labelled areas, in decreasing edit frequency —
//                  Detection & Conviction  (what the bot watches / trusts)
//                  Appearance              (theme)
//                  History & Audit         (read-only; NOT settings)
// The Rule Book moved to the Rules page, where a rules reference belongs.

import { GlobalSettings } from "../components/settings/GlobalSettings";
import { AllSettings } from "../components/settings/AllSettings";
import { AuditLog } from "../components/settings/AuditLog";
import { BreakerHistory } from "../components/settings/BreakerHistory";
import { NewsSources } from "../components/settings/NewsSources";
import { ThemeSwitcher } from "../components/settings/ThemeSwitcher";

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        textTransform: "uppercase",
        letterSpacing: "0.08em",
        fontSize: 11,
        opacity: 0.6,
        margin: "20px 0 8px",
      }}
    >
      {children}
    </div>
  );
}

export default function Settings() {
  return (
    <div>
      <h1 className="page-title">Settings</h1>
      <div className="layout-2">
        <div>
          <SectionLabel>Trading parameters</SectionLabel>
          <GlobalSettings />

          <SectionLabel>Everything else</SectionLabel>
          <AllSettings />
        </div>
        <div>
          <SectionLabel>Detection &amp; conviction</SectionLabel>
          <NewsSources />

          <SectionLabel>Appearance</SectionLabel>
          <ThemeSwitcher />

          <SectionLabel>History &amp; audit (read-only)</SectionLabel>
          <BreakerHistory />
          <div style={{ marginTop: 16 }}>
            <AuditLog />
          </div>
        </div>
      </div>
    </div>
  );
}
