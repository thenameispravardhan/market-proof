// AllSettings — every operator knob, rendered from the backend's own schema.
//
// This component names no setting. It asks GET /api/settings/schema for the
// grouped field specs and draws whatever it is handed, so a knob declared in
// app/config.py reaches the UI with zero frontend work. That is the fix for
// the 67 settings that used to exist only in config.py, unreachable from the
// frontend despite "frontend-only control" being a design invariant.
//
// The purpose-built cards (Exits page, News Sources) are better
// UX for their own keys and stay as they are; this is the complete surface
// underneath them, collapsed by default so it informs without shouting.
import { useEffect, useMemo, useRef, useState } from "react";
import {
  useImportSettings,
  useResetSetting,
  useResetSettings,
  useSettingsSchema,
  useUpdateSettings,
} from "../../hooks/useApi";
import { Toggle } from "../common/Toggle";
import type { SettingsField } from "../../types";

type Values = Record<string, unknown>;

function FieldRow({
  field,
  value,
  onChange,
  onReset,
}: {
  field: SettingsField;
  value: unknown;
  onChange: (key: string, value: unknown) => void;
  onReset: (key: string) => void;
}) {
  const id = `set-${field.key}`;
  const disabled = field.read_only;

  return (
    <div className="field" key={field.key}>
      <label htmlFor={id}>
        {field.label}
        {field.restart_required && (
          <span className="field-hint" style={{ marginLeft: 6 }}>
            (restart)
          </span>
        )}
        {field.overridden && !disabled && (
          <>
            <span
              className="field-hint"
              style={{ marginLeft: 6 }}
              title={`Default: ${String(field.default)}`}
            >
              · changed from {String(field.default)}
            </span>
            <button
              type="button"
              onClick={() => onReset(field.key)}
              data-testid={`reset-${field.key}`}
              title="Reset this setting to its default"
              style={{
                marginLeft: 6,
                background: "none",
                border: "none",
                padding: 0,
                cursor: "pointer",
                fontSize: 11,
                textDecoration: "underline",
                opacity: 0.75,
              }}
            >
              reset
            </button>
          </>
        )}
      </label>

      {field.widget === "toggle" ? (
        <Toggle
          on={Boolean(value)}
          data-testid={`${id}-toggle`}
          onChange={(next) => !disabled && onChange(field.key, next)}
        />
      ) : field.widget === "select" ? (
        <select
          id={id}
          disabled={disabled}
          value={String(value ?? "")}
          onChange={(e) => onChange(field.key, e.target.value)}
        >
          {(field.choices ?? []).map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      ) : field.widget === "time" ? (
        <input
          id={id}
          type="time"
          disabled={disabled}
          value={String(value ?? "")}
          onChange={(e) => onChange(field.key, e.target.value)}
        />
      ) : field.widget === "number" ? (
        <input
          id={id}
          type="number"
          disabled={disabled}
          min={field.min ?? undefined}
          max={field.max ?? undefined}
          step={field.step ?? undefined}
          value={value === null || value === undefined ? "" : String(value)}
          onChange={(e) =>
            onChange(field.key, e.target.value === "" ? "" : Number(e.target.value))
          }
        />
      ) : (
        <input
          id={id}
          type="text"
          disabled={disabled}
          value={String(value ?? "")}
          onChange={(e) => onChange(field.key, e.target.value)}
        />
      )}

      {disabled && (
        <span className="field-hint">
          Changed from its own control — see the Dashboard.
        </span>
      )}
    </div>
  );
}

export function AllSettings() {
  const { data: schema, isLoading } = useSettingsSchema();
  const update = useUpdateSettings();
  const resetOne = useResetSetting();
  const resetMany = useResetSettings();
  const importSettings = useImportSettings();
  const fileInput = useRef<HTMLInputElement>(null);
  const [values, setValues] = useState<Values>({});
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [filter, setFilter] = useState("");
  const [onlyModified, setOnlyModified] = useState(false);
  const [saved, setSaved] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!schema) return;
    const next: Values = {};
    for (const group of schema.groups) {
      for (const f of group.fields) next[f.key] = f.value;
    }
    setValues(next);
  }, [schema]);

  const handleChange = (key: string, value: unknown) => {
    setValues((prev) => ({ ...prev, [key]: value }));
    setSaved(false);
  };

  // Only send what the operator actually changed. A blanket save would rewrite
  // every key as an explicit override, freezing them against future default
  // changes in config.py.
  const dirty = useMemo(() => {
    if (!schema) return {} as Values;
    const out: Values = {};
    for (const group of schema.groups) {
      for (const f of group.fields) {
        if (f.read_only) continue;
        const v = values[f.key];
        if (v !== undefined && v !== "" && v !== f.value) out[f.key] = v;
      }
    }
    return out;
  }, [schema, values]);

  const dirtyCount = Object.keys(dirty).length;

  const groups = useMemo(() => {
    if (!schema) return [];
    const q = filter.trim().toLowerCase();
    if (!q && !onlyModified) return schema.groups;
    return schema.groups
      .map((g) => ({
        ...g,
        fields: g.fields.filter(
          (f) =>
            (!onlyModified || f.overridden) &&
            (!q ||
              f.key.toLowerCase().includes(q) ||
              f.label.toLowerCase().includes(q)),
        ),
      }))
      .filter((g) => g.fields.length > 0);
  }, [schema, filter, onlyModified]);

  const run = async (fn: () => Promise<unknown>, message?: string) => {
    setError(null);
    setNote(null);
    try {
      await fn();
      if (message) setNote(message);
      return true;
    } catch (e) {
      setError((e as Error).message);
      return false;
    }
  };

  const handleSave = () =>
    run(async () => {
      await update.mutateAsync({ global: dirty as never });
      setSaved(true);
    });

  const handleResetOne = (key: string) =>
    run(() => resetOne.mutateAsync(key), `${key} reset to its default.`);

  const handleResetGroup = (group: string, title: string) => {
    if (!confirm(`Reset every changed setting in "${title}" to its default?`)) return;
    void run(
      () => resetMany.mutateAsync({ group, confirm: true }),
      `${title} reset to defaults.`,
    );
  };

  const handleResetAll = () => {
    if (
      !confirm(
        `Reset all ${schema?.overridden_count ?? 0} changed settings to their defaults?\n\n` +
          "This discards your tuning. Export first if you might want it back.",
      )
    )
      return;
    void run(
      () => resetMany.mutateAsync({ confirm: true }),
      "All settings reset to defaults.",
    );
  };

  // Export hits the endpoint directly rather than reconstructing the file from
  // the schema: the server decides what an override is, and only overrides
  // travel (exporting effective values would pin every key on import).
  const handleExport = () =>
    run(async () => {
      const res = await fetch("/api/settings/export", { credentials: "same-origin" });
      if (!res.ok) throw new Error(`export failed: ${res.status}`);
      const blob = new Blob([JSON.stringify(await res.json(), null, 2)], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `tradebot-settings-${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      URL.revokeObjectURL(url);
    }, "Settings exported.");

  const handleImportFile = async (file: File) => {
    await run(async () => {
      const parsed = JSON.parse(await file.text());
      const overrides = parsed.overrides ?? parsed;
      if (typeof overrides !== "object" || overrides === null) {
        throw new Error("not a settings export — expected an 'overrides' object");
      }
      const r = await importSettings.mutateAsync({ overrides });
      setNote(
        `Imported ${r.imported.length} setting${r.imported.length === 1 ? "" : "s"}` +
          (r.ignored.length ? ` · ignored ${r.ignored.length} unknown` : ""),
      );
    });
    if (fileInput.current) fileInput.current.value = "";
  };

  const total = schema?.groups.reduce((n, g) => n + g.fields.length, 0) ?? 0;

  return (
    <div className="widget" data-testid="all-settings">
      <h3>All Settings</h3>
      <p className="empty">
        Every operator control in one place — {total} in total, grouped by what
        they affect. The cards above are friendlier editors for the same values.
      </p>

      {isLoading ? (
        <p className="empty">Loading…</p>
      ) : (
        <>
          <div className="field">
            <label htmlFor="settings-filter">Find a setting</label>
            <input
              id="settings-filter"
              type="search"
              placeholder="e.g. stop, sector, decay"
              value={filter}
              data-testid="settings-filter"
              onChange={(e) => setFilter(e.target.value)}
            />
          </div>

          <div
            style={{
              display: "flex",
              flexWrap: "wrap",
              gap: 8,
              alignItems: "center",
              margin: "10px 0 4px",
            }}
          >
            <button
              type="button"
              onClick={() => setOnlyModified((v) => !v)}
              data-testid="toggle-only-modified"
              aria-pressed={onlyModified}
            >
              {onlyModified ? "Showing changed only" : "Show changed only"} (
              {schema?.overridden_count ?? 0})
            </button>
            <button type="button" onClick={handleExport} data-testid="export-settings">
              Export
            </button>
            <button
              type="button"
              onClick={() => fileInput.current?.click()}
              data-testid="import-settings"
            >
              Import
            </button>
            <input
              ref={fileInput}
              type="file"
              accept="application/json,.json"
              style={{ display: "none" }}
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) void handleImportFile(f);
              }}
            />
            <button
              type="button"
              onClick={handleResetAll}
              disabled={(schema?.overridden_count ?? 0) === 0}
              data-testid="reset-all-settings"
              title="Drop every override and fall back to the shipped defaults"
            >
              Reset all
            </button>
          </div>

          <span className="field-hint" style={{ display: "block", marginBottom: 8 }}>
            Export writes only the values you changed, so importing it on
            another machine — or on the server — will not pin everything else to
            today's defaults.
          </span>

          {groups.map((group) => {
            // A search always reveals its hits; otherwise the operator decides.
            const expanded = filter.trim() !== "" || onlyModified || open[group.id];
            const changedHere = group.fields.filter((f) => f.overridden).length;
            return (
              <div key={group.id} data-testid={`settings-group-${group.id}`}>
                <button
                  type="button"
                  className="settings-group-title"
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    width: "100%",
                    background: "none",
                    border: "none",
                    padding: "8px 0",
                    cursor: "pointer",
                    textAlign: "left",
                  }}
                  aria-expanded={expanded}
                  onClick={() =>
                    setOpen((prev) => ({ ...prev, [group.id]: !prev[group.id] }))
                  }
                >
                  <span>
                    {group.title}{" "}
                    <span className="field-hint">
                      ({group.fields.length}
                      {changedHere > 0 && `, ${changedHere} changed`})
                    </span>
                  </span>
                  <span aria-hidden>{expanded ? "−" : "+"}</span>
                </button>

                {expanded && (
                  <>
                    {group.note && (
                      <span
                        className="field-hint"
                        style={{ display: "block", marginBottom: 6 }}
                      >
                        {group.note}
                      </span>
                    )}
                    {group.fields.map((f) => (
                      <FieldRow
                        key={f.key}
                        field={f}
                        value={values[f.key]}
                        onChange={handleChange}
                        onReset={handleResetOne}
                      />
                    ))}
                    {changedHere > 0 && (
                      <button
                        type="button"
                        onClick={() => handleResetGroup(group.id, group.title)}
                        data-testid={`reset-group-${group.id}`}
                        style={{ marginTop: 4 }}
                      >
                        Reset {group.title} to defaults
                      </button>
                    )}
                  </>
                )}
              </div>
            );
          })}

          {error && <p className="pnl-neg">{error}</p>}
          {note && <p className="pnl-pos">{note}</p>}
          {saved && <p className="pnl-pos">✓ Saved</p>}
          <button
            className="primary"
            onClick={handleSave}
            disabled={update.isPending || dirtyCount === 0}
            data-testid="save-all-settings"
          >
            {update.isPending
              ? "Saving…"
              : dirtyCount === 0
                ? "No changes"
                : `Save ${dirtyCount} change${dirtyCount === 1 ? "" : "s"}`}
          </button>
        </>
      )}
    </div>
  );
}
