"use client";

import { PanelLeftClose, PanelLeftOpen, Search } from "lucide-react";
import { useMemo, useState } from "react";

import { categoryStyle, NodeIcon } from "@/components/node-icon";
import type { NodeType } from "@/lib/types";

import { DND_NODE_TYPE, useCanvasCenter } from "./canvas";
import { freePosition } from "./graph";
import { getEditorStore, useEditor } from "./store";
import { useEditorUi } from "./ui-store";

// A group only appears when the registry has nodes in it.
const GROUP_ORDER = ["General", "LLM", "Lists", "Data sources", "Integrations", "Documents", "Knowledge", "Audio"];

export function groupCatalog(entries: NodeType[], query: string): [string, NodeType[]][] {
  const q = query.trim().toLowerCase();
  const matches = entries.filter(
    (e) => !q || e.label.toLowerCase().includes(q) || e.type.includes(q) || e.description.toLowerCase().includes(q),
  );
  const groups = new Map<string, NodeType[]>();
  for (const entry of matches) groups.set(entry.group, [...(groups.get(entry.group) ?? []), entry]);
  return [...groups.entries()].sort(([a], [b]) => {
    const ia = GROUP_ORDER.indexOf(a);
    const ib = GROUP_ORDER.indexOf(b);
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib) || a.localeCompare(b);
  });
}

export function NodeLibrary() {
  const catalog = useEditor((s) => s.catalog);
  const open = useEditorUi((s) => s.libraryOpen);
  const setUi = useEditorUi((s) => s.set);
  const [query, setQuery] = useState("");
  const center = useCanvasCenter();
  const groups = useMemo(() => groupCatalog(Object.values(catalog), query), [catalog, query]);

  if (!open) {
    return (
      <div className="flex w-10 flex-col items-center border-r border-slate-200 bg-white py-3">
        <button
          type="button"
          onClick={() => setUi({ libraryOpen: true })}
          className="rounded-md p-1.5 text-slate-500 hover:bg-slate-100 hover:text-slate-800"
          aria-label="Show node library"
          title="Show node library"
        >
          <PanelLeftOpen className="size-4" />
        </button>
      </div>
    );
  }

  const add = (type: string) => {
    const store = getEditorStore().getState();
    store.addNode(type, freePosition(store.nodes, center()));
    setUi({ rightPanel: null });
  };

  return (
    <aside className="flex w-64 shrink-0 flex-col border-r border-slate-200 bg-white" aria-label="Node library">
      <div className="flex items-center justify-between px-3 pb-2 pt-3">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Nodes</h2>
        <button
          type="button"
          onClick={() => setUi({ libraryOpen: false })}
          className="rounded-md p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
          aria-label="Hide node library"
          title="Hide node library"
        >
          <PanelLeftClose className="size-4" />
        </button>
      </div>
      <div className="px-3 pb-2">
        <label className="relative block">
          <Search className="pointer-events-none absolute left-2.5 top-2 size-4 text-slate-400" aria-hidden />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search nodes"
            aria-label="Search nodes"
            className="w-full rounded-md border border-slate-200 bg-slate-50 py-1.5 pl-8 pr-2 text-sm placeholder:text-slate-400 focus:border-indigo-400 focus:bg-white focus:outline-none focus:ring-2 focus:ring-indigo-100"
          />
        </label>
      </div>
      <div className="flex-1 space-y-4 overflow-y-auto px-3 pb-4">
        {groups.length === 0 && <p className="px-1 text-sm text-slate-400">No nodes match “{query}”.</p>}
        {groups.map(([group, entries]) => (
          <section key={group}>
            <h3 className="mb-1.5 px-1 text-[11px] font-semibold uppercase tracking-wide text-slate-400">{group}</h3>
            <ul className="space-y-1">
              {entries.map((entry) => {
                const style = categoryStyle(entry.category);
                return (
                  <li key={entry.type}>
                    <button
                      type="button"
                      draggable
                      data-testid={`library-${entry.type}`}
                      onDragStart={(e) => {
                        e.dataTransfer.setData(DND_NODE_TYPE, entry.type);
                        e.dataTransfer.effectAllowed = "move";
                      }}
                      onClick={() => add(entry.type)}
                      title={`${entry.description} Drag onto the canvas, or click to add.`}
                      className="flex w-full cursor-grab items-center gap-2.5 rounded-lg border border-transparent px-2 py-1.5 text-left hover:border-slate-200 hover:bg-slate-50 active:cursor-grabbing"
                    >
                      <span className={`grid size-7 shrink-0 place-items-center rounded-md ${style.tile}`}>
                        <NodeIcon name={entry.icon} className="size-4" />
                      </span>
                      <span className="min-w-0">
                        <span className="block truncate text-sm font-medium text-slate-800">{entry.label}</span>
                        <span className="block truncate text-xs text-slate-500">{entry.description}</span>
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </section>
        ))}
      </div>
    </aside>
  );
}
