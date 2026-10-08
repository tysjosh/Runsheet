"use client";

/**
 * Hub tab state, shared by every hub (R2.3, R2.4).
 *
 * Resolves the session's roles once, filters the hub's tabs through `canSee`
 * (or a custom predicate), and binds the active tab to `?tab=` via
 * `useUrlTab`: restored on load and refresh, legacy values mapped through
 * `aliases`, unknown or hidden values falling back to the first visible tab,
 * changes written with `router.replace`.
 */
import { useEffect, useMemo, useState } from "react";
import { canSee } from "../../config/modules";
import { getCurrentUserRoles } from "../../utils/auth";
import { type TabItem, useUrlTab } from "../ui/Tabs";

export interface HubTabsOptions {
  aliases?: Record<string, string>;
  param?: string;
  /** Override visibility (default: `canSee(tab.id, { roles })`). */
  visible?: (tab: TabItem, roles: readonly string[] | null) => boolean;
  /** Preferred default when the URL names no tab. */
  fallback?: string;
}

export function useSessionRoles(): readonly string[] | null {
  const [roles, setRoles] = useState<readonly string[] | null>(null);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const r = await getCurrentUserRoles();
      if (!cancelled) setRoles(r);
    })();
    return () => {
      cancelled = true;
    };
  }, []);
  return roles;
}

export function useHubTabs<T extends TabItem>(
  all: T[],
  { aliases, param, visible, fallback }: HubTabsOptions = {},
) {
  const roles = useSessionRoles();
  const tabs = useMemo(
    () =>
      all.filter((t) =>
        visible ? visible(t, roles) : canSee(t.id, { roles }),
      ),
    [all, roles, visible],
  );
  const [active, setActive] = useUrlTab(
    tabs.map((t) => t.id),
    { aliases, param, fallback },
  );
  /** True when `id` is the active tab and the user may see it. */
  const shows = (id: string) => active === id && tabs.some((t) => t.id === id);
  return { roles, tabs, active, setActive, shows };
}
