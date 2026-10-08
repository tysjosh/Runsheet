/**
 * @jest-environment node
 *
 * Portal colour checks (R14.1, AC 1, design §11.4, §11.5):
 *
 * - the portal-local `open` and `partial` badges meet the token contrast
 *   rules (text 4.5:1, dot 3:1);
 * - the level-bar fills are ≥ 3:1 on the slate-200 track;
 * - status dots that co-occur in the portal keep CIE76 ΔE ≥ 4 under
 *   deuteranopia and protanopia (Machado 2009, severity 1.0), except the
 *   declared pairs.
 *
 * The maths is the same as `src/styles/tokens.test.ts` (copied, because that
 * file is shared and Phase 3P doesn't edit it; follow-up: move `open` and
 * `partial` into `tokens.json` and these cases into `tokens.test.ts`).
 */
import { STATUS } from "../../../styles/tokens";
import {
  PORTAL_EXTRA_STATUS,
  type PortalStatusKey,
  portalStatusToken,
} from "../portalStatusMap";
import { LEVEL_FILL, LEVEL_TRACK } from "../tankLevel";

type RGB = [number, number, number];
const hexToRgb = (hex: string): RGB => {
  const n = Number.parseInt(hex.slice(1), 16);
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
};
const toLinear = (c: number) =>
  c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
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
  return m.map((row) =>
    Math.min(1, Math.max(0, row[0] * c[0] + row[1] * c[1] + row[2] * c[2])),
  ) as RGB;
};
const linearToLab = ([r, g, b]: RGB) => {
  const x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047;
  const y = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  const z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883;
  const f = (t: number) => (t > 0.008856 ? Math.cbrt(t) : 7.787 * t + 16 / 116);
  return [116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))];
};
const deltaE = (a: number[], b: number[]) =>
  Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);
const WHITE = "#ffffff";

describe("portal-local statuses (D21)", () => {
  it.each(Object.keys(PORTAL_EXTRA_STATUS))(
    "%s badge text fg/bg ≥ 4.5:1",
    (k) => {
      const t = PORTAL_EXTRA_STATUS[k as "open" | "partial"];
      expect(contrast(t.fg, t.bg)).toBeGreaterThanOrEqual(4.5);
      expect(contrast(t.fg, WHITE)).toBeGreaterThanOrEqual(4.5);
    },
  );
  it.each(Object.keys(PORTAL_EXTRA_STATUS))("%s dot on white ≥ 3:1", (k) => {
    const t = PORTAL_EXTRA_STATUS[k as "open" | "partial"];
    expect(contrast(t.dot, WHITE)).toBeGreaterThanOrEqual(3);
  });
  it("open and partial share the blue family and differ by icon and label", () => {
    expect(PORTAL_EXTRA_STATUS.open.dot).toBe(PORTAL_EXTRA_STATUS.partial.dot);
    expect(PORTAL_EXTRA_STATUS.open.icon).not.toBe(
      PORTAL_EXTRA_STATUS.partial.icon,
    );
    expect(PORTAL_EXTRA_STATUS.open.label).not.toBe(
      PORTAL_EXTRA_STATUS.partial.label,
    );
  });
});

describe("level bar fills (design §11.5)", () => {
  it.each(Object.entries(LEVEL_FILL))(
    "%s fill on the slate-200 track ≥ 3:1",
    (_k, hex) => {
      expect(contrast(hex, LEVEL_TRACK)).toBeGreaterThanOrEqual(3);
    },
  );
});

// design §11.4 portal co-occurrence set.
const PORTAL_SET: PortalStatusKey[] = [
  "draft",
  "planned",
  "in_transit",
  "delivered",
  "exception",
  "cancelled",
  "open",
  "partial",
  "overdue",
  "paid",
  "ok",
  "warning",
  "critical",
];

/**
 * Declared pairs (design §11.4): open/partial (same family by design) and
 * warning/overdue (tank level badges and invoice badges never share a panel).
 * Pairs on the same token hue mean the same thing in different vocabularies
 * (Delivered = Paid = OK on brand green; Exception = Critical on red) and are
 * the same colour on purpose, as in `tokens.test.ts`'s grouping.
 */
const DECLARED = new Set(["open|partial", "warning|overdue"]);
const hueOf = (k: PortalStatusKey) =>
  k === "open" || k === "partial" ? "blue" : STATUS[k].hue;

describe("colour-vision separation of portal status dots", () => {
  const cases: [string, PortalStatusKey, PortalStatusKey][] = [];
  for (const vision of ["deuteranopia", "protanopia"]) {
    for (let i = 0; i < PORTAL_SET.length; i++) {
      for (let j = i + 1; j < PORTAL_SET.length; j++) {
        const a = PORTAL_SET[i];
        const b = PORTAL_SET[j];
        if (DECLARED.has(`${a}|${b}`) || DECLARED.has(`${b}|${a}`)) continue;
        if (hueOf(a) === hueOf(b)) continue;
        cases.push([vision, a, b]);
      }
    }
  }
  it.each(cases)("%s: %s vs %s ΔE ≥ 4", (vision, a, b) => {
    const m = vision === "deuteranopia" ? DEUTERANOPIA : PROTANOPIA;
    const la = linearToLab(simulate(portalStatusToken(a).dot, m));
    const lb = linearToLab(simulate(portalStatusToken(b).dot, m));
    expect(deltaE(la, lb)).toBeGreaterThanOrEqual(4);
  });
});
