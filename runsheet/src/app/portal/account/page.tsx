"use client";

/**
 * Account (PE6, accepted as v1.1): read-only, from `/api/portal/me` only. The
 * signed-in email, the customer, the supplier and Sign out. No profile
 * editing and no other portal users here; changes go through the supplier.
 */
import { LogOut } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { usePortalMe } from "../../../components/portal/PortalContext";
import PortalTitleRow from "../../../components/portal/PortalTitleRow";
import {
  listSection,
  secondaryButton,
  space,
} from "../../../components/portal/styles";
import { signOut } from "../../../utils/auth";

function Row({ term, children }: { term: string; children: React.ReactNode }) {
  return (
    <div
      className={`border-t border-slate-100 first:border-t-0 sm:grid sm:grid-cols-[180px_1fr] sm:gap-4 md:px-4 ${space.rowY}`}
    >
      <dt className="text-sm text-text-muted">{term}</dt>
      <dd className="break-words text-[15px] font-medium text-text">
        {children}
      </dd>
    </div>
  );
}

export default function PortalAccountPage() {
  const me = usePortalMe();
  const router = useRouter();
  const [signingOut, setSigningOut] = useState(false);

  const handleSignOut = async () => {
    if (signingOut) return;
    setSigningOut(true);
    try {
      await signOut();
    } finally {
      router.replace("/signin");
    }
  };

  return (
    <>
      <PortalTitleRow title="Account" />
      <div className="space-y-6 md:space-y-4">
        <section
          aria-labelledby="account-heading"
          data-portal-first
          className={listSection}
        >
          <h2 id="account-heading" className="sr-only">
            Your account
          </h2>
          <dl>
            <Row term="Signed in as">{me.email || "—"}</Row>
            <Row term="Customer">{me.customer_display_name || "—"}</Row>
            <Row term="Supplier">{me.supplier_name}</Row>
            <Row term="Units">
              {me.measurement_units.volume === "gal" ? "Gallons" : "Litres"}
            </Row>
          </dl>
        </section>
        <p className="text-sm text-text-muted">
          To change your email, add someone from your team or update your
          details, contact {me.supplier_name}.
        </p>
        <button
          type="button"
          className={secondaryButton}
          onClick={() => void handleSignOut()}
          aria-disabled={signingOut || undefined}
        >
          <LogOut aria-hidden="true" className="h-4 w-4" />
          Sign out
        </button>
      </div>
    </>
  );
}
