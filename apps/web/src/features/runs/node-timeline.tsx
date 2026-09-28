"use client";

import { ChevronDown, ChevronRight } from "lucide-react";
import { useState } from "react";

import { StatusDot } from "@/components/ui/status";
import { formatDuration, formatTime, prettyJson } from "@/lib/format";

import { OutputView } from "./output-view";
import { type NodeRun, orderedNodes, type RunState } from "./run-state";

function Json({ label, value }: { label: string; value: unknown }) {
  if (value === null || value === undefined) return null;
  return (
    <div className="min-w-0">
      <p className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400">{label}</p>
      <pre className="max-h-56 overflow-auto rounded-md bg-slate-900 p-2 text-[11px] leading-4 text-slate-100">{prettyJson(value)}</pre>
    </div>
  );
}

function Row({ node, defaultOpen }: { node: NodeRun; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const streaming = node.status === "running" && node.tokens;
  const expandable = Boolean(node.input || node.output || node.error || node.tokens);
  return (
    <li className="rounded-lg border border-slate-200 bg-white" data-testid={`timeline-${node.key}`} data-status={node.status}>
      <button
        type="button"
        onClick={() => expandable && setOpen((v) => !v)}
        className="flex w-full items-center gap-2.5 px-3 py-2 text-left"
        aria-expanded={open}
      >
        {expandable ? (
          open ? <ChevronDown className="size-3.5 text-slate-400" /> : <ChevronRight className="size-3.5 text-slate-400" />
        ) : (
          <span className="size-3.5" />
        )}
        <StatusDot status={node.status} />
        <span className="min-w-0 flex-1 truncate text-sm font-medium text-slate-800">
          {node.label} <span className="font-mono text-[11px] font-normal text-slate-400">{node.key}</span>
        </span>
        {node.queue && (
          <span
            className="hidden max-w-44 truncate rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-500 md:inline"
            title={node.worker ? `Ran on ${node.worker} (queue "${node.queue}")` : `Queue "${node.queue}"`}
            data-testid={`ran-on-${node.key}`}
          >
            {node.queue}
            {node.worker ? ` · ${node.worker}` : ""}
          </span>
        )}
        <span className="text-[11px] text-slate-500">{node.status}</span>
        <span className="w-16 text-right font-mono text-[11px] text-slate-500">{formatDuration(node.durationMs)}</span>
        <span className="hidden w-20 text-right font-mono text-[11px] text-slate-400 sm:inline">{formatTime(node.startedAt)}</span>
      </button>
      {(open || streaming) && (
        <div className="space-y-2 border-t border-slate-100 px-3 py-2.5">
          {streaming && (
            <div>
              <p className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-blue-500">
                Streaming{node.tokenProvider ? ` from ${node.tokenProvider}` : ""}…
              </p>
              <p className="whitespace-pre-wrap rounded-md bg-blue-50 p-2 text-xs text-blue-900" data-testid={`stream-${node.key}`}>
                {node.tokens}
              </p>
            </div>
          )}
          {open && node.error && (
            <p className={`rounded-md p-2 text-xs ${node.status === "failed" ? "bg-red-50 text-red-700" : "bg-slate-50 text-slate-600"}`}>{node.error}</p>
          )}
          {open && (
            <div className="grid gap-2 lg:grid-cols-2">
              <Json label="Input" value={node.input} />
              <OutputView output={node.output} />
            </div>
          )}
        </div>
      )}
    </li>
  );
}

/** Per-node timeline for a run: status, duration, input, output, error, live tokens. */
export function NodeTimeline({ run, expandFailed = true }: { run: RunState; expandFailed?: boolean }) {
  const nodes = orderedNodes(run);
  if (!nodes.length) return <p className="text-sm text-slate-500">No nodes.</p>;
  return (
    <ol className="space-y-1.5">
      {nodes.map((node) => (
        <Row key={node.key} node={node} defaultOpen={expandFailed && node.status === "failed"} />
      ))}
    </ol>
  );
}
