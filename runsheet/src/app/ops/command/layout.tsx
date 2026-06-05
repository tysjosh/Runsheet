"use client";

/**
 * Layout for the /ops/command route.
 *
 * The parent ops layout only redirects the bare ``/ops`` landing route,
 * so ``/ops/command`` already renders directly. This pass-through layout
 * is kept as an explicit boundary for the Command Interface in case the
 * parent redirect rules change in the future.
 */
export default function CommandLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
