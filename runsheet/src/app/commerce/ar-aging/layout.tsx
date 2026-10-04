import type { Metadata } from "next";

export const metadata: Metadata = { title: "AR Aging" };

export default function ArAgingLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
