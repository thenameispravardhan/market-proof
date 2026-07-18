// News Sources — frontend control over the multi-channel detection race.
//
// Each source is an independent monitor polling its own URL; detection
// speed is the FASTEST enabled source (dedupe collapses the losers'
// copies). Toggles apply live within one poll interval — no restart.
// The tick telemetry under each toggle proves the source is actually
// polling (fetch duration + real gap between polls).

import { useEffect, useState } from "react";
import {
  useExecutionLatency,
  useGlobalSettings,
  useUpdateSettings,
} from "../../hooks/useApi";
import { Toggle } from "../common/Toggle";
import type { GlobalSettings as GS } from "../../types";

interface SourceSpec {
  key: "NSE_API_ENABLED" | "BSE_API_ENABLED" | "NSE_RSS_ENABLED";
  label: string;
  statsKey: string; // MONITOR_TICK_STATS key: "<exchange>-<channel>"
  defaultOn: boolean;
  hintOn: string;
  hintOff: string;
}

const SOURCES: SourceSpec[] = [
  {
    key: "NSE_API_ENABLED",
    label: "NSE — corporate announcements API",
    statsKey: "NSE-API",
    defaultOn: true,
    hintOn: "The primary NSE channel (full detail, authoritative symbols).",
    hintOff: "NSE filings are NOT being monitored via the API — only enabled sources detect news.",
  },
  {
    key: "BSE_API_ENABLED",
    label: "BSE — announcements API",
    statsKey: "BSE-API",
    defaultOn: true,
    hintOn: "The primary BSE channel — measured as the fastest publisher overall.",
    hintOff: "BSE filings are NOT being monitored — only enabled sources detect news.",
  },
  {
    key: "NSE_RSS_ENABLED",
    label: "NSE — RSS racer (experimental)",
    statsKey: "NSE-RSS",
    defaultOn: false,
    hintOn: "Races the NSE API: the RSS feed was measured publishing some filings EARLIER than the API. Duplicates are collapsed automatically; company names that can't be exactly matched to a symbol are skipped (the API still delivers those).",
    hintOff: "Off (default). Enable to race the NSE RSS feed against the API — detection takes whichever channel publishes first.",
  },
];

export function NewsSources() {
  const { data: settings, isLoading } = useGlobalSettings();
  const { data: lat } = useExecutionLatency(100);
  const update = useUpdateSettings();
  const [values, setValues] = useState<Partial<GS>>({});
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (settings?.global) setValues({ ...settings.global });
  }, [settings]);

  const handleSave = async () => {
    setError(null);
    const slice: Partial<GS> = {};
    for (const s of SOURCES) {
      if (values[s.key] !== undefined) (slice as Record<string, unknown>)[s.key] = values[s.key];
    }
    try {
      await update.mutateAsync({ global: slice as GS });
      setSaved(true);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="widget" data-testid="news-sources">
      <h3>News Sources — detection race</h3>
      <p className="empty">
        Every enabled source polls independently (phases staggered); the first
        one to see a filing wins and duplicates are discarded. More sources =
        faster detection, never double trades.
      </p>
      {isLoading ? (
        <p className="empty">Loading…</p>
      ) : (
        <>
          {SOURCES.map((s) => {
            const on = Boolean(values[s.key] ?? s.defaultOn);
            const t = lat?.monitor_ticks?.[s.statsKey];
            return (
              <div className="field" key={s.key}>
                <label>{s.label}</label>
                <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <Toggle
                    on={on}
                    data-testid={`source-toggle-${s.key}`}
                    onChange={(next) => {
                      setValues((prev) => ({ ...prev, [s.key]: next }));
                      setSaved(false);
                    }}
                  />
                  <span className="field-hint" style={{ marginTop: 0 }}>
                    {on ? s.hintOn : s.hintOff}
                  </span>
                </div>
                {t && (
                  <span className="field-hint mono">
                    live: polling every ~{t.last_gap_s ?? "?"}s · fetch{" "}
                    {Math.round(t.avg_tick_ms)}ms · {t.ticks_sampled} ticks sampled
                  </span>
                )}
              </div>
            );
          })}
          <p className="empty">
            Per-source detection latency is on the Timing page (&quot;Detection
            race&quot;) — judge a new source there before trusting it.
          </p>
          {error && <p className="pnl-neg">{error}</p>}
          {saved && <p className="pnl-pos">✓ Saved — applies within one poll interval</p>}
          <button className="primary" onClick={handleSave} disabled={update.isPending}>
            {update.isPending ? "Saving…" : "Save"}
          </button>
        </>
      )}
    </div>
  );
}
