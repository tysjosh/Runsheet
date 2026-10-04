import type { Metadata } from "next";

export const metadata: Metadata = { title: "Fuel Ops" };

export default function FuelOpsLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
