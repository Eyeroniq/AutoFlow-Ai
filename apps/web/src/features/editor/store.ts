/**
 * The editor's state: the graph (React Flow nodes/edges + workflow variables), bounded
 * undo/redo, the save state machine, validation issues, and the live run.
 *
 * createEditorStore() takes its I/O as a dependency so the logic is unit tested without a
 * browser; the app uses the `editorStore` singleton wired to the real API.
 */
import {
  applyEdgeChanges,
  applyNodeChanges,
  type Connection,
  type EdgeChange,
  type NodeChange,
  type XYPosition,
} from "@xyflow/react";
import { useStore } from "zustand";
import { createStore, type StoreApi } from "zustand/vanilla";

import { api } from "@/lib/api";
import type { ExecutionEvent, NodeType, ValidationIssue, Workflow, WorkflowUpdate, WorkflowVariable } from "@/lib/types";

import { idleRun, reduceRun, type RunState } from "../runs/run-state";
import {
  type Catalog,
  defaultConfig,
  duplicateOffset,
  edgeId,
  type FlowEdge,
  type FlowNode,
  fromApiGraph,
  toApiGraph,
  uniqueNodeId,
} from "./graph";

export const HISTORY_LIMIT = 100;
/** Edits of the same thing within this window become one undo step (typing, nudging). */
export const COALESCE_MS = 1000;

interface Snapshot {
  nodes: FlowNode[];
  edges: FlowEdge[];
  variables: WorkflowVariable[];
}

export type SaveStatus = "saved" | "dirty" | "saving" | "error";

export interface SaveState {
  status: SaveStatus;
  /** Bumped on every change that needs saving. */
  revision: number;
  /** The newest revision the server has confirmed. */
  savedRevision: number;
  inFlight: boolean;
  error: string | null;
  lastSavedAt: number | null;
}

export interface EditorDeps {
  saveWorkflow: (id: string, body: WorkflowUpdate) => Promise<Pick<Workflow, "version">>;
  now?: () => number;
}

export interface EditorState {
  workflowId: string | null;
  name: string;
  version: number;
  /** Increments on every load(); async results from an older load are dropped. */
  session: number;
  catalog: Catalog;
  nodes: FlowNode[];
  edges: FlowEdge[];
  variables: WorkflowVariable[];
  past: Snapshot[];
  future: Snapshot[];
  historyKey: string | null;
  historyAt: number;
  /** Bumps when the graph changes from outside the config form (load, undo, redo). */
  formEpoch: number;
  save: SaveState;
  issues: ValidationIssue[];
  run: RunState;
  renamingId: string | null;

  load: (workflow: Workflow, catalog: NodeType[]) => void;
  setName: (name: string) => void;
  onNodesChange: (changes: NodeChange<FlowNode>[]) => void;
  onEdgesChange: (changes: EdgeChange<FlowEdge>[]) => void;
  onConnect: (connection: Connection) => void;
  beginDrag: () => void;
  addNode: (type: string, position: XYPosition) => string | null;
  deleteSelection: () => void;
  deleteElements: (nodeIds: string[], edgeIds: string[]) => void;
  duplicateNodes: (ids: string[]) => string[];
  renameNode: (id: string, label: string) => void;
  setDescription: (id: string, description: string) => void;
  toggleCollapsed: (id: string) => void;
  updateConfig: (id: string, config: Record<string, unknown>, field?: string) => void;
  setVariables: (variables: WorkflowVariable[]) => void;
  selectOnly: (id: string | null) => void;
  setRenaming: (id: string | null) => void;
  undo: () => void;
  redo: () => void;
  saveNow: () => Promise<boolean>;
  setIssues: (issues: ValidationIssue[]) => void;
  setRun: (run: RunState) => void;
  applyRunMessage: (message: ExecutionEvent) => void;
}

const freshSave = (): SaveState => ({
  status: "saved",
  revision: 0,
  savedRevision: 0,
  inFlight: false,
  error: null,
  lastSavedAt: null,
});

const sameJson = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

