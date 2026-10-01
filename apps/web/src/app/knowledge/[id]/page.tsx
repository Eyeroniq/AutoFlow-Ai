import type { Metadata } from "next";

import { KnowledgeDetailScreen } from "@/features/knowledge/knowledge";

export const metadata: Metadata = { title: "Knowledge base" };

export default async function KnowledgeBasePage({ params }: PageProps<"/knowledge/[id]">) {
  const { id } = await params;
  return <KnowledgeDetailScreen id={id} />;
}
