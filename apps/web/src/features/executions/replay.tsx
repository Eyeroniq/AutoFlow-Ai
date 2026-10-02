"use client";

import "@xyflow/react/dist/style.css";

import { useQuery } from "@tanstack/react-query";
import {
  Background,
  Controls,
  type Edge,
  Handle,
  type Node,
  type NodeProps,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useNodesState,
} from "@xyflow/react";
import { Pause, Play, RotateCcw } from "lucide-react";
import { memo, useEffect, useMemo, useRef, useState } from "react";

import { categoryStyle, NodeIcon } from "@/components/node-icon";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import type { ExecutionDetail, NodeType } from "@/lib/types";

import {
  buildTimeline,
  edgeState,
  formatOffset,
  type ReplayStatus,
  SPEEDS,
  stateAt,
} from "./replay-model";

// The live canvas's colors (features/editor/canvas.tsx and the status dots).
const EDGE_COLORS = {
  idle: "#94a3b8",
  running: "#3b82f6",
  success: "#10b981",
  failed: "#ef4444",
} as const;
const RING: Record<ReplayStatus, string> = {
  idle: "border-slate-200",
  running: "border-blue-500 ring-4 ring-blue-100",
  success: "border-emerald-500",
  failed: "border-red-500 ring-2 ring-red-100",
  skipped: "border-slate-200 opacity-50",
};
const DOT: Record<ReplayStatus, string> = {
  idle: "bg-slate-300",
  running: "bg-blue-500 animate-pulse",
  success: "bg-emerald-500",
  failed: "bg-red-500",
  skipped: "bg-slate-300",
};

type ReplayNodeData = {
  label: string;
  nodeType: string;
  status: ReplayStatus;
  entry?: NodeType;
  branches: string[];
};

function ReplayNodeCard({ data }: NodeProps<Node<ReplayNodeData>>) {
  const style = categoryStyle(data.entry?.category);
  return (
    <div
      className={`relative w-56 rounded-xl border-2 bg-white px-3 py-2 shadow-sm transition-all duration-200 ${RING[data.status]}`}
      data-testid="replay-node"
      data-status={data.status}
    >
      <span
        className={`absolute inset-y-0 left-0 w-1 rounded-l-xl ${style.accent}`}
        aria-hidden
      />
      {data.entry?.has_input !== false && (
        <Handle
          type="target"
          position={Position.Left}
          className="!size-2 !bg-slate-400"
        />
      )}
      <div className="flex items-center gap-2">
        <span
          className={`grid size-7 shrink-0 place-items-center rounded-lg ${style.tile}`}
        >
          <NodeIcon name={data.entry?.icon} className="size-4" />
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-semibold text-slate-900">
            {data.label}
          </p>
          <p className="truncate font-mono text-[10px] text-slate-400">
            {data.nodeType}
          </p>
        </div>
        <span
          className={`size-2.5 shrink-0 rounded-full ${DOT[data.status]}`}
          aria-label={data.status}
        />
      </div>
      {data.branches.length ? (
        data.branches.map((branch, i) => (
          <Handle
            key={branch}
            id={branch}
            type="source"
            position={Position.Right}
            style={{ top: `${((i + 1) / (data.branches.length + 1)) * 100}%` }}
            className="!size-2 !bg-slate-400"
          />
        ))
      ) : (
        <Handle
          type="source"
          position={Position.Right}
          className="!size-2 !bg-slate-400"
        />
      )}
    </div>
  );
}

const nodeTypes = { replay: memo(ReplayNodeCard) };

