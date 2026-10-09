import type { Metadata } from "next";
import PortalGate from "../../components/portal/PortalGate";

/**
 * Customer portal route group (PD20, design §10.2).
 *
 * This server layout only sets the title; the client `PortalGate` owns the
 * session gate (no session → /signin, non-customer → /dashboard) and renders
 * `PortalShell`. Nothing from the staff shell is imported here or below it.
 */

// A plain string title would drop the root "%s · Runsheet" template for
// child routes, so this layout re-declares it for the pages beneath it.
export const metadata: Metadata = {
  title: { default: "Customer portal", template: "%s · Runsheet" },
};

export default function PortalLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <PortalGate>{children}</PortalGate>;
}
