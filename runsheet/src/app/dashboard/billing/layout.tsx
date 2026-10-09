import type { Metadata } from "next";

// Object title so child routes keep the " · Runsheet" template.
export const metadata: Metadata = {
  title: { default: "Billing", template: "%s · Runsheet" },
};

export default function BillingLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
