import type { Metadata } from "next";

export const metadata: Metadata = { title: "Operations Control" };

export default function OperationsControlLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
