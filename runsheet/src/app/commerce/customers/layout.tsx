import type { Metadata } from "next";

// A plain string title would drop the root "%s · Runsheet" template for
// child routes, so this layout re-declares it for the pages beneath it.
export const metadata: Metadata = {
  title: { default: "Commerce Customers", template: "%s · Runsheet" },
};

export default function CommerceCustomersLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
