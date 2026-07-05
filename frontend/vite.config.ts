import { defineConfig } from "vitest/config";
import { createLogger } from "vite";
import react from "@vitejs/plugin-react";

// WebSocket teardown errors are cosmetic. They fire every time a `/ws`
// sub-connection is torn down — an HMR reload, a backend restart, a
// page refresh, or a backgrounded tab — and the client reconnects on
// its own (see useWebSocket's exponential backoff). They are NOT a
// real fault, so we silence them on two fronts:
//
//   1. The PROXY instance's own `error` event (the `[proxy] …` lines).
//   2. Vite's internal logger, which logs `ws proxy error:` /
//      `ws proxy socket error:` itself — a proxy `error` handler can't
//      intercept those, so we wrap the logger below.
//
// Genuine HTTP `/api` proxy errors (e.g. a real 500) are NOT matched by
// the teardown regex, so they still surface.
const WS_TEARDOWN_RE =
  /ECONNABORTED|ECONNRESET|EPIPE|write after end|writeAfterFIN|socket hang up|ended by the other party/i;

function quietProxyErrors(proxy: any) {
  proxy.on("error", (err: Error) => {
    if (err && WS_TEARDOWN_RE.test(String(err.message))) {
      return; // cosmetic teardown — the client reconnects on its own
    }
    // eslint-disable-next-line no-console
    console.error("[proxy]", err.message);
  });
}

// Wrap Vite's logger so its own `ws proxy error` / `ws proxy socket
// error` lines (which the proxy `error` handler above cannot reach)
// are dropped. Everything else logs normally.
const quietLogger = createLogger();
const _baseError = quietLogger.error.bind(quietLogger);
quietLogger.error = (msg, opts) => {
  if (typeof msg === "string" && /ws proxy/i.test(msg)) return;
  _baseError(msg, opts);
};

export default defineConfig({
  plugins: [react() as any],
  customLogger: quietLogger,
  server: {
    port: 5173,
    proxy: {
      "/api": {
        // Bind to 127.0.0.1 explicitly. On Windows, "localhost" can
        // resolve to ::1 (IPv6) while the backend is on IPv4 only,
        // and Node's http-proxy will silently fail.
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        configure: quietProxyErrors,
      },
      "/ws": {
        target: "ws://127.0.0.1:8000",
        ws: true,
        changeOrigin: true,
        configure: quietProxyErrors,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    rollupOptions: {
      output: {
        // Split heavy vendor libs out of the main bundle so the initial
        // chunk stays small and they cache independently of app code.
        // recharts (+ d3) is the heaviest; keep it on its own.
        manualChunks: {
          react: ["react", "react-dom"],
          query: ["@tanstack/react-query"],
          charts: ["recharts"],
          lwcharts: ["lightweight-charts"],
        },
      },
    },
  },
  test: {
    // jsdom is more battle-tested with React 18's act() and concurrent
    // rendering than happy-dom — the useWebSocket tests need that.
    environment: "jsdom",
    setupFiles: ["./tests/setup.ts"],
    globals: true,
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
