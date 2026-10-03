import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Heliotrope",
  description: "Heliotrope — coming soon",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="h-full bg-black antialiased">
      <body className="min-h-full bg-black text-white">{children}</body>
    </html>
  );
}
