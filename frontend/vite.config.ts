/// <reference types="vitest/config" />
import { fileURLToPath, URL } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The Studio is served by the Cacophony backend in production, so the dev
// server proxies the API to it rather than duplicating any of it here. That
// keeps development and production talking to exactly the same routes.
const BACKEND = process.env.CACOPHONY_API ?? "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: BACKEND, changeOrigin: true, ws: true },
    },
  },
  build: {
    // Emitted into the package so `cacophony serve` can serve it.
    outDir: "../backend/cacophony/api/static",
    emptyOutDir: true,
    sourcemap: true,
    // The graph is loaded on demand, so the initial bundle stays well under
    // the default warning threshold.
    chunkSizeWarningLimit: 400,
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    include: ["tests/**/*.test.{ts,tsx}"],
    css: false,
  },
});
