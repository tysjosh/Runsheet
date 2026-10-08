"use client";

/**
 * Client gate for `app/portal/` (design §10.2): no session goes to /signin, a
 * non-customer session goes to /dashboard, and a customer gets `PortalShell`
 * with `/api/portal/me` in context. The backend still enforces every rule;
 * this only decides what to render.
 */

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import Session from "supertokens-auth-react/recipe/session";
import { isCustomerRole } from "../../config/modules";
import { configureFormat } from "../../lib/format";
import { ApiError } from "../../services/api";
import { getPortalMe, type PortalMe } from "../../services/portalApi";
import { getCurrentUserRoles } from "../../utils/auth";
import { PortalMeContext } from "./PortalContext";
import PortalShell from "./PortalShell";
import { card, primaryButton } from "./styles";

type GateState =
  | { kind: "checking" }
  | { kind: "redirecting" }
  | { kind: "ready"; me: PortalMe }
  | { kind: "error"; message: string; retry: boolean };

/** Customer-facing text for a failed `/me` load. */
export function portalLoadMessage(error: unknown): {
  message: string;
  retry: boolean;
} {
  if (error instanceof ApiError) {
    if (error.status === 404) {
      return {
        message:
          "The customer portal isn't available right now. Please contact your supplier.",
        retry: false,
      };
    }
    if (error.code === "PORTAL_ACCESS_SUSPENDED") {
      return {
        message:
          "Your portal access is suspended. Please contact your supplier.",
        retry: false,
      };
    }
    if (error.status === 403) {
      return {
        message:
          "This account can't use the customer portal. Please contact your supplier.",
        retry: false,
      };
    }
  }
  return {
    message: "We couldn't load your account. Please try again.",
    retry: true,
  };
}

export default function PortalGate({
  children,
}: {
  children: React.ReactNode;
}) {
  const router = useRouter();
  const [state, setState] = useState<GateState>({ kind: "checking" });

  const load = useCallback(async () => {
    // PD23: portal times use the customer's browser zone, never the tenant
    // zone the staff shell configures.
    configureFormat({ timeZone: undefined });
    setState({ kind: "checking" });
    try {
      if (!(await Session.doesSessionExist())) {
        setState({ kind: "redirecting" });
        router.replace("/signin");
        return;
      }
      const roles = await getCurrentUserRoles();
      if (!isCustomerRole(roles)) {
        setState({ kind: "redirecting" });
        router.replace("/dashboard");
        return;
      }
      const response = await getPortalMe();
      setState({ kind: "ready", me: response.data });
    } catch (error) {
      setState({ kind: "error", ...portalLoadMessage(error) });
    }
  }, [router]);

  useEffect(() => {
    void load();
  }, [load]);

  if (state.kind === "checking" || state.kind === "redirecting") {
    return (
      <div
        role="status"
        className="flex min-h-screen items-center justify-center bg-canvas p-8 text-[15px] text-text-muted"
      >
        Loading your account…
      </div>
    );
  }

  if (state.kind === "error") {
    return (
      <main
        id="main"
        className="flex min-h-screen items-start justify-center bg-canvas px-4 py-16"
      >
        <div className={`${card} w-full max-w-[400px] p-6`}>
          <h1 className="text-xl font-bold text-text">Customer portal</h1>
          <p role="alert" className="mt-3 text-[15px] text-text">
            {state.message}
          </p>
          {state.retry && (
            <button
              type="button"
              className={`${primaryButton} mt-4`}
              onClick={() => void load()}
            >
              Try again
            </button>
          )}
        </div>
      </main>
    );
  }

  return (
    <PortalMeContext.Provider value={state.me}>
      <PortalShell
        supplierName={state.me.supplier_name}
        customerName={state.me.customer_display_name}
        email={state.me.email}
        invoicesAvailable={state.me.invoices_available}
      >
        {children}
      </PortalShell>
    </PortalMeContext.Provider>
  );
}
