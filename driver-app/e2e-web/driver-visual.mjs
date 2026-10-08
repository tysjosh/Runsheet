#!/usr/bin/env node
/**
 * Driver app visual and accessibility pass (UI revamp task 4.6).
 *
 * Runs against the Expo web build in demo mode (in-memory fake API, no
 * backend, no real account):
 *
 *   cd driver-app
 *   EXPO_PUBLIC_DEMO_MODE=true npx expo start --web --port 8099   # then stop it
 *   PW_NODE_MODULES=<dir with @playwright/test and axe-core> node e2e-web/driver-visual.mjs
 *
 * The driver app has no Playwright dependency of its own (design.md: no new
 * dependencies), so the script borrows the web app's: `PW_NODE_MODULES`
 * defaults to `../runsheet/node_modules`.
 *
 * For each screen at 390×844, in the day and night themes
 * (`colorScheme: light | dark`), it saves a screenshot under
 * `e2e-web/__screenshots__/<theme>/`, runs axe (wcag2a, wcag2aa, wcag21aa,
 * wcag22aa; `document-title` excluded because it is web-only), and measures
 * every interactive element: buttons, radios, links ≥ 44×44 CSS px, text
 * inputs ≥ 48 px tall. Results go to `e2e-web/report.json`; the exit code is
 * 1 when any screen has a critical/serious axe violation or an undersized
 * target.
 */

import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const MODULES = path.resolve(process.env.PW_NODE_MODULES ?? path.join(HERE, '../../runsheet/node_modules'));
const require = createRequire(path.join(MODULES, 'noop.js'));
const { chromium } = require('@playwright/test');
const AXE_SOURCE = readFileSync(require.resolve('axe-core/axe.min.js'), 'utf8');

const BASE = process.env.DRIVER_WEB_URL ?? 'http://localhost:8099';
const VIEWPORT = { width: 390, height: 844 };
const OUT = path.join(HERE, '__screenshots__');
const ORDER = 'ord-demo-1001';

/** name → how to reach it from a signed-in Work screen. */
const SCREENS = [
  ['02-work', async (page) => goto(page, '/')],
  [
    '03-duty-status',
    async (page) => {
      await goto(page, '/');
      await page.getByTestId('duty-chip').click();
      await page.getByText('Hours of Service').first().waitFor();
      // Let the slide-in finish: react-native-web only sets role="dialog"
      // once the modal is active.
      await page.waitForTimeout(1000);
    },
  ],
  ['04-route', async (page) => goto(page, '/route')],
  ['05-dispatch', async (page) => goto(page, '/messages')],
  ['06-profile', async (page) => goto(page, '/profile')],
  ['07-delivery', async (page) => goto(page, `/order/${ORDER}`)],
  ['08-pod', async (page) => goto(page, `/order/${ORDER}/pod`)],
  ['09-exception', async (page) => goto(page, `/order/${ORDER}/exception`)],
  ['10-inspection', async (page) => goto(page, '/inspection/new?inspectionType=pre_trip&assetId=Tanker%2024')],
];

async function goto(page, route) {
  // Client-side navigation keeps the in-memory demo session.
  await page.evaluate((href) => {
    window.history.pushState({}, '', href);
    window.dispatchEvent(new PopStateEvent('popstate'));
  }, route);
  await page.waitForTimeout(1200);
}

async function signIn(page) {
  // The first request after `expo start --clear` waits for the bundle.
  await page.goto(`${BASE}/sign-in`, { waitUntil: 'load', timeout: 180_000 });
  await page.getByText('Open demo').waitFor({ timeout: 120_000 });
  await page.waitForTimeout(1000);
}

async function axe(page) {
  await page.addScriptTag({ content: AXE_SOURCE });
  return page.evaluate(async () => {
    // The signature canvas is a WebView, which does not run on web (audit
    // §i-f); its library loading spinner is web-only and excluded.
    // eslint-disable-next-line no-undef
    const result = await window.axe.run({ exclude: [['[data-testid="signature-canvas"]']] }, {
      runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa'] },
      rules: { 'document-title': { enabled: false } },
    });
    return result.violations
      .filter((v) => v.impact === 'critical' || v.impact === 'serious')
      .map((v) => ({ id: v.id, impact: v.impact, nodes: v.nodes.slice(0, 5).map((n) => n.target.join(' ')) }));
  });
}

