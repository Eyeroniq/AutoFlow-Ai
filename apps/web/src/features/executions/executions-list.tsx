"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { AppShell, Spinner } from "@/components/app-shell";
import { ErrorAlert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status";
import { TriggerBadge } from "@/components/ui/trigger-badge";
import { api } from "@/lib/api";
import { formatDateTime, formatDuration, formatRelative } from "@/lib/format";
import type { ExecutionListItem, ExecutionStatus, ExecutionTrigger } from "@/lib/types";

import { isTerminal } from "../runs/run-state";

const PAGE = 25;
const STATUSES: ExecutionStatus[] = ["pending", "running", "success", "failed", "stopped"];
const TRIGGERS: ExecutionTrigger[] = ["manual", "schedule", "email", "webhook"];

/** A filter from the page's URL (?workflow_id=...&trigger=...), e.g. from the Triggers panel. */
function initialFilter(name: string): string {
  if (typeof window === "undefined") return "";
  return new URLSearchParams(window.location.search).get(name) ?? "";
}

/** Poll while anything listed is still queued or running. */
export const pollWhileActive = (rows: ExecutionListItem[] | undefined) => (rows?.some((row) => !isTerminal(row.status)) ? 3000 : false);

export function ExecutionRows({ rows, compact = false }: { rows: ExecutionListItem[]; compact?: boolean }) {
  const router = useRouter();
  return (
    <div className="overflow-hidden rounded-xl border border-slate-200 bg-white">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-slate-200 bg-slate-50 text-xs font-medium text-slate-500">
          <tr>
            <th className="px-4 py-2.5">Pipeline</th>
            <th className="px-4 py-2.5">Status</th>
            {!compact && <th className="hidden px-4 py-2.5 md:table-cell">Execution</th>}
            <th className="px-4 py-2.5">Trigger</th>
            <th className="px-4 py-2.5">Started</th>
            <th className="px-4 py-2.5 text-right">Duration</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {rows.map((row) => (
            <tr
              key={row.id}
              onClick={() => router.push(`/executions/${row.id}`)}
              className="cursor-pointer hover:bg-slate-50"
              data-testid="execution-row"
              data-status={row.status}
            >
              <td className="max-w-64 truncate px-4 py-2.5 font-medium text-slate-900">
                <Link href={`/executions/${row.id}`} onClick={(e) => e.stopPropagation()} className="hover:text-indigo-600">
                  {row.workflow_name}
                </Link>
              </td>
              <td className="px-4 py-2.5">
                <StatusBadge status={row.status} />
              </td>
              {!compact && <td className="hidden px-4 py-2.5 font-mono text-xs text-slate-500 md:table-cell">{row.id.slice(0, 8)}</td>}
              <td className="px-4 py-2.5">
                <TriggerBadge trigger={row.trigger} />
              </td>
              <td className="px-4 py-2.5 text-slate-600" title={formatDateTime(row.started_at ?? row.created_at)}>
                {row.started_at ? formatRelative(row.started_at) : `queued ${formatRelative(row.created_at)}`}
              </td>
              <td className="px-4 py-2.5 text-right font-mono text-xs text-slate-600">{formatDuration(row.duration_ms)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ExecutionsScreen() {
  return (
    <AppShell>
      <ExecutionsList />
    </AppShell>
  );
}

function ExecutionsList() {
  const [status, setStatus] = useState<ExecutionStatus | "">("");
  const [workflowId, setWorkflowId] = useState(() => initialFilter("workflow_id"));
  const [trigger, setTrigger] = useState<ExecutionTrigger | "">(() => initialFilter("trigger") as ExecutionTrigger | "");
  const [page, setPage] = useState(0);
  const workflows = useQuery({ queryKey: ["workflows"], queryFn: api.workflows.list });
  const executions = useQuery({
    queryKey: ["executions", { status, workflowId, trigger, page }],
    queryFn: () =>
      // One extra row tells us whether there's a next page.
      api.executions.list({
        limit: PAGE + 1,
        offset: page * PAGE,
        status: status || undefined,
        workflow_id: workflowId || undefined,
        trigger: trigger || undefined,
      }),
    placeholderData: keepPreviousData,
    refetchInterval: (query) => pollWhileActive(query.state.data),
  });
  const rows = executions.data?.slice(0, PAGE) ?? [];
  const hasNext = (executions.data?.length ?? 0) > PAGE;

  const select = "rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-700 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-100";
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex-1">
          <h1 className="text-xl font-semibold text-slate-900">Executions</h1>
          <p className="mt-1 text-sm text-slate-500">Every run of your pipelines, newest first. Running ones update live.</p>
        </div>
        <select
          aria-label="Filter by pipeline"
          value={workflowId}
          onChange={(e) => {
            setWorkflowId(e.target.value);
            setPage(0);
          }}
          className={select}
        >
          <option value="">All pipelines</option>
          {workflows.data?.map((w) => (
            <option key={w.id} value={w.id}>
              {w.name}
            </option>
          ))}
        </select>
        <select
          aria-label="Filter by status"
          value={status}
          onChange={(e) => {
            setStatus(e.target.value as ExecutionStatus | "");
            setPage(0);
          }}
          className={select}
        >
          <option value="">Any status</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <select
          aria-label="Filter by trigger"
          value={trigger}
          onChange={(e) => {
            setTrigger(e.target.value as ExecutionTrigger | "");
            setPage(0);
          }}
          className={select}
          data-testid="trigger-filter"
        >
          <option value="">Any trigger</option>
          {TRIGGERS.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      </div>

      {executions.isPending ? (
        <Spinner label="Loading executions…" />
      ) : executions.isError ? (
        <div className="space-y-3">
          <ErrorAlert message={executions.error.message} />
          <Button variant="secondary" onClick={() => void executions.refetch()}>
            Try again
          </Button>
        </div>
      ) : rows.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-300 bg-white p-10 text-center">
          <p className="text-sm text-slate-600">{status || workflowId || trigger ? "No executions match these filters." : "No runs yet."}</p>
          {!status && !workflowId && !trigger && (
            <Link href="/dashboard" className="mt-2 inline-block text-sm font-medium text-indigo-600 hover:text-indigo-500">
              Run a pipeline from the dashboard
            </Link>
          )}
        </div>
      ) : (
        <>
          <ExecutionRows rows={rows} />
          <div className="flex items-center justify-between text-sm text-slate-500">
            <span>
              Showing {page * PAGE + 1}–{page * PAGE + rows.length}
            </span>
            <div className="flex gap-2">
              <Button variant="secondary" size="sm" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>
                Previous
              </Button>
              <Button variant="secondary" size="sm" disabled={!hasNext} onClick={() => setPage((p) => p + 1)}>
                Next
              </Button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
