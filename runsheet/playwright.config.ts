import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright configuration for Runsheet E2E tests.
 * 
 * This configuration supports:
 * - Testing against the Next.js development server
 * - Multiple browser support (chromium, firefox, webkit)
 * - Screenshot and video capture on failure
 * - Proper timeouts for E2E tests
 * - Both local development and CI environments
 * 
 * @see https://playwright.dev/docs/test-configuration
 */
export default defineConfig({
  // Directory containing test files
  testDir: './e2e',
  
  // Run tests in files in parallel
  fullyParallel: true,
  
  // Fail the build on CI if you accidentally left test.only in the source code
  forbidOnly: !!process.env.CI,
  
  // Retry on CI only
  retries: process.env.CI ? 2 : 0,
  
  // Opt out of parallel tests on CI for more stable results
  workers: process.env.CI ? 1 : undefined,
  
  // Reporter to use
  reporter: [
    ['html', { outputFolder: 'playwright-report' }],
    ['list'],
    // Add JUnit reporter for CI integration
    ...(process.env.CI ? [['junit', { outputFile: 'playwright-results.xml' }] as const] : []),
  ],
  
  // Shared settings for all the projects below
  use: {
    // Base URL to use in actions like `await page.goto('/')`
    baseURL: process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:3000',
    
    // Collect trace when retrying the failed test
    trace: 'on-first-retry',
    
    // Capture screenshot on failure
    screenshot: 'only-on-failure',
    
    // Record video on failure
    video: 'on-first-retry',
    
    // Maximum time each action such as `click()` can take
    actionTimeout: 15000,
    
    // Maximum time for navigation actions
    navigationTimeout: 30000,
  },
  
  // Global timeout for each test
  timeout: 60000,
  
  // Timeout for expect() assertions
  expect: {
    timeout: 10000,
  },
  
  // Configure projects for major browsers
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'firefox',
      use: { ...devices['Desktop Firefox'] },
    },
    {
      name: 'webkit',
      use: { ...devices['Desktop Safari'] },
    },
    
    // Test against mobile viewports
    {
      name: 'Mobile Chrome',
      use: { ...devices['Pixel 5'] },
    },
    {
      name: 'Mobile Safari',
      use: { ...devices['iPhone 12'] },
    },
  ],
  
  // Run your local dev server before starting the tests
  webServer: {
    command: 'npm run dev',
    url: 'http://localhost:3000',
    reuseExistingServer: !process.env.CI,
    timeout: 120000, // 2 minutes to start the dev server
    stdout: 'pipe',
    stderr: 'pipe',
  },
  
  // Output folder for test artifacts (screenshots, videos, traces)
  outputDir: 'test-results',
});
