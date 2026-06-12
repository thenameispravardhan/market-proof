// Vitest setup: import jest-dom matchers and stub a couple of
// browser APIs that jsdom doesn't ship. We intentionally keep this
// minimal — the frontend is small and we don't need a full DOM polyfill.

import "@testing-library/jest-dom/vitest";

// jsdom doesn't implement matchMedia (used by some libraries).
if (typeof window !== "undefined" && !window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

// jsdom doesn't ship ResizeObserver — recharts' ResponsiveContainer needs
// a no-op stub so it doesn't throw during mount.
if (typeof window !== "undefined" && !("ResizeObserver" in window)) {
  class RO {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  (window as unknown as { ResizeObserver: typeof RO }).ResizeObserver = RO;
  (globalThis as unknown as { ResizeObserver: typeof RO }).ResizeObserver = RO;
}
