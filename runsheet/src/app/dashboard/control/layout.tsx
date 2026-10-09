import type { Metadata } from "next";

export const metadata: Metadata = { title: "Live" };

export default function ControlCenterLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
