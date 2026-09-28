"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, Ellipsis, Play, Plus, Trash2, Workflow as WorkflowIcon } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { AppShell, Spinner } from "@/components/app-shell";
import { ErrorAlert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ConfirmDialog, Dialog } from "@/components/ui/dialog";
import { FormField } from "@/components/ui/form-field";
import { Menu } from "@/components/ui/menu";
import { StatusBadge } from "@/components/ui/status";
import { toast } from "@/components/ui/toast";
import { api, runIssues } from "@/lib/api";
import { formatDateTime, formatRelative } from "@/lib/format";
import type { WorkflowListItem, WorkflowStatus } from "@/lib/types";

import { ExecutionRows, pollWhileActive } from "../executions/executions-list";
import { isTerminal } from "../runs/run-state";

const WORKFLOW_STATUS: Record<WorkflowStatus, string> = {
  draft: "bg-slate-100 text-slate-600",
  active: "bg-emerald-50 text-emerald-700",
  archived: "bg-amber-50 text-amber-800",
};

const createSchema = z.object({ name: z.string().trim().min(1, "Give it a name").max(255, "At most 255 characters") });

function CreateDialog({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const form = useForm<z.infer<typeof createSchema>>({ resolver: zodResolver(createSchema), defaultValues: { name: "" } });
  const create = useMutation({
    mutationFn: (name: string) => api.workflows.create({ name }),
    onSuccess: (workflow) => router.push(`/pipelines/${workflow.id}`),
  });
  const submit = form.handleSubmit(({ name }) => create.mutate(name));
  return (
    <Dialog
      open
      title="New pipeline"
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={() => void submit()} loading={create.isPending} data-testid="create-pipeline">
            Create and open
          </Button>
        </>
      }
    >
      <form onSubmit={(e) => void submit(e)} className="space-y-3">
        <FormField label="Name" placeholder="Summarize and email" autoFocus registration={form.register("name")} error={form.formState.errors.name} />
        <p className="text-xs text-slate-500">It starts empty; add nodes from the library in the editor.</p>
      </form>
    </Dialog>
  );
}

