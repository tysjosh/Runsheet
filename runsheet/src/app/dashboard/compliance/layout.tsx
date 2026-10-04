import type { Metadata } from "next";

export const metadata: Metadata = { title: "Compliance" };

export default function ComplianceLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
