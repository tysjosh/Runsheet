import { ImageResponse } from "next/og";

/**
 * apple-touch-icon, 180x180 PNG.
 *
 * `layout.tsx` previously pointed `icons.apple` at `runsheet_logo.svg`. iOS
 * ignores SVG for apple-touch-icon and falls back to a screenshot of the page,
 * so a saved-to-home-screen shortcut showed a shrunken screenshot rather than a
 * mark — configured-looking, non-functional.
 *
 * Generated as a PNG here so no binary asset is needed. 180x180 is the size iOS
 * requests; smaller variants are downscaled from it.
 */
export const size = { width: 180, height: 180 };
export const contentType = "image/png";

export default async function AppleIcon() {
  return new ImageResponse(
    <div
      style={{
        width: "100%",
        height: "100%",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "#0a0a0b",
      }}
    >
      {/* "R/" — the RUN/SHEET mark reduced to what stays legible at 40px on a
          home screen. The full wordmark is unreadable at icon scale. */}
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          fontSize: 104,
          fontWeight: 800,
          letterSpacing: "-0.06em",
          color: "#f5f4ef",
        }}
      >
        R<span style={{ color: "#16b88c" }}>/</span>
      </div>
    </div>,
    size,
  );
}
