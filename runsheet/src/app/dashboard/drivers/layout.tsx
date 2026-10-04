import type { Metadata } from "next";

export const metadata: Metadata = { title: "Drivers" };

export default function DriversLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
