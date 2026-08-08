/**
 * Site identity and the single decision about whether this deployment may be
 * indexed by search engines.
 *
 * ---------------------------------------------------------------------------
 * Why this exists
 * ---------------------------------------------------------------------------
 * Indexability was declared in THREE places, all derived from
 * `NEXT_PUBLIC_SITE_URL` and none of them environment-aware:
 *
 *   app/robots.ts    Allow: / , /request-pilot , /signin  + a sitemap pointer
 *   app/sitemap.ts   absolute URLs for all three routes
 *   app/layout.tsx   robots: { index: true, follow: true }
 *
 * Because `deploy-ui` sets `NEXT_PUBLIC_SITE_URL` to the staging host, staging
 * served this, verbatim, to any crawler that found it:
 *
 *   $ curl https://app.staging.runsheetops.com/robots.txt
 *   User-Agent: *
 *   Allow: /
 *   Allow: /request-pilot
 *   ...
 *   Sitemap: https://app.staging.runsheetops.com/sitemap.xml
 *
 * A crawler reaching staging would index a non-production environment and
 * compete with production for the same queries. Gating one of the three would
 * not fix it: `<meta name="robots">` and robots.txt are read independently, and
 * a sitemap is itself a crawl invitation.
 *
 * ---------------------------------------------------------------------------
 * Fail closed
 * ---------------------------------------------------------------------------
 * The test is an allowlist of production hosts, not a denylist of known
 * non-production ones. Anything unrecognised — staging, localhost, a preview
 * deployment, a hostname nobody has thought of yet — is therefore NOT indexable.
 * Getting that wrong in the safe direction costs nothing; getting it wrong the
 * other way is discovered from a search result, months later.
 *
 * `NEXT_PUBLIC_SITE_URL` is inlined at BUILD time, so this resolves per image
 * rather than per request. That is a feature here: the staging image cannot be
 * flipped to indexable by an environment change.
 */

/** Absolute origin of this deployment. Localhost keeps dev and CI quiet. */
export const SITE_URL =
  process.env.NEXT_PUBLIC_SITE_URL ?? "http://localhost:3000";

/**
 * Hosts permitted to be indexed.
 *
 * Staging deliberately runs on `app.staging.runsheetops.com`, so it cannot
 * match. The apex and `www` are listed against the marketing site later moving
 * off the `app.` host.
 */
export const INDEXABLE_HOSTS: readonly string[] = [
  "app.runsheetops.com",
  "runsheetops.com",
  "www.runsheetops.com",
];

/**
 * Whether search engines may index this deployment.
 *
 * Parses `SITE_URL` rather than matching substrings: a substring test for
 * "runsheetops.com" would also match `app.staging.runsheetops.com`, which is the
 * exact host this must exclude. Returns `false` on an unparseable value, keeping
 * the fail-closed property.
 */
export function isIndexableSite(siteUrl: string = SITE_URL): boolean {
  try {
    return INDEXABLE_HOSTS.includes(new URL(siteUrl).host.toLowerCase());
  } catch {
    return false;
  }
}
