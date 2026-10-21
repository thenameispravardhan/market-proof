// theme — the app-wide dark / light skin. The palette lives in App.css as
// CSS variables; this flips `data-theme` on <html>, remembers the choice and
// tells listeners (the charts re-read their colours from the variables).

export type AppTheme = "dark" | "light";

const KEY = "app:theme";
export const THEME_EVENT = "app:theme";

export function getTheme(): AppTheme {
  try {
    return localStorage.getItem(KEY) === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

export function applyTheme(t: AppTheme = getTheme()): void {
  if (typeof document === "undefined") return;
  if (t === "light") document.documentElement.dataset.theme = "light";
  else delete document.documentElement.dataset.theme;
}

export function setTheme(t: AppTheme): void {
  try {
    localStorage.setItem(KEY, t);
  } catch {
    /* best-effort */
  }
  applyTheme(t);
  window.dispatchEvent(new Event(THEME_EVENT));
}

export function toggleTheme(): AppTheme {
  const next: AppTheme = getTheme() === "light" ? "dark" : "light";
  setTheme(next);
  return next;
}