export function createEditorStore(deps: EditorDeps): StoreApi<EditorState> {
  const now = deps.now ?? Date.now;
  let saving: Promise<boolean> | null = null;

  return createStore<EditorState>()((set, get) => {
    const snapshot = (): Snapshot => {
      const { nodes, edges, variables } = get();
      return { nodes, edges, variables };
    };

    const pushHistory = (key?: string) => {
      const state = get();
      const t = now();
      if (key && key === state.historyKey && t - state.historyAt < COALESCE_MS) {
        set({ historyAt: t });
        return;
      }
      set({
        past: [...state.past, snapshot()].slice(-HISTORY_LIMIT),
        future: [],
        historyKey: key ?? null,
        historyAt: t,
      });
    };

    const markDirty = () => {
      const { save } = get();
      set({ save: { ...save, revision: save.revision + 1, status: save.inFlight ? "saving" : "dirty" } });
    };

    const updateNode = (id: string, patch: (node: FlowNode) => FlowNode) =>
      set({ nodes: get().nodes.map((n) => (n.id === id ? patch(n) : n)) });

    const restore = (target: Snapshot) => {
      set({
        nodes: target.nodes,
        edges: target.edges,
        variables: target.variables,
        historyKey: null,
        formEpoch: get().formEpoch + 1,
      });
      markDirty();
    };

    const saveOnce = async (): Promise<boolean> => {
      const state = get();
      if (!state.workflowId) return true;
      if (state.save.revision === state.save.savedRevision && state.save.status !== "error") return true;
      const { session, workflowId } = state;
      const revision = state.save.revision;
      const body: WorkflowUpdate = { name: state.name, graph: toApiGraph(state.nodes, state.edges, state.variables) };
      set({ save: { ...state.save, inFlight: true, status: "saving", error: null } });
      try {
        const saved = await deps.saveWorkflow(workflowId, body);
        if (get().session !== session) return false; // another workflow was loaded meanwhile
        const current = get().save;
        const savedRevision = Math.max(current.savedRevision, revision);
        set({
          version: saved.version,
          save: {
            ...current,
            inFlight: false,
            savedRevision,
            error: null,
            lastSavedAt: now(),
            status: current.revision > savedRevision ? "dirty" : "saved",
          },
        });
        return true;
      } catch (error) {
        if (get().session !== session) return false;
        const message = error instanceof Error ? error.message : "Save failed";
        set({ save: { ...get().save, inFlight: false, status: "error", error: message } });
        return false;
      }
    };

    return {
      workflowId: null,
      name: "",
      version: 0,
      session: 0,
      catalog: {},
      nodes: [],
      edges: [],
      variables: [],
      past: [],
      future: [],
      historyKey: null,
      historyAt: 0,
      formEpoch: 0,
      save: freshSave(),
      issues: [],
      run: idleRun,
      renamingId: null,

      load: (workflow, catalog) => {
        const byType = Object.fromEntries(catalog.map((entry) => [entry.type, entry]));
        const graph = fromApiGraph(workflow.graph, byType);
        set({
          workflowId: workflow.id,
          name: workflow.name,
          version: workflow.version,
          session: get().session + 1,
          catalog: byType,
          nodes: graph.nodes,
          edges: graph.edges,
          variables: graph.variables,
          past: [],
          future: [],
          historyKey: null,
          historyAt: 0,
          formEpoch: get().formEpoch + 1,
          save: freshSave(),
          issues: [],
          run: idleRun,
          renamingId: null,
        });
      },

      setName: (name) => {
        if (name === get().name) return;
        set({ name });
        markDirty();
      },

      onNodesChange: (changes) => {
        const removed = changes.filter((c) => c.type === "remove").map((c) => c.id);
        if (removed.length) get().deleteElements(removed, []);
        const rest = changes.filter((c) => c.type !== "remove");
        if (!rest.length) return;
        const dragEnded = rest.some((c) => c.type === "position" && c.dragging === false);
        const nudged = rest.some((c) => c.type === "position" && c.dragging === undefined && c.position);
        if (nudged) pushHistory("nudge");
        set({ nodes: applyNodeChanges(rest, get().nodes) });
        if (dragEnded || nudged) markDirty();
      },

      onEdgesChange: (changes) => {
        const removed = changes.filter((c) => c.type === "remove").map((c) => c.id);
        if (removed.length) get().deleteElements([], removed);
        const rest = changes.filter((c) => c.type !== "remove");
        if (rest.length) set({ edges: applyEdgeChanges(rest, get().edges) });
      },

      onConnect: ({ source, target, sourceHandle, targetHandle }) => {
        if (!source || !target || source === target) return;
        const id = edgeId(source, sourceHandle, target);
        if (get().edges.some((e) => e.id === id)) return;
        pushHistory();
        set({ edges: [...get().edges, { id, source, target, sourceHandle: sourceHandle ?? null, targetHandle: targetHandle ?? null }] });
        markDirty();
      },

      beginDrag: () => pushHistory(),

      addNode: (type, position) => {
        const { catalog, nodes } = get();
        const entry = catalog[type];
        if (!entry) return null;
        const id = uniqueNodeId(type, nodes.map((n) => n.id));
        const node: FlowNode = {
          id,
          type: "flow",
          position,
          selected: true,
          data: { nodeType: type, label: entry.label, description: "", config: defaultConfig(type, id, nodes), collapsed: false },
        };
        pushHistory();
        set({ nodes: [...nodes.map((n) => (n.selected ? { ...n, selected: false } : n)), node] });
        markDirty();
        return id;
      },

      deleteSelection: () => {
        const { nodes, edges } = get();
        get().deleteElements(
          nodes.filter((n) => n.selected).map((n) => n.id),
          edges.filter((e) => e.selected).map((e) => e.id),
        );
      },

      deleteElements: (nodeIds, edgeIds) => {
        const nodeSet = new Set(nodeIds);
        const edgeSet = new Set(edgeIds);
        const { nodes, edges } = get();
        const keptEdges = edges.filter((e) => !edgeSet.has(e.id) && !nodeSet.has(e.source) && !nodeSet.has(e.target));
        if (!nodes.some((n) => nodeSet.has(n.id)) && keptEdges.length === edges.length) return;
        pushHistory();
        set({
          nodes: nodes.filter((n) => !nodeSet.has(n.id)),
          edges: keptEdges,
          renamingId: nodeSet.has(get().renamingId ?? "") ? null : get().renamingId,
        });
        markDirty();
      },

      duplicateNodes: (ids) => {
        const { nodes, edges } = get();
        const originals = nodes.filter((n) => ids.includes(n.id));
        if (!originals.length) return [];
        const taken = nodes.map((n) => n.id);
        const mapping = new Map<string, string>();
        const offset = duplicateOffset(originals, nodes);
        const copies = originals.map((original) => {
          const id = uniqueNodeId(original.data.nodeType, taken);
          taken.push(id);
          mapping.set(original.id, id);
          const config = { ...original.data.config };
          // Input/Output names must stay unique across the graph.
          if (original.data.nodeType === "input" || original.data.nodeType === "output") config.name = id;
          return {
            ...original,
            id,
            selected: true,
            position: { x: original.position.x + offset.x, y: original.position.y + offset.y },
            data: { ...original.data, config },
          } satisfies FlowNode;
        });
        const copiedEdges = edges
          .filter((e) => mapping.has(e.source) && mapping.has(e.target))
          .map((e) => {
            const source = mapping.get(e.source)!;
            const target = mapping.get(e.target)!;
            return { ...e, id: edgeId(source, e.sourceHandle, target), source, target, selected: false };
          });
        pushHistory();
        set({
          nodes: [...nodes.map((n) => (n.selected ? { ...n, selected: false } : n)), ...copies],
          edges: [...edges, ...copiedEdges],
        });
        markDirty();
        return copies.map((c) => c.id);
      },

      renameNode: (id, label) => {
        const node = get().nodes.find((n) => n.id === id);
        if (!node || node.data.label === label) return;
        pushHistory(`label:${id}`);
        updateNode(id, (n) => ({ ...n, data: { ...n.data, label } }));
        markDirty();
      },

      setDescription: (id, description) => {
        const node = get().nodes.find((n) => n.id === id);
        if (!node || node.data.description === description) return;
        pushHistory(`description:${id}`);
        updateNode(id, (n) => ({ ...n, data: { ...n.data, description } }));
        markDirty();
      },

      toggleCollapsed: (id) => {
        pushHistory();
        updateNode(id, (n) => ({ ...n, data: { ...n.data, collapsed: !n.data.collapsed } }));
        markDirty();
      },

      updateConfig: (id, config, field) => {
        const node = get().nodes.find((n) => n.id === id);
        if (!node || sameJson(node.data.config, config)) return;
        pushHistory(`config:${id}:${field ?? "*"}`);
        updateNode(id, (n) => ({ ...n, data: { ...n.data, config } }));
        markDirty();
      },

      setVariables: (variables) => {
        if (sameJson(variables, get().variables)) return;
        pushHistory("variables");
        set({ variables });
        markDirty();
      },

      selectOnly: (id) =>
        set({
          nodes: get().nodes.map((n) => (Boolean(n.selected) === (n.id === id) ? n : { ...n, selected: n.id === id })),
          edges: get().edges.map((e) => (e.selected ? { ...e, selected: false } : e)),
        }),

      setRenaming: (id) => set({ renamingId: id }),

      undo: () => {
        const { past, future } = get();
        const target = past.at(-1);
        if (!target) return;
        set({ past: past.slice(0, -1), future: [snapshot(), ...future].slice(0, HISTORY_LIMIT) });
        restore(target);
      },

      redo: () => {
        const { past, future } = get();
        const target = future[0];
        if (!target) return;
        set({ past: [...past, snapshot()].slice(-HISTORY_LIMIT), future: future.slice(1) });
        restore(target);
      },

      saveNow: () => {
        // One save at a time: callers during a save share it, and it saves again if
        // edits arrived while the request was in flight.
        saving ??= (async () => {
          try {
            while (true) {
              const ok = await saveOnce();
              const { save } = get();
              if (!ok || save.revision <= save.savedRevision) return ok;
            }
          } finally {
            saving = null;
          }
        })();
        return saving;
      },

      setIssues: (issues) => set({ issues }),
      setRun: (run) => set({ run }),
      applyRunMessage: (message) => set({ run: reduceRun(get().run, message) }),
    };
  });
}

// --- the app's instance -------------------------------------------------------------------------

let appStore: StoreApi<EditorState> | null = null;

export function getEditorStore(): StoreApi<EditorState> {
  appStore ??= createEditorStore({ saveWorkflow: (id, body) => api.workflows.update(id, body) });
  return appStore;
}

export function useEditor<T>(selector: (state: EditorState) => T): T {
  return useStore(getEditorStore(), selector);
}
