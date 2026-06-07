import type { Metadata } from "next";
import { Inter } from "next/font/google";
import { SuperTokensProvider } from "../components/SuperTokensProvider";
import "./globals.css";

// Modern, highly-legible UI typeface loaded via next/font (self-hosted, no
// layout shift). Exposed as `--font-sans` so design tokens and Tailwind's
// font-sans utility resolve to it across the whole app.
const inter = Inter({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-inter",
});

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
    <html lang="en" className={inter.variable}>
      <body className="antialiased">
        <SuperTokensProvider>{children}</SuperTokensProvider>
      </body>
    </html>
  );
}
