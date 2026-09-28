"use client";

import { Handle, type NodeProps, Position } from "@xyflow/react";
import { ChevronDown, ChevronRight, Copy, Ellipsis, FlaskConical, Pencil, Trash2 } from "lucide-react";
import { Fragment, memo, useEffect, useRef, useState } from "react";

import { categoryStyle, NodeIcon } from "@/components/node-icon";
import { Menu } from "@/components/ui/menu";
import { StatusDot } from "@/components/ui/status";

import { configSummary, type FlowNode } from "./graph";
import { getEditorStore, useEditor } from "./store";
import { useEditorUi } from "./ui-store";

const HANDLE = "!size-2.5 !border-2 !border-white !bg-slate-400";

function RenameInput({ id, label }: { id: string; label: string }) {
  const [value, setValue] = useState(label);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    input.current?.focus();
    input.current?.select();
  }, []);
  const finish = (commit: boolean) => {
    const store = getEditorStore().getState();
    if (commit && value.trim()) store.renameNode(id, value.trim());
    store.setRenaming(null);
  };
  return (
    <input
      ref={input}
      value={value}
      aria-label="Node name"
      onChange={(e) => setValue(e.target.value)}
      onBlur={() => finish(true)}
      onKeyDown={(e) => {
        e.stopPropagation();
        if (e.key === "Enter") finish(true);
        if (e.key === "Escape") finish(false);
      }}
      className="nodrag w-full min-w-0 rounded border border-indigo-300 px-1 py-0.5 text-sm font-semibold text-slate-900 outline-none focus:ring-2 focus:ring-indigo-200"
    />
  );
}

function FlowNodeCard({ id, data, selected }: NodeProps<FlowNode>) {
  const entry = useEditor((s) => s.catalog[data.nodeType]);
  const run = useEditor((s) => s.run.nodes[id]);
  const issueCount = useEditor((s) => s.issues.filter((issue) => issue.node_id === id).length);
  const renaming = useEditor((s) => s.renamingId === id);
  const style = categoryStyle(entry?.category);
  const branches = entry?.branches ?? [];
  const status = run?.status ?? "idle";
  const streaming = run?.status === "running" && run.tokens;

  const actions = () => getEditorStore().getState();
  const menuItems = [
    { label: "Rename", icon: <Pencil className="size-4" />, onSelect: () => actions().setRenaming(id) },
    { label: "Duplicate", icon: <Copy className="size-4" />, shortcut: "Ctrl+D", onSelect: () => actions().duplicateNodes([id]) },
    {
      label: data.collapsed ? "Expand" : "Collapse",
      icon: data.collapsed ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />,
      onSelect: () => actions().toggleCollapsed(id),
    },
    {
      label: "Test node",
      icon: <FlaskConical className="size-4" />,
      onSelect: () => {
        actions().selectOnly(id);
        useEditorUi.getState().set({ rightPanel: null, testRequest: useEditorUi.getState().testRequest + 1 });
      },
    },
    { label: "Delete", icon: <Trash2 className="size-4" />, shortcut: "Del", danger: true, onSelect: () => actions().deleteElements([id], []) },
  ];

  return (
    <div
      data-testid={`node-${id}`}
      data-status={status}
      className={`group relative w-60 rounded-xl border bg-white text-left shadow-sm transition-shadow ${
        selected ? "border-indigo-500 ring-2 ring-indigo-200" : issueCount ? "border-red-300" : "border-slate-200 hover:shadow-md"
      }`}
    >
      <span className={`absolute inset-y-0 left-0 w-1 rounded-l-xl ${style.accent}`} aria-hidden />
      {entry?.has_input !== false && <Handle type="target" position={Position.Left} className={HANDLE} />}

      <div className="flex items-center gap-2 py-2 pl-3.5 pr-2">
        <span className={`grid size-7 shrink-0 place-items-center rounded-lg ${style.tile}`}>
          <NodeIcon name={entry?.icon} className="size-4" />
        </span>
        <div className="min-w-0 flex-1">
          {renaming ? (
            <RenameInput id={id} label={data.label} />
          ) : (
            <p className="truncate text-sm font-semibold text-slate-900" title={data.label} onDoubleClick={() => actions().setRenaming(id)}>
              {data.label}
            </p>
          )}
          <p className="truncate font-mono text-[10px] text-slate-400">{id}</p>
        </div>
        {issueCount > 0 && (
          <span
            className="grid h-5 min-w-5 place-items-center rounded-full bg-red-500 px-1 text-[10px] font-semibold text-white"
            title={`${issueCount} validation issue${issueCount > 1 ? "s" : ""}`}
            data-testid={`node-issues-${id}`}
          >
            {issueCount}
          </span>
        )}
        <StatusDot status={status} />
        <Menu
          label={`Actions for ${data.label}`}
          trigger={<Ellipsis className="size-4 text-slate-400 hover:text-slate-700" />}
          items={menuItems}
        />
      </div>

      {!data.collapsed && (
        <div className="space-y-1 border-t border-slate-100 py-2 pl-3.5 pr-3">
          <p className="line-clamp-2 text-xs text-slate-500">{data.description || entry?.description}</p>
          <p className="truncate font-mono text-[11px] text-slate-600" title={configSummary(data.nodeType, data.config, entry)}>
            {configSummary(data.nodeType, data.config, entry)}
          </p>
          {streaming && (
            <p className="line-clamp-3 rounded bg-blue-50 px-1.5 py-1 text-[11px] text-blue-800" data-testid={`node-tokens-${id}`}>
              {run.tokens.slice(-160)}
            </p>
          )}
        </div>
      )}

      {branches.length ? (
        branches.map((branch, index) => {
          const top = `${((index + 1) / (branches.length + 1)) * 100}%`;
          return (
            <Fragment key={branch}>
              <Handle
                type="source"
                id={branch}
                position={Position.Right}
                style={{ top }}
                className={`${HANDLE} ${branch === "true" ? "!bg-emerald-500" : "!bg-red-400"}`}
              />
              <span
                className="pointer-events-none absolute left-full ml-2 -translate-y-1/2 text-[10px] font-medium text-slate-500"
                style={{ top }}
              >
                {branch}
              </span>
            </Fragment>
          );
        })
      ) : (
        <Handle type="source" position={Position.Right} className={HANDLE} />
      )}
    </div>
  );
}

export const FlowNodeView = memo(FlowNodeCard);
