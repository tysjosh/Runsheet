import type { Metadata } from "next";
import { SuperTokensProvider } from "../components/SuperTokensProvider";
import "./globals.css";

export const metadata: Metadata = {
  title: "Runsheet - Fleet Management Platform",
  description:
    "Advanced fleet tracking and logistics management platform for efficient operations",
  icons: {
    icon: [
      {
        url: "/runsheet_logo.svg",
        type: "image/svg+xml",
      },
    ],
    shortcut: "/runsheet_logo.svg",
    apple: "/runsheet_logo.svg",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className="antialiased">
        <SuperTokensProvider>{children}</SuperTokensProvider>
      </body>
    </html>
  );
}