async function targets(page) {
  return page.evaluate(() => {
    const out = [];
    const nodes = document.querySelectorAll(
      'button, a[href], input, textarea, select, [role="button"], [role="radio"], [role="link"], [role="checkbox"], [role="switch"], [role="tab"]',
    );
    for (const el of nodes) {
      const style = getComputedStyle(el);
      if (style.visibility === 'hidden' || style.display === 'none' || el.closest('[aria-hidden="true"]')) continue;
      const r = el.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) continue;
      const isField = el.tagName === 'INPUT' || el.tagName === 'TEXTAREA';
      const minH = isField ? 48 : 44;
      const minW = isField ? 0 : 44;
      if (r.height + 0.5 < minH || r.width + 0.5 < minW) {
        out.push({
          name: (el.getAttribute('aria-label') || el.textContent || el.tagName).trim().slice(0, 60),
          role: el.getAttribute('role') || el.tagName.toLowerCase(),
          width: Math.round(r.width),
          height: Math.round(r.height),
        });
      }
    }
    return out;
  });
}

/**
 * Expo's dev LogBox overlay. The one known benign case is the web font loader's
 * 6000 ms timeout while a cold bundle loads (`fontfaceobserver`); dismiss that.
 * Any other overlay is a real runtime error and is reported as a finding.
 */
async function handleDevOverlay(page) {
  const overlay = page.locator('#error-overlay');
  if (!(await overlay.count())) return null;
  const text = (await overlay.innerText().catch(() => '')).replace(/\s+/g, ' ');
  if (/timeout exceeded/.test(text) && /fontfaceobserver/.test(text)) {
    await overlay.getByText('Dismiss').click().catch(() => undefined);
    await page.waitForTimeout(300);
    return null;
  }
  return text.slice(0, 300);
}

async function capture(page, theme, name, report) {
  const devError = await handleDevOverlay(page);
  if (devError) {
    report.push({ theme, screen: name, violations: [{ id: 'dev-error-overlay', impact: 'critical', nodes: [devError] }], undersized: [] });
    console.log(`FAIL ${theme.padEnd(5)} ${name}  dev error overlay: ${devError}`);
    return;
  }
  const dir = path.join(OUT, theme);
  mkdirSync(dir, { recursive: true });
  await page.screenshot({ path: path.join(dir, `${name}.png`), fullPage: true });
  const violations = await axe(page);
  const small = await targets(page);
  report.push({ theme, screen: name, violations, undersized: small });
  const status = violations.length || small.length ? 'FAIL' : 'ok';
  console.log(`${status.padEnd(4)} ${theme.padEnd(5)} ${name}  axe=${violations.length} undersized=${small.length}`);
}

const browser = await chromium.launch();
const report = [];
try {
  for (const theme of ['light', 'dark']) {
    const context = await browser.newContext({ viewport: VIEWPORT, colorScheme: theme, deviceScaleFactor: 2 });
    const page = await context.newPage();
    await signIn(page);
    await capture(page, theme, '01-sign-in', report);
    await page.getByText('Open demo').click();
    await page.getByText('Today').first().waitFor({ timeout: 30_000 });
    // Outlast the dev font loader's 6 s timeout, then clear its overlay.
    await page.waitForTimeout(7000);
    await handleDevOverlay(page);
    for (const [name, open] of SCREENS) {
      await handleDevOverlay(page);
      await open(page);
      await capture(page, theme, name, report);
    }
    await context.close();
  }
} finally {
  await browser.close();
}

writeFileSync(path.join(HERE, 'report.json'), `${JSON.stringify(report, null, 2)}\n`);
const failed = report.filter((r) => r.violations.length || r.undersized.length);
console.log(`${report.length} captures, ${failed.length} with findings`);
process.exit(failed.length ? 1 : 0);
