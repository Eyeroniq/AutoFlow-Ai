"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, CircleAlert, Copy, KeyRound, LogIn, LogOut, Rocket, TriangleAlert } from "lucide-react";
import { type ReactNode, useMemo, useState } from "react";

import { ErrorAlert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { toast } from "@/components/ui/toast";
import { api, runIssues } from "@/lib/api";
import { API_URL } from "@/lib/config";
import { formatDateTime, formatRelative } from "@/lib/format";
import type { Deployment, DeploymentInput, DeploymentOutput, DeploymentWithKey, ValidationIssue } from "@/lib/types";

import { curlExample, exampleInputs, graphIo } from "./deploy";
import { getEditorStore, useEditor } from "./store";
import { useEditorUi } from "./ui-store";

const TYPE_TONE: Record<string, string> = {
  text: "bg-slate-100 text-slate-600",
  number: "bg-amber-50 text-amber-700",
  json: "bg-violet-50 text-violet-700",
  file: "bg-sky-50 text-sky-700",
};

function copyText(text: string, what: string) {
  if (!navigator.clipboard) {
    toast.error("Couldn't copy", "Select the text and copy it instead.");
    return;
  }
  navigator.clipboard.writeText(text).then(
    () => toast.info("Copied", what),
    () => toast.error("Couldn't copy", "Select the text and copy it instead."),
  );
}

function CopyButton({ text, what, testId }: { text: string; what: string; testId?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      onClick={() => {
        copyText(text, what);
        setDone(true);
        setTimeout(() => setDone(false), 1500);
      }}
      aria-label={`Copy ${what}`}
      className="flex shrink-0 items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-slate-600 hover:bg-slate-100 hover:text-indigo-600"
      data-testid={testId}
    >
      {done ? <Check className="size-3.5 text-emerald-600" aria-hidden /> : <Copy className="size-3.5" aria-hidden />}
      {done ? "Copied" : "Copy"}
    </button>
  );
}

function Section({ title, children, aside }: { title: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <section className="space-y-1.5">
      <div className="flex items-center justify-between">
        <h3 className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">{title}</h3>
        {aside}
      </div>
      {children}
    </section>
  );
}

function IoColumn({ title, icon, empty, children, testId }: { title: string; icon: ReactNode; empty: string; children: ReactNode[]; testId: string }) {
  return (
    <div className="min-w-0 rounded-lg border border-slate-200 bg-slate-50/60 p-3" data-testid={testId}>
      <h3 className="mb-2 flex items-center gap-1.5 text-sm font-semibold text-slate-900">
        {icon} {title}
      </h3>
      {children.length ? <ul className="space-y-1.5">{children}</ul> : <p className="text-xs text-slate-400">{empty}</p>}
    </div>
  );
}

function InputItem({ input }: { input: DeploymentInput }) {
  return (
    <li className="min-w-0 rounded-md bg-white px-2.5 py-1.5 ring-1 ring-slate-200">
      <p className="truncate text-xs font-medium text-slate-800" title={input.label}>
        {input.label}
      </p>
      <p className="mt-0.5 flex min-w-0 items-center gap-1.5 text-[11px]">
        <code className="truncate text-slate-600" title={input.name}>
          {input.name}
        </code>
        <span className={`shrink-0 rounded px-1.5 py-px font-medium ${TYPE_TONE[input.type] ?? TYPE_TONE.text}`}>{input.type}</span>
        <span className="shrink-0 text-slate-400">{input.required ? "required" : "optional"}</span>
      </p>
    </li>
  );
}

function OutputItem({ output }: { output: DeploymentOutput }) {
  return (
    <li className="min-w-0 rounded-md bg-white px-2.5 py-1.5 ring-1 ring-slate-200">
      <p className="truncate text-xs font-medium text-slate-800" title={output.label}>
        {output.label}
      </p>
      <code className="mt-0.5 block truncate text-[11px] text-slate-600" title={output.name}>
        final_output.{output.name}
      </code>
    </li>
  );
}

function Issues({ issues }: { issues: ValidationIssue[] }) {
  const setUi = useEditorUi((s) => s.set);
  return (
    <div className="rounded-lg bg-red-50 p-3 text-xs text-red-800" role="alert" data-testid="deploy-issues">
      <p className="mb-1 flex items-center gap-1.5 font-semibold">
        <CircleAlert className="size-3.5" aria-hidden /> Fix {issues.length === 1 ? "this problem" : `these ${issues.length} problems`} before deploying
      </p>
      <ul className="list-disc space-y-0.5 pl-5">
        {issues.slice(0, 5).map((issue, index) => (
          <li key={`${issue.node_id}-${issue.code}-${index}`}>{issue.message}</li>
        ))}
      </ul>
      <button
        type="button"
        onClick={() => setUi({ deployOpen: false, rightPanel: "validation" })}
        className="mt-2 font-semibold text-red-700 underline-offset-2 hover:underline"
      >
        Show them in the editor
      </button>
    </div>
  );
}

/** "Deploy" in the editor: publishes the pipeline as POST /api/v1/deployments/{id}/run. */
export function DeployDialog({ workflowId }: { workflowId: string }) {
  const open = useEditorUi((s) => s.deployOpen);
  const setUi = useEditorUi((s) => s.set);
  if (!open) return null;
  // Mounted only while open, so an API key shown once is dropped when the dialog closes.
  return <DeployDialogBody workflowId={workflowId} onClose={() => setUi({ deployOpen: false })} />;
}

function DeployDialogBody({ workflowId, onClose }: { workflowId: string; onClose: () => void }) {
  const queryClient = useQueryClient();
  const queryKey = ["deployment", workflowId];
  const existing = useQuery({
    queryKey,
    queryFn: async () => (await api.deployments.list({ workflow_id: workflowId }))[0] ?? null,
    meta: { silent: true },
  });
  const name = useEditor((s) => s.name);
  const nodes = useEditor((s) => s.nodes);
  const edges = useEditor((s) => s.edges);
  const version = useEditor((s) => s.version);
  const unsaved = useEditor((s) => s.save.status !== "saved");
  const draftIo = useMemo(() => graphIo(nodes, edges), [nodes, edges]);

  const [busy, setBusy] = useState<"deploy" | "rotate" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [issues, setIssues] = useState<ValidationIssue[]>([]);
  const [confirmRotate, setConfirmRotate] = useState(false);
  // The key from this dialog's deploy or rotation; the only time it can be shown.
  const [issued, setIssued] = useState<{ deploymentId: string; key: string } | null>(null);

  const deployment: Deployment | null = existing.data ?? null;
  const key = issued && deployment && issued.deploymentId === deployment.id ? issued.key : null;
  const behind = Boolean(deployment && (deployment.workflow_version !== version || unsaved));
  const io = deployment ?? draftIo;

  const received = ({ api_key: apiKey, ...result }: DeploymentWithKey) => {
    queryClient.setQueryData(queryKey, result);
    // The Triggers panel's webhook card shows the deployment.
    void queryClient.invalidateQueries({ queryKey: ["triggers", workflowId] });
    if (apiKey) setIssued({ deploymentId: result.id, key: apiKey });
    return apiKey;
  };

  const deploy = async () => {
    setBusy("deploy");
    setError(null);
    setIssues([]);
    try {
      // Deploy what's on the canvas: flush unsaved edits first.
      if (!(await getEditorStore().getState().saveNow())) {
        setError(`Your latest changes couldn't be saved, so nothing was deployed. ${getEditorStore().getState().save.error ?? ""}`.trim());
        return;
      }
      const redeploy = Boolean(deployment);
      const result = await api.deployments.deploy(workflowId);
      const apiKey = received(result);
      toast.success(
        redeploy ? `Redeployed (v${result.version})` : "Pipeline deployed",
        apiKey ? "Copy the API key now: it won't be shown again." : "The endpoint now runs the current version.",
      );
    } catch (caught) {
      const found = runIssues(caught);
      if (found.length) {
        setIssues(found);
        getEditorStore().getState().setIssues(found);
      } else {
        setError(caught instanceof Error ? caught.message : "Deploy failed");
      }
    } finally {
      setBusy(null);
    }
  };

  // Only the key changes: unsaved or undeployed edits stay unpublished.
  const rotateKey = async (deploymentId: string) => {
    setBusy("rotate");
    setError(null);
    try {
      received(await api.deployments.rotateKey(deploymentId));
      setConfirmRotate(false);
      toast.success("New API key issued", "The old key no longer works. Copy the new one now.");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Couldn't generate a new key");
    } finally {
      setBusy(null);
    }
  };

  const endpointUrl = deployment ? `${API_URL}${deployment.endpoint}` : null;
  const curl = deployment
    ? curlExample({ apiUrl: API_URL, endpoint: deployment.endpoint, apiKey: key, inputs: exampleInputs(deployment.inputs) })
    : null;

  return (
    <Dialog
      open
      wide
      title="Deploy pipeline"
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            Close
          </Button>
          <Button
            onClick={() => void deploy()}
            loading={busy === "deploy"}
            disabled={busy !== null || existing.isPending}
            variant={deployment && !behind ? "secondary" : "primary"}
            data-testid="deploy-confirm"
          >
            <Rocket className="size-4" aria-hidden /> {deployment ? "Redeploy" : "Deploy"}
          </Button>
        </>
      }
    >
      <div className="space-y-5" data-testid="deploy-dialog">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="min-w-0">
            <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">Pipeline</p>
            <p className="truncate text-base font-semibold text-slate-900" data-testid="deploy-pipeline-name" title={deployment?.name ?? name}>
              {deployment?.name ?? name}
            </p>
          </div>
          {existing.isPending ? (
            <span className="text-xs text-slate-400">Checking…</span>
          ) : deployment ? (
            <span
              className="flex items-center gap-1.5 rounded-full bg-emerald-50 px-2.5 py-1 text-xs font-medium text-emerald-700 ring-1 ring-inset ring-emerald-200"
              title={`Deployed ${formatDateTime(deployment.deployed_at)}`}
              data-testid="deploy-status"
            >
              <span className="size-1.5 rounded-full bg-emerald-500" aria-hidden /> Deployed v{deployment.version} · {formatRelative(deployment.deployed_at)}
            </span>
          ) : (
            <span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-medium text-slate-600" data-testid="deploy-status">
              Not deployed yet
            </span>
          )}
        </div>

        {existing.error && <ErrorAlert message={`Couldn't load the deployment: ${existing.error.message}`} />}
        {error && <ErrorAlert message={error} />}
        {issues.length > 0 && <Issues issues={issues} />}
        {behind && (
          <p className="flex items-start gap-2 rounded-lg bg-amber-50 p-3 text-xs text-amber-900" data-testid="deploy-behind">
            <TriangleAlert className="mt-px size-3.5 shrink-0" aria-hidden />
            The pipeline has changed since v{deployment?.version} was deployed. The endpoint keeps running the deployed version until you redeploy.
          </p>
        )}

        <div className="grid gap-3 sm:grid-cols-2">
          <IoColumn title="Inputs" icon={<LogIn className="size-4 text-emerald-600" aria-hidden />} empty="No Input nodes: the endpoint takes no inputs." testId="deploy-inputs">
            {io.inputs.map((input) => (
              <InputItem key={input.node_id} input={input} />
            ))}
          </IoColumn>
          <IoColumn title="Outputs" icon={<LogOut className="size-4 text-indigo-600" aria-hidden />} empty="No Output nodes: runs return an empty result." testId="deploy-outputs">
            {io.outputs.map((output) => (
              <OutputItem key={output.node_id} output={output} />
            ))}
          </IoColumn>
        </div>

        <Section title="Endpoint">
          <div className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white py-1 pl-2 pr-1">
            <span className="shrink-0 rounded bg-indigo-600 px-1.5 py-0.5 text-[10px] font-bold text-white">POST</span>
            {endpointUrl ? (
              <>
                <code className="min-w-0 flex-1 truncate text-xs text-slate-800" title={endpointUrl} data-testid="deploy-endpoint">
                  {endpointUrl}
                </code>
                <CopyButton text={endpointUrl} what="Endpoint URL" />
              </>
            ) : (
              <code className="min-w-0 flex-1 truncate py-1 text-xs text-slate-400" data-testid="deploy-endpoint">
                {API_URL}/api/v1/deployments/<span className="italic">{"{generated on deploy}"}</span>/run
              </code>
            )}
          </div>
        </Section>

        {deployment && (
          <Section title="API key">
            {key ? (
              <div className="space-y-1.5">
                <div className="flex items-center gap-2 rounded-lg border border-amber-300 bg-amber-50 py-1 pl-2 pr-1">
                  <KeyRound className="size-4 shrink-0 text-amber-700" aria-hidden />
                  <code className="min-w-0 flex-1 break-all text-xs text-slate-900" data-testid="deploy-api-key">
                    {key}
                  </code>
                  <CopyButton text={key} what="API key" testId="deploy-copy-key" />
                </div>
                <p className="text-[11px] text-amber-800">Copy it now. It is shown only this once: FlowForge stores just a hash of it.</p>
              </div>
            ) : confirmRotate ? (
              <div className="flex flex-wrap items-center gap-2 rounded-lg bg-red-50 p-2.5 text-xs text-red-800">
                <span className="flex-1">
                  The current key (<code>{deployment.api_key_prefix}…</code>) stops working immediately.
                </span>
                <Button size="sm" variant="secondary" onClick={() => setConfirmRotate(false)} disabled={busy !== null}>
                  Cancel
                </Button>
                <Button size="sm" className="bg-red-600 hover:bg-red-500" onClick={() => void rotateKey(deployment.id)} loading={busy === "rotate"} disabled={busy !== null} data-testid="deploy-rotate-confirm">
                  Revoke and generate
                </Button>
              </div>
            ) : (
              <div className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white py-1 pl-2 pr-1">
                <KeyRound className="size-4 shrink-0 text-slate-400" aria-hidden />
                <code className="min-w-0 flex-1 truncate text-xs text-slate-600" data-testid="deploy-key-prefix">
                  {deployment.api_key_prefix}••••••••
                </code>
                <span className="hidden shrink-0 text-[11px] text-slate-400 sm:inline">created {formatDateTime(deployment.key_created_at)}</span>
                <Button size="sm" variant="ghost" onClick={() => setConfirmRotate(true)} disabled={busy !== null} data-testid="deploy-rotate">
                  Generate new key
                </Button>
              </div>
            )}
          </Section>
        )}

        {curl && (
          <Section title="Try it" aside={<CopyButton text={curl} what="curl command" testId="deploy-copy-curl" />}>
            <pre className="overflow-x-auto rounded-lg bg-slate-900 p-3 text-[11px] leading-5 text-slate-100" data-testid="deploy-curl">
              {curl}
            </pre>
            <p className="text-[11px] leading-4 text-slate-500">
              {key ? "" : "Set FLOWFORGE_API_KEY to the key first (or generate a new one). "}
              With <code>?wait=true</code> the call waits for the run and answers with its final output. Leave it out to get an{" "}
              <code>execution_id</code> at once, then poll <code>links.status</code> with the same key.
            </p>
          </Section>
        )}
      </div>
    </Dialog>
  );
}
