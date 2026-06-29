// Toggle — a small on/off switch used for enable/disable controls.
// Pure presentational: the caller owns the state and the persistence.
interface Props {
  on: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  label?: string;          // optional text shown before the switch
  title?: string;
  size?: "sm" | "md";
  "data-testid"?: string;
}

export function Toggle({
  on,
  onChange,
  disabled,
  label,
  title,
  size = "md",
  ...rest
}: Props) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      title={title ?? (on ? "On — click to turn off" : "Off — click to turn on")}
      disabled={disabled}
      onClick={(e) => {
        e.stopPropagation();
        onChange(!on);
      }}
      className={`switch${on ? " on" : ""}${size === "sm" ? " switch-sm" : ""}`}
      data-testid={rest["data-testid"]}
    >
      {label && <span className="switch-label">{label}</span>}
      <span className="switch-track">
        <span className="switch-knob" />
      </span>
      <span className="switch-state">{on ? "ON" : "OFF"}</span>
    </button>
  );
}
