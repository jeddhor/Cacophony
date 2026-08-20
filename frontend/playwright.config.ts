/**
 * Layout regression tests (design document sections 45, 46, 48).
 *
 * The unit tests render components into jsdom, which has no layout: it will
 * happily report that a 216px sidebar and a 1,200px table fit inside a 700px
 * window. These run the built Studio in a real browser at real widths, and
 * assert the two things that actually break - the page scrolling sideways, and
 * a control ending up somewhere nobody can reach.
 *
 * The API is intercepted rather than served, so a layout test never depends on
 * a backend, a store or a generation run.
 */

import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  // On CI the HTML report is what gets uploaded when something fails; the
  // line reporter is what gets read in the log while it runs.
  reporter: process.env.CI ? [["line"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: "http://127.0.0.1:5174",
    trace: "on-first-retry",
  },
  projects: [
    {
      // A maximised window on a laptop: the layout everything is designed for.
      name: "desktop",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
    },
    {
      // Cacophony beside a terminal, which is how it is actually used.
      name: "half-screen",
      use: { ...devices["Desktop Chrome"], viewport: { width: 960, height: 900 } },
    },
    {
      // The narrow end of what this claims to support (section 46's navigation
      // becomes a strip here rather than a column).
      name: "narrow",
      use: { ...devices["Desktop Chrome"], viewport: { width: 700, height: 900 } },
    },
    {
      // Not a phone target - a quarter-screen window, and the narrowest thing
      // the layout claims to survive. The navigation is glyphs here.
      name: "quarter-screen",
      use: { ...devices["Desktop Chrome"], viewport: { width: 480, height: 900 } },
    },
  ],
  webServer: {
    // The dev server rather than a preview of the build: it is the same
    // application, and it does not need `npm run build` to have run first.
    command: "npm run dev -- --port 5174 --strictPort --host 127.0.0.1",
    url: "http://127.0.0.1:5174",
    reuseExistingServer: !process.env.CI,
    stdout: "ignore",
    timeout: 120_000,
  },
});
