import { defineConfig, devices } from "@playwright/test";

// Dispatch Board DnD spike (plan task 1, throwaway; deleted after). Own port
// so it never reuses another checkout's dev server on :3000.
export default defineConfig({
  testDir: "./e2e",
  testMatch: "dnd-spike.spec.ts",
  timeout: 90000,
  reporter: [["list"]],
  use: { baseURL: "http://localhost:3123", trace: "off" },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "webkit", use: { ...devices["Desktop Safari"] } },
    { name: "ipad-webkit", use: { ...devices["iPad Pro 11"] } },
  ],
  webServer: {
    command: "npx next dev --turbopack -p 3123",
    url: "http://localhost:3123/dnd-spike",
    reuseExistingServer: false,
    timeout: 180000,
    stdout: "pipe",
    stderr: "pipe",
  },
  outputDir: "../../../tmp/dnd-spike-results",
});
