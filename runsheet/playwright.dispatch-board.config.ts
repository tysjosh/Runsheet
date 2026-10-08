import { defineConfig, devices } from "@playwright/test";

/**
 * Dispatch Board e2e, perf and accessibility checks (plan tasks 37–40).
 *
 * Starts its own dev server on port 3124 with NEXT_PUBLIC_E2E_HARNESS=1, so
 * the `/e2e/dispatch-board` harness route exists (see `pageExtensions` in
 * next.config.ts) and no other checkout's server is reused. Every API call
 * and socket is answered by `e2e/dispatchBoardFake.ts`; nothing reaches a
 * backend or staging.
 *
 *   npx playwright test -c playwright.dispatch-board.config.ts
 *
 * PW_BOARD_PROD=1 builds and serves a production bundle instead of the dev
 * server, for the K15 frame-time check (dev React is several times slower).
 * The build writes `.next`; delete it afterwards.
 */
const prod = process.env.PW_BOARD_PROD === "1";
export default defineConfig({
  testDir: "./e2e",
  testMatch: "dispatch-board.spec.ts",
  timeout: 90_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: "http://localhost:3124",
    trace: "retain-on-failure",
    actionTimeout: 15_000,
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "webkit", use: { ...devices["Desktop Safari"] } },
    { name: "ipad-webkit", use: { ...devices["iPad Pro 11"] } },
  ],
  webServer: {
    command: prod
      ? "npx next build && npx next start -p 3124"
      : "npx next dev --turbopack -p 3124",
    url: "http://localhost:3124/e2e/dispatch-board",
    env: { NEXT_PUBLIC_E2E_HARNESS: "1", NEXT_PUBLIC_TENANT_ID: "e2e-tenant" },
    reuseExistingServer: false,
    timeout: prod ? 600_000 : 180_000,
    stdout: "pipe",
    stderr: "pipe",
  },
  outputDir: "test-results/dispatch-board",
});
