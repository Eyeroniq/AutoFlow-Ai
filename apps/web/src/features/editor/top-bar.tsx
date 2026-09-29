"use client";

import { ArrowLeft, CalendarClock, Check, CircleAlert, Play, Redo2, Rocket, Save, ShieldCheck, Square, Undo2, Variable } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { UserMenu } from "@/components/app-shell";

import { isTerminal } from "../runs/run-state";
import { useRun } from "./run-controller";
import { getEditorStore, useEditor } from "./store";
import { overallTone, type TriggerTone } from "./triggers";
import { useTriggers } from "./triggers-panel";
import { useEditorUi } from "./ui-store";

const TRIGGER_DOT: Record<TriggerTone, string> = {
  off: "",
  on: "bg-emerald-400",
  warning: "bg-amber-400",
  disabled: "bg-red-500",
};

function TriggersButton() {
  const rightPanel = useEditorUi((s) => s.rightPanel);
  const setUi = useEditorUi((s) => s.set);
  const { data } = useTriggers();
  const tone = data ? overallTone(data.triggers, data.settings) : "off";
  const title = tone === "disabled" ? "A trigger was switched off after repeated failures" : "Schedule, email, and webhook triggers";
  return (
    <button
      type="button"
      className={`${barButton} ${rightPanel === "triggers" ? "bg-indigo-600" : ""}`}
      onClick={() => setUi({ rightPanel: rightPanel === "triggers" ? null : "triggers" })}
      title={title}
      data-testid="triggers-button"
      data-tone={tone}
    >
      <CalendarClock className="size-4" aria-hidden /> Triggers
      {tone !== "off" && <span className={`size-2 rounded-full ${TRIGGER_DOT[tone]}`} aria-label={tone} />}
    </button>
  );
}

function WorkflowName() {
  const name = useEditor((s) => s.name);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(name);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editing) input.current?.select();
  }, [editing]);

  const finish = (commit: boolean) => {
    const value = draft.trim();
    if (commit && value) getEditorStore().getState().setName(value.slice(0, 255));
    setEditing(false);
  };

  if (editing) {
    return (
      <input
        ref={input}
        value={draft}
        aria-label="Workflow name"
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => finish(true)}
        onKeyDown={(e) => {
          if (e.key === "Enter") finish(true);
          if (e.key === "Escape") finish(false);
        }}
        className="w-72 rounded-md border border-indigo-300 bg-white px-2 py-1 text-sm font-semibold text-slate-900 outline-none ring-2 ring-indigo-300"
      />
    );
  }
  return (
    <button
      type="button"
      onClick={() => {
        setDraft(name);
        setEditing(true);
      }}
      title="Rename workflow"
      data-testid="workflow-name"
      className="max-w-80 truncate rounded-md px-2 py-1 text-sm font-semibold text-white hover:bg-indigo-600"
    >
      {name || "Untitled pipeline"}
    </button>
  );
}

function SaveIndicator() {
  const save = useEditor((s) => s.save);
  const retry = () => void getEditorStore().getState().saveNow();
  if (save.status === "error") {
    return (
      <span className="flex items-center gap-2 rounded-md bg-red-500/90 px-2 py-1 text-xs font-medium text-white" role="alert" data-testid="save-state">
        <CircleAlert className="size-3.5" aria-hidden />
        <span title={save.error ?? undefined}>Save failed</span>
        <button type="button" onClick={retry} className="rounded bg-white/20 px-1.5 py-0.5 hover:bg-white/30">
          Retry
        </button>
      </span>
    );
  }
  const label = save.status === "saving" ? "Saving…" : save.status === "dirty" ? "Unsaved changes" : "Saved";
  return (
    <span className="flex items-center gap-1.5 text-xs text-indigo-100" data-testid="save-state" aria-live="polite">
      {save.status === "saving" && <span className="size-3 animate-spin rounded-full border-2 border-indigo-200 border-t-transparent" />}
      {save.status === "saved" && <Check className="size-3.5" aria-hidden />}
      {label}
    </span>
  );
}

const barButton =
  "flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium text-indigo-50 hover:bg-indigo-600 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent";

