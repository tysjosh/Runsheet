"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";

/**
 * The top-level ops views are consolidated into the main dashboard
 * shell (Fleet / Dispatch / Fuel Ops / Analytics / Control tabs), so the
 * bare ``/ops`` landing route redirects back to the dashboard.
 *
 * Deeper ops routes — cargo manifests (`/ops/scheduling/:id/cargo`), the
 * command interface (`/ops/command`), fuel tanks/depots, and the scheduling
 * job board the cargo page links back to — are real, deep-linkable pages and
 * must render normally.
 * Previously this layout redirected *every* ``/ops/*`` path, which broke
 * those links (e.g. the cargo link on the job board bounced users to the
 * dashboard).
 */
const REDIRECT_PATHS = new Set(["/ops", "/ops/"]);

export default function OpsLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const shouldRedirect = REDIRECT_PATHS.has(pathname);

  useEffect(() => {
    if (shouldRedirect) {
      router.replace("/dashboard");
    }
  }, [router, shouldRedirect]);

  if (shouldRedirect) {
    return (
      <div className="h-screen flex items-center justify-center bg-gray-50">
        <p className="text-gray-500">Redirecting...</p>
      </div>
    );
  }

  return <>{children}</>;
}
