"use client";

import "@xyflow/react/dist/style.css";

import {
  Background,
  BackgroundVariant,
  Controls,
  type Edge,
  MarkerType,
  MiniMap,
  ReactFlow,
  useNodesInitialized,
  useReactFlow,
} from "@xyflow/react";
import { type DragEvent, useCallback, useEffect, useMemo, useRef } from "react";

import { categoryStyle } from "@/components/node-icon";

import { edgeRunStatus, type EdgeRunStatus, type FlowNode } from "./graph";
import { FlowNodeView } from "./flow-node";
import { getEditorStore, useEditor } from "./store";
import { useEditorUi } from "./ui-store";

export const DND_NODE_TYPE = "application/x-flowforge-node";

const nodeTypes = { flow: FlowNodeView };

const EDGE_COLORS: Record<EdgeRunStatus, string> = {
  idle: "#94a3b8", // gray: not run
  running: "#3b82f6", // blue, animated
  success: "#10b981", // green
  failed: "#ef4444", // red
};

/** The canvas's current center in flow coordinates (for click-to-add). */
export function useCanvasCenter() {
  const { screenToFlowPosition } = useReactFlow();
  return useCallback(() => {
    const rect = document.getElementById("editor-canvas")?.getBoundingClientRect();
    if (!rect) return { x: 0, y: 0 };
    return screenToFlowPosition({ x: rect.left + rect.width / 2 - 120, y: rect.top + rect.height / 2 - 40 });
  }, [screenToFlowPosition]);
}

export function Canvas() {
  const nodes = useEditor((s) => s.nodes);
  const edges = useEditor((s) => s.edges);
  const runNodes = useEditor((s) => s.run.nodes);
  const catalog = useEditor((s) => s.catalog);
  const session = useEditor((s) => s.session);
  const onNodesChange = useEditor((s) => s.onNodesChange);
  const onEdgesChange = useEditor((s) => s.onEdgesChange);
  const onConnect = useEditor((s) => s.onConnect);
  const beginDrag = useEditor((s) => s.beginDrag);
  const { screenToFlowPosition, fitView } = useReactFlow();
  const initialized = useNodesInitialized();
  const fittedSession = useRef(-1);

  // Fit the view once per loaded workflow, after the nodes have been measured.
  useEffect(() => {
    if (fittedSession.current === session || (!initialized && nodes.length)) return;
    fittedSession.current = session;
    requestAnimationFrame(() => void fitView({ padding: 0.25, maxZoom: 1.1, duration: 0 }));
  }, [initialized, session, nodes.length, fitView]);

  // A run opens the run panel and shrinks the canvas: re-fit so every node's status shows.
  const executionId = useEditor((s) => s.run.executionId);
  useEffect(() => {
    if (!executionId) return;
    const timer = setTimeout(() => void fitView({ padding: 0.2, maxZoom: 1.1, duration: 250 }), 60);
    return () => clearTimeout(timer);
  }, [executionId, fitView]);

  const styledEdges = useMemo<Edge[]>(
    () =>
      edges.map((edge) => {
        const status = edgeRunStatus(runNodes[edge.source]?.status, runNodes[edge.target]?.status);
        const color = edge.selected ? "#6366f1" : EDGE_COLORS[status];
        return {
          ...edge,
          type: "smoothstep",
          animated: status === "running",
          data: { runStatus: status },
          className: `edge-${status}`,
          style: { stroke: color, strokeWidth: edge.selected ? 2.5 : 1.75 },
          markerEnd: { type: MarkerType.ArrowClosed, color, width: 16, height: 16 },
        };
      }),
    [edges, runNodes],
  );

  const onDragOver = useCallback((event: DragEvent) => {
    if (event.dataTransfer.types.includes(DND_NODE_TYPE)) {
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
    }
  }, []);

  const onDrop = useCallback(
    (event: DragEvent) => {
      const type = event.dataTransfer.getData(DND_NODE_TYPE);
      if (!type) return;
      event.preventDefault();
      const position = screenToFlowPosition({ x: event.clientX - 120, y: event.clientY - 30 });
      getEditorStore().getState().addNode(type, position);
      useEditorUi.getState().set({ rightPanel: null });
    },
    [screenToFlowPosition],
  );

  return (
    <div id="editor-canvas" className="absolute inset-0 bg-white" data-testid="canvas">
      <ReactFlow<FlowNode, Edge>
        nodes={nodes}
        edges={styledEdges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        onNodeDragStart={beginDrag}
        onNodeClick={() => useEditorUi.getState().set({ rightPanel: null })}
        onDragOver={onDragOver}
        onDrop={onDrop}
        // Deletion goes through the keyboard hook so a node and its edges are one undo step.
        deleteKeyCode={null}
        selectionKeyCode="Shift"
        multiSelectionKeyCode={["Meta", "Control"]}
        snapToGrid
        snapGrid={[16, 16]}
        minZoom={0.2}
        maxZoom={2}
        // React Flow's attribution stays visible (hiding it requires React Flow Pro).
        attributionPosition="bottom-left"
      >
        <Background variant={BackgroundVariant.Dots} gap={16} size={1.4} color="#cbd5e1" />
        <Controls position="bottom-right" orientation="horizontal" showInteractive={false} style={{ marginRight: 184 }} />
        <MiniMap
          position="bottom-right"
          pannable
          zoomable
          style={{ width: 160, height: 100 }}
          ariaLabel="Minimap"
          nodeColor={(node) => categoryStyle(catalog[(node as FlowNode).data?.nodeType]?.category).color}
          nodeBorderRadius={6}
          className="!rounded-lg !border !border-slate-200 !shadow-sm"
        />
      </ReactFlow>
    </div>
  );
}
