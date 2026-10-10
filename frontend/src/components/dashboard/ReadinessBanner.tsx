// Readiness banner — the pre-market self-test, on screen.
//
// Since April 2026 a Fyers session needs a DAILY 2FA login (no refresh
// token), orders are accepted only from a type-200 app, and only from the
// whitelisted static IP. Each failure is silent until the first order is
// refused, so the 08:45 preflight checks all three and this banner shows
// the result. It also surfaces a stale or failed database backup — the
// trade/outcome log is the evidence everything else depends on.
//
// Renders nothing when every check passes: a banner that is always there
// is a banner nobody reads.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";

interface SelfTestCheck {
  name: string;
  status: "ok" | "error" | "warn" | "skip";
  detail: string;
}

interface SelfTestResponse {
  enabled: boolean;
  result: { ran_at: string; ok: boolean; problems: string[]; checks: SelfTestCheck[] } | null;
}

interface BackupResponse {
  max_age_hours: number;
  status: {
    newest_age_hours: number | null;
    offsite: string;
    local_copies: number;
  } | null;
}

export function backupProblem(b: BackupResponse | undefined): string | null {
  if (!b || !b.status || !b.max_age_hours) return null;
  const { newest_age_hours: age, offsite } = b.status;
  if (age === null) return "No database backup exists yet — install the nightly backup cron.";
  if (age > b.max_age_hours) return `Newest database backup is ${Math.round(age)}h old — the backup cron has stopped.`;
  if (offsite === "failed") return "Last offsite backup upload failed — copies only exist on the live disk.";
  return null;
}

export function ReadinessBanner() {
  const qc = useQueryClient();
  const selftest = useQuery<SelfTestResponse>({
    queryKey: ["system", "fyers-selftest"],
    queryFn: () => api.get("/api/system/fyers-selftest"),
    refetchInterval: 60_000,
  });
  const backups = useQuery<BackupResponse>({
    queryKey: ["system", "backups"],
    queryFn: () => api.get("/api/system/backups"),
    refetchInterval: 300_000,
  });
  const recheck = useMutation({
    mutationFn: () => api.post<SelfTestResponse>("/api/system/fyers-selftest/run", {}),
    onSuccess: (data) => qc.setQueryData(["system", "fyers-selftest"], data),
  });

  const failed = (selftest.data?.result?.checks ?? []).filter(
    (c) => c.status === "error" || c.status === "warn",
  );
  const backup = backupProblem(backups.data);
  if (failed.length === 0 && !backup) return null;

  const severe = failed.some((c) => c.status === "error") || backup !== null;
  return (
    <div
      className={`mode-banner ${severe ? "readiness-error" : "readiness-warn"}`}
      role="alert"
      style={{ flexDirection: "column", alignItems: "flex-start", letterSpacing: 0 }}
    >
      <div style={{ display: "flex", gap: 10, alignItems: "center", width: "100%" }}>
        <span>NOT READY TO TRADE</span>
        {selftest.data?.result && (
          <span className="mono" style={{ fontWeight: 400, opacity: 0.8 }}>
            checked {new Date(selftest.data.result.ran_at).toLocaleTimeString("en-IN")}
          </span>
        )}
        <button
          className="btn-sm"
          style={{ marginLeft: "auto" }}
          disabled={recheck.isPending}
          onClick={() => recheck.mutate()}
        >
          {recheck.isPending ? "Checking…" : "Re-check"}
        </button>
      </div>
      <ul style={{ margin: "6px 0 0 16px", padding: 0, fontWeight: 400 }}>
        {failed.map((c) => (
          <li key={c.name}>
            <strong>{c.name}</strong>: {c.detail}
          </li>
        ))}
        {backup && <li><strong>backup</strong>: {backup}</li>}
      </ul>
    </div>
  );
}
