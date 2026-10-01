import type { Metadata } from "next";

import { KnowledgeListScreen } from "@/features/knowledge/knowledge";

export const metadata: Metadata = { title: "Knowledge bases" };

export default function KnowledgePage() {
  return <KnowledgeListScreen />;
}
