// Link a symbol to the popout chart (#/trade?popout=1&…), centred on a time.
// Timeframe comes from Settings → "Symbol-link chart timeframe".

export const CHART_LINK_IV_KEY = "chart:linkIv";

export function chartLinkIv(): string {
  try {
    return localStorage.getItem(CHART_LINK_IV_KEY) || "5";
  } catch {
    return "5";
  }
}

/** `at`: ISO time (naive = UTC, as the API sends it). */
export function chartHref(symbol: string, at?: string | null): string {
  const q = new URLSearchParams({ popout: "1", symbol, iv: chartLinkIv() });
  if (at) {
    const s = /Z$|[+-]\d{2}:?\d{2}$/.test(at.trim()) ? at.trim() : `${at.trim()}Z`;
    const t = Date.parse(s);
    if (!Number.isNaN(t)) q.set("at", String(Math.floor(t / 1000)));
  }
  return `${window.location.pathname}#/trade?${q.toString()}`;
}
