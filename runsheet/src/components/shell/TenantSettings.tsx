"use client";

/**
 * Tenant settings for the dashboard shell (design.md §3.1).
 *
 * The shell loads the account profile once and publishes the tenant time zone
 * here, and also pushes it into `lib/format` so every `format.date/time` call
 * renders in the tenant's zone. The profile has no time-zone field yet, so the
 * browser zone is the fallback (and today's effective value).
 */
import { createContext, useContext, useEffect, useMemo, useState } from "react";
import { configureFormat } from "../../lib/format";
import { apiService } from "../../services/api";

export interface AccountProfile {
  user_id: string;
  email: string;
  tenant_id: string;
  roles: string[];
  has_pii_access: boolean;
  /** Not served yet; read when the backend adds it. */
  time_zone?: string | null;
  timezone?: string | null;
}

export interface TenantSettings {
  timeZone: string;
  profile: AccountProfile | null;
}

const browserZone = () => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
};

const TenantSettingsContext = createContext<TenantSettings>({
  timeZone: "UTC",
  profile: null,
});

export function TenantSettingsProvider({
  children,
  enabled = true,
}: {
  children: React.ReactNode;
  /** Only fetch once the session is known to exist. */
  enabled?: boolean;
}) {
  const [profile, setProfile] = useState<AccountProfile | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    (async () => {
      try {
        const p =
          (await apiService.getAccountProfile()) as AccountProfile | null;
        if (!cancelled) setProfile(p);
      } catch {
        // Non-fatal: the browser zone stays in effect.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [enabled]);

  const value = useMemo<TenantSettings>(() => {
    const tz = profile?.time_zone || profile?.timezone || browserZone();
    return { timeZone: tz, profile };
  }, [profile]);

  useEffect(() => {
    configureFormat({ timeZone: value.timeZone });
  }, [value.timeZone]);

  return (
    <TenantSettingsContext.Provider value={value}>
      {children}
    </TenantSettingsContext.Provider>
  );
}

export function useTenantSettings(): TenantSettings {
  return useContext(TenantSettingsContext);
}
