"use client";

import { useReactFlow } from "@xyflow/react";
import { CircleAlert, CircleCheck, Plus, RefreshCw, Trash2, X } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import type { VariableType, WorkflowVariable } from "@/lib/types";

import { IDENTIFIER } from "./graph";
import { getEditorStore, useEditor } from "./store";
import { useEditorUi } from "./ui-store";

function PanelHeader({ title, onClose }: { title: string; onClose: () => void }) {
  return (
    <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
      <h2 className="text-sm font-semibold text-slate-900">{title}</h2>
      <button type="button" onClick={onClose} className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700" aria-label="Close panel">
        <X className="size-4" />
      </button>
    </div>
  );
}

interface Row extends WorkflowVariable {
  rowId: number;
}

function rowProblem(row: Row, rows: Row[]): string | null {
  if (!row.key) return "Key required";
  if (!IDENTIFIER.test(row.key)) return "Letters, digits, _ and - only";
  if (rows.filter((r) => r.key === row.key).length > 1) return "Duplicate key";
  return null;
}

let nextRowId = 1;

/** Workflow variables ({{vars.<key>}}), saved with the graph through the workflow API. */
export function VariablesPanel() {
  const variables = useEditor((s) => s.variables);
  const formEpoch = useEditor((s) => s.formEpoch);
  return <VariablesEditor key={formEpoch} initial={variables} />;
}

function VariablesEditor({ initial }: { initial: WorkflowVariable[] }) {
  const [rows, setRows] = useState<Row[]>(() => initial.map((v) => ({ ...v, rowId: nextRowId++ })));
  const close = () => useEditorUi.getState().set({ rightPanel: null });

  const commit = (next: Row[]) => {
    setRows(next);
    // Only valid, unique keys reach the graph (the API rejects invalid ones); the rest
    // stay here, flagged, until fixed.
    const valid = next.filter((row) => !rowProblem(row, next)).map(({ key, value, type }) => ({ key, value, type }));
    getEditorStore().getState().setVariables(valid);
  };
  const update = (rowId: number, patch: Partial<WorkflowVariable>) => commit(rows.map((r) => (r.rowId === rowId ? { ...r, ...patch } : r)));

  return (
    <aside className="flex w-96 shrink-0 flex-col border-l border-slate-200 bg-white" aria-label="Variables" data-testid="variables-panel">
      <PanelHeader title="Workflow variables" onClose={close} />
      <div className="flex-1 space-y-3 overflow-y-auto p-4">
        <p className="text-xs text-slate-500">
          Use them anywhere as <code className="rounded bg-slate-100 px-1">{"{{vars.key}}"}</code>. They&apos;re saved with the workflow.
        </p>
        {rows.length === 0 && <p className="text-sm text-slate-400">No variables yet.</p>}
        {rows.map((row) => {
          const problem = rowProblem(row, rows);
          return (
            <div key={row.rowId} className="space-y-1.5 rounded-lg border border-slate-200 p-2.5" data-testid="variable-row">
              <div className="flex gap-1.5">
                <input
                  value={row.key}
                  onChange={(e) => update(row.rowId, { key: e.target.value })}
                  placeholder="key"
                  aria-label="Variable key"
                  aria-invalid={Boolean(problem) || undefined}
                  className={`nodrag w-32 rounded-md border px-2 py-1 font-mono text-xs focus:outline-none focus:ring-2 ${problem ? "border-red-400 focus:ring-red-100" : "border-slate-300 focus:ring-indigo-100"}`}
                />
                <select
                  value={row.type}
                  onChange={(e) => update(row.rowId, { type: e.target.value as VariableType })}
                  aria-label="Variable type"
                  className="nodrag rounded-md border border-slate-300 px-1.5 py-1 text-xs"
                >
                  <option value="workflow">workflow</option>
                  <option value="environment">environment</option>
                </select>
                <button type="button" onClick={() => commit(rows.filter((r) => r.rowId !== row.rowId))} className="ml-auto rounded p-1 text-slate-400 hover:bg-red-50 hover:text-red-600" aria-label={`Delete variable ${row.key}`}>
                  <Trash2 className="size-3.5" />
                </button>
              </div>
              <input
                value={row.value ?? ""}
                onChange={(e) => update(row.rowId, { value: e.target.value })}
                placeholder="value"
                aria-label={`Value of ${row.key || "variable"}`}
                className="nodrag w-full rounded-md border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-indigo-100"
              />
              {problem && <p className="text-[11px] text-red-600">{problem}: not saved until fixed.</p>}
            </div>
          );
        })}
        <Button variant="secondary" size="sm" onClick={() => commit([...rows, { key: "", value: "", type: "workflow", rowId: nextRowId++ }])}>
          <Plus className="size-3.5" aria-hidden /> Add variable
        </Button>
      </div>
    </aside>
  );
}

/** Backend validation issues; clicking one selects and centers its node. */
export function ValidationPanel({ onValidate }: { onValidate: () => void }) {
  const issues = useEditor((s) => s.issues);
  const nodes = useEditor((s) => s.nodes);
  const { fitView } = useReactFlow();
  const labels = new Map(nodes.map((n) => [n.id, n.data.label]));
  const close = () => useEditorUi.getState().set({ rightPanel: null });

  const jump = (nodeId: string) => {
    getEditorStore().getState().selectOnly(nodeId);
    void fitView({ nodes: [{ id: nodeId }], duration: 300, maxZoom: 1.2, padding: 0.8 });
    useEditorUi.getState().set({ rightPanel: null });
  };

  return (
    <aside className="flex w-96 shrink-0 flex-col border-l border-slate-200 bg-white" aria-label="Validation" data-testid="validation-panel">
      <PanelHeader title="Validation" onClose={close} />
      <div className="flex items-center justify-between border-b border-slate-100 px-4 py-2">
        <p className="text-xs text-slate-500">{issues.length ? `${issues.length} problem${issues.length === 1 ? "" : "s"} would stop a run` : "Checked by the server"}</p>
        <Button variant="ghost" size="sm" onClick={onValidate}>
          <RefreshCw className="size-3.5" aria-hidden /> Re-check
        </Button>
      </div>
      <div className="flex-1 overflow-y-auto p-3">
        {issues.length === 0 ? (
          <p className="flex items-center gap-2 rounded-lg bg-emerald-50 p-3 text-sm text-emerald-700">
            <CircleCheck className="size-4" aria-hidden /> Everything checks out. Ready to run.
          </p>
        ) : (
          <ul className="space-y-2">
            {issues.map((issue, index) => (
              <li key={`${issue.code}-${index}`}>
                <button
                  type="button"
                  disabled={!issue.node_id || !labels.has(issue.node_id)}
                  onClick={() => issue.node_id && jump(issue.node_id)}
                  className="flex w-full gap-2 rounded-lg border border-red-100 bg-red-50/60 p-2.5 text-left hover:border-red-200 hover:bg-red-50 disabled:cursor-default"
                  data-testid="validation-issue"
                >
                  <CircleAlert className="mt-0.5 size-4 shrink-0 text-red-500" aria-hidden />
                  <span className="min-w-0">
                    <span className="block text-xs text-red-800">{issue.message}</span>
                    <span className="mt-1 flex flex-wrap gap-1 text-[10px]">
                      <span className="rounded bg-white px-1 font-mono text-red-600">{issue.code}</span>
                      {issue.node_id && labels.has(issue.node_id) && <span className="rounded bg-white px-1 text-slate-600">{labels.get(issue.node_id)} →</span>}
                      {issue.field && <span className="rounded bg-white px-1 font-mono text-slate-500">{issue.field}</span>}
                    </span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </aside>
  );
}
