import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "GIS Mapper by buck0001",
  description:
    "Automated GIS analysis platform: AOI, DEM, terrain, hydrology, maps and downloads.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body>{children}</body>
    </html>
  );
}
