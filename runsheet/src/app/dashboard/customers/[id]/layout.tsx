import type { Metadata } from "next";

export const metadata: Metadata = { title: "Customer" };

export default function CustomerDetailLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
