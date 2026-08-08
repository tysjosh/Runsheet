import type { Metadata } from "next";
import { Inter } from "next/font/google";
import { isIndexableSite, SITE_URL } from "@/config/site";
import { SuperTokensProvider } from "../components/SuperTokensProvider";
import "./globals.css";

// Modern, highly-legible UI typeface loaded via next/font (self-hosted, no
// layout shift). Exposed as `--font-sans` so design tokens and Tailwind's
// font-sans utility resolve to it across the whole app.
const inter = Inter({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-inter",
});

// `metadataBase` resolves Open Graph / canonical URLs, which must be absolute.
// Without it Next emits a build warning and social crawlers receive relative
// paths they cannot fetch. `SITE_URL` now comes from `config/site.ts`, which also
// owns the indexability decision used by `robots` below and by robots.txt and
// sitemap.xml — one source of truth for all three.

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  // The previous title and description described a generic "Fleet Management
  // Platform" doing "fleet tracking". That is what Google and every Slack,
  // LinkedIn and iMessage preview rendered — and it contradicted the landing
  // page itself, which sells autonomous fuel distribution to US regional
  // distributors. Search snippets are the highest-leverage copy on the site
  // because they are read before anyone reaches the page.
  title: {
    default: "Runsheet — Autonomous Fuel Distribution Operations",
    template: "%s · Runsheet",
  },
  description:
    "Runsheet forecasts tank runout, prioritizes deliveries, builds compliant multi-compartment load plans and optimizes routes for US regional fuel distributors — with net-gallon accuracy and a human-in-the-loop agent layer.",
  applicationName: "Runsheet",
  keywords: [
    "fuel distribution software",
    "heating oil delivery software",
    "propane delivery software",
    "tank monitoring",
    "IFTA reporting",
    "net gallons",
    "multi-compartment load planning",
  ],
  openGraph: {
    type: "website",
    siteName: "Runsheet",
    url: SITE_URL,
    title: "Runsheet — Autonomous Fuel Distribution Operations",
    description:
      "Runout forecasting, compliant load planning and route optimization for US regional fuel distributors. Human-in-the-loop agents, net-gallon accuracy.",
  },
  twitter: {
    card: "summary_large_image",
    title: "Runsheet — Autonomous Fuel Distribution Operations",
    description:
      "Runout forecasting, compliant load planning and route optimization for US regional fuel distributors.",
  },
  // Emits `<meta name="robots" content="noindex, nofollow">` on staging,
  // localhost and previews. This is separate from robots.txt on purpose: a
  // crawler that reached a page via an inbound link may never fetch robots.txt,
  // and the meta tag is what removes an already-indexed page. Both are gated on
  // the same `isIndexableSite()` so they cannot disagree.
  robots: {
    index: isIndexableSite(),
    follow: isIndexableSite(),
  },
  // `apple` is deliberately absent: `src/app/apple-icon.tsx` generates a
  // 180x180 PNG and Next wires it automatically. Declaring it here as the SVG
  // would override that with a format iOS ignores.
  icons: {
    icon: [{ url: "/runsheet_logo.svg", type: "image/svg+xml" }],
    shortcut: "/runsheet_logo.svg",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className={inter.variable}>
      <body className="antialiased">
        <SuperTokensProvider>{children}</SuperTokensProvider>
      </body>
    </html>
  );
}
