"use client";

/**
 * AudienceGuard — keeps customers in the portal and staff out of it.
 *
 * Mounted once in the root layout, inside the SuperTokens provider (customer
 * portal design §10.1). It only routes; the backend's central deny is the
 * security boundary.
 *
 * - The audience resolves on mount and again only when a
 *   `runsheet:session-changed` event arrives (sign-in, sign-out, claims
 *   update, unauthorised). Pathname changes read the cached value and never
 *   trigger a role read, so a staff navigation inside `/dashboard` never
 *   unmounts the shell.
 * - A re-resolution keeps the previous audience visible until the new one
 *   lands; it never goes back to `pending`.
 * - `customer` on a staff prefix goes to `/portal`; `staff` on `/portal` goes
 *   to `/dashboard`. `unknown` and `signed_out` never redirect.
 * - It renders `null` only while pending on a staff or portal path (so a
 *   customer never briefly mounts the staff shell and its WebSockets), and
 *   while its own redirect is in flight.
 */

import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import Session from "supertokens-auth-react/recipe/session";
import { isCustomerRole } from "../config/modules";
import { SESSION_CHANGED_EVENT } from "../config/supertokens";
import { getCurrentUserRoles } from "../utils/auth";

export type Audience =
  | "pending"
  | "customer"
  | "staff"
  | "unknown"
  | "signed_out";

/** Path prefixes of the staff app (design §10.1). */
export const STAFF_PREFIXES = [
  "/dashboard",
  "/admin",
  "/commerce",
  "/compliance",
  "/ops",
  "/orders",
] as const;

export const PORTAL_PREFIX = "/portal";

/** Segment-aware prefix match: `/portal` and `/portal/x`, not `/portalx`. */
function underPrefix(pathname: string, prefix: string): boolean {
  return pathname === prefix || pathname.startsWith(`${prefix}/`);
}

export function isStaffPath(pathname: string): boolean {
  return STAFF_PREFIXES.some((p) => underPrefix(pathname, p));
}

export function isPortalPath(pathname: string): boolean {
  return underPrefix(pathname, PORTAL_PREFIX);
}

/** Where the guard sends this audience from this path, or `null` to stay. */
export function redirectTarget(
  audience: Audience,
  pathname: string,
): string | null {
  if (audience === "customer" && isStaffPath(pathname)) return PORTAL_PREFIX;
  if (audience === "staff" && isPortalPath(pathname)) return "/dashboard";
  return null;
}

/** Resolve the signed-in user's audience from the verified session. */
export async function resolveAudience(): Promise<Audience> {
  try {
    if (!(await Session.doesSessionExist())) return "signed_out";
    const roles = await getCurrentUserRoles();
    if (isCustomerRole(roles)) return "customer";
    if (roles.length > 0) return "staff";
    return "unknown";
  } catch {
    return "unknown";
  }
}

export default function AudienceGuard({
  children,
}: {
  children: React.ReactNode;
}) {
  const router = useRouter();
  const pathname = usePathname() ?? "/";
  const [audience, setAudience] = useState<Audience>("pending");
  // Only the newest resolution may land, so a slow first read can't overwrite
  // the answer to a later session event.
  const generation = useRef(0);

  useEffect(() => {
    let active = true;
    const resolve = () => {
      generation.current += 1;
      const mine = generation.current;
      void resolveAudience().then((next) => {
        if (active && mine === generation.current) setAudience(next);
      });
    };
    resolve();
    window.addEventListener(SESSION_CHANGED_EVENT, resolve);
    return () => {
      active = false;
      window.removeEventListener(SESSION_CHANGED_EVENT, resolve);
    };
  }, []);

  const target = redirectTarget(audience, pathname);

  useEffect(() => {
    if (target) router.replace(target);
  }, [target, router]);

  if (target) return null;
  if (
    audience === "pending" &&
    (isStaffPath(pathname) || isPortalPath(pathname))
  ) {
    return null;
  }
  return <>{children}</>;
}
