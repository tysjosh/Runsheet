import type { Metadata } from "next";

export const metadata: Metadata = { title: "Control Center" };

export default function ControlCenterLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
