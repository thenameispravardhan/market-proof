// AllSettings — every operator knob, rendered from the backend's own schema.
//
// This component names no setting. It asks GET /api/settings/schema for the
// grouped field specs and draws whatever it is handed, so a knob declared in
// app/config.py reaches the UI with zero frontend work. That is the fix for
// the 67 settings that used to exist only in config.py, unreachable from the
// frontend despite "frontend-only control" being a design invariant.
//
// The purpose-built cards (Exits page, News Sources, Edge Memory) are better
// UX for their own keys and stay as they are; this is the complete surface
// underneath them, collapsed by default so it informs without shouting.
import { useEffect, useMemo, useState } from "react";
import { useSettingsSchema, useUpdateSettings } from "../../hooks/useApi";
import { Toggle } from "../common/Toggle";
import type { SettingsField } from "../../types";

type Values = Record<string, unknown>;

function FieldRow({
  field,
  value,
  onChange,
}: {
  field: SettingsField;
  value: unknown;
  onChange: (key: string, value: unknown) => void;
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
  const [values, setValues] = useState<Values>({});
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [filter, setFilter] = useState("");
  const [saved, setSaved] = useState(false);
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
    if (!q) return schema.groups;
    return schema.groups
      .map((g) => ({
        ...g,
        fields: g.fields.filter(
          (f) =>
            f.key.toLowerCase().includes(q) || f.label.toLowerCase().includes(q),
        ),
      }))
      .filter((g) => g.fields.length > 0);
  }, [schema, filter]);

  const handleSave = async () => {
    setError(null);
    try {
      await update.mutateAsync({ global: dirty as never });
      setSaved(true);
    } catch (e) {
      setError((e as Error).message);
    }
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

          {groups.map((group) => {
            // A search always reveals its hits; otherwise the operator decides.
            const expanded = filter.trim() !== "" || open[group.id];
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
                    <span className="field-hint">({group.fields.length})</span>
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
                      />
                    ))}
                  </>
                )}
              </div>
            );
          })}

          {error && <p className="pnl-neg">{error}</p>}
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
