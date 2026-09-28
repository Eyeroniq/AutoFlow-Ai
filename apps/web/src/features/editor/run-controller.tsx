"use client";

import { createContext, type ReactNode, useCallback, useContext, useEffect, useMemo, useRef } from "react";

import { toast } from "@/components/ui/toast";
import { ApiError, api, runIssues } from "@/lib/api";

import { ExecutionSocket } from "../runs/execution-socket";
import { isTerminal, startRun } from "../runs/run-state";
import { topologicalOrder } from "./graph";
import { getEditorStore, useEditor } from "./store";
import { useEditorUi } from "./ui-store";

interface RunController {
  /** Save, queue the run (202), and follow it live. Resolves false if it didn't start. */
  run: (inputs: Record<string, unknown>) => Promise<boolean>;
  stop: () => Promise<void>;
  attach: (executionId: string) => void;
}

const RunContext = createContext<RunController | null>(null);

export function useRun(): RunController {
  const controller = useContext(RunContext);
  if (!controller) throw new Error("useRun must be used inside <RunControllerProvider>");
  return controller;
}

function nodeInfos() {
  const { nodes, edges } = getEditorStore().getState();
  const byId = new Map(nodes.map((n) => [n.id, n]));
  return topologicalOrder(nodes, edges).map((id) => {
    const node = byId.get(id)!;
    return { key: id, label: node.data.label, type: node.data.nodeType };
  });
}

export function RunControllerProvider({ workflowId, children }: { workflowId: string; children: ReactNode }) {
  const socket = useRef<ExecutionSocket | null>(null);
  const session = useEditor((s) => s.session);

  const attach = useCallback((executionId: string) => {
    socket.current?.close();
    const next = new ExecutionSocket(executionId, {
      onMessage: (message) => {
        if (socket.current !== next) return;
        getEditorStore().getState().applyRunMessage(message);
      },
      onStatus: (status, detail) => {
        if (socket.current !== next) return;
        useEditorUi.getState().set({ connection: { status, detail } });
        if (status === "error" && detail) toast.error("Live updates stopped", detail);
      },
    });
    socket.current = next;
    void next.connect();
  }, []);

  useEffect(
    () => () => {
      socket.current?.close();
      socket.current = null;
    },
    [],
  );

  // Late join: if this workflow has a run in progress, follow it (the snapshot replays state).
  useEffect(() => {
    if (!session) return;
    let cancelled = false;
    api.workflows
      .executions(workflowId, { limit: 1 })
      .then(([latest]) => {
        if (cancelled || !latest || isTerminal(latest.status)) return;
        getEditorStore().getState().setRun(startRun(latest.id, nodeInfos()));
        useEditorUi.getState().set({ runPanelOpen: true });
        attach(latest.id);
        toast.info("Following a run in progress", `Execution ${latest.id.slice(0, 8)} is ${latest.status}.`);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [session, workflowId, attach]);

  const run = useCallback(
    async (inputs: Record<string, unknown>) => {
      const store = getEditorStore().getState();
      // The server runs the saved graph, so flush edits first.
      if (!(await store.saveNow())) {
        toast.error("The run didn't start because saving failed", getEditorStore().getState().save.error ?? undefined);
        return false;
      }
      try {
        const accepted = await api.workflows.run(workflowId, inputs);
        getEditorStore().getState().setRun(startRun(accepted.execution_id, nodeInfos()));
        useEditorUi.getState().set({ runPanelOpen: true });
        attach(accepted.execution_id);
        return true;
      } catch (error) {
        const issues = runIssues(error);
        if (issues.length) {
          getEditorStore().getState().setIssues(issues);
          useEditorUi.getState().set({ rightPanel: "validation" });
          toast.error("Fix the workflow before running", `${issues.length} problem${issues.length === 1 ? "" : "s"} found.`);
        } else {
          toast.error("Couldn't start the run", error instanceof Error ? error.message : undefined);
        }
        return false;
      }
    },
    [workflowId, attach],
  );

  const stop = useCallback(async () => {
    const executionId = getEditorStore().getState().run.executionId;
    if (!executionId) return;
    try {
      const execution = await api.executions.stop(executionId);
      if (!isTerminal(execution.status)) toast.info("Stop requested", "The current node finishes first if it can't be interrupted.");
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 409)) {
        toast.error("Couldn't stop the run", error instanceof Error ? error.message : undefined);
      }
    }
  }, []);

  const value = useMemo(() => ({ run, stop, attach }), [run, stop, attach]);
  return <RunContext.Provider value={value}>{children}</RunContext.Provider>;
}
