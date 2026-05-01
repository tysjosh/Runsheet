import bundleAnalyzer from "@next/bundle-analyzer";
import type { NextConfig } from "next";

const withBundleAnalyzer = bundleAnalyzer({
  enabled: process.env.ANALYZE === "true",
});

const nextConfig: NextConfig = {
  reactStrictMode: false,

  // Security headers for all routes
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-XSS-Protection", value: "1; mode=block" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          {
            key: "Permissions-Policy",
            value: "camera=(), microphone=(), geolocation=(self)",
          },
        ],
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

  // Webpack configuration for additional optimizations
  webpack: (config, { isServer, dev }) => {
    // Only apply production optimizations in production builds
    if (!dev) {
      // Enable module concatenation for better tree shaking
      config.optimization = {
        ...config.optimization,
        moduleIds: "deterministic",
        // Ensure tree shaking is enabled
        usedExports: true,
        // Enable side effects optimization
        sideEffects: true,
      };
    }

    return config;
  },
};

export default withBundleAnalyzer(nextConfig);
