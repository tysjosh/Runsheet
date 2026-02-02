import { test, expect } from '@playwright/test';

/**
 * Example E2E test to verify Playwright setup is working correctly.
 * This test can be removed once actual E2E tests are implemented.
 */
test.describe('Example Tests', () => {
  test('should load the homepage', async ({ page }) => {
    // Navigate to the homepage
    await page.goto('/');
    
    // Verify the page loads successfully
    await expect(page).toHaveTitle(/Runsheet/i);
  });

  test('should have visible main content', async ({ page }) => {
    await page.goto('/');
    
    // Wait for the page to be fully loaded
    await page.waitForLoadState('networkidle');
    
    // Verify the body is visible
    await expect(page.locator('body')).toBeVisible();
  });
});
