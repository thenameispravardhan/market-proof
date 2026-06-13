import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [
    react() as any,
    {
      // The Vite dev server logs a noisy `ws proxy socket error:
      // write ECONNABORTED` whenever the backend closes a WebSocket
      // sub-connection (e.g. on hot-reload, or if the backend
      // restarts mid-session). The error is cosmetic — the
      // browser's WS client automatically reconnects — but it
      // floods the terminal. We filter it out and log a single
      // line so the operator knows what happened.
      name: "silence-ws-proxy-noise",
      configureServer(server) {
        const orig = server.ws.on as any;
        server.ws.on("error", (err: Error) => {
          if (err && /ECONNABORTED|write after end/i.test(String(err.message))) {
            return; // drop cosmetic proxy teardown errors
          }
          // Re-emit any other error verbatim.
          // eslint-disable-next-line no-console
          console.error("[ws proxy]", err.message);
        });
      },
    },
  ],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        // Bind to 127.0.0.1 explicitly. On Windows, "localhost" can
        // resolve to ::1 (IPv6) while the backend is on IPv4 only,
        // and Node's http-proxy will silently fail.
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/ws": {
        target: "ws://127.0.0.1:8000",
        ws: true,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
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
