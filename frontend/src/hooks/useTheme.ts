// useTheme — manages the active theme and applies it as a `data-theme`
// attribute on the <html> element so every page picks up the change
// automatically. Persists choice in localStorage.

import { useCallback, useEffect, useState } from "react";
import {
  DEFAULT_THEME,
  isThemeId,
  type ThemeId,
} from "../themes";

const STORAGE_KEY = "mavis.theme";

function readStoredTheme(): ThemeId {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (isThemeId(v)) return v;
  } catch {
    /* localStorage may be unavailable (private mode); fall through */
  }
  return DEFAULT_THEME;
}

/**
 * Apply a theme by writing `data-theme` to <html>. The "default" theme
 * is represented by the absence of the attribute so the original
 * :root tokens in App.css remain authoritative.
 */
function applyTheme(id: ThemeId): void {
  const root = document.documentElement;
  if (id === DEFAULT_THEME) {
    root.removeAttribute("data-theme");
  } else {
    root.setAttribute("data-theme", id);
  }
}

export function useTheme(): {
  theme: ThemeId;
  setTheme: (id: ThemeId) => void;
} {
  const [theme, setThemeState] = useState<ThemeId>(readStoredTheme);

  // Apply on mount + whenever theme changes.
  useEffect(() => {
    applyTheme(theme);
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      /* ignore */
    }
  }, [theme]);

  // React to theme changes from other tabs.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key !== STORAGE_KEY) return;
      const next = e.newValue;
      if (next === null) {
        setThemeState(DEFAULT_THEME);
      } else if (isThemeId(next)) {
        setThemeState(next);
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const setTheme = useCallback((id: ThemeId) => {
    setThemeState(id);
  }, []);

  return { theme, setTheme };
}
