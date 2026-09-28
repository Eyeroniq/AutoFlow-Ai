import type { Metadata } from "next";

import { EditorScreen } from "@/features/editor/editor-screen";

export const metadata: Metadata = { title: "Editor" };

export default async function PipelinePage({ params }: PageProps<"/pipelines/[id]">) {
  const { id } = await params;
  return <EditorScreen workflowId={id} />;
}
