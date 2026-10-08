/**
 * @jest-environment node
 *
 * Portal grep guards (R14.4, AC 2, AC 16):
 *
 * - no `toFixed(` or `toLocale*` in portal code (formatting goes through
 *   `lib/format` / `portalFormat`);
 * - no `status_code` or `product_code` rendered as JSX text;
 * - no portal module imports the staff shell or a WebSocket hook.
 */
import fs from "node:fs";
import path from "node:path";

const ROOT = path.join(__dirname, "..", "..", "..");
const DIRS = [
  path.join(ROOT, "app", "portal"),
  path.join(ROOT, "components", "portal"),
];

function sources(): { file: string; text: string }[] {
  const out: { file: string; text: string }[] = [];
  const walk = (dir: string) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        if (entry.name === "__tests__" || entry.name === "__fixtures__")
          continue;
        walk(p);
      } else if (
        /\.(ts|tsx)$/.test(entry.name) &&
        !/\.test\.tsx?$/.test(entry.name)
      ) {
        // Comments may name the banned calls; only code counts.
        const code = fs
          .readFileSync(p, "utf8")
          .replace(/\/\*[\s\S]*?\*\//g, "")
          .replace(/^\s*\/\/.*$/gm, "");
        out.push({ file: path.relative(ROOT, p), text: code });
      }
    }
  };
  for (const d of DIRS) walk(d);
  return out;
}

describe("portal guards", () => {
  const files = sources();

  it("finds the portal sources", () => {
    expect(files.length).toBeGreaterThan(20);
  });

  it("has no toFixed( or toLocale calls", () => {
    const hits = files
      .filter((f) => /toFixed\(|toLocale/.test(f.text))
      .map((f) => f.file);
    expect(hits).toEqual([]);
  });

  it("never renders status_code or product_code as text", () => {
    // `{x.status_code}` / `{x.product_code}` as a JSX child (not an attribute
    // value, which is `={…}`), or inside a template literal shown as text.
    const child = /(?<![=\w])\{\s*[\w.?]+\.(status_code|product_code)\s*\}/;
    const templ = /`[^`]*\$\{\s*[\w.?]+\.(status_code|product_code)\s*\}[^`]*`/;
    const hits = files
      .filter((f) => child.test(f.text) || templ.test(f.text))
      .map((f) => f.file);
    expect(hits).toEqual([]);
  });

  it("imports nothing from the staff shell or the socket hooks", () => {
    const forbidden =
      /from\s+"[^"]*(components\/shell\/|\/Sidebar"|\/Header"|\/AIChat"|\/GlobalSearch"|\/NotificationBell"|hooks\/use\w*WebSocket|hooks\/use\w*Socket|dashboard\/shell-context)/;
    const hits = files.filter((f) => forbidden.test(f.text)).map((f) => f.file);
    expect(hits).toEqual([]);
  });
});
