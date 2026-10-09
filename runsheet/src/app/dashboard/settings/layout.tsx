import type { Metadata } from "next";

// Object title so child routes (depot detail) keep the " · Runsheet" template.
export const metadata: Metadata = {
  title: { default: "Settings", template: "%s · Runsheet" },
};

export default function SettingsLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
