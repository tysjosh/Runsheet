import type { MetadataRoute } from "next";

/**
 * sitemap.xml
 *
 * Lists only the publicly meaningful routes. The application routes
 * (`/dashboard`, `/ops`, `/commerce`) are deliberately absent: they require a
 * session, so a crawler following them lands on sign-in.
 *
 * `/signin` is included but ranked lowest — it is a legitimate destination for
 * someone searching "runsheet login", and excluding it tends to surface a
 * third-party page for that query instead.
 */
const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL ?? "http://localhost:3000";

export default function sitemap(): MetadataRoute.Sitemap {
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
      url: `${SITE_URL}/signin`,
      lastModified,
      changeFrequency: "yearly",
      priority: 0.3,
    },
  ];
}
