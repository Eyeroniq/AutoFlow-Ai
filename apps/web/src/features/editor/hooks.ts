"use client";

import { useCallback, useEffect, useRef } from "react";

import { toast } from "@/components/ui/toast";
import { ApiError, api } from "@/lib/api";
import type { ValidationIssue } from "@/lib/types";

import { toApiGraph } from "./graph";
import { getEditorStore, useEditor } from "./store";

export const AUTOSAVE_DELAY_MS = 1000;
const VALIDATE_DELAY_MS = 700;

/**
 * Debounced autosave (~1 s after the last edit). A failed save stays visible in the top
 * bar and is retried with backoff (2 s, 4 s, ... 30 s); the first failure of a streak also
 * raises a toast with a Retry action. Leaving with unsaved changes asks first.
 */
export function useAutosave() {
  const revision = useEditor((s) => s.save.revision);
  const status = useEditor((s) => s.save.status);
  const session = useEditor((s) => s.session);
  const failures = useRef(0);

  useEffect(() => {
    if (revision === 0) return;
    const timer = setTimeout(() => void getEditorStore().getState().saveNow(), AUTOSAVE_DELAY_MS);
    return () => clearTimeout(timer);
  }, [revision, session]);

  useEffect(() => {
    if (status === "saved") {
      failures.current = 0;
      return;
    }
    if (status !== "error") return;
    failures.current += 1;
    if (failures.current === 1) {
      const error = getEditorStore().getState().save.error;
      toast.error("Couldn't save your changes", {
        message: `${error ?? "Save failed"}. Retrying automatically.`,
        action: { label: "Retry now", onClick: () => void getEditorStore().getState().saveNow() },
      });
    }
    const delay = Math.min(30_000, 2_000 * 2 ** (failures.current - 1));
    const timer = setTimeout(() => void getEditorStore().getState().saveNow(), delay);
    return () => clearTimeout(timer);
  }, [status]);

  useEffect(() => {
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      const { save } = getEditorStore().getState();
      if (save.revision > save.savedRevision || save.inFlight) {
        event.preventDefault();
      }
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, []);
}

/**
 * Validates the *current* (possibly unsaved) graph with the backend shortly after each
 * change, so node badges and inline field errors are exactly the server's messages.
 * Returns validateNow() for the Validate button.
 */
export function useLiveValidation() {
  const revision = useEditor((s) => s.save.revision);
  const session = useEditor((s) => s.session);
  const latest = useRef(0);

  const validateNow = useCallback(async (): Promise<ValidationIssue[] | null> => {
    const state = getEditorStore().getState();
    if (!state.workflowId) return null;
    const request = ++latest.current;
    const mySession = state.session;
    try {
      const result = await api.workflows.validate(state.workflowId, toApiGraph(state.nodes, state.edges, state.variables));
      if (request !== latest.current || getEditorStore().getState().session !== mySession) return null; // stale
      getEditorStore().getState().setIssues(result.errors);
      return result.errors;
    } catch (error) {
      if (request !== latest.current) return null;
      // A 422 here means the graph itself is malformed (e.g. an invalid variable key).
      const message = error instanceof ApiError ? error.message : "Validation request failed";
      const issues: ValidationIssue[] = [{ code: "request_failed", message, node_id: null, edge_id: null, field: null }];
      getEditorStore().getState().setIssues(issues);
      return issues;
    }
  }, []);

  useEffect(() => {
    if (!getEditorStore().getState().workflowId) return;
    const timer = setTimeout(() => void validateNow(), VALIDATE_DELAY_MS);
    return () => clearTimeout(timer);
  }, [revision, session, validateNow]);

  return validateNow;
}

/** Ctrl/Cmd+Z undo, Ctrl/Cmd+Shift+Z or Ctrl+Y redo, Ctrl/Cmd+D duplicate, Ctrl/Cmd+S save,
 * Delete/Backspace delete the selection, Escape clears it. Ignored while typing (except save). */
export function useKeyboardShortcuts() {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const store = getEditorStore().getState();
      const mod = event.ctrlKey || event.metaKey;
      const key = event.key.toLowerCase();
      const target = event.target as HTMLElement | null;
      const typing = Boolean(target?.closest("input, textarea, select, [contenteditable='true']"));

      if (mod && key === "s") {
        event.preventDefault();
        void store.saveNow();
        return;
      }
      if (typing || !store.workflowId) return;
      if (mod && key === "z" && !event.shiftKey) {
        event.preventDefault();
        store.undo();
      } else if (mod && (key === "y" || (key === "z" && event.shiftKey))) {
        event.preventDefault();
        store.redo();
      } else if (mod && key === "d") {
        event.preventDefault();
        store.duplicateNodes(store.nodes.filter((n) => n.selected).map((n) => n.id));
      } else if (event.key === "Delete" || event.key === "Backspace") {
        if (store.nodes.some((n) => n.selected) || store.edges.some((e) => e.selected)) {
          event.preventDefault();
          store.deleteSelection();
        }
      } else if (event.key === "Escape") {
        store.selectOnly(null);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);
}
