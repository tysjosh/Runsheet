/**
 * @jest-environment node
 *
 * Driver tokens (UI revamp task 4.1, R13.1, R13.7, D5, D6, D15).
 *
 * - `lib/tokens.ts` and the `global.css` block are what the generator writes
 *   (`--with-driver --check`), so nobody hand-edits them.
 * - Shared colours are the web app's: the driver `tokens.ts` is the web
 *   `tokens.ts` plus a `DRIVER` export.
 * - Contrast is measured on the HSL triplets NativeWind actually renders, in
 *   the day theme and the night theme (`.dark:root`).
 * - Colour vision: pairs that share a driver screen stay apart under
 *   deuteranopia and protanopia (Machado 2009, CIE76 ΔE ≥ 4, same maths as
 *   `runsheet/src/styles/tokens.test.ts`).
 */

import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import path from 'node:path';

import { PALETTE } from '../lib/theme';
import { COLOR, DRIVER, PRODUCT, STATUS, STATUS_KEYS, type StatusKey } from '../lib/tokens';

const ROOT = path.join(__dirname, '..', '..');

// ── colour maths ─────────────────────────────────────────────────────────
type RGB = [number, number, number];
const hexToRgb = (hex: string): RGB => {
  const n = Number.parseInt(hex.slice(1), 16);
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
};
const toLinear = (c: number) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
const luminance = (hex: string) => {
  const [r, g, b] = hexToRgb(hex).map(toLinear);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};
const contrast = (a: string, b: string) => {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
};
const DEUTERANOPIA = [
  [0.367322, 0.860646, -0.227968],
  [0.280085, 0.672501, 0.047413],
  [-0.01182, 0.04294, 0.968881],
];
const PROTANOPIA = [
  [0.152286, 1.052583, -0.204868],
  [0.114503, 0.786281, 0.099216],
  [-0.003882, -0.048116, 1.051998],
];
const simulate = (hex: string, m: number[][]): RGB => {
  const c = hexToRgb(hex).map(toLinear);
  return m.map((row) => Math.min(1, Math.max(0, row[0] * c[0] + row[1] * c[1] + row[2] * c[2]))) as RGB;
};
const linearToLab = ([r, g, b]: RGB) => {
  const x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047;
  const y = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  const z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883;
  const f = (t: number) => (t > 0.008856 ? Math.cbrt(t) : 7.787 * t + 16 / 116);
  return [116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))];
};
const deltaE = (a: string, b: string, m: number[][]) => {
  const [p, q] = [linearToLab(simulate(a, m)), linearToLab(simulate(b, m))];
  return Math.hypot(p[0] - q[0], p[1] - q[1], p[2] - q[2]);
};

function hslToHex(triplet: string): string {
  const [h, s, l] = triplet.replace(/%/g, '').split(/\s+/).map(Number);
  const sat = s / 100;
  const lig = l / 100;
  const k = (n: number) => (n + h / 30) % 12;
  const a = sat * Math.min(lig, 1 - lig);
  const f = (n: number) => lig - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
  return `#${[f(0), f(8), f(4)].map((v) => Math.round(v * 255).toString(16).padStart(2, '0')).join('')}`;
}

/** The variables NativeWind resolves, read from the generated block. */
function themeVars(selector: ':root' | '.dark:root'): Record<string, string> {
  const css = readFileSync(path.join(__dirname, '..', 'global.css'), 'utf8');
  const block = css.slice(css.indexOf('/* tokens:start */'), css.indexOf('/* tokens:end */'));
  const start = block.indexOf(`${selector} {`);
  const body = block.slice(start, block.indexOf('}', start));
  const vars: Record<string, string> = {};
  for (const match of body.matchAll(/--([\w-]+):\s*([^;]+);/g)) {
    vars[match[1]] = hslToHex(match[2]);
  }
  return vars;
}

const TEXT_MIN = 4.5;
const UI_MIN = 3;

describe('generated driver tokens', () => {
  it('match a fresh generation (`build-tokens.mjs --with-driver --check`)', () => {
    expect(() =>
      execFileSync('node', ['scripts/build-tokens.mjs', '--with-driver', '--check'], { cwd: ROOT, stdio: 'pipe' }),
    ).not.toThrow();
  });

  it('share every status and product colour with the web app', () => {
    const web = readFileSync(path.join(ROOT, 'runsheet/src/styles/tokens.ts'), 'utf8').trimEnd();
    const driver = readFileSync(path.join(__dirname, '..', 'lib/tokens.ts'), 'utf8');
    expect(driver.startsWith(web)).toBe(true);
  });

  it('use brand green, not the Expo template blue, as primary in both themes', () => {
    expect(themeVars(':root').primary).toBe(hslToHex('164 84% 27%'));
    expect(contrast(themeVars(':root').primary, COLOR.brand['600'])).toBeLessThan(1.05);
    expect(contrast(themeVars('.dark:root').primary, COLOR.brand['500'])).toBeLessThan(1.05);
    expect(PALETTE.light.tint).toBe(COLOR.brand['600']);
    expect(PALETTE.dark.tint).toBe(COLOR.brand['500']);
  });
});

