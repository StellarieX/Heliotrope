import type { Metadata } from "next";
import "./globals.css";

const DESCRIPTION =
  "Shift deferrable electricity loads into low-carbon windows, always ready on time. Deadlines and physics are hard constraints; carbon is the objective.";

export const metadata: Metadata = {
  title: "Heliotrope — carbon-aware load scheduling",
  description: DESCRIPTION,
  openGraph: {
    title: "Heliotrope",
    description: DESCRIPTION,
    type: "website",
  },
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
