import type { ExecutionStatus, NodeExecutionStatus } from "@/lib/types";

type AnyStatus = ExecutionStatus | NodeExecutionStatus | "idle";

const DOT: Record<AnyStatus, string> = {
  idle: "bg-slate-300",
  pending: "bg-slate-400",
  running: "bg-blue-500 animate-pulse",
  success: "bg-emerald-500",
  failed: "bg-red-500",
  stopped: "bg-amber-500",
  skipped: "bg-slate-300",
};

const BADGE: Record<AnyStatus, string> = {
  idle: "bg-slate-100 text-slate-600 ring-slate-200",
  pending: "bg-slate-100 text-slate-700 ring-slate-200",
  running: "bg-blue-50 text-blue-700 ring-blue-200",
  success: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  failed: "bg-red-50 text-red-700 ring-red-200",
  stopped: "bg-amber-50 text-amber-800 ring-amber-200",
  skipped: "bg-slate-50 text-slate-500 ring-slate-200",
};

export function StatusDot({ status, className = "" }: { status: AnyStatus; className?: string }) {
  return <span aria-label={status} title={status} className={`inline-block size-2.5 shrink-0 rounded-full ${DOT[status] ?? DOT.idle} ${className}`} />;
}

export function StatusBadge({ status }: { status: AnyStatus }) {
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${BADGE[status] ?? BADGE.idle}`}>
      <StatusDot status={status} className="size-1.5" />
      {status}
    </span>
  );
}
