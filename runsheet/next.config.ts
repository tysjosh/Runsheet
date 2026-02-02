import type { NextConfig } from "next";
import bundleAnalyzer from "@next/bundle-analyzer";

const withBundleAnalyzer = bundleAnalyzer({
  enabled: process.env.ANALYZE === "true",
});

const nextConfig: NextConfig = {
  reactStrictMode: false,

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
