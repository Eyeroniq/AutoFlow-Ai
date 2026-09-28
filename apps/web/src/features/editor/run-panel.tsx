"use client";

import { ChevronDown, ChevronUp, ExternalLink, Wifi, WifiOff } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { StatusBadge } from "@/components/ui/status";
import { formatDuration, prettyJson } from "@/lib/format";

import { NodeTimeline } from "../runs/node-timeline";
import { topologicalOrder } from "./graph";
import { useRun } from "./run-controller";
import { useEditor } from "./store";
import { useEditorUi } from "./ui-store";

function ConnectionPill() {
  const { status, detail } = useEditorUi((s) => s.connection);
  if (status === "idle" || status === "closed") return null;
  const tone =
    status === "open" ? "text-emerald-700 bg-emerald-50" : status === "error" ? "text-red-700 bg-red-50" : "text-amber-800 bg-amber-50";
  const label = status === "open" ? "Live" : status === "connecting" ? "Connecting…" : status === "reconnecting" ? detail ?? "Reconnecting…" : detail ?? "Disconnected";
  return (
    <span className={`flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ${tone}`} data-testid="connection-status" title={detail ?? undefined}>
      {status === "open" ? <Wifi className="size-3" /> : <WifiOff className="size-3" />}
      {label}
    </span>
  );
}

export function RunPanel() {
  const run = useEditor((s) => s.run);
  const open = useEditorUi((s) => s.runPanelOpen);
  const setUi = useEditorUi((s) => s.set);
  if (!run.executionId) return null;

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setUi({ runPanelOpen: true })}
        className="flex h-8 w-full items-center gap-2 border-t border-slate-200 bg-white px-4 text-xs text-slate-600 hover:bg-slate-50"
      >
        <ChevronUp className="size-3.5" /> Last run <StatusBadge status={run.status} />
      </button>
    );
  }

  return (
    <section className="flex h-72 shrink-0 flex-col border-t border-slate-200 bg-slate-50" aria-label="Run" data-testid="run-panel">
      <div className="flex items-center gap-2 border-b border-slate-200 bg-white px-4 py-2">
        <h2 className="text-sm font-semibold text-slate-900">Run</h2>
        <span data-testid="run-status">
          <StatusBadge status={run.status} />
        </span>
        <span className="font-mono text-[11px] text-slate-500">{run.executionId.slice(0, 8)}</span>
        {run.durationMs !== null && <span className="text-[11px] text-slate-500">{formatDuration(run.durationMs)}</span>}
        {run.worker && <span className="hidden text-[11px] text-slate-400 md:inline">on {run.worker}</span>}
        <ConnectionPill />
        <Link href={`/executions/${run.executionId}`} className="ml-auto flex items-center gap-1 text-xs font-medium text-indigo-600 hover:text-indigo-500">
          Details <ExternalLink className="size-3" />
        </Link>
        <button type="button" onClick={() => setUi({ runPanelOpen: false })} className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700" aria-label="Collapse run panel">
          <ChevronDown className="size-4" />
        </button>
      </div>
      <div className="grid flex-1 gap-3 overflow-y-auto p-3 lg:grid-cols-[1fr_20rem]">
        <NodeTimeline run={run} />
        <div className="space-y-2">
          {run.error && (
            <div className={`rounded-lg p-2.5 text-xs ${run.status === "stopped" ? "bg-amber-50 text-amber-900" : "bg-red-50 text-red-700"}`} data-testid="run-error">
              {run.error}
            </div>
          )}
          {run.finalOutput && (
            <div>
              <p className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400">Final output</p>
              <pre className="max-h-48 overflow-auto rounded-md bg-white p-2 text-[11px] text-slate-800 ring-1 ring-slate-200" data-testid="final-output">
                {prettyJson(run.finalOutput)}
              </pre>
            </div>
          )}
          {(run.status === "pending" || run.status === "running") && !run.error && (
            <p className="text-xs text-slate-500">{run.status === "pending" ? "Queued: waiting for a worker…" : "Running on a worker. Node colors update live."}</p>
          )}
        </div>
      </div>
    </section>
  );
}