function PipelineRow({ workflow, onDelete }: { workflow: WorkflowListItem; onDelete: () => void }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const last = workflow.last_execution;

  const run = useMutation({
    meta: { silent: true },
    mutationFn: () => api.workflows.run(workflow.id, {}),
    onSuccess: (accepted) => {
      void queryClient.invalidateQueries({ queryKey: ["workflows"] });
      void queryClient.invalidateQueries({ queryKey: ["executions"] });
      router.push(`/executions/${accepted.execution_id}`);
    },
    onError: (error) => {
      const issues = runIssues(error);
      toast.error(issues.length ? `“${workflow.name}” needs fixing before it can run` : "Couldn't start the run", {
        message: issues.length ? issues.slice(0, 2).map((i) => i.message).join(" · ") : error.message,
        action: { label: "Open editor", onClick: () => router.push(`/pipelines/${workflow.id}`) },
      });
    },
  });
  const duplicate = useMutation({
    mutationFn: () => api.workflows.duplicate(workflow.id),
    onSuccess: (copy) => {
      void queryClient.invalidateQueries({ queryKey: ["workflows"] });
      toast.success("Pipeline duplicated", `Created “${copy.name}”.`);
    },
  });

  return (
    <tr className="hover:bg-slate-50" data-testid="pipeline-row" data-name={workflow.name}>
      <td className="max-w-80 px-4 py-3">
        <Link href={`/pipelines/${workflow.id}`} className="block truncate font-medium text-slate-900 hover:text-indigo-600">
          {workflow.name}
        </Link>
        <p className="truncate text-xs text-slate-500">
          {workflow.node_count} node{workflow.node_count === 1 ? "" : "s"}
          {workflow.description ? ` · ${workflow.description}` : ""}
        </p>
      </td>
      <td className="px-4 py-3">
        <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${WORKFLOW_STATUS[workflow.status]}`}>{workflow.status}</span>
      </td>
      <td className="px-4 py-3">
        {last ? (
          <Link href={`/executions/${last.id}`} className="flex items-center gap-2 hover:underline" title={formatDateTime(last.started_at ?? last.created_at)}>
            <StatusBadge status={last.status} />
            <span className="text-xs text-slate-500">{formatRelative(last.started_at ?? last.created_at)}</span>
          </Link>
        ) : (
          <span className="text-xs text-slate-400">Never run</span>
        )}
      </td>
      <td className="px-4 py-3 text-sm text-slate-600" title={formatDateTime(workflow.updated_at)}>
        {formatRelative(workflow.updated_at)}
      </td>
      <td className="px-4 py-3">
        <div className="flex items-center justify-end gap-1">
          <Link
            href={`/pipelines/${workflow.id}`}
            className="rounded-md px-2.5 py-1.5 text-sm font-medium text-indigo-600 hover:bg-indigo-50"
          >
            Open
          </Link>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => run.mutate()}
            loading={run.isPending}
            disabled={workflow.node_count === 0}
            title={workflow.node_count === 0 ? "Add nodes first" : "Run with default inputs"}
            aria-label={`Run ${workflow.name}`}
          >
            <Play className="size-3.5" aria-hidden /> Run
          </Button>
          <Menu
            label={`More actions for ${workflow.name}`}
            trigger={<Ellipsis className="size-4 text-slate-500" />}
            items={[
              { label: "Duplicate", icon: <Copy className="size-4" />, onSelect: () => duplicate.mutate() },
              { label: "Delete", icon: <Trash2 className="size-4" />, danger: true, onSelect: onDelete },
            ]}
          />
        </div>
      </td>
    </tr>
  );
}

export function DashboardScreen() {
  return (
    <AppShell>
      <Dashboard />
    </AppShell>
  );
}

function Dashboard() {
  const queryClient = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<WorkflowListItem | null>(null);
  const workflows = useQuery({
    queryKey: ["workflows"],
    queryFn: api.workflows.list,
    // Keep "last run" fresh while one of them is running.
    refetchInterval: (query) => (query.state.data?.some((w) => w.last_execution && !isTerminal(w.last_execution.status)) ? 3000 : false),
  });
  const recent = useQuery({
    queryKey: ["executions", "recent"],
    queryFn: () => api.executions.list({ limit: 6 }),
    refetchInterval: (query) => pollWhileActive(query.state.data),
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.workflows.remove(id),
    onSuccess: () => {
      setDeleting(null);
      void queryClient.invalidateQueries({ queryKey: ["workflows"] });
      void queryClient.invalidateQueries({ queryKey: ["executions"] });
      toast.success("Pipeline deleted");
    },
  });

  return (
    <div className="space-y-10">
      <section className="space-y-4">
        <div className="flex items-end justify-between gap-4">
          <div>
            <h1 className="text-xl font-semibold text-slate-900">Pipelines</h1>
            <p className="mt-1 text-sm text-slate-500">Build them in the editor; run them here or there.</p>
          </div>
          <Button onClick={() => setCreating(true)} data-testid="new-pipeline">
            <Plus className="size-4" aria-hidden /> New pipeline
          </Button>
        </div>

        {workflows.isPending ? (
          <Spinner label="Loading pipelines…" />
        ) : workflows.isError ? (
          <div className="space-y-3">
            <ErrorAlert message={workflows.error.message} />
            <Button variant="secondary" onClick={() => void workflows.refetch()}>
              Try again
            </Button>
          </div>
        ) : workflows.data.length === 0 ? (
          <div className="rounded-xl border border-dashed border-slate-300 bg-white p-12 text-center">
            <WorkflowIcon className="mx-auto size-8 text-slate-300" aria-hidden />
            <p className="mt-3 text-sm font-medium text-slate-700">No pipelines yet</p>
            <p className="mt-1 text-sm text-slate-500">Create one and drag nodes onto the canvas.</p>
            <Button className="mt-4" onClick={() => setCreating(true)}>
              <Plus className="size-4" aria-hidden /> New pipeline
            </Button>
          </div>
        ) : (
          <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white">
            <table className="w-full text-left text-sm">
              <thead className="border-b border-slate-200 bg-slate-50 text-xs font-medium text-slate-500">
                <tr>
                  <th className="px-4 py-2.5">Name</th>
                  <th className="px-4 py-2.5">Status</th>
                  <th className="px-4 py-2.5">Last run</th>
                  <th className="px-4 py-2.5">Modified</th>
                  <th className="px-4 py-2.5">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {workflows.data.map((workflow) => (
                  <PipelineRow key={workflow.id} workflow={workflow} onDelete={() => setDeleting(workflow)} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="space-y-4">
        <div className="flex items-end justify-between">
          <h2 className="text-base font-semibold text-slate-900">Recent executions</h2>
          <Link href="/executions" className="text-sm font-medium text-indigo-600 hover:text-indigo-500">
            View all
          </Link>
        </div>
        {recent.isPending ? (
          <Spinner label="Loading executions…" />
        ) : recent.isError ? (
          <ErrorAlert message={recent.error.message} />
        ) : recent.data.length === 0 ? (
          <p className="rounded-xl border border-dashed border-slate-300 bg-white p-6 text-center text-sm text-slate-500">No runs yet.</p>
        ) : (
          <ExecutionRows rows={recent.data} compact />
        )}
      </section>

      {creating && <CreateDialog onClose={() => setCreating(false)} />}
      <ConfirmDialog
        open={deleting !== null}
        title="Delete pipeline?"
        message={
          <>
            “{deleting?.name}” and its run history will be deleted. This can&apos;t be undone.
          </>
        }
        onConfirm={() => deleting && remove.mutate(deleting.id)}
        onClose={() => setDeleting(null)}
        busy={remove.isPending}
      />
    </div>
  );
}