export function TopBar({ onValidate, onRun }: { onValidate: () => void; onRun: () => void }) {
  const canSave = useEditor((s) => s.save.status === "dirty" || s.save.status === "error");
  const canUndo = useEditor((s) => s.past.length > 0);
  const canRedo = useEditor((s) => s.future.length > 0);
  const issueCount = useEditor((s) => s.issues.length);
  const runStatus = useEditor((s) => s.run.status);
  const rightPanel = useEditorUi((s) => s.rightPanel);
  const setUi = useEditorUi((s) => s.set);
  const { stop } = useRun();
  const active = runStatus === "pending" || runStatus === "running";

  return (
    <header className="flex h-12 shrink-0 items-center gap-2 bg-indigo-700 px-3 text-white">
      <Link href="/dashboard" className="rounded-md p-1.5 text-indigo-100 hover:bg-indigo-600" aria-label="Back to dashboard" title="Dashboard">
        <ArrowLeft className="size-4" />
      </Link>
      <WorkflowName />
      <SaveIndicator />

      <div className="ml-4 flex items-center gap-0.5">
        <button
          type="button"
          className={barButton}
          disabled={!canSave}
          onClick={() => void getEditorStore().getState().saveNow()}
          title="Save now (Ctrl+S)"
          data-testid="save-button"
        >
          <Save className="size-4" aria-hidden /> Save
        </button>
        <button type="button" className={barButton} disabled={!canUndo} onClick={() => getEditorStore().getState().undo()} title="Undo (Ctrl+Z)">
          <Undo2 className="size-4" aria-hidden /> Undo
        </button>
        <button type="button" className={barButton} disabled={!canRedo} onClick={() => getEditorStore().getState().redo()} title="Redo (Ctrl+Shift+Z)">
          <Redo2 className="size-4" aria-hidden /> Redo
        </button>
      </div>

      <div className="ml-auto flex items-center gap-1">
        <button
          type="button"
          className={`${barButton} ${rightPanel === "variables" ? "bg-indigo-600" : ""}`}
          onClick={() => setUi({ rightPanel: rightPanel === "variables" ? null : "variables" })}
        >
          <Variable className="size-4" aria-hidden /> Variables
        </button>
        <button
          type="button"
          className={`${barButton} ${rightPanel === "validation" ? "bg-indigo-600" : ""}`}
          onClick={onValidate}
          data-testid="validate-button"
        >
          <ShieldCheck className="size-4" aria-hidden /> Validate
          {issueCount > 0 && (
            <span className="rounded-full bg-red-500 px-1.5 text-[10px] font-semibold text-white" data-testid="issue-count">
              {issueCount}
            </span>
          )}
        </button>
        <TriggersButton />
        <button
          type="button"
          className={barButton}
          onClick={() => setUi({ deployOpen: true })}
          title="Publish this pipeline as an API endpoint"
          data-testid="deploy-button"
        >
          <Rocket className="size-4" aria-hidden /> Deploy
        </button>
        {active ? (
          <button
            type="button"
            onClick={() => void stop()}
            className="ml-1 flex items-center gap-1.5 rounded-md bg-red-500 px-3 py-1.5 text-xs font-semibold text-white hover:bg-red-400"
            data-testid="stop-button"
          >
            <Square className="size-3.5" aria-hidden /> Stop
          </button>
        ) : (
          <button
            type="button"
            onClick={onRun}
            className="ml-1 flex items-center gap-1.5 rounded-md bg-white px-3 py-1.5 text-xs font-semibold text-indigo-700 hover:bg-indigo-50"
          >
            <Play className="size-3.5" aria-hidden /> Run
          </button>
        )}
        <div className="ml-2">
          <UserMenu tone="dark" />
        </div>
      </div>
    </header>
  );
}

/** The floating Run/Stop button at the bottom center of the canvas. */
export function FloatingRunButton({ onRun }: { onRun: () => void }) {
  const runStatus = useEditor((s) => s.run.status);
  const { stop } = useRun();
  const active = runStatus === "pending" || runStatus === "running";
  const finished = isTerminal(runStatus);
  return (
    <div className="pointer-events-none absolute inset-x-0 bottom-5 z-10 flex justify-center">
      {active ? (
        <button
          type="button"
          onClick={() => void stop()}
          className="pointer-events-auto flex items-center gap-2 rounded-full bg-red-600 px-5 py-2.5 text-sm font-semibold text-white shadow-lg hover:bg-red-500"
        >
          <span className="size-2 animate-pulse rounded-full bg-white" aria-hidden />
          {runStatus === "pending" ? "Queued…" : "Running…"} Stop
        </button>
      ) : (
        <button
          type="button"
          onClick={onRun}
          data-testid="run-button"
          className="pointer-events-auto flex items-center gap-2 rounded-full bg-indigo-600 px-5 py-2.5 text-sm font-semibold text-white shadow-lg hover:bg-indigo-500"
        >
          <Play className="size-4" aria-hidden /> {finished ? "Run again" : "Run"}
        </button>
      )}
    </div>
  );
}
