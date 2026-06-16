// ThemeSwitcher — grid of theme cards for the Settings page.
// Selecting a card immediately applies the theme app-wide; the choice
// persists in localStorage. Sections, layout, and component structure
// are unchanged — only CSS variable values are swapped.

import { useTheme } from "../../hooks/useTheme";
import { THEMES, type ThemeId } from "../../themes";

export function ThemeSwitcher() {
  const { theme, setTheme } = useTheme();

  return (
    <div className="widget" data-testid="theme-switcher">
      <h3>
        Theme
        <span className="meta">UI appearance · applies to all pages</span>
      </h3>
      <p className="text-dim" style={{ fontSize: 11, marginBottom: 12 }}>
        Pick a color palette. Layout, fonts, and sections stay the same —
        only the colors change.
      </p>
      <div className="theme-grid" role="radiogroup" aria-label="Theme">
        {THEMES.map((t) => {
          const active = theme === t.id;
          return (
            <button
              key={t.id}
              type="button"
              role="radio"
              aria-checked={active}
              data-testid={`theme-${t.id}`}
              className={`theme-card${active ? " active" : ""}`}
              onClick={() => setTheme(t.id as ThemeId)}
            >
              <div
                className="theme-swatch"
                aria-hidden="true"
                style={{
                  background: `linear-gradient(135deg, ${t.swatch[1]} 0%, ${t.swatch[2]} 100%)`,
                  borderColor: active ? t.swatch[1] : "var(--border)",
                }}
              >
                {t.swatch.map((c, i) => (
                  <span key={i} style={{ background: c }} />
                ))}
              </div>
              <div className="theme-info">
                <div className="theme-name">{t.name}</div>
                <div className="theme-blurb">{t.blurb}</div>
              </div>
              {active && <span className="theme-check" aria-hidden="true">●</span>}
            </button>
          );
        })}
      </div>
    </div>
  );
}
