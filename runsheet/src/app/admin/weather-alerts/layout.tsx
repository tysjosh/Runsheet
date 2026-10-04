import type { Metadata } from "next";

export const metadata: Metadata = { title: "Weather Alerts" };

export default function WeatherAlertsLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
