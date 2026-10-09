import type { MetadataRoute } from "next";

import { isIndexableSite, SITE_URL } from "@/config/site";

/**
 * sitemap.xml
 *
 * Empty on a non-production deployment. A sitemap is a crawl invitation listing
 * absolute URLs, so a staging sitemap actively asks search engines to index
 * staging — which is what it did before `isIndexableSite` gated it.
 *
 * On production it lists only the publicly meaningful routes. The application
 * routes (`/dashboard`, `/ops`, `/commerce`) are deliberately absent: they
 * require a session, so a crawler following them lands on sign-in.
 *
 * `/signin` is included but ranked lowest — it is a legitimate destination for
 * someone searching "runsheet login", and excluding it tends to surface a
 * third-party page for that query instead.
 */
export default function sitemap(): MetadataRoute.Sitemap {
  if (!isIndexableSite()) return [];

  const lastModified = new Date();
  return [
    {
      url: SITE_URL,
      lastModified,
      changeFrequency: "monthly",
      priority: 1,
    },
    {
      url: `${SITE_URL}/request-pilot`,
      lastModified,
      changeFrequency: "monthly",
      priority: 0.8,
    },
    {
      url: `${SITE_URL}/privacy`,
      lastModified,
      changeFrequency: "yearly",
      priority: 0.3,
    },
    {
      url: `${SITE_URL}/signin`,
      lastModified,
      changeFrequency: "yearly",
      priority: 0.3,
    },
  ];
}
