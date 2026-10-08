import { defineConfig, devices } from "@playwright/test";

/**
 * Customer-portal fixture specs (UI revamp Phase 3P, design §11.8, §11.11):
 * chrome budget, visual baselines at 390×844 and 1280×800, axe, and the
 * sign-in keyboard order. Builds and serves the production bundle on port
 * 3126; `e2e/portal/fixtures/portalFake.ts` answers every portal GET and
 * refuses every write, so nothing reaches a backend or staging.
 *
 *   npx playwright test -c playwright.portal.config.ts --project=chromium
 *
 * The staging specs in `e2e/portal` (a11y, reflow, keyboard-flow) keep
 * running under the default config with PLAYWRIGHT_BASE_URL and a QA login.
 * PW_UI_DEV=1 uses the dev server instead. The build writes `.next`; delete
 * it afterwards.
 */
const dev = process.env.PW_UI_DEV === "1";
const PORT = 3126;

// The fixture specs skip unless this config loaded them.
process.env.PORTAL_FIXTURES = "1";

export default defineConfig({
  testDir: "./e2e/portal",
  testMatch: ["chrome.spec.ts", "visual.spec.ts", "signin-keyboard.spec.ts"],
  timeout: 60_000,
  expect: {
    timeout: 10_000,
    toHaveScreenshot: { threshold: 0.2, maxDiffPixelRatio: 0.01 },
  },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  snapshotPathTemplate: "e2e/portal/__screenshots__/{arg}-{platform}{ext}",
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
  outputDir: "test-results/portal",
});
