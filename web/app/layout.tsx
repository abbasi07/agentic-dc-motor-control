import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "Control Design Copilot",
  description:
    "Chat-first adaptive DC-motor controller design (simulation only — no hardware).",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="font-sans">{children}</body>
    </html>
  );
}
