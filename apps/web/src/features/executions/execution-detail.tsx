"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  Clapperboard,
  ExternalLink,
  Square,
  Wifi,
  WifiOff,
} from "lucide-react";
import Link from "next/link";
import { type ReactNode, useState } from "react";

import { AppShell, Spinner } from "@/components/app-shell";
import { ErrorAlert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status";
import { toast } from "@/components/ui/toast";
import { TriggerBadge } from "@/components/ui/trigger-badge";
import { ApiError, api } from "@/lib/api";
import { formatDateTime, formatDuration, prettyJson } from "@/lib/format";

import { NodeTimeline } from "../runs/node-timeline";
import { OutputDownloads } from "../runs/output-downloads";
import { isTerminal } from "../runs/run-state";
import {
  type LiveExecution,
  useLiveExecution,
} from "../runs/use-live-execution";
import { PrivacyReport } from "./privacy-report";
import { ReplayView } from "./replay";

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-[11px] font-medium uppercase tracking-wide text-slate-400">
        {label}
      </dt>
      <dd className="mt-0.5 truncate text-sm text-slate-800">{children}</dd>
    </div>
  );
}

function JsonCard({
  title,
  value,
  testId,
  actions,
}: {
  title: string;
  value: unknown;
  testId?: string;
  actions?: ReactNode;
}) {
  return (
    <section className="rounded-xl border border-slate-200 bg-white">
      <div className="flex items-center justify-between border-b border-slate-100 px-4 py-2.5">
        <h2 className="text-sm font-semibold text-slate-900">{title}</h2>
        {actions}
      </div>
      {value === null ||
      value === undefined ||
      (typeof value === "object" && !Object.keys(value).length) ? (
        <p className="px-4 py-3 text-sm text-slate-400">None</p>
      ) : (
        <pre
          className="max-h-80 overflow-y-auto whitespace-pre-wrap wrap-anywhere p-4 text-xs leading-5 text-slate-800"
          data-testid={testId}
        >
          {prettyJson(value)}
        </pre>
      )}
    </section>
  );
}

function Connection({
  connection,
}: {
  connection: LiveExecution["connection"];
}) {
  if (connection.status === "idle" || connection.status === "closed")
    return null;
  const open = connection.status === "open";
  const failed = connection.status === "error";
  return (
    <span
      className={`flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${open ? "bg-emerald-50 text-emerald-700" : failed ? "bg-red-50 text-red-700" : "bg-amber-50 text-amber-800"}`}
      data-testid="connection-status"
    >
      {open ? <Wifi className="size-3" /> : <WifiOff className="size-3" />}
      {open
        ? "Live"
        : (connection.detail ??
          (connection.status === "connecting"
            ? "Connecting…"
            : "Reconnecting…"))}
    </span>
  );
}

export function ExecutionDetailScreen({
  executionId,
}: {
  executionId: string;
}) {
  return (
    <AppShell>
      <ExecutionDetail executionId={executionId} />
    </AppShell>
  );
}

