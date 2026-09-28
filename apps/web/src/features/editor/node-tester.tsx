"use client";

import { useMutation } from "@tanstack/react-query";
import { FlaskConical, TriangleAlert } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status";
import { api } from "@/lib/api";
import { formatDuration, prettyJson } from "@/lib/format";

import { OutputView } from "../runs/output-view";
import { ancestorsOf, outputKeysFor } from "./graph";
import { getEditorStore, useEditor } from "./store";
import { useEditorUi } from "./ui-store";

function JsonBlock({ label, value }: { label: string; value: unknown }) {
  return (
    <div>
      <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-400">{label}</p>
      <pre className="max-h-48 overflow-auto rounded-md bg-slate-900 p-2 text-[11px] leading-4 text-slate-100">{prettyJson(value)}</pre>
    </div>
  );
}

/** Runs this node alone against sample upstream outputs (POST .../nodes/{key}/test). */
export function NodeTester({ nodeId }: { nodeId: string }) {
  const workflowId = useEditor((s) => s.workflowId);
  const nodeType = useEditor((s) => s.nodes.find((n) => n.id === nodeId)?.data.nodeType);
  const edges = useEditor((s) => s.edges);
  const runNodes = useEditor((s) => s.run.nodes);
  const nodes = useEditor((s) => s.nodes);
  const catalog = useEditor((s) => s.catalog);
  const testRequest = useEditorUi((s) => s.testRequest);
  const section = useRef<HTMLDetailsElement>(null);
  const upstream = useMemo(() => [...ancestorsOf(nodeId, edges)], [nodeId, edges]);

  // Prefill with the last run's outputs where there are any, else each node's output keys.
  const buildSample = () =>
    JSON.stringify(
      Object.fromEntries(
        upstream.map((id) => {
          const node = nodes.find((n) => n.id === id);
          const skeleton = node ? Object.fromEntries(outputKeysFor(node, catalog).map((key) => [key, ""])) : {};
          return [id, runNodes[id]?.output ?? skeleton];
        }),
      ),
      null,
      2,
    );
  const upstreamKey = upstream.join("|");
  const [sample, setSample] = useState(buildSample);
  const [inputs, setInputs] = useState("{}");
  const [parseError, setParseError] = useState<string | null>(null);
  // Re-prefill only when the set of upstream nodes changes; don't clobber edits on every event.
  const [sampleFor, setSampleFor] = useState(upstreamKey);
  if (sampleFor !== upstreamKey) {
    setSampleFor(upstreamKey);
    setSample(buildSample());
  }

  useEffect(() => {
    if (!testRequest || !section.current) return;
    section.current.open = true;
    section.current.scrollIntoView({ block: "nearest" });
  }, [testRequest]);

  const test = useMutation({
    meta: { silent: true },
    mutationFn: async () => {
      let upstreamOutputs: Record<string, Record<string, unknown>>;
      let runInputs: Record<string, unknown>;
      try {
        upstreamOutputs = JSON.parse(sample || "{}");
        runInputs = JSON.parse(inputs || "{}");
      } catch {
        throw new Error("Sample data must be valid JSON.");
      }
      // The endpoint runs the node from the saved graph; save edits (a new node) first.
      const store = getEditorStore().getState();
      if (!(await store.saveNow())) throw new Error(`Save failed: ${getEditorStore().getState().save.error}`);
      const node = getEditorStore().getState().nodes.find((n) => n.id === nodeId);
      return api.workflows.testNode(workflowId!, nodeId, {
        config: node?.data.config,
        upstream_outputs: upstreamOutputs,
        inputs: runInputs,
      });
    },
  });

  const onSampleChange = (value: string, set: (v: string) => void) => {
    set(value);
    try {
      JSON.parse(value || "{}");
      setParseError(null);
    } catch {
      setParseError("Not valid JSON");
    }
  };

  const result = test.data;
  return (
    <details ref={section} className="group rounded-lg border border-slate-200" data-testid="node-tester">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-xs font-semibold text-slate-700">
        <FlaskConical className="size-4 text-slate-500" aria-hidden /> Test node
        <span className="ml-auto text-[11px] font-normal text-slate-400 group-open:hidden">Run just this node with sample data</span>
      </summary>
      <div className="space-y-3 border-t border-slate-100 p-3">
        {nodeType === "gmail" && (
          <p className="flex gap-1.5 rounded-md bg-amber-50 p-2 text-[11px] text-amber-800">
            <TriangleAlert className="size-3.5 shrink-0" aria-hidden /> This sends a real email (unless auth is “mock”).
          </p>
        )}
        <label className="block">
          <span className="mb-1 block text-[11px] font-medium text-slate-600">
            Upstream outputs {upstream.length ? `(${upstream.join(", ")})` : "(none)"}
          </span>
          <textarea
            value={sample}
            aria-label="Upstream outputs (JSON)"
            onChange={(e) => onSampleChange(e.target.value, setSample)}
            rows={Math.min(8, 2 + upstream.length * 2)}
            spellCheck={false}
            className="nodrag block w-full rounded-md border border-slate-300 px-2 py-1.5 font-mono text-[11px] focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-100"
          />
        </label>
        {nodeType === "input" && (
          <label className="block">
            <span className="mb-1 block text-[11px] font-medium text-slate-600">Run inputs</span>
            <textarea
              value={inputs}
              aria-label="Run inputs (JSON)"
              onChange={(e) => onSampleChange(e.target.value, setInputs)}
              rows={2}
              spellCheck={false}
              className="nodrag block w-full rounded-md border border-slate-300 px-2 py-1.5 font-mono text-[11px] focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-100"
            />
          </label>
        )}
        {parseError && <p className="text-[11px] text-red-600">{parseError}</p>}
        <Button size="sm" onClick={() => test.mutate()} loading={test.isPending} disabled={Boolean(parseError)} data-testid="run-node-test">
          Run test
        </Button>
        {test.isError && <p className="text-[11px] text-red-600">{test.error instanceof Error ? test.error.message : "Test failed"}</p>}
        {result && (
          <div className="space-y-2" data-testid="node-test-result">
            <div className="flex items-center gap-2 text-xs">
              <StatusBadge status={result.status} />
              <span className="text-slate-500">{formatDuration(result.duration_ms)}</span>
            </div>
            {result.error && <p className="rounded-md bg-red-50 p-2 text-[11px] text-red-700">{result.error}</p>}
            <JsonBlock label="Input (resolved)" value={result.input} />
            <OutputView output={result.output} />
          </div>
        )}
      </div>
    </details>
  );
}
