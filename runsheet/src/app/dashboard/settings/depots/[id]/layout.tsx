import type { Metadata } from "next";

export const metadata: Metadata = { title: "Depot" };

export default function DepotDetailLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
