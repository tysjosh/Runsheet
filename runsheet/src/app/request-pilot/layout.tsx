import type { Metadata } from "next";

export const metadata: Metadata = { title: "Request a Pilot" };

export default function RequestPilotLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
