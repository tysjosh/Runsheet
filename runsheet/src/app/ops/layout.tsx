"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

/**
 * Ops routes are now consolidated into the main page.
 * This layout redirects any direct /ops/* URL access back to the main page.
 */
export default function OpsLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();

  useEffect(() => {
    router.replace("/");
  }, [router]);

  return (
    <div className="h-screen flex items-center justify-center bg-gray-50">
      <p className="text-gray-500">Redirecting...</p>
    </div>
  );
}
