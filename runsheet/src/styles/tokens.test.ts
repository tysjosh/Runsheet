/**
 * @jest-environment node
 *
 * Token guard (UI revamp R1.5, R1.6; design.md §10 "Tokens").
 *
 * - Contrast: text pairs ≥ 4.5:1, UI elements (dots, borders, focus) ≥ 3:1.
 * - Colour vision: status dots simulated under deuteranopia and protanopia
 *   (Machado 2009, severity 1.0, applied in linear RGB as palette.html's
 *   feColorMatrix does) keep CIE76 ΔE ≥ 4 for statuses that share a screen.
 * - Every catalog product code has a product token.
 * - The generated files match design/tokens.json.
 */
import { execFileSync } from "node:child_process";
import path from "node:path";
import {
  CHART,
  IDENTITY,
  IDENTITY_ADJACENCY_AVOID,
  PRODUCT,
  PRODUCT_CODES,
  SEMANTIC,
  STATUS,
  STATUS_KEYS,
  type StatusKey,
} from "./tokens";

// ── colour maths ─────────────────────────────────────────────────────────
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

// Machado, Oliveira & Fernandes (2009), severity 1.0. Same matrices as
// ../../../.agents/tasks/ui-revamp-2026-10-08/palette.html filters.
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
const deltaE76 = (a: number[], b: number[]) =>
  Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);

const WHITE = "#ffffff";
const TEXT_MIN = 4.5;
const UI_MIN = 3;

// ── contrast ─────────────────────────────────────────────────────────────
describe("token contrast", () => {
  it.each(STATUS_KEYS)("status %s badge text fg/bg ≥ 4.5:1", (key) => {
    expect(contrast(STATUS[key].fg, STATUS[key].bg)).toBeGreaterThanOrEqual(
      TEXT_MIN,
    );
  });

  it.each(STATUS_KEYS)("status %s badge text fg/white ≥ 4.5:1", (key) => {
    // Labels sometimes render on the white surface (e.g. a dot + label).
    expect(contrast(STATUS[key].fg, WHITE)).toBeGreaterThanOrEqual(TEXT_MIN);
  });

  it.each(STATUS_KEYS)("status %s dot on white ≥ 3:1 (UI)", (key) => {
    expect(contrast(STATUS[key].dot, WHITE)).toBeGreaterThanOrEqual(UI_MIN);
  });

  it.each(Object.keys(PRODUCT))("product %s symbol fg/bg ≥ 4.5:1", (code) => {
    const p = PRODUCT[code as keyof typeof PRODUCT];
    expect(contrast(p.fg, p.bg)).toBeGreaterThanOrEqual(TEXT_MIN);
  });

  it.each(Object.keys(PRODUCT))(
    "product %s cap is distinguishable from white (bg or border ≥ 3:1)",
    (code) => {
      const p = PRODUCT[code as keyof typeof PRODUCT];
      expect(
        Math.max(contrast(p.bg, WHITE), contrast(p.border, WHITE)),
      ).toBeGreaterThanOrEqual(UI_MIN);
    },
  );

  const textPairs: [string, string, string][] = [
    ["text on surface", SEMANTIC.text, SEMANTIC.surface],
    ["text on canvas", SEMANTIC.text, SEMANTIC.canvas],
    ["text-muted on surface", SEMANTIC["text-muted"], SEMANTIC.surface],
    ["text-muted on canvas", SEMANTIC["text-muted"], SEMANTIC.canvas],
    ["link on surface", SEMANTIC.link, SEMANTIC.surface],
    ["link on canvas", SEMANTIC.link, SEMANTIC.canvas],
    ["primary as text on surface", SEMANTIC.primary, SEMANTIC.surface],
    ["on-primary on primary", SEMANTIC["on-primary"], SEMANTIC.primary],
    [
      "on-primary on primary-hover",
      SEMANTIC["on-primary"],
      SEMANTIC["primary-hover"],
    ],
    ["primary on primary-soft", SEMANTIC.primary, SEMANTIC["primary-soft"]],
    ["white on danger", WHITE, SEMANTIC.danger],
    ["white on danger-hover", WHITE, SEMANTIC["danger-hover"]],
    ["danger as text on surface", SEMANTIC.danger, SEMANTIC.surface],
  ];
  it.each(textPairs)("%s ≥ 4.5:1", (_label, fg, bg) => {
    expect(contrast(fg, bg)).toBeGreaterThanOrEqual(TEXT_MIN);
  });

  it("focus ring is ≥ 3:1 on surface and canvas", () => {
    expect(contrast(SEMANTIC.focus, SEMANTIC.surface)).toBeGreaterThanOrEqual(
      UI_MIN,
    );
    expect(contrast(SEMANTIC.focus, SEMANTIC.canvas)).toBeGreaterThanOrEqual(
      UI_MIN,
    );
  });

  it.each(IDENTITY.map((c) => [c.token, c.hex]))(
    "identity %s carries white initials at ≥ 4.5:1",
    (_token, hex) => {
      expect(contrast(WHITE, hex)).toBeGreaterThanOrEqual(TEXT_MIN);
    },
  );

  it.each(CHART.map((hex, i) => [i, hex]))(
    "chart series %s is ≥ 3:1 on white (UI)",
    (_i, hex) => {
      expect(contrast(hex as string, WHITE)).toBeGreaterThanOrEqual(UI_MIN);
    },
  );
});

