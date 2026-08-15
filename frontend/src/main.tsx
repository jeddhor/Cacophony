/**
 * Cacophony Studio entry point (design document sections 40, 45).
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { App } from "./App";
import "./styles/theme.css";

const client = new QueryClient({
  defaultOptions: {
    queries: {
      // A local backend is fast and always reachable, so a failed request is
      // usually a real error worth showing rather than a blip worth retrying
      // three times while the user waits.
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

const container = document.getElementById("root");
if (!container) {
  throw new Error("Cacophony Studio could not find its mount point (#root).");
}

createRoot(container).render(
  <StrictMode>
    <QueryClientProvider client={client}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
