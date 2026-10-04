import type { Metadata } from "next";

export const metadata: Metadata = { title: "Commerce Customer" };

export default function CommerceCustomerDetailLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