function ExecutionDetail({ executionId }: { executionId: string }) {
  const { execution, run, error, missing, connection, refetch } =
    useLiveExecution(executionId);
  const [replaying, setReplaying] = useState(false);
  const queryClient = useQueryClient();
  const workflow = useQuery({
    queryKey: ["workflow", execution?.workflow_id],
    queryFn: ({ signal }) => api.workflows.get(execution!.workflow_id, signal),
    enabled: Boolean(execution),
    retry: false,
    meta: { silent: true },
  });
  const stop = useMutation({
    mutationFn: () => api.executions.stop(executionId),
    meta: { silent: true },
    onSuccess: () => {
      toast.info(
        "Stop requested",
        "The current node finishes first if it can't be interrupted.",
      );
      void queryClient.invalidateQueries({
        queryKey: ["execution", executionId],
      });
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409)
        refetch(); // already finished
      else
        toast.error(
          "Couldn't stop the run",
          err instanceof Error ? err.message : undefined,
        );
    },
  });

  const back = (
    <Link
      href="/executions"
      className="inline-flex items-center gap-1 text-sm font-medium text-slate-500 hover:text-slate-800"
    >
      <ArrowLeft className="size-4" /> All executions
    </Link>
  );

  if (error) {
    return (
      <div className="space-y-4">
        {back}
        <ErrorAlert
          message={
            missing
              ? "This execution doesn't exist, or it isn't yours."
              : error instanceof Error
                ? error.message
                : "Couldn't load the execution."
          }
        />
        {!missing && (
          <Button variant="secondary" onClick={refetch}>
            Try again
          </Button>
        )}
      </div>
    );
  }
  if (!execution) return <Spinner label="Loading the execution…" />;

  const active = !isTerminal(run.status);
  const replayable =
    !active &&
    Boolean(execution.graph) &&
    execution.node_executions.some((n) => n.started_at);
  const name =
    workflow.data?.name ?? (workflow.isError ? "Deleted pipeline" : "…");

  return (
    <div className="space-y-6">
      {back}
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="truncate text-xl font-semibold text-slate-900">
              {name}
            </h1>
            <span data-testid="execution-status">
              <StatusBadge
                status={run.status === "idle" ? "pending" : run.status}
              />
            </span>
            <Connection connection={connection} />
          </div>
          <p className="mt-1 font-mono text-xs text-slate-500">
            {execution.id}
          </p>
        </div>
        {workflow.data && (
          <Link
            href={`/pipelines/${execution.workflow_id}`}
            className="flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
          >
            Open in editor <ExternalLink className="size-3.5" />
          </Link>
        )}
        {replayable && (
          <Button
            variant={replaying ? "primary" : "secondary"}
            onClick={() => setReplaying((r) => !r)}
            data-testid="replay-toggle"
          >
            <Clapperboard className="size-3.5" aria-hidden />{" "}
            {replaying ? "Back to details" : "Replay"}
          </Button>
        )}
        {active && (
          <Button
            onClick={() => stop.mutate()}
            loading={stop.isPending}
            className="bg-red-600 hover:bg-red-500"
            data-testid="stop-execution"
          >
            <Square className="size-3.5" aria-hidden /> Stop
          </Button>
        )}
      </div>

      <dl className="grid grid-cols-2 gap-4 rounded-xl border border-slate-200 bg-white p-4 sm:grid-cols-3 lg:grid-cols-6">
        <Fact label="Started">
          {formatDateTime(run.startedAt ?? execution.started_at)}
        </Fact>
        <Fact label="Finished">
          {formatDateTime(run.finishedAt ?? execution.finished_at)}
        </Fact>
        <Fact label="Duration">
          {formatDuration(run.durationMs ?? execution.duration_ms)}
        </Fact>
        <Fact label="Trigger">
          <TriggerBadge trigger={execution.trigger} />
        </Fact>
        <Fact label="Queue">{execution.queue ?? "—"}</Fact>
        <Fact label="Worker">
          {run.worker ?? execution.worker_hostname ?? "—"}
        </Fact>
      </dl>

      {run.error && (
        <div
          className={`rounded-xl p-4 text-sm ${run.status === "stopped" ? "bg-amber-50 text-amber-900" : "bg-red-50 text-red-700"}`}
          data-testid="execution-error"
        >
          {run.error}
        </div>
      )}

      {replaying && replayable ? (
        <ReplayView execution={execution} />
      ) : (
        <div className="grid gap-6 lg:grid-cols-[1fr_22rem]">
          <section>
            <h2 className="mb-2 text-sm font-semibold text-slate-900">Nodes</h2>
            <NodeTimeline run={run} />
          </section>
          <div className="min-w-0 space-y-4">
            <PrivacyReport executionId={execution.id} status={run.status} />
            <JsonCard title="Inputs" value={execution.inputs} />
            <JsonCard
              title="Final output"
              value={run.finalOutput}
              testId="execution-output"
              actions={
                run.finalOutput && isTerminal(run.status) ? (
                  <OutputDownloads executionId={execution.id} />
                ) : undefined
              }
            />
          </div>
        </div>
      )}
    </div>
  );
}