// ── legacy aliases (tokens.css) ──────────────────────────────────────────
describe("legacy aliases now pass AA", () => {
  // Parsed from the generated CSS so the test checks what pages actually get.
  const css = require("node:fs").readFileSync(
    path.join(__dirname, "tokens.css"),
    "utf8",
  ) as string;
  const rs = (name: string) => {
    const m = css.match(new RegExp(`--rs-${name}: (#[0-9a-f]{6});`));
    if (!m) throw new Error(`--rs-${name} missing`);
    return m[1];
  };
  it.each(["success", "error", "warning", "info"])(
    "white on the %s alias ≥ 4.5:1 (same ratio as the alias as text on white)",
    (name) => {
      expect(contrast(WHITE, rs(name))).toBeGreaterThanOrEqual(TEXT_MIN);
    },
  );
});

// ── colour vision ────────────────────────────────────────────────────────
// Statuses that can appear on the same page. Same-hue statuses in different
// groups (e.g. Delivered/OK/Paid) mean the same thing and never compete.
const CO_OCCURRING: Record<string, StatusKey[]> = {
  operations: [
    "draft",
    "planned",
    "dispatched",
    "in_transit",
    "delivered",
    "delayed",
    "exception",
    "cancelled",
  ],
  health: ["ok", "warning", "critical"],
  billing: ["draft", "paid", "overdue", "cancelled"],
};
// Delayed (operations) and Overdue (billing) never share a screen (R1.6).
const EXEMPT = new Set(["delayed|overdue", "overdue|delayed"]);

describe("colour-vision separation of status dots", () => {
  const cases: [string, string, StatusKey, StatusKey][] = [];
  for (const [vision] of [["deuteranopia"], ["protanopia"]]) {
    for (const group of Object.values(CO_OCCURRING)) {
      for (let i = 0; i < group.length; i++) {
        for (let j = i + 1; j < group.length; j++) {
          if (EXEMPT.has(`${group[i]}|${group[j]}`)) continue;
          cases.push([
            vision,
            `${group[i]} vs ${group[j]}`,
            group[i],
            group[j],
          ]);
        }
      }
    }
  }
  it.each(cases)("%s: %s ΔE ≥ 4", (vision, _label, a, b) => {
    const m = vision === "deuteranopia" ? DEUTERANOPIA : PROTANOPIA;
    const d = deltaE76(
      linearToLab(simulate(STATUS[a].dot, m)),
      linearToLab(simulate(STATUS[b].dot, m)),
    );
    expect(d).toBeGreaterThanOrEqual(4);
  });

  it("reproduces the audit's closest deuteranopia pair (Delayed vs Overdue ≈ 2.1)", () => {
    // Sanity check on the simulation itself: the exempt pair is genuinely
    // close, so the exemption is doing real work.
    const d = deltaE76(
      linearToLab(simulate(STATUS.delayed.dot, DEUTERANOPIA)),
      linearToLab(simulate(STATUS.overdue.dot, DEUTERANOPIA)),
    );
    expect(d).toBeGreaterThan(1.5);
    expect(d).toBeLessThan(3);
  });

  it("identity adjacency-avoid pairs name real identity colours", () => {
    const tokens = new Set(IDENTITY.map((c) => c.token as string));
    for (const pair of IDENTITY_ADJACENCY_AVOID) {
      for (const t of pair) expect(tokens.has(t)).toBe(true);
    }
  });
});

// ── product catalog coverage ─────────────────────────────────────────────
describe("product tokens", () => {
  // Hard-coded from Runsheet-backend/fuel/services/fuel_product_catalog.py
  // (product_code= entries, lines 118–190). Update both when the catalog grows.
  const CATALOG_CODES = [
    "DIESEL_2",
    "HEATING_OIL",
    "GASOLINE_REG",
    "GASOLINE_PREM",
    "PROPANE",
    "KEROSENE",
    "OFF_ROAD_DIESEL",
    "DEF",
    "ETHANOL_E85",
  ];
  it.each(CATALOG_CODES)(
    "%s has a product token with a name and symbol",
    (c) => {
      const p = PRODUCT[c as keyof typeof PRODUCT];
      expect(p).toBeDefined();
      expect(p.name).toBeTruthy();
      expect(p.symbol).toBeTruthy();
    },
  );
  it("has no token for a code outside the catalog", () => {
    expect([...PRODUCT_CODES].sort()).toEqual([...CATALOG_CODES].sort());
  });
});

// ── generator freshness ──────────────────────────────────────────────────
describe("generated token files", () => {
  it("match design/tokens.json (build-tokens.mjs --check)", () => {
    const repo = path.resolve(__dirname, "../../..");
    expect(() =>
      execFileSync(
        process.execPath,
        [path.join(repo, "scripts/build-tokens.mjs"), "--check"],
        { stdio: "pipe" },
      ),
    ).not.toThrow();
  });
});
