/**
 * @jest-environment node
 *
 * Formatting guard (R5.3). Component code formats numbers and dates through
 * `src/lib/format.ts`. This counts the direct `toFixed(` and `toLocale…` calls
 * left in `src/components/**\/*.tsx` (tests excluded) and fails if the count
 * grows. Each migration task lowers BASELINE; Phase 3 (task 3.10) takes it to 0.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";

// Counted at the start of the UI revamp, Phase 1 (task 1.4): 199, plus 1
// from app/admin/weather-alerts/page.tsx moving into components/admin/ in
// task 1.10 (relocated code, not new calls). Phase 2 left 185; Phase 3
// iteration 1 (tasks 3.2, 3.7, 3.9) took it to 142; iteration 2 (3.1, 3.3,
// Notifications, Orders/Jobs dialogs) to 127.
const BASELINE = 127;

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = path.join(dir, name);
    if (statSync(p).isDirectory()) {
      if (name === "__tests__") continue;
      walk(p, out);
    } else if (p.endsWith(".tsx") && !p.endsWith(".test.tsx")) {
      out.push(p);
    }
  }
  return out;
}

describe("format guard", () => {
  const root = path.join(__dirname, "..", "components");
  const counts = walk(root)
    .map((file) => {
      const src = readFileSync(file, "utf8");
      const n = (src.match(/toFixed\(|toLocale/g) ?? []).length;
      return [path.relative(root, file), n] as const;
    })
    .filter(([, n]) => n > 0);
  const total = counts.reduce((sum, [, n]) => sum + n, 0);

  it(`has no more than ${BASELINE} direct toFixed/toLocale calls in components`, () => {
    if (total > BASELINE) {
      const top = [...counts].sort((a, b) => b[1] - a[1]).slice(0, 10);
      throw new Error(
        `${total} direct toFixed/toLocale calls (baseline ${BASELINE}). Use src/lib/format.ts.\n` +
          top.map(([f, n]) => `  ${n}  ${f}`).join("\n"),
      );
    }
    expect(total).toBeLessThanOrEqual(BASELINE);
  });
});