interface InputField {
  node: string;
  name: string;
  type: "text" | "number" | "json";
  required: boolean;
  defaultValue: unknown;
  label: string;
}

/** Values for the workflow's Input nodes, asked for before a run. */
export function RunInputsDialog() {
  const open = useEditorUi((s) => s.runInputsOpen);
  const setUi = useEditorUi((s) => s.set);
  const nodes = useEditor((s) => s.nodes);
  const edges = useEditor((s) => s.edges);
  const { run } = useRun();

  const fields = useMemo<InputField[]>(() => {
    const byId = new Map(nodes.map((n) => [n.id, n]));
    return topologicalOrder(nodes, edges)
      .map((id) => byId.get(id)!)
      .filter((n) => n.data.nodeType === "input")
      .map((n) => ({
        node: n.id,
        name: String(n.data.config.name || n.id),
        type: (n.data.config.input_type as InputField["type"]) ?? "text",
        required: n.data.config.required !== false,
        defaultValue: n.data.config.default,
        label: n.data.label,
      }));
  }, [nodes, edges]);

  if (!open) return null;
  return <InputsForm key={String(open)} fields={fields} onClose={() => setUi({ runInputsOpen: false })} onRun={run} />;
}

function InputsForm({ fields, onClose, onRun }: { fields: InputField[]; onClose: () => void; onRun: (inputs: Record<string, unknown>) => Promise<boolean> }) {
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      fields.map((f) => [f.name, f.defaultValue === undefined || f.defaultValue === null ? "" : typeof f.defaultValue === "string" ? f.defaultValue : JSON.stringify(f.defaultValue)]),
    ),
  );
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    const inputs: Record<string, unknown> = {};
    const problems: Record<string, string> = {};
    for (const field of fields) {
      const raw = values[field.name] ?? "";
      if (!raw.trim()) {
        if (field.required && (field.defaultValue === undefined || field.defaultValue === null)) problems[field.name] = "Required";
        continue; // empty: the Input node's default applies
      }
      if (field.type === "number") {
        if (Number.isNaN(Number(raw))) problems[field.name] = "Enter a number";
        else inputs[field.name] = Number(raw);
      } else if (field.type === "json") {
        try {
          inputs[field.name] = JSON.parse(raw);
        } catch {
          problems[field.name] = "Enter valid JSON";
        }
      } else {
        inputs[field.name] = raw;
      }
    }
    setErrors(problems);
    if (Object.keys(problems).length) return;
    setBusy(true);
    const started = await onRun(inputs);
    setBusy(false);
    if (started) onClose();
  };

  return (
    <Dialog
      open
      title="Run workflow"
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={() => void submit()} loading={busy} data-testid="confirm-run">
            Run
          </Button>
        </>
      }
    >
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <p className="text-xs text-slate-500">Values for the workflow&apos;s Input nodes. Leave one empty to use its default.</p>
        {fields.map((field) => (
          <label key={field.node} className="block">
            <span className="mb-1 flex items-baseline gap-1 text-xs font-medium text-slate-700">
              {field.label} <code className="font-normal text-slate-400">{field.name}</code>
              <span className="font-normal text-slate-400">({field.type})</span>
              {field.required && field.defaultValue == null && <span className="text-red-500">*</span>}
            </span>
            {field.type === "json" ? (
              <textarea
                value={values[field.name]}
                onChange={(e) => setValues({ ...values, [field.name]: e.target.value })}
                rows={3}
                className="block w-full rounded-md border border-slate-300 px-2.5 py-1.5 font-mono text-xs focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-100"
              />
            ) : (
              <input
                value={values[field.name]}
                onChange={(e) => setValues({ ...values, [field.name]: e.target.value })}
                inputMode={field.type === "number" ? "decimal" : undefined}
                data-testid={`run-input-${field.name}`}
                className="block w-full rounded-md border border-slate-300 px-2.5 py-1.5 text-sm focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-100"
              />
            )}
            {errors[field.name] && <span className="mt-1 block text-[11px] text-red-600">{errors[field.name]}</span>}
          </label>
        ))}
        <button type="submit" className="hidden" />
      </form>
    </Dialog>
  );
}
