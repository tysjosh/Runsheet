import bundleAnalyzer from "@next/bundle-analyzer";
import type { NextConfig } from "next";
import { nextRedirects } from "./src/config/redirects";
import { buildSecurityHeaders } from "./src/config/securityHeaders";

const withBundleAnalyzer = bundleAnalyzer({
  enabled: process.env.ANALYZE === "true",
});

const nextConfig: NextConfig = {
  reactStrictMode: false,
  // `*.e2e.tsx` routes (the Dispatch Board Playwright harness) exist only
  // when the e2e config starts the dev server with NEXT_PUBLIC_E2E_HARNESS=1;
  // every other build ignores those files.
  pageExtensions:
    process.env.NEXT_PUBLIC_E2E_HARNESS === "1"
      ? ["e2e.tsx", "tsx", "ts", "jsx", "js"]
      : ["tsx", "ts", "jsx", "js"],

  // Retired routes (UI revamp design.md §4) → their /dashboard homes, 308.
  async redirects() {
    return nextRedirects();
  },
  // Security headers for all routes, including HSTS and a report-only CSP
  // (staging finding F6). See src/config/securityHeaders.ts.
  async headers() {
    return [
      {
        source: "/:path*",
        headers: buildSecurityHeaders({
          apiUrl: process.env.NEXT_PUBLIC_API_URL,
          wsUrl: process.env.NEXT_PUBLIC_WS_URL,
          stApiDomain: process.env.NEXT_PUBLIC_ST_API_DOMAIN,
          isDev: process.env.NODE_ENV !== "production",
        }),
      },
    ];
  },

  // Production build optimizations
  // Next.js 15 enables minification and tree shaking by default in production builds
  // SWC minification is enabled by default in Next.js 15+

  // Compiler options for production optimization
  compiler: {
    // Remove console.log in production (except errors and warnings)
    removeConsole:
      process.env.NODE_ENV === "production"
        ? {
            exclude: ["error", "warn"],
          }
        : false,
  },

  // Experimental features for better tree shaking
  experimental: {
    // Enable optimized package imports for better tree shaking
    optimizePackageImports: ["lucide-react"],
  },

  // Output configuration for production
  output: process.env.STANDALONE === "true" ? "standalone" : undefined,

  // Image optimization settings
  images: {
    // Enable image optimization
    unoptimized: false,
    // Configure remote patterns if needed
    remotePatterns: [],
  },
};

export default withBundleAnalyzer(nextConfig);
