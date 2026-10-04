import type { Metadata } from "next";

export const metadata: Metadata = { title: "Cargo Manifest" };

export default function CargoManifestLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
