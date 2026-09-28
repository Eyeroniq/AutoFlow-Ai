"use client";

import { Copy, Trash2, X } from "lucide-react";
import { useMemo } from "react";

import { categoryStyle, NodeIcon } from "@/components/node-icon";
import { Button } from "@/components/ui/button";
import { toast } from "@/components/ui/toast";

import { type FieldRenderer, SchemaForm } from "./config-form";
import { ancestorsOf, outputKeysFor } from "./graph";
import { InputDefaultField } from "./input-default-field";
import { NodeTester } from "./node-tester";
import { ConnectionTest, llmRenderers } from "./provider-fields";
import { getEditorStore, useEditor } from "./store";

const inputClass =
  "nodrag block w-full rounded-md border border-slate-300 px-2.5 py-1.5 text-sm shadow-sm focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-100";

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-3 border-b border-slate-100 px-4 py-4">
      <h3 className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">{title}</h3>
      {children}
    </section>
  );
}

export function ConfigPanel({ nodeId }: { nodeId: string }) {
  const node = useEditor((s) => s.nodes.find((n) => n.id === nodeId));
  const entry = useEditor((s) => (node ? s.catalog[node.data.nodeType] : undefined));
  const formEpoch = useEditor((s) => s.formEpoch);
  const allIssues = useEditor((s) => s.issues);
  const edges = useEditor((s) => s.edges);
  const nodes = useEditor((s) => s.nodes);
  const catalog = useEditor((s) => s.catalog);
  const issues = useMemo(() => allIssues.filter((i) => i.node_id === nodeId), [allIssues, nodeId]);
  const upstream = useMemo(() => [...ancestorsOf(nodeId, edges)], [nodeId, edges]);
  // LLM nodes and the LLM document nodes (Summarize, Entity Extraction) share the provider,
  // model, and fallback controls.
  const usesLLM = Boolean(entry?.config_schema.properties?.provider?.enum && entry.config_schema.properties?.fallback);
  const renderers = useMemo<Record<string, FieldRenderer>>(() => {
    if (entry?.type === "input") return { default: (props) => <InputDefaultField {...props} /> };
    return usesLLM && entry ? llmRenderers((entry.config_schema.properties?.provider?.enum ?? []).map(String)) : {};
  }, [entry, usesLLM]);

  if (!node || !entry) return null;
  const store = getEditorStore().getState;
  const style = categoryStyle(entry.category);
  const outputs = outputKeysFor(node, catalog);
  const general = issues.filter((i) => !i.field || !(i.field in (entry.config_schema.properties ?? {})) && !i.field.includes("."));
  const provider = String(node.data.config.provider ?? entry.config_schema.properties?.provider?.default ?? "");
  const auth = String(node.data.config.auth ?? "gmail");
  const copy = (text: string) => {
    void navigator.clipboard?.writeText(text);
    toast.info("Copied", text);
  };

  return (
    <aside className="flex w-96 shrink-0 flex-col border-l border-slate-200 bg-white" aria-label="Node configuration" data-testid="config-panel">
      <div className="flex items-center gap-2.5 border-b border-slate-200 px-4 py-3">
        <span className={`grid size-8 place-items-center rounded-lg ${style.tile}`}>
          <NodeIcon name={entry.icon} className="size-4" />
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-semibold text-slate-900">{node.data.label}</p>
          <button type="button" onClick={() => copy(`{{${nodeId}}}`)} className="flex items-center gap-1 font-mono text-[11px] text-slate-500 hover:text-indigo-600" title="Copy reference">
            {entry.label} · {`{{${nodeId}}}`} <Copy className="size-3" aria-hidden />
          </button>
        </div>
        <button type="button" onClick={() => store().selectOnly(null)} className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700" aria-label="Close panel">
          <X className="size-4" />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto">
        <Section title="Node">
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-slate-700">Name</span>
            <input value={node.data.label} onChange={(e) => store().renameNode(nodeId, e.target.value)} className={inputClass} data-testid="node-name-input" />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-slate-700">Description</span>
            <textarea value={node.data.description} onChange={(e) => store().setDescription(nodeId, e.target.value)} rows={2} placeholder={entry.description} className={inputClass} />
          </label>
        </Section>

        <Section title="Settings">
          {general.length > 0 && (
            <div className="space-y-1 rounded-md bg-red-50 p-2">
              {general.map((issue) => (
                <p key={issue.message} className="text-[11px] text-red-700">
                  {issue.message}
                </p>
              ))}
            </div>
          )}
          {/* Remount when the config changes from outside the form (load, undo, redo). */}
          <SchemaForm key={`${nodeId}:${formEpoch}`} nodeId={nodeId} schema={entry.config_schema} config={node.data.config} issues={issues} renderers={renderers} />
          {usesLLM && provider !== "mock" && <ConnectionTest provider={provider} label={`${provider} connection`} />}
          {(node.data.nodeType === "gmail" || node.data.nodeType === "gmail_read") && auth !== "mock" && (
            <ConnectionTest provider="gmail" label="Gmail (SMTP + IMAP) connection" />
          )}
        </Section>

        <Section title="Inputs and outputs">
          <div>
            <p className="mb-1 text-xs font-medium text-slate-700">Receives from</p>
            {upstream.length ? (
              <ul className="flex flex-wrap gap-1">
                {nodes.filter((n) => upstream.includes(n.id)).map((n) => (
                  <li key={n.id} className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-600">
                    {n.data.label} <span className="font-mono text-slate-400">{n.id}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-[11px] text-slate-500">{entry.has_input ? "Nothing yet: connect a node into this one." : "This node starts the workflow."}</p>
            )}
          </div>
          <div>
            <p className="mb-1 text-xs font-medium text-slate-700">Outputs (click to copy the reference)</p>
            <ul className="flex flex-wrap gap-1">
              {outputs.map((key) => (
                <li key={key}>
                  <button type="button" onClick={() => copy(`{{${nodeId}.${key}}}`)} className="rounded bg-violet-50 px-1.5 py-0.5 font-mono text-[11px] text-violet-700 hover:bg-violet-100">
                    {`{{${nodeId}.${key}}}`}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        </Section>

        <div className="px-4 py-4">
          <NodeTester nodeId={nodeId} />
        </div>
      </div>

      <div className="border-t border-slate-200 px-4 py-3">
        <Button variant="danger" size="sm" onClick={() => store().deleteElements([nodeId], [])} className="w-full">
          <Trash2 className="size-3.5" aria-hidden /> Delete node
        </Button>
      </div>
    </aside>
  );
}

export function MultiSelectionPanel({ ids }: { ids: string[] }) {
  const store = getEditorStore().getState;
  return (
    <aside className="flex w-72 shrink-0 flex-col gap-3 border-l border-slate-200 bg-white p-4" aria-label="Selection">
      <p className="text-sm font-semibold text-slate-900">{ids.length} nodes selected</p>
      <p className="text-xs text-slate-500">Shift+drag to box-select, Ctrl/Cmd+click to add or remove nodes.</p>
      <Button variant="secondary" size="sm" onClick={() => store().duplicateNodes(ids)}>
        <Copy className="size-3.5" aria-hidden /> Duplicate
      </Button>
      <Button variant="danger" size="sm" onClick={() => store().deleteSelection()}>
        <Trash2 className="size-3.5" aria-hidden /> Delete
      </Button>
    </aside>
  );
}
