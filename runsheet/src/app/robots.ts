import type { MetadataRoute } from "next";

import { isIndexableSite, SITE_URL } from "@/config/site";

/**
 * robots.txt
 *
 * On a NON-production deployment (staging, localhost, previews) the whole site
 * is disallowed and no sitemap is advertised. See `config/site.ts` for why that
 * is an allowlist of production hosts rather than a denylist — staging used to
 * serve `Allow: /` plus a sitemap of its own URLs.
 *
 * On production, only the three public marketing routes are crawlable.
 * `/dashboard`, `/ops` and `/commerce` are the authenticated application: they
 * redirect an anonymous crawler to sign-in, so indexing them yields a set of
 * useless near-duplicate login pages that dilute the real content. `/api` and
 * `/auth` are non-HTML surfaces.
 *
 * Disallow here is a crawl directive, not access control — those routes are
 * protected by SuperTokens session checks and the backend role gates. This exists
 * to keep the index clean, and nothing about it should be read as a security
 * boundary. Note in particular that disallowing a path does not hide it: a
 * non-production deployment is still publicly reachable by anyone with the URL.
 */
export default function robots(): MetadataRoute.Robots {
  if (!isIndexableSite()) {
    return {
      rules: [{ userAgent: "*", disallow: "/" }],
      // No `sitemap` and no `host`. A sitemap is an invitation to crawl, so
      // advertising one here would undercut the disallow above.
    };
  }

  return {
    rules: [
      {
        userAgent: "*",
        allow: ["/", "/request-pilot", "/signin", "/privacy"],
        disallow: ["/dashboard", "/ops", "/commerce", "/api", "/auth"],
      },
    ],
    sitemap: `${SITE_URL}/sitemap.xml`,
    host: SITE_URL,
  };
}
