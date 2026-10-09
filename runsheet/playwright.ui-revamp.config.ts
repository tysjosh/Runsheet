import { defineConfig, devices } from "@playwright/test";

/**
 * UI revamp e2e: chrome height, axe, visual baselines, redirects and URL tabs
 * (tasks 1.10, 1.11). Builds and serves the production bundle on port 3125;
 * every API call and socket is answered by `e2e/ui-revamp/shellFake.ts`, so
 * nothing reaches a backend or staging.
 *
 *   npx playwright test -c playwright.ui-revamp.config.ts --project=chromium
 *
 * PW_UI_DEV=1 uses the dev server instead (faster to start, slower pages).
 * The build writes `.next`; delete it afterwards.
 */
const dev = process.env.PW_UI_DEV === "1";
const PORT = 3125;

export default defineConfig({
  testDir: "./e2e/ui-revamp",
  timeout: 60_000,
  expect: {
    timeout: 10_000,
    toHaveScreenshot: { threshold: 0.2, maxDiffPixelRatio: 0.01 },
  },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  snapshotPathTemplate: "e2e/ui-revamp/__screenshots__/{arg}-{platform}{ext}",
  use: {
    baseURL: `http://localhost:${PORT}`,
    trace: "retain-on-failure",
    actionTimeout: 15_000,
    timezoneId: "America/Chicago",
    locale: "en-US",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: dev
      ? `npx next dev --turbopack -p ${PORT}`
      : `npx next build && npx next start -p ${PORT}`,
    url: `http://localhost:${PORT}/signin`,
    env: {
      NEXT_PUBLIC_API_URL: "http://localhost:8080/api",
      NEXT_PUBLIC_WS_URL: "ws://localhost:8080",
      NEXT_PUBLIC_SITE_URL: `http://localhost:${PORT}`,
      NEXT_PUBLIC_ST_API_DOMAIN: "http://localhost:8080",
      NEXT_PUBLIC_ST_WEBSITE_DOMAIN: `http://localhost:${PORT}`,
      NEXT_PUBLIC_TENANT_ID: "e2e-tenant",
    },
    reuseExistingServer: false,
    timeout: dev ? 180_000 : 600_000,
    stdout: "pipe",
    stderr: "pipe",
  },
  outputDir: "test-results/ui-revamp",
});
