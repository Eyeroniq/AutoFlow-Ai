import type { Metadata } from "next";

import { DashboardScreen } from "@/features/dashboard/dashboard";

export const metadata: Metadata = { title: "Dashboard" };

export default function DashboardPage() {
  return <DashboardScreen />;
}
