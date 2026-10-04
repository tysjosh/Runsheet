import type { Metadata } from "next";

export const metadata: Metadata = { title: "Today" };

export default function TodayLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
