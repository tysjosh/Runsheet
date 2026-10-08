"use client";

import { createContext, useContext } from "react";
import type { PortalMe } from "../../services/portalApi";

/** The signed-in customer's `/api/portal/me`, loaded once by the portal gate. */
export const PortalMeContext = createContext<PortalMe | null>(null);

/** Read the portal account; only valid under `app/portal/`. */
export function usePortalMe(): PortalMe {
  const me = useContext(PortalMeContext);
  if (!me) throw new Error("usePortalMe must be used inside the portal layout");
  return me;
}
