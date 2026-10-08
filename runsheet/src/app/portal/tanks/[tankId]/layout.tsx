import type { Metadata } from "next";

export const metadata: Metadata = { title: "Your tank" };

export default function PortalTankLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
