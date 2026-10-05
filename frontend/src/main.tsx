import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import "./App.css";
import { applyTheme } from "./lib/theme";
import { initWorkspace } from "./workspace";

applyTheme(); // before the first paint, so a light skin doesn't flash dark

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      refetchIntervalInBackground: false,
      staleTime: 10_000,
    },
  },
});

const root = document.getElementById("root");
if (!root) {
  throw new Error("#root not found in index.html");
}

// This tab's workspace id (URL ?ws=) must be settled before any state loads.
void initWorkspace().then(() => ReactDOM.createRoot(root).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>
));
