import type { Metadata } from "next";

export const metadata: Metadata = { title: "Command Interface" };

export default function CommandInterfaceLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
