import type { Metadata } from "next";
import { Geist } from "next/font/google";
import "./globals.css";

import { Toaster } from "@/components/ui/toast";

import { Providers } from "./providers";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: { default: "FlowForge AI", template: "%s · FlowForge AI" },
  description: "Build, run, and monitor AI workflows visually.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} h-full antialiased`}
    >
      <body className="flex min-h-full flex-col">
        <Providers>{children}</Providers>
        <Toaster />
      </body>
    </html>
  );
}
