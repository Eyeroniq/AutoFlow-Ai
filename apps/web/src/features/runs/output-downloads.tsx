"use client";

import { Download } from "lucide-react";
import { useState } from "react";

import { toast } from "@/components/ui/toast";
import { api } from "@/lib/api";

/** Download the run's final output as JSON, or as CSV (one row per record, e.g. per entity). */
export function OutputDownloads({ executionId }: { executionId: string }) {
  const [busy, setBusy] = useState<"json" | "csv" | null>(null);
  const download = async (format: "json" | "csv") => {
    setBusy(format);
    try {
      await api.executions.downloadOutput(executionId, format);
    } catch (error) {
      toast.error("Download failed", error instanceof Error ? error.message : undefined);
    } finally {
      setBusy(null);
    }
  };
  return (
    <span className="flex items-center gap-1">
      {(["json", "csv"] as const).map((format) => (
        <button
          key={format}
          type="button"
          onClick={() => void download(format)}
          disabled={busy !== null}
          className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium text-slate-500 hover:bg-slate-100 hover:text-indigo-600 disabled:opacity-50"
          data-testid={`download-${format}`}
          title={format === "csv" ? "One row per record (e.g. per extracted entity)" : "The final output as JSON"}
        >
          <Download className="size-3" aria-hidden /> {format.toUpperCase()}
        </button>
      ))}
    </span>
  );
}
