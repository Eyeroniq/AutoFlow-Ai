import type { Metadata } from "next";

import { ExecutionDetailScreen } from "@/features/executions/execution-detail";

export const metadata: Metadata = { title: "Execution" };

export default async function ExecutionPage({ params }: PageProps<"/executions/[id]">) {
  const { id } = await params;
  return <ExecutionDetailScreen executionId={id} />;
}
