"use client";

import { type ChangeEvent, type KeyboardEvent, useId, useMemo, useRef, useState } from "react";

import { buildReferenceSuggestions, filterSuggestions, findOpenReference, insertReference, type ReferenceKind } from "./references";
import type { FlowNode } from "./graph";
import { getEditorStore, useEditor } from "./store";

const KIND_STYLE: Record<ReferenceKind, string> = {
  item: "bg-teal-50 text-teal-700",
  input: "bg-emerald-50 text-emerald-700",
  node: "bg-violet-50 text-violet-700",
  variable: "bg-amber-50 text-amber-800",
  system: "bg-slate-100 text-slate-600",
};

/** What makes a node referenceable; positions and selection don't, so drags stay cheap. */
const referenceSignature = (nodes: FlowNode[]) =>
  nodes.map((n) => `${n.id}:${n.data.nodeType}:${String(n.data.config.name ?? "")}:${n.data.label}`).join("|");

export function useReferenceSuggestions(nodeId: string, field?: string) {
  const signature = useEditor((s) => referenceSignature(s.nodes));
  const edges = useEditor((s) => s.edges);
  const variables = useEditor((s) => s.variables);
  const catalog = useEditor((s) => s.catalog);
  return useMemo(() => {
    void signature; // recompute when a referenceable property of a node changes
    return buildReferenceSuggestions({ nodeId, nodes: getEditorStore().getState().nodes, edges, variables, catalog, field });
  }, [nodeId, signature, edges, variables, catalog, field]);
}

interface ReferenceFieldProps {
  id?: string;
  nodeId: string;
  /** The config field (per-item fields also offer {{item}} and {{index}}). */
  field?: string;
  value: string;
  onChange: (value: string) => void;
  onBlur?: () => void;
  multiline?: boolean;
  rows?: number;
  placeholder?: string;
  invalid?: boolean;
  describedBy?: string;
  inputMode?: "text" | "decimal" | "numeric";
  ariaLabel?: string;
}

/** A text input/textarea where typing "{{" offers every reference valid at this node. */
export function ReferenceField({
  id,
  nodeId,
  field,
  value,
  onChange,
  onBlur,
  multiline,
  rows = 4,
  placeholder,
  invalid,
  describedBy,
  inputMode,
  ariaLabel,
}: ReferenceFieldProps) {
  const listId = useId();
  const element = useRef<HTMLInputElement & HTMLTextAreaElement>(null);
  const [open, setOpen] = useState<{ start: number; query: string } | null>(null);
  const [active, setActive] = useState(0);
  const all = useReferenceSuggestions(nodeId, field);
  const items = open ? filterSuggestions(all, open.query, 12) : [];

  const sync = (text: string, caret: number | null) => {
    const next = caret === null ? null : findOpenReference(text, caret);
    setOpen(next);
    setActive(0);
  };

  const choose = (ref: string) => {
    const el = element.current;
    if (!el || !open) return;
    const caret = el.selectionStart ?? value.length;
    const result = insertReference(value, caret, open.start, ref);
    onChange(result.text);
    setOpen(null);
    requestAnimationFrame(() => {
      el.focus();
      el.setSelectionRange(result.caret, result.caret);
    });
  };

  const onKeyDown = (event: KeyboardEvent) => {
    if (!open) return;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      setOpen(null);
      return;
    }
    if (!items.length) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((i) => (i + 1) % items.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((i) => (i - 1 + items.length) % items.length);
    } else if (event.key === "Enter" || event.key === "Tab") {
      event.preventDefault();
      choose(items[active].ref);
    }
  };

  const common = {
    id,
    ref: element,
    value,
    placeholder,
    "aria-label": ariaLabel,
    "aria-invalid": invalid || undefined,
    "aria-describedby": describedBy,
    "aria-autocomplete": "list" as const,
    "aria-controls": open ? listId : undefined,
    "aria-expanded": Boolean(open && items.length),
    onChange: (event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => {
      onChange(event.target.value);
      sync(event.target.value, event.target.selectionStart);
    },
    onKeyDown,
    onClick: () => sync(value, element.current?.selectionStart ?? null),
    onBlur: () => {
      // Let a click on a suggestion land first.
      setTimeout(() => setOpen(null), 120);
      onBlur?.();
    },
    className: `nodrag block w-full rounded-md border bg-white px-2.5 py-1.5 text-sm text-slate-900 shadow-sm placeholder:text-slate-400 focus:outline-none focus:ring-2 ${
      multiline ? "resize-y font-mono text-[13px] leading-5" : ""
    } ${invalid ? "border-red-400 focus:ring-red-100" : "border-slate-300 focus:border-indigo-500 focus:ring-indigo-100"}`,
  };

  return (
    <div className="relative">
      {multiline ? <textarea rows={rows} {...common} /> : <input type="text" inputMode={inputMode} {...common} />}
      {open && (
        <div
          id={listId}
          role="listbox"
          aria-label="References"
          className="absolute left-0 right-0 top-full z-40 mt-1 max-h-64 overflow-y-auto rounded-lg border border-slate-200 bg-white py-1 shadow-lg"
        >
          {items.length === 0 ? (
            <p className="px-3 py-2 text-xs text-slate-500">No references match “{open.query}”. Only upstream nodes can be referenced.</p>
          ) : (
            items.map((item, index) => (
              <button
                key={item.ref}
                type="button"
                role="option"
                aria-selected={index === active}
                onMouseDown={(e) => {
                  e.preventDefault();
                  choose(item.ref);
                }}
                onMouseEnter={() => setActive(index)}
                className={`flex w-full items-center gap-2 px-3 py-1.5 text-left ${index === active ? "bg-indigo-50" : ""}`}
              >
                <code className="flex-1 truncate text-xs text-slate-800">{`{{${item.ref}}}`}</code>
                <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] ${KIND_STYLE[item.kind]}`}>{item.detail}</span>
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}
