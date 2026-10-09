import type { Metadata } from "next";

// Object title so child routes keep the " · Runsheet" template.
export const metadata: Metadata = {
  title: { default: "Compliance", template: "%s · Runsheet" },
};

export default function ComplianceLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
