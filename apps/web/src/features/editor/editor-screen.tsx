"use client";

import { useQuery } from "@tanstack/react-query";
import { ReactFlowProvider } from "@xyflow/react";
import Link from "next/link";
import { useCallback, useEffect, useRef } from "react";

import { RequireAuth, Spinner } from "@/components/app-shell";
import { ErrorAlert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ApiError, api } from "@/lib/api";

import { Canvas } from "./canvas";
import { ConfigPanel, MultiSelectionPanel } from "./config-panel";
import { DeployDialog } from "./deploy-dialog";
import { useAutosave, useKeyboardShortcuts, useLiveValidation } from "./hooks";
import { NodeLibrary } from "./node-library";
import { RunControllerProvider, useRun } from "./run-controller";
import { RunInputsDialog, RunPanel } from "./run-panel";
import { ValidationPanel, VariablesPanel } from "./side-panels";
import { getEditorStore, useEditor } from "./store";
import { FloatingRunButton, TopBar } from "./top-bar";
import { PrivacyPanel } from "./privacy-panel";
import { TriggersPanel } from "./triggers-panel";
import { useEditorUi } from "./ui-store";

export function EditorScreen({ workflowId }: { workflowId: string }) {
  return (
    <RequireAuth>
      <ReactFlowProvider>
        <EditorLoader workflowId={workflowId} />
      </ReactFlowProvider>
    </RequireAuth>
  );
}

function EditorLoader({ workflowId }: { workflowId: string }) {
  const catalog = useQuery({ queryKey: ["nodes"], queryFn: api.nodes.list, staleTime: Infinity, meta: { silent: true } });
  // Loaded once per visit: refetching would overwrite unsaved edits.
  const workflow = useQuery({
    queryKey: ["workflow-editor", workflowId],
    queryFn: ({ signal }) => api.workflows.get(workflowId, signal),
    staleTime: Infinity,
    gcTime: 0,
    retry: (count, error) => !(error instanceof ApiError && error.status === 404) && count < 1,
    meta: { silent: true },
  });
  const loadedFor = useRef<string | null>(null);
  const loaded = useEditor((s) => s.workflowId === workflowId && s.session > 0);

  useEffect(() => {
    if (!workflow.data || !catalog.data || loadedFor.current === workflowId) return;
    loadedFor.current = workflowId;
    getEditorStore().getState().load(workflow.data, catalog.data);
    useEditorUi.getState().set({ rightPanel: null, runPanelOpen: false, connection: { status: "idle", detail: null } });
  }, [workflow.data, catalog.data, workflowId]);

  // Flush unsaved edits when leaving the editor.
  useEffect(() => () => void getEditorStore().getState().saveNow(), []);

  const error = workflow.error ?? catalog.error;
  if (error) {
    const missing = error instanceof ApiError && error.status === 404;
    return (
      <div className="flex flex-1 items-center justify-center p-8">
        <div className="max-w-md space-y-4">
          <ErrorAlert message={missing ? "This pipeline doesn't exist, or it isn't yours." : error.message} />
          <div className="flex gap-2">
            {!missing && (
              <Button variant="secondary" onClick={() => void (workflow.error ? workflow.refetch() : catalog.refetch())}>
                Try again
              </Button>
            )}
            <Link href="/dashboard" className="rounded-lg px-4 py-2.5 text-sm font-semibold text-indigo-600 hover:bg-indigo-50">
              Back to dashboard
            </Link>
          </div>
        </div>
      </div>
    );
  }
  if (!loaded) {
    return (
      <div className="flex flex-1 items-center justify-center p-8">
        <Spinner label="Loading the editor…" />
      </div>
    );
  }
  return (
    <RunControllerProvider workflowId={workflowId}>
      <EditorLayout workflowId={workflowId} />
    </RunControllerProvider>
  );
}

function RightPanel({ onValidate }: { onValidate: () => void }) {
  const rightPanel = useEditorUi((s) => s.rightPanel);
  const selected = useEditor((s) => s.nodes.filter((n) => n.selected).map((n) => n.id).join(","));
  const ids = selected ? selected.split(",") : [];
  if (rightPanel === "variables") return <VariablesPanel />;
  if (rightPanel === "validation") return <ValidationPanel onValidate={onValidate} />;
  if (rightPanel === "triggers") return <TriggersPanel />;
  if (rightPanel === "privacy") return <PrivacyPanel />;
  if (ids.length === 1) return <ConfigPanel key={ids[0]} nodeId={ids[0]} />;
  if (ids.length > 1) return <MultiSelectionPanel ids={ids} />;
  return null;
}

function EditorLayout({ workflowId }: { workflowId: string }) {
  useAutosave();
  useKeyboardShortcuts();
  const validateNow = useLiveValidation();
  const { run } = useRun();

  const onValidate = useCallback(() => {
    useEditorUi.getState().set({ rightPanel: "validation" });
    void validateNow();
  }, [validateNow]);

  const onRun = useCallback(() => {
    const hasInputs = getEditorStore().getState().nodes.some((n) => n.data.nodeType === "input");
    if (hasInputs) useEditorUi.getState().set({ runInputsOpen: true });
    else void run({});
  }, [run]);

  // The command palette's "Run current pipeline" bumps runRequest.
  const runRequest = useEditorUi((s) => s.runRequest);
  const handled = useRef(runRequest);
  useEffect(() => {
    if (runRequest === handled.current) return;
    handled.current = runRequest;
    onRun();
  }, [runRequest, onRun]);

  return (
    <div className="flex h-screen flex-col overflow-hidden" data-testid="editor">
      <TopBar onValidate={onValidate} onRun={onRun} />
      <div className="flex min-h-0 flex-1">
        <NodeLibrary />
        <div className="flex min-w-0 flex-1 flex-col">
          <div className="relative min-h-0 flex-1">
            <Canvas />
            <FloatingRunButton onRun={onRun} />
          </div>
          <RunPanel />
        </div>
        <RightPanel onValidate={onValidate} />
      </div>
      <RunInputsDialog />
      <DeployDialog workflowId={workflowId} />
    </div>
  );
}
