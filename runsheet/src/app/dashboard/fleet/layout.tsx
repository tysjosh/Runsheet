import type { Metadata } from "next";

export const metadata: Metadata = { title: "Fleet" };

export default function FleetLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
