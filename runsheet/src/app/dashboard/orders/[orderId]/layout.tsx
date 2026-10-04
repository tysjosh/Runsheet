import type { Metadata } from "next";

export const metadata: Metadata = { title: "Order" };

export default function OrderLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