export function ReplayView({ execution }: { execution: ExecutionDetail }) {
  const catalog = useQuery({
    queryKey: ["nodes"],
    queryFn: api.nodes.list,
    staleTime: Infinity,
    meta: { silent: true },
  });
  const timeline = useMemo(() => buildTimeline(execution), [execution]);
  const [at, setAt] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<number>(1);
  const last = useRef<number | null>(null);

  // Advance the clock by real elapsed time x speed, one animation frame at a time.
  useEffect(() => {
    if (!playing) return;
    let frame = 0;
    const tick = (now: number) => {
      const step = last.current === null ? 0 : (now - last.current) * speed;
      last.current = now;
      setAt((current) => {
        const next = Math.min(timeline.duration, current + step);
        if (next >= timeline.duration) setPlaying(false);
        return next;
      });
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(frame);
      last.current = null;
    };
  }, [playing, speed, timeline.duration]);

  const states = useMemo(() => stateAt(timeline, at), [timeline, at]);
  const byType = useMemo(
    () => Object.fromEntries((catalog.data ?? []).map((n) => [n.type, n])),
    [catalog.data],
  );
  const graphNodes = (execution.graph?.nodes ?? []) as {
    id: string;
    type: string;
    label?: string | null;
    position?: { x: number; y: number };
  }[];
  const graphEdges = (execution.graph?.edges ?? []) as {
    source: string;
    target: string;
    source_handle?: string | null;
  }[];
  const labels = Object.fromEntries(
    timeline.steps.map((s) => [s.nodeKey, s.label]),
  );
  // React Flow keeps the nodes (and their measured sizes) in its own state; each frame only
  // changes a node's status, so nothing has to be measured again.
  const initial = useMemo<Node<ReplayNodeData>[]>(
    () =>
      graphNodes.map((n, i) => ({
        id: n.id,
        type: "replay",
        position: n.position ?? { x: i * 300, y: 0 },
        draggable: false,
        data: {
          label: n.label || labels[n.id] || byType[n.type]?.label || n.id,
          nodeType: n.type,
          status: "idle",
          entry: byType[n.type],
          branches: byType[n.type]?.branches ?? [],
        },
      })),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [execution.id, byType],
  );
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<ReplayNodeData>>(initial);
  useEffect(() => setNodes(initial), [initial, setNodes]);
  useEffect(() => {
    setNodes((current) =>
      current.map((n) => {
        const status = states[n.id] ?? "idle";
        return n.data.status === status ? n : { ...n, data: { ...n.data, status } };
      }),
    );
  }, [states, setNodes]);
  const edges: Edge[] = graphEdges.map((e) => {
    const status = edgeState(states[e.source], states[e.target]);
    return {
      id: `${e.source}:${e.source_handle ?? ""}->${e.target}`,
      source: e.source,
      target: e.target,
      sourceHandle: e.source_handle ?? undefined,
      animated: status === "running",
      style: { stroke: EDGE_COLORS[status], strokeWidth: 1.75 },
    };
  });
  const passed = timeline.events.filter((e) => e.at <= at);
  const finished = at >= timeline.duration;

  return (
    <section className="space-y-3" data-testid="replay">
      <div className="flex flex-wrap items-center gap-3 rounded-xl border border-slate-200 bg-white p-3">
        <Button
          size="sm"
          onClick={() => {
            if (finished) setAt(0);
            setPlaying((p) => !p || finished);
          }}
          data-testid="replay-play"
          aria-label={playing ? "Pause" : "Play"}
        >
          {playing ? (
            <Pause className="size-3.5" aria-hidden />
          ) : finished && at > 0 ? (
            <RotateCcw className="size-3.5" aria-hidden />
          ) : (
            <Play className="size-3.5" aria-hidden />
          )}
          {playing ? "Pause" : finished && at > 0 ? "Replay" : "Play"}
        </Button>
        <input
          type="range"
          min={0}
          max={timeline.duration}
          step={1}
          value={at}
          onChange={(e) => {
            setPlaying(false);
            setAt(Number(e.target.value));
          }}
          aria-label="Scrub through the run"
          className="min-w-48 flex-1 accent-indigo-600"
          data-testid="replay-scrub"
        />
        <span
          className="w-32 text-right font-mono text-xs tabular-nums text-slate-600"
          data-testid="replay-clock"
        >
          {formatOffset(at)} / {formatOffset(timeline.duration)}
        </span>
        <label className="flex items-center gap-1 text-xs text-slate-600">
          Speed
          <select
            value={speed}
            onChange={(e) => setSpeed(Number(e.target.value))}
            className="rounded border border-slate-300 px-1 py-0.5 text-xs"
            data-testid="replay-speed"
          >
            {SPEEDS.map((s) => (
              <option key={s} value={s}>
                {s}×
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="grid gap-3 lg:grid-cols-[1fr_18rem]">
        <div className="h-[28rem] overflow-hidden rounded-xl border border-slate-200 bg-slate-50">
          <ReactFlowProvider>
            <ReactFlow
              nodes={nodes}
              onNodesChange={onNodesChange}
              edges={edges}
              nodeTypes={nodeTypes}
              fitView
              nodesConnectable={false}
              elementsSelectable={false}
              proOptions={{ hideAttribution: true }}
            >
              <Background gap={16} />
              <Controls showInteractive={false} />
            </ReactFlow>
          </ReactFlowProvider>
        </div>
        <ol
          className="max-h-[28rem] space-y-1 overflow-y-auto rounded-xl border border-slate-200 bg-white p-3 text-xs"
          data-testid="replay-events"
        >
          <li className="pb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
            What happened
          </li>
          {timeline.events.map((event, i) => (
            <li
              key={`${event.nodeKey}-${event.kind}-${i}`}
              className={`flex gap-2 ${event.at <= at ? "text-slate-800" : "text-slate-300"}`}
              data-testid="replay-event"
              data-passed={event.at <= at}
            >
              <button
                type="button"
                className="w-14 shrink-0 text-right font-mono tabular-nums hover:text-indigo-600"
                onClick={() => {
                  setPlaying(false);
                  setAt(event.at);
                }}
              >
                +{formatOffset(event.at)}
              </button>
              <span className="truncate">
                {event.label}{" "}
                {event.kind === "started" ? "started" : event.status}
              </span>
            </li>
          ))}
          {timeline.events.length === 0 && (
            <li className="text-slate-400">No step of this run started.</li>
          )}
          <li className="pt-1 text-[11px] text-slate-400">
            {passed.length} of {timeline.events.length} events
          </li>
        </ol>
      </div>
    </section>
  );
}
