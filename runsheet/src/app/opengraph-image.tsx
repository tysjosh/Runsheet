import { ImageResponse } from "next/og";

/**
 * Open Graph / Twitter card image, 1200x630.
 *
 * Generated with `next/og` rather than checked in as a binary so the card
 * cannot drift from the positioning it advertises — editing the copy here is
 * the same commit as editing the page.
 *
 * `twitter:card` was already declared as `summary_large_image` in
 * `layout.tsx` while no image existed, which renders a large empty card on
 * LinkedIn, Slack and X — worse than no declaration at all. This closes that.
 *
 * Colours mirror the landing page (`#0a0a0b` ground, `#16b88c` accent,
 * `#f5f4ef` type). No remote font is fetched: a webfont request at build or
 * edge-render time is a failure mode for a purely decorative asset, and the
 * system stack is legible at this size.
 */
export const alt =
  "Runsheet — autonomous fuel distribution operations for US regional distributors";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

export default async function Image() {
  return new ImageResponse(
    <div
      style={{
        width: "100%",
        height: "100%",
        display: "flex",
        flexDirection: "column",
        justifyContent: "space-between",
        background: "#0a0a0b",
        padding: "72px 80px",
        // A single accent wash, echoing the hero's radial glow.
        backgroundImage:
          "radial-gradient(900px 480px at 82% 8%, rgba(22,184,140,0.22), transparent 70%)",
      }}
    >
      {/* Satori requires an explicit `display` on any element with more than one
          child, and does not lay out `<br />`. Every multi-child node below is
          therefore an explicit flex container with single-text-node children. */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          fontSize: 30,
          fontWeight: 800,
          letterSpacing: "-0.02em",
          color: "#f5f4ef",
        }}
      >
        <div style={{ display: "flex" }}>RUN</div>
        <div style={{ display: "flex", color: "#16b88c" }}>/</div>
        <div style={{ display: "flex" }}>SHEET</div>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 26 }}>
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            fontSize: 78,
            fontWeight: 800,
            lineHeight: 1.04,
            letterSpacing: "-0.035em",
            color: "#f5f4ef",
            maxWidth: 940,
          }}
        >
          <div style={{ display: "flex" }}>Autonomous fuel</div>
          <div style={{ display: "flex" }}>distribution operations.</div>
        </div>
        <div
          style={{
            display: "flex",
            fontSize: 29,
            lineHeight: 1.35,
            color: "rgba(245,244,239,0.68)",
            maxWidth: 880,
          }}
        >
          {
            "Runout forecasting · compliant load planning · route optimization — for US regional fuel distributors."
          }
        </div>
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
        {["Net gallons at 60°F", "IFTA", "Human-in-the-loop"].map((chip) => (
          <div
            key={chip}
            style={{
              fontSize: 21,
              color: "rgba(245,244,239,0.72)",
              border: "1px solid rgba(245,244,239,0.18)",
              borderRadius: 999,
              padding: "9px 20px",
            }}
          >
            {chip}
          </div>
        ))}
      </div>
    </div>,
    size,
  );
}
