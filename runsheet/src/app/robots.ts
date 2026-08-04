import type { MetadataRoute } from "next";

/**
 * robots.txt
 *
 * Only the three public marketing routes are crawlable. `/dashboard`, `/ops`
 * and `/commerce` are the authenticated application: they redirect to sign-in
 * for an anonymous crawler, so indexing them yields a set of useless
 * near-duplicate login pages that dilute the real content. `/api` and
 * `/auth` are non-HTML surfaces.
 *
 * Disallow here is a crawl directive, not access control — the routes are
 * protected by SuperTokens session checks and the backend role gates. This
 * exists to keep the index clean, and nothing about it should be read as a
 * security boundary.
 */
const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL ?? "http://localhost:3000";

export default function robots(): MetadataRoute.Robots {
  return {
    rules: [
      {
        userAgent: "*",
        allow: ["/", "/request-pilot", "/signin"],
        disallow: ["/dashboard", "/ops", "/commerce", "/api", "/auth"],
      },
    ],
    sitemap: `${SITE_URL}/sitemap.xml`,
    host: SITE_URL,
  };
}
