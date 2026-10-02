"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, FileText, Mail, Sparkles, Trash2, Upload } from "lucide-react";
import { useRef, useState } from "react";

import { AppShell, Spinner } from "@/components/app-shell";
import { ErrorAlert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { toast } from "@/components/ui/toast";
import { useCurrentUser } from "@/hooks/use-auth";
import { api } from "@/lib/api";
import { formatDateTime, formatRelative } from "@/lib/format";
import type { AtsOutput, ContentOutput, MatchOutput, ParsedResume, Refinement, RefinementSummary, Stage } from "@/lib/resume-types";

import {
  AgentSection,
  AtsBody,
  ContentBody,
  ExtractBody,
  MatchBody,
  ParsedBody,
  PipelineProgress,
  RewriteBody,
} from "./agent-sections";

const MAX_JD_CHARS = 30_000;
const STATUS_TONE: Record<RefinementSummary["status"], string> = {
  pending: "bg-slate-100 text-slate-700 ring-slate-200",
  running: "bg-blue-50 text-blue-700 ring-blue-200",
  success: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  failed: "bg-red-50 text-red-700 ring-red-200",
};

function stageOf(refinement: Refinement, key: Stage["key"]): Stage | undefined {
  return refinement.stages.find((s) => s.key === key);
}

function headline(stage: Stage | undefined): string | undefined {
  if (!stage || stage.status !== "success" || !stage.output) return undefined;
  const out = stage.output;
  if (stage.key === "ats") {
    const ats = out as unknown as AtsOutput;
    const blockers = ats.issues.filter((i) => i.severity === "blocker").length;
    return `Score ${ats.score}/100 · ${blockers} blocker${blockers === 1 ? "" : "s"}, ${ats.issues.length - blockers} warning${ats.issues.length - blockers === 1 ? "" : "s"}`;
  }
  if (stage.key === "content") {
    const flagged = (out as unknown as ContentOutput).bullets.filter((b) => b.flags.length > 0).length;
    return `${flagged} of ${(out as unknown as ContentOutput).bullets.length} bullets need work`;
  }
  if (stage.key === "job_match") {
    const m = out as unknown as MatchOutput;
    return `Match ${m.match_score}/100 · ${m.matched_keywords.length} matched, ${m.missing_keywords.length} missing`;
  }
  return undefined;
}

// --- Results ----------------------------------------------------------------------------------------

function Results({ refinement }: { refinement: Refinement }) {
  const queryClient = useQueryClient();
  const me = useCurrentUser();
  const parsed = stageOf(refinement, "parse")?.output as unknown as ParsedResume | undefined;
  const ats = stageOf(refinement, "ats");
  const content = stageOf(refinement, "content");
  const match = stageOf(refinement, "job_match");
  const rewrite = stageOf(refinement, "rewrite");
  const extract = stageOf(refinement, "extract");
  const parse = stageOf(refinement, "parse");
  const done = refinement.status === "success" && refinement.result;
  const email = useMutation({
    meta: { silent: true },
    mutationFn: () => api.resume.email(refinement.id),
    onSuccess: (sent) => {
      toast.success("Sent to your inbox", `${sent.to_email}: the PDF and the DOCX.`);
      void queryClient.invalidateQueries({ queryKey: ["resume-refinement", refinement.id] });
      void queryClient.invalidateQueries({ queryKey: ["resume-refinements"] });
    },
  });
  const download = useMutation({
    meta: { silent: true },
    mutationFn: (format: "pdf" | "docx") => api.resume.download(refinement.id, format),
  });
  const address = me.status === "authenticated" ? me.user.email : "your account's email address";

  return (
    <section className="space-y-4" data-testid="refinement-results" data-status={refinement.status}>
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-lg font-semibold text-slate-900">
          {refinement.filename} <span className="text-sm font-normal text-slate-500">version {refinement.version}</span>
        </h2>
        <span className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${STATUS_TONE[refinement.status]}`} data-testid="refinement-status">
          {refinement.status === "running" ? "analyzing…" : refinement.status}
        </span>
        <span className="text-xs text-slate-500">{refinement.has_job_description ? "with a job description" : "no job description"}</span>
      </div>
      <PipelineProgress stages={refinement.stages} />
      {(refinement.status === "pending" || refinement.status === "running") && <Spinner label="The agents are working through your resume…" />}
      {refinement.status === "failed" && refinement.error_message && <ErrorAlert message={refinement.error_message} />}

      <div className="space-y-3">
        {extract && extract.status !== "pending" && (
          <AgentSection stage={extract} headline={extract.output ? `${String(extract.output.page_count)} page(s) read` : undefined}>
            {extract.output && <ExtractBody output={extract.output} />}
          </AgentSection>
        )}
        {parse && parse.status !== "pending" && (
          <AgentSection stage={parse} headline={parsed ? `${parsed.experience.length} job(s), ${parsed.skills.length} skills` : undefined}>
            {parsed && <ParsedBody parsed={parsed} />}
          </AgentSection>
        )}
        {ats && ats.status !== "pending" && (
          <AgentSection stage={ats} headline={headline(ats)} defaultOpen>
            {ats.output && <AtsBody ats={ats.output as unknown as AtsOutput} />}
          </AgentSection>
        )}
        {content && content.status !== "pending" && (
          <AgentSection stage={content} headline={headline(content)} defaultOpen>
            {content.output && <ContentBody content={content.output as unknown as ContentOutput} parsed={parsed} />}
          </AgentSection>
        )}
        {match && match.status !== "pending" && (
          <AgentSection stage={match} headline={headline(match)} defaultOpen>
            {match.output && <MatchBody match={match.output as unknown as MatchOutput} parsed={parsed} />}
          </AgentSection>
        )}
        {rewrite && rewrite.status !== "pending" && (
          <AgentSection stage={rewrite} headline={done ? "Before and after, bullet by bullet" : undefined} defaultOpen>
            {refinement.result && <RewriteBody result={refinement.result} />}
          </AgentSection>
        )}
      </div>

      {done && (
        <div className="rounded-xl border border-indigo-200 bg-indigo-50/50 p-4" data-testid="deliver">
          <h3 className="text-sm font-semibold text-slate-900">Your refined resume</h3>
          <p className="mt-1 text-sm text-slate-600">
            A clean single-column draft with real headings and text, as a PDF and as an editable Word file. Read it through before you use it.
          </p>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Button variant="secondary" onClick={() => download.mutate("pdf")} loading={download.isPending && download.variables === "pdf"} data-testid="download-pdf">
              <Download className="size-4" aria-hidden /> Download PDF
            </Button>
            <Button variant="secondary" onClick={() => download.mutate("docx")} loading={download.isPending && download.variables === "docx"} data-testid="download-docx">
              <Download className="size-4" aria-hidden /> Download DOCX
            </Button>
            <Button onClick={() => email.mutate()} loading={email.isPending} data-testid="email-me">
              <Mail className="size-4" aria-hidden /> Email me the refined resume
            </Button>
          </div>
          <p className="mt-2 text-xs text-slate-500">
            Emailing sends both files to <span className="font-medium text-slate-700">{address}</span> only. It can&apos;t be sent to anyone else from here.
          </p>
          {download.isError && <div className="mt-2"><ErrorAlert message={download.error.message} /></div>}
          {email.isError && <div className="mt-2"><ErrorAlert message={email.error.message} /></div>}
          {refinement.emails.length > 0 && (
            <div className="mt-3 border-t border-indigo-100 pt-3" data-testid="email-history">
              <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Sent</h4>
              <ul className="mt-1 space-y-1 text-sm text-slate-700">
                {refinement.emails.map((e) => (
                  <li key={e.id}>
                    Version {e.version} to {e.to_email}, {formatDateTime(e.created_at)}
                    <span className="block text-xs text-slate-500">{e.summary}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

// --- Screen -------------------------------------------------------------------------------------------

export function ResumeRefinerScreen() {
  const queryClient = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [jd, setJd] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [progress, setProgress] = useState<number | null>(null);

  const history = useQuery({ queryKey: ["resume-refinements"], queryFn: api.resume.list, meta: { silent: true } });
  const detail = useQuery({
    queryKey: ["resume-refinement", selected],
    queryFn: () => api.resume.get(selected as string),
    enabled: selected !== null,
    // Poll while the agents work; the stages fill in as they finish.
    refetchInterval: (query) => (query.state.data && ["pending", "running"].includes(query.state.data.status) ? 1500 : false),
    meta: { silent: true },
  });

  const analyze = useMutation({
    meta: { silent: true },
    mutationFn: async () => {
      if (!file) throw new Error("Choose a PDF first");
      setProgress(0);
      const uploaded = await api.files.upload(file, { onProgress: setProgress });
      return api.resume.start(uploaded.id, jd);
    },
    onSuccess: (started) => {
      setSelected(started.id);
      queryClient.setQueryData(["resume-refinement", started.id], started);
      void queryClient.invalidateQueries({ queryKey: ["resume-refinements"] });
    },
    onSettled: () => setProgress(null),
  });
  const remove = useMutation({
    meta: { silent: true },
    mutationFn: (id: string) => api.resume.remove(id),
    onSuccess: (_, id) => {
      if (selected === id) setSelected(null);
      void queryClient.invalidateQueries({ queryKey: ["resume-refinements"] });
    },
  });

  const busy = analyze.isPending;
  return (
    <AppShell>
      <div className="mb-6">
        <h1 className="text-2xl font-semibold text-slate-900">Resume refiner</h1>
        <p className="mt-1 text-sm text-slate-600">
          Five specialist agents, one after another: a parser, an ATS checker, a content coach, a job matcher (if you paste a job description), and a rewriter.
          You see what each one found, then the improved draft.
        </p>
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <form
          className="space-y-4 rounded-xl border border-slate-200 bg-white p-4 shadow-sm lg:col-span-2"
          onSubmit={(e) => {
            e.preventDefault();
            analyze.mutate();
          }}
          data-testid="refine-form"
        >
          <div>
            <label htmlFor="resume-file" className="mb-1 block text-sm font-medium text-slate-800">
              Resume (PDF)
            </label>
            <button
              type="button"
              onClick={() => fileInput.current?.click()}
              className="flex w-full items-center gap-3 rounded-lg border border-dashed border-slate-300 px-4 py-4 text-left text-sm text-slate-600 hover:border-indigo-400 hover:bg-indigo-50/40"
            >
              {file ? <FileText className="size-5 text-indigo-600" aria-hidden /> : <Upload className="size-5 text-slate-400" aria-hidden />}
              <span>{file ? `${file.name} (${Math.round(file.size / 1024)} KB)` : "Choose a PDF resume (a text-based PDF, not a scan)"}</span>
            </button>
            <input
              ref={fileInput}
              id="resume-file"
              data-testid="resume-file"
              type="file"
              accept="application/pdf,.pdf"
              className="sr-only"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
          </div>
          <div>
            <label htmlFor="resume-jd" className="mb-1 block text-sm font-medium text-slate-800">
              Job description <span className="font-normal text-slate-500">(optional: adds the Job-Match agent and tailors the rewrite)</span>
            </label>
            <textarea
              id="resume-jd"
              data-testid="resume-jd"
              value={jd}
              onChange={(e) => setJd(e.target.value.slice(0, MAX_JD_CHARS))}
              rows={6}
              placeholder="Paste the job posting here…"
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-900 shadow-sm placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            />
          </div>
          {analyze.isError && <ErrorAlert message={analyze.error.message} />}
          <div className="flex items-center gap-3">
            <Button type="submit" loading={busy} disabled={!file} data-testid="analyze">
              <Sparkles className="size-4" aria-hidden /> Analyze
            </Button>
            {progress !== null && progress < 1 && <span className="text-xs text-slate-500">Uploading {Math.round(progress * 100)}%</span>}
          </div>
        </form>

        <aside className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm" data-testid="refine-history">
          <h2 className="text-sm font-semibold text-slate-900">Your refinements</h2>
          {history.isPending && <p className="mt-2 text-sm text-slate-500">Loading…</p>}
          {history.data && history.data.length === 0 && <p className="mt-2 text-sm text-slate-500">Nothing yet.</p>}
          <ul className="mt-2 space-y-1">
            {history.data?.map((row) => (
              <li key={row.id} className="flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => setSelected(row.id)}
                  data-testid="history-item"
                  className={`flex min-w-0 flex-1 flex-col rounded-md px-2 py-1.5 text-left text-sm hover:bg-slate-50 ${selected === row.id ? "bg-indigo-50" : ""}`}
                >
                  <span className="truncate font-medium text-slate-800">
                    {row.filename} · v{row.version}
                  </span>
                  <span className="text-xs text-slate-500">
                    {row.status} · {formatRelative(row.created_at)}
                    {row.emails_sent > 0 ? ` · emailed ${row.emails_sent}×` : ""}
                  </span>
                </button>
                <Button variant="ghost" size="sm" aria-label={`Delete ${row.filename} version ${row.version}`} onClick={() => remove.mutate(row.id)}>
                  <Trash2 className="size-3.5" aria-hidden />
                </Button>
              </li>
            ))}
          </ul>
        </aside>
      </div>

      <div className="mt-8">
        {selected && detail.isPending && <Spinner label="Loading…" />}
        {detail.isError && <ErrorAlert message={detail.error.message} />}
        {detail.data && <Results key={detail.data.id} refinement={detail.data} />}
      </div>
    </AppShell>
  );
}
