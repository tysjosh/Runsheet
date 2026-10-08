import type { Metadata } from "next";

export const metadata: Metadata = { title: "Pay invoice" };

export default function PortalPayLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
