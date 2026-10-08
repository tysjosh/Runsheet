import type { Metadata } from "next";

// Object title so child routes keep the " · Runsheet" template.
export const metadata: Metadata = {
  title: { default: "Dispatch", template: "%s · Runsheet" },
};

export default function DispatchLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
