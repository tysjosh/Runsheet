import type { Metadata } from "next";

export const metadata: Metadata = { title: "Tank" };

export default function TankDetailLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
