"use client";

import { useQuery } from "@tanstack/react-query";
import {
  Command,
  CornerDownLeft,
  FileText,
  LayoutDashboard,
  Library,
  Play,
  Plug,
  Plus,
  Rocket,
  Search,
  Sparkles,
  Workflow as WorkflowIcon,
} from "lucide-react";
import { usePathname, useRouter } from "next/navigation";
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";

import { toast } from "@/components/ui/toast";
import { useEditorUi } from "@/features/editor/ui-store";
import { useHasSession } from "@/hooks/use-auth";
import { api } from "@/lib/api";

import {
  moveSelection,
  type PaletteItem,
  rankItems,
} from "./command-palette-model";

interface Entry extends PaletteItem {
  icon: ReactNode;
  run: () => void | Promise<void>;
}

const EDITOR_PATH = /^\/pipelines\/([0-9a-f-]{36})$/;
/** Opens the dashboard's Generate with AI dialog (it also opens on /dashboard?generate=1). */
export const GENERATE_EVENT = "flowforge:generate";

/** Ctrl/Cmd+K from anywhere: jump to a pipeline or page, or run a quick action. */
export function CommandPalette() {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  return open ? <Palette onClose={() => setOpen(false)} /> : null;
}

function Palette({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const pathname = usePathname();
  const hasSession = useHasSession();
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const list = useRef<HTMLUListElement>(null);
  const workflows = useQuery({
    queryKey: ["workflows"],
    queryFn: api.workflows.list,
    enabled: hasSession === true,
    meta: { silent: true },
  });
  const editorId = pathname.match(EDITOR_PATH)?.[1] ?? null;

  const entries = useMemo<Entry[]>(() => {
    const go = (href: string) => () => router.push(href);
    const actions: Entry[] = [
      {
        id: "create",
        label: "Create pipeline",
        group: "Action",
        keywords: ["new pipeline"],
        icon: <Plus className="size-4" />,
        run: async () => {
          const workflow = await api.workflows.create({
            name: "Untitled pipeline",
          });
          router.push(`/pipelines/${workflow.id}`);
        },
      },
      {
        id: "generate",
        label: "Generate pipeline with AI",
        group: "Action",
        keywords: ["ai", "prompt"],
        icon: <Sparkles className="size-4" />,
        run: () => {
          if (pathname === "/dashboard")
            window.dispatchEvent(new Event(GENERATE_EVENT));
          else router.push("/dashboard?generate=1");
        },
      },
    ];
    if (editorId) {
      actions.unshift(
        {
          id: "run",
          label: "Run current pipeline",
          group: "Action",
          hint: "Editor",
          icon: <Play className="size-4" />,
          run: () =>
            useEditorUi
              .getState()
              .set({ runRequest: useEditorUi.getState().runRequest + 1 }),
        },
        {
          id: "deploy",
          label: "Deploy current pipeline",
          group: "Action",
          hint: "Editor",
          icon: <Rocket className="size-4" />,
          run: () => useEditorUi.getState().set({ deployOpen: true }),
        },
      );
    }
    const pages: Entry[] = [
      {
        id: "page-dashboard",
        label: "Dashboard",
        group: "Page",
        keywords: ["home", "pipelines", "templates"],
        icon: <LayoutDashboard className="size-4" />,
        run: go("/dashboard"),
      },
      {
        id: "page-executions",
        label: "Executions",
        group: "Page",
        keywords: ["runs", "history"],
        icon: <FileText className="size-4" />,
        run: go("/executions"),
      },
      {
        id: "page-knowledge",
        label: "Knowledge bases",
        group: "Page",
        keywords: ["documents", "rag", "search"],
        icon: <Library className="size-4" />,
        run: go("/knowledge"),
      },
      {
        id: "page-resume",
        label: "Resume refiner",
        group: "Page",
        keywords: ["cv", "ats", "job", "rewrite", "agents"],
        icon: <FileText className="size-4" />,
        run: go("/resume"),
      },
      {
        id: "page-integrations",
        label: "Integrations",
        group: "Page",
        keywords: ["keys", "credentials", "settings", "connect"],
        icon: <Plug className="size-4" />,
        run: go("/integrations"),
      },
    ];
    const pipelines: Entry[] = (workflows.data ?? []).map((w) => ({
      id: `pipeline-${w.id}`,
      label: w.name,
      group: "Pipeline",
      hint: `${w.node_count} node${w.node_count === 1 ? "" : "s"}`,
      icon: <WorkflowIcon className="size-4" />,
      run: go(`/pipelines/${w.id}`),
    }));
    return [...actions, ...pages, ...pipelines];
  }, [editorId, pathname, router, workflows.data]);

  const results = useMemo(
    () => rankItems(entries, query, 30),
    [entries, query],
  );
  const current = Math.min(selected, Math.max(0, results.length - 1));

  useEffect(() => {
    list.current
      ?.querySelector<HTMLElement>(`[data-index="${current}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [current]);

  const choose = async (entry: Entry | undefined) => {
    if (!entry) return;
    onClose();
    try {
      await entry.run();
    } catch (error) {
      toast.error(
        `Couldn't ${entry.label.toLowerCase()}`,
        error instanceof Error ? error.message : undefined,
      );
    }
  };

  return (
    <div
      className="fixed inset-0 z-[60] flex items-start justify-center bg-slate-900/40 p-4 pt-[12vh]"
      onMouseDown={onClose}
      data-testid="command-palette-backdrop"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onMouseDown={(e) => e.stopPropagation()}
        className="w-full max-w-xl overflow-hidden rounded-xl bg-white shadow-2xl ring-1 ring-slate-200"
        data-testid="command-palette"
      >
        <div className="flex items-center gap-2 border-b border-slate-200 px-4">
          <Search className="size-4 text-slate-400" aria-hidden />
          <input
            autoFocus
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setSelected(0);
            }}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown" || e.key === "ArrowUp") {
                e.preventDefault();
                setSelected(
                  moveSelection(
                    current,
                    e.key === "ArrowDown" ? 1 : -1,
                    results.length,
                  ),
                );
              } else if (e.key === "Enter") {
                e.preventDefault();
                void choose(results[current]);
              } else if (e.key === "Escape") {
                e.preventDefault();
                onClose();
              }
            }}
            placeholder="Search pipelines, pages, and actions…"
            className="h-12 flex-1 bg-transparent text-sm text-slate-900 outline-none placeholder:text-slate-400"
            role="combobox"
            aria-expanded="true"
            aria-controls="command-palette-results"
            aria-activedescendant={
              results[current] ? `palette-${results[current].id}` : undefined
            }
            data-testid="command-palette-input"
          />
          <kbd className="rounded border border-slate-200 px-1.5 py-0.5 text-[10px] text-slate-400">
            Esc
          </kbd>
        </div>
        <ul
          ref={list}
          id="command-palette-results"
          role="listbox"
          className="max-h-80 overflow-y-auto py-1"
        >
          {results.length === 0 && (
            <li className="px-4 py-6 text-center text-sm text-slate-400">
              No matches
            </li>
          )}
          {results.map((entry, index) => (
            <li
              key={entry.id}
              id={`palette-${entry.id}`}
              role="option"
              aria-selected={index === current}
              data-index={index}
              data-testid="command-palette-item"
              data-label={entry.label}
              onMouseMove={() => setSelected(index)}
              onClick={() => void choose(entry)}
              className={`flex cursor-pointer items-center gap-3 px-4 py-2 text-sm ${index === current ? "bg-indigo-50 text-indigo-900" : "text-slate-700"}`}
            >
              <span
                className={
                  index === current ? "text-indigo-600" : "text-slate-400"
                }
              >
                {entry.icon}
              </span>
              <span className="min-w-0 flex-1 truncate">{entry.label}</span>
              {entry.hint && (
                <span className="text-xs text-slate-400">{entry.hint}</span>
              )}
              <span className="w-16 text-right text-[11px] uppercase tracking-wide text-slate-400">
                {entry.group}
              </span>
              {index === current && (
                <CornerDownLeft
                  className="size-3.5 text-indigo-500"
                  aria-hidden
                />
              )}
            </li>
          ))}
        </ul>
        <div className="flex items-center gap-3 border-t border-slate-100 px-4 py-2 text-[11px] text-slate-400">
          <span className="flex items-center gap-1">
            <Command className="size-3" aria-hidden />K to open
          </span>
          <span>↑↓ to move</span>
          <span>Enter to go</span>
        </div>
      </div>
    </div>
  );
}
