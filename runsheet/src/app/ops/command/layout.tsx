"use client";

/**
 * Layout for the /ops/command route.
 *
 * Overrides the parent ops layout redirect so the Command Interface
 * page renders directly without being redirected to the main page.
 */
export default function CommandLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
