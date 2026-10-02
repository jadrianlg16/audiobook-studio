import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end smoke + accessibility run against the app in mock mode.
 *
 *   npm run e2e                     local: builds the app with VITE_MOCK=1 into
 *                                   dist-e2e/, serves it with `vite preview` and
 *                                   drives the installed Google Chrome
 *   PW_DEV=1 npm run e2e            against the dev server instead (HMR, source
 *                                   modules; slower, and see below)
 *   PW_CHANNEL=msedge npm run e2e   another installed browser
 *   CI=1 npm run e2e                bundled Chromium (`npx playwright install
 *                                   chromium` first), no server reuse
 *   PW_PORT=5199 npm run e2e        another port for the server
 *
 * Why the production build: it is what ships, and it is a handful of requests
 * per page. The dev server serves every source module as its own request —
 * about a hundred per page — and with ten browsers in parallel on Windows some
 * of them failed with net::ERR_NO_BUFFER_SPACE, leaving a blank page that
 * looked like an app bug (2026-09-26, reproduced 5 of 48 page loads).
 *
 * Nothing here needs the studio, the gateway or the network: see
 * e2e/support.ts for the two things that are stubbed.
 */
const CI = Boolean(process.env.CI);
const HOST = "127.0.0.1";
const PORT = Number(process.env.PW_PORT || 5190);
const BASE_URL = `http://${HOST}:${PORT}`;
// Empty counts as unset, so `PW_CHANNEL=` also means "the default".
const CHANNEL = process.env.PW_CHANNEL || (CI ? undefined : "chrome");
const DEV = Boolean(process.env.PW_DEV);
const SERVE = `--host ${HOST} --port ${PORT} --strictPort`;

export default defineConfig({
  testDir: "./e2e",
  outputDir: "./test-results",
  globalTeardown: "./e2e/axe-baseline.ts",
  fullyParallel: true,
  forbidOnly: CI,
  // CI retries twice and reports a pass-on-retry as flaky; locally a failure is a failure.
  retries: CI ? 2 : 0,
  reporter: CI
    ? [["github"], ["list"], ["html", { open: "never", outputFolder: "playwright-report" }]]
    : [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], channel: CHANNEL },
    },
  ],
  webServer: {
    command: DEV
      ? `npx vite ${SERVE}`
      : `npm run build:e2e && npx vite preview --outDir dist-e2e ${SERVE}`,
    url: BASE_URL,
    env: { VITE_MOCK: "1" },
    reuseExistingServer: !CI,
    timeout: 120_000,
  },
});
