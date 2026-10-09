import type { Metadata } from "next";

export const metadata: Metadata = { title: "Terminal" };

export default function TerminalDetailLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
