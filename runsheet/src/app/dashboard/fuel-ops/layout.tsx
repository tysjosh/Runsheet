import type { Metadata } from "next";

export const metadata: Metadata = { title: "Fuel" };

export default function FuelOpsLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