describe.each([
  ['day', ':root' as const],
  ['night', '.dark:root' as const],
])('%s theme contrast (rendered HSL values)', (_name, selector) => {
  const v = themeVars(selector);
  const text: [string, string, string][] = [
    ['foreground', 'background', 'body text'],
    ['card-foreground', 'card', 'card text'],
    ['muted-foreground', 'background', 'secondary text'],
    ['muted-foreground', 'card', 'secondary text on cards'],
    ['muted-foreground', 'muted', 'secondary text on muted panels'],
    ['foreground', 'muted', 'text on muted panels'],
    ['primary-foreground', 'primary', 'primary button label'],
    ['destructive-foreground', 'destructive', 'destructive button / badge label'],
    ['destructive', 'background', 'destructive text'],
    ['destructive', 'card', 'destructive text on cards'],
    ['secondary-foreground', 'secondary', 'secondary button label'],
    ['accent-foreground', 'accent', 'pressed / selected option'],
    ['foreground', 'accent', 'text on a selected option'],
  ];
  it.each(text)('%s on %s (%s) ≥ 4.5:1', (fg, bg) => {
    expect(contrast(v[fg], v[bg])).toBeGreaterThanOrEqual(TEXT_MIN);
  });
  it('primary as text and the focus ring hold up on the background', () => {
    expect(contrast(v.primary, v.background)).toBeGreaterThanOrEqual(TEXT_MIN);
    expect(contrast(v.ring, v.background)).toBeGreaterThanOrEqual(UI_MIN);
  });
});

describe('navigation palette (headers, tab bar)', () => {
  it.each(['light', 'dark'] as const)('%s: text, muted text, tint and danger ≥ 4.5:1 on surface and canvas', (scheme) => {
    const p = PALETTE[scheme];
    for (const fg of [p.text, p.textMuted, p.tint, p.danger, p.icon]) {
      expect(contrast(fg, p.surface)).toBeGreaterThanOrEqual(TEXT_MIN);
      expect(contrast(fg, p.canvas)).toBeGreaterThanOrEqual(TEXT_MIN);
    }
  });
});

describe('badges and product caps', () => {
  it.each(STATUS_KEYS)('status %s badge fg/bg ≥ 4.5:1 (same in both themes)', (key) => {
    expect(contrast(STATUS[key].fg, STATUS[key].bg)).toBeGreaterThanOrEqual(TEXT_MIN);
  });
  it.each(Object.keys(PRODUCT))('product %s cap symbol ≥ 4.5:1 and visible on day and night surfaces', (code) => {
    const p = PRODUCT[code as keyof typeof PRODUCT];
    expect(contrast(p.fg, p.bg)).toBeGreaterThanOrEqual(TEXT_MIN);
    // By day the cap itself must stand out from the white surface.
    expect(Math.max(contrast(p.bg, PALETTE.light.surface), contrast(p.border, PALETTE.light.surface))).toBeGreaterThanOrEqual(
      UI_MIN,
    );
    // At night every cap sits in the light ring: the ring must stand out from
    // the night surfaces, and the cap from the ring.
    for (const surface of [PALETTE.dark.surface, PALETTE.dark.canvas]) {
      expect(contrast(DRIVER.night['text-muted'], surface)).toBeGreaterThanOrEqual(UI_MIN);
    }
    expect(
      Math.max(contrast(p.bg, DRIVER.night['text-muted']), contrast(p.border, DRIVER.night['text-muted'])),
    ).toBeGreaterThanOrEqual(UI_MIN);
  });
  it('the cross-contamination text (red.800 by day, red.300 at night) passes AA', () => {
    expect(contrast(PALETTE.light.danger, '#ffffff')).toBeGreaterThanOrEqual(7);
    expect(contrast(PALETTE.dark.danger, PALETTE.dark.surface)).toBeGreaterThanOrEqual(7);
    // The old #ef4444 on white failed.
    expect(contrast(COLOR.red['500'], '#ffffff')).toBeLessThan(TEXT_MIN);
  });
});

describe('colour vision', () => {
  // Statuses that share a driver screen: duty chip tones (ok / warning /
  // draft / cancelled), order badges (dispatched / in transit / delivered /
  // exception) and the night primary vs night destructive.
  const groups: StatusKey[][] = [
    ['ok', 'warning', 'draft', 'cancelled'],
    ['dispatched', 'in_transit', 'delivered', 'exception'],
  ];
  const pairs: [string, string, string][] = [];
  for (const group of groups) {
    for (let i = 0; i < group.length; i += 1) {
      for (let j = i + 1; j < group.length; j += 1) {
        pairs.push([`${group[i]} / ${group[j]}`, STATUS[group[i]].dot, STATUS[group[j]].dot]);
      }
    }
  }
  pairs.push(['night primary / night destructive', themeVars('.dark:root').primary, themeVars('.dark:root').destructive]);
  pairs.push(['day primary / day destructive', themeVars(':root').primary, themeVars(':root').destructive]);

  it.each(pairs)('%s stay apart (ΔE ≥ 4) under deuteranopia and protanopia', (_label, a, b) => {
    expect(deltaE(a, b, DEUTERANOPIA)).toBeGreaterThanOrEqual(4);
    expect(deltaE(a, b, PROTANOPIA)).toBeGreaterThanOrEqual(4);
  });
});
