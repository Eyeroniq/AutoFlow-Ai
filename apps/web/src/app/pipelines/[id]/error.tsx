"use client";

import Link from "next/link";
import { useEffect } from "react";

import { ErrorAlert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";

// Error boundary for the editor: a crash here never loses the saved graph (edits autosave
// within a second), so retrying reloads the pipeline from the server.
export default function EditorError({ error, retry }: { error: Error & { digest?: string }; retry: () => void }) {
  useEffect(() => {
    console.error(error);
  }, [error]);
  return (
    <div className="flex flex-1 items-center justify-center p-8">
      <div className="max-w-lg space-y-4">
        <h1 className="text-lg font-semibold text-slate-900">The editor hit an unexpected error</h1>
        <ErrorAlert message={error.message || "Unknown error"} />
        <p className="text-sm text-slate-500">Your last autosaved version is safe on the server.</p>
        <div className="flex gap-2">
          <Button onClick={() => retry()}>Reload the editor</Button>
          <Link href="/dashboard" className="rounded-lg px-4 py-2.5 text-sm font-semibold text-indigo-600 hover:bg-indigo-50">
            Back to dashboard
          </Link>
        </div>
      </div>
    </div>
  );
}
