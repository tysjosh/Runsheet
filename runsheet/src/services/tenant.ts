const TENANT_STORAGE_KEYS = ["tenant_id", "tenantId", "runsheet.tenant_id"];

function readBrowserTenantId(): string | null {
  if (typeof window === "undefined") {
    return null;
  }

  for (const key of TENANT_STORAGE_KEYS) {
    const value =
      window.localStorage.getItem(key) ?? window.sessionStorage.getItem(key);
    if (value?.trim()) {
      return value.trim();
    }
  }

  return null;
}

export function getCurrentTenantId(): string {
  const configured = process.env.NEXT_PUBLIC_TENANT_ID?.trim();
  const tenantId = configured || readBrowserTenantId();

  if (!tenantId) {
    throw new Error(
      "Missing tenant context. Set NEXT_PUBLIC_TENANT_ID or store tenant_id after authentication.",
    );
  }

  return tenantId;
}
