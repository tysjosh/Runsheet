import type { Metadata } from "next";

export const metadata: Metadata = { title: "Dispatch" };

export default function DispatchLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
