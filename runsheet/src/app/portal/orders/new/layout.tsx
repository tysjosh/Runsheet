import type { Metadata } from "next";

export const metadata: Metadata = { title: "Request a delivery" };

export default function PortalNewOrderLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
