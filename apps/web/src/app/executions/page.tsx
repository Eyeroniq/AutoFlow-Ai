import type { Metadata } from "next";

import { ExecutionsScreen } from "@/features/executions/executions-list";

export const metadata: Metadata = { title: "Executions" };

export default function ExecutionsPage() {
  return <ExecutionsScreen />;
}
