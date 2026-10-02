"use client";

import { Check, ChevronDown, CircleAlert, Minus, TriangleAlert, X } from "lucide-react";
import type { ReactNode } from "react";

import { formatDuration } from "@/lib/format";
import type {
  AtsOutput,
  BulletChange,
  BulletFlag,
  ContentOutput,
  MatchOutput,
  ParsedResume,
  RefinementResult,
  Stage,
  StageKey,
  StageStatus,
} from "@/lib/resume-types";

import { type DiffPart, diffWords } from "./diff";

export const FLAG_LABEL: Record<BulletFlag, string> = {
  weak_opening_verb: "Weak opening verb",
  no_metric: "No metric",
  vague_claim: "Vague claim",
  passive_voice: "Passive voice",
};

export const STAGE_ORDER: StageKey[] = ["extract", "parse", "ats", "content", "job_match", "rewrite"];

const STAGE_SHORT: Record<StageKey, string> = {
  extract: "Extract",
  parse: "Parser",
  ats: "ATS",
  content: "Content",
  job_match: "Job match",
  rewrite: "Rewrite",
};

/** "Title · Company" without the blanks a project or volunteering entry may have. */
export function jobHeading(job: { title: string; company: string }): string {
  return [job.title, job.company].filter((part) => part.trim()).join(" · ") || "Experience";
}

/** Bullet text by id ("e<job>.b<bullet>"), the ids the agents refer to. */
export function bulletTexts(parsed: ParsedResume | undefined): Map<string, string> {
  const map = new Map<string, string>();
  parsed?.experience.forEach((job, i) => job.bullets.forEach((text, j) => map.set(`e${i}.b${j}`, text)));
  return map;
}

function StageIcon({ status }: { status: StageStatus }) {
  if (status === "success") return <Check className="size-3.5 text-emerald-600" aria-hidden />;
  if (status === "failed") return <X className="size-3.5 text-red-600" aria-hidden />;
  if (status === "skipped") return <Minus className="size-3.5 text-slate-400" aria-hidden />;
  if (status === "running") {
    return <span className="size-3 animate-spin rounded-full border-2 border-indigo-500 border-t-transparent" aria-hidden />;
  }
  return <span className="size-2 rounded-full bg-slate-300" aria-hidden />;
}

/** The pipeline at a glance: one pill per stage, filling in as the agents finish. */
export function PipelineProgress({ stages }: { stages: Stage[] }) {
  return (
    <ol className="flex flex-wrap items-center gap-2" data-testid="pipeline-progress">
      {STAGE_ORDER.map((key) => {
        const stage = stages.find((s) => s.key === key);
        const status = stage?.status ?? "pending";
        return (
          <li
            key={key}
            data-testid={`stage-${key}`}
            data-status={status}
            className="inline-flex items-center gap-1.5 rounded-full bg-white px-2.5 py-1 text-xs font-medium text-slate-700 ring-1 ring-inset ring-slate-200"
          >
            <StageIcon status={status} />
            {STAGE_SHORT[key]}
          </li>
        );
      })}
    </ol>
  );
}

function Chip({ children, tone }: { children: ReactNode; tone: "red" | "amber" | "green" | "slate" | "indigo" }) {
  const tones = {
    red: "bg-red-50 text-red-700 ring-red-200",
    amber: "bg-amber-50 text-amber-800 ring-amber-200",
    green: "bg-emerald-50 text-emerald-700 ring-emerald-200",
    slate: "bg-slate-100 text-slate-700 ring-slate-200",
    indigo: "bg-indigo-50 text-indigo-700 ring-indigo-200",
  };
  return <span className={`inline-flex shrink-0 items-center whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${tones[tone]}`}>{children}</span>;
}

function RawOutput({ stage }: { stage: Stage }) {
  const failed = stage.attempts.filter((a) => a.problems.length > 0);
  return (
    <details className="mt-4 rounded-lg border border-slate-200 bg-slate-50" data-testid={`raw-${stage.key}`}>
      <summary className="cursor-pointer select-none px-3 py-2 text-xs font-medium text-slate-600">
        The agent&apos;s raw output ({stage.attempts.length} {stage.attempts.length === 1 ? "reply" : "replies"})
      </summary>
      <div className="space-y-3 border-t border-slate-200 p-3 text-xs">
        <p className="text-slate-500">
          Answered by <span className="font-medium text-slate-700">{stage.provider_used ?? "no provider"}</span>
          {stage.model ? ` (${stage.model})` : ""}
          {stage.duration_ms != null ? ` in ${formatDuration(stage.duration_ms)}` : ""}
          {stage.fallback_errors.length > 0 && ` after ${stage.fallback_errors.length} failed provider attempt(s)`}.
        </p>
        {stage.fallback_errors.map((e, i) => (
          <p key={i} className="rounded bg-amber-50 p-2 text-amber-800">
            {e.provider}: {e.error}
          </p>
        ))}
        {stage.warnings && stage.warnings.length > 0 && (
          <div className="rounded bg-amber-50 p-2 text-amber-800">
            Accepted with warnings (corrected afterwards):
            <ul className="mt-1 list-disc pl-4">
              {stage.warnings.map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
          </div>
        )}
        {stage.prompt && (
          <details>
            <summary className="cursor-pointer font-medium text-slate-600">Instructions this agent was given</summary>
            <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap rounded bg-white p-2 text-slate-700 ring-1 ring-slate-200">{stage.prompt}</pre>
          </details>
        )}
        {stage.attempts.map((attempt, i) => (
          <details key={i} open={stage.attempts.length === 1 || i === stage.attempts.length - 1}>
            <summary className="cursor-pointer font-medium text-slate-600">
              Reply {i + 1}
              {attempt.problems.length > 0 ? ` (rejected: ${attempt.problems.length} problem${attempt.problems.length === 1 ? "" : "s"})` : " (accepted)"}
            </summary>
            {attempt.problems.length > 0 && (
              <ul className="mt-1 list-disc pl-4 text-red-700">
                {attempt.problems.map((p, k) => (
                  <li key={k}>{p.replace(/^~/, "")}</li>
                ))}
              </ul>
            )}
            <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap rounded bg-white p-2 text-slate-700 ring-1 ring-slate-200">{attempt.raw}</pre>
          </details>
        ))}
        {failed.length === 0 && stage.attempts.length === 0 && <p className="text-slate-500">No model reply was recorded.</p>}
      </div>
    </details>
  );
}

/** One agent as an expandable section: what it is for, how it ran, its findings, and its raw output. */
export function AgentSection({
  stage,
  headline,
  defaultOpen = false,
  children,
}: {
  stage: Stage;
  headline?: ReactNode;
  defaultOpen?: boolean;
  children?: ReactNode;
}) {
  return (
    <details
      open={defaultOpen}
      className="group rounded-xl border border-slate-200 bg-white shadow-sm"
      data-testid={`section-${stage.key}`}
      data-status={stage.status}
    >
      <summary className="flex cursor-pointer list-none items-center gap-3 px-4 py-3">
        <StageIcon status={stage.status} />
        <div className="min-w-0 flex-1">
          <h3 className="text-sm font-semibold text-slate-900">{stage.title}</h3>
          <p className="truncate text-xs text-slate-500">{stage.status === "skipped" ? stage.note : headline ?? stage.role}</p>
        </div>
        {stage.duration_ms != null && <span className="hidden text-xs text-slate-400 sm:inline">{formatDuration(stage.duration_ms)}</span>}
        <ChevronDown className="size-4 text-slate-400 transition-transform group-open:rotate-180" aria-hidden />
      </summary>
      <div className="border-t border-slate-100 px-4 pb-4 pt-3">
        <p className="mb-3 text-xs text-slate-500">{stage.role}</p>
        {stage.status === "failed" && stage.error && (
          <div role="alert" className="mb-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
            {stage.error}
          </div>
        )}
        {children}
        {stage.attempts.length > 0 && <RawOutput stage={stage} />}
      </div>
    </details>
  );
}

// --- Extraction and parse -------------------------------------------------------------------------------

export function ExtractBody({ output }: { output: Record<string, unknown> }) {
  const layout = output.layout as { pages: { page: number; columns: number; images: number; tables: unknown[]; text_chars: number }[]; nonstandard_headers: string[]; section_headers: string[] } | undefined;
  return (
    <div className="space-y-2 text-sm text-slate-700">
      <p>
        {String(output.page_count ?? "?")} page(s), {String(output.char_count ?? "?")} characters of selectable text.
      </p>
      {layout?.pages.map((p) => (
        <p key={p.page} className="text-xs text-slate-500">
          Page {p.page}: {p.columns} text column{p.columns === 1 ? "" : "s"}, {p.images} image(s), {p.tables.length} table(s).
        </p>
      ))}
      {layout && layout.section_headers.length > 0 && (
        <p className="text-xs text-slate-500">Headers found: {layout.section_headers.join(", ")}</p>
      )}
    </div>
  );
}

export function ParsedBody({ parsed }: { parsed: ParsedResume }) {
  return (
    <div className="space-y-3 text-sm text-slate-700" data-testid="parsed-structure">
      <p>
        <span className="font-medium">{parsed.contact_info.name || "(no name)"}</span>
        <span className="text-slate-500">
          {[parsed.contact_info.email, parsed.contact_info.phone, parsed.contact_info.location].filter(Boolean).map((v) => ` · ${v}`)}
        </span>
      </p>
      {parsed.summary && <p className="text-slate-600">{parsed.summary}</p>}
      {parsed.experience.map((job, i) => (
        <div key={i}>
          <p className="font-medium">
            {jobHeading(job)} {job.dates && <span className="font-normal text-slate-500">({job.dates})</span>}
          </p>
          <p className="text-xs text-slate-500">{job.bullets.length} bullet(s)</p>
        </div>
      ))}
      <p className="text-xs text-slate-500">
        {parsed.education.length} education entr{parsed.education.length === 1 ? "y" : "ies"} · {parsed.skills.length} skills
      </p>
    </div>
  );
}

// --- ATS -------------------------------------------------------------------------------------------------

export function AtsBody({ ats }: { ats: AtsOutput }) {
  const tone = ats.score >= 80 ? "green" : ats.score >= 55 ? "amber" : "red";
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <Chip tone={tone}>ATS score {ats.score}/100</Chip>
        <p className="text-sm text-slate-600">{ats.summary}</p>
      </div>
      {ats.issues.length === 0 && <p className="text-sm text-emerald-700">No ATS problems found.</p>}
      <ul className="space-y-2">
        {ats.issues.map((issue, i) => (
          <li key={i} className="rounded-lg border border-slate-200 p-3" data-testid="ats-issue" data-severity={issue.severity}>
            <div className="flex flex-wrap items-center gap-2">
              {issue.severity === "blocker" ? (
                <Chip tone="red">
                  <CircleAlert className="mr-1 size-3" aria-hidden />
                  Blocker
                </Chip>
              ) : (
                <Chip tone="amber">
                  <TriangleAlert className="mr-1 size-3" aria-hidden />
                  Warning
                </Chip>
              )}
              <span className="text-sm font-medium text-slate-900">{issue.title}</span>
              {issue.source === "layout" && <Chip tone="slate">measured in the PDF</Chip>}
            </div>
            <p className="mt-1.5 text-sm text-slate-600">{issue.explanation}</p>
            {issue.evidence && <p className="mt-1 text-xs text-slate-500">Evidence: {issue.evidence}</p>}
            <p className="mt-1 text-sm text-slate-800">
              <span className="font-medium">Fix:</span> {issue.fix}
            </p>
          </li>
        ))}
      </ul>
    </div>
  );
}

// --- Content ------------------------------------------------------------------------------------------------

export function ContentBody({ content, parsed }: { content: ContentOutput; parsed: ParsedResume | undefined }) {
  const texts = bulletTexts(parsed);
  const byId = new Map(content.bullets.map((b) => [b.id, b]));
  return (
    <div className="space-y-4">
      <p className="text-sm text-slate-600">{content.overall}</p>
      {content.summary_review && (
        <p className="rounded-lg bg-indigo-50 px-3 py-2 text-sm text-indigo-900">
          <span className="font-medium">Summary: </span>
          {content.summary_review}
        </p>
      )}
      {parsed?.experience.map((job, i) => (
        <div key={i}>
          <h4 className="mb-1.5 text-sm font-semibold text-slate-800">{jobHeading(job)}</h4>
          <ul className="space-y-2">
            {job.bullets.map((_, j) => {
              const id = `e${i}.b${j}`;
              const item = byId.get(id);
              const flagged = !!item && item.flags.length > 0;
              return (
                <li
                  key={id}
                  data-testid="content-bullet"
                  data-flagged={flagged}
                  className={`rounded-lg border p-3 text-sm ${flagged ? "border-amber-200 bg-amber-50/40" : "border-slate-200"}`}
                >
                  <p className="text-slate-800">{texts.get(id)}</p>
                  {flagged && item ? (
                    <div className="mt-2 space-y-1.5">
                      <div className="flex flex-wrap gap-1.5">
                        {item.flags.map((f) => (
                          <Chip key={f} tone="amber">
                            {FLAG_LABEL[f]}
                          </Chip>
                        ))}
                      </div>
                      <p className="text-slate-600">{item.explanation}</p>
                      <p className="rounded bg-emerald-50 px-2 py-1.5 text-emerald-900">
                        <span className="font-medium">Suggested: </span>
                        {item.rewrite}
                      </p>
                    </div>
                  ) : (
                    <p className="mt-1 flex items-center gap-1 text-xs text-emerald-700">
                      <Check className="size-3" aria-hidden /> Strong as written
                    </p>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </div>
  );
}

// --- Job match ------------------------------------------------------------------------------------------------

const STRENGTH_TONE = { strong: "green", partial: "amber", weak: "slate", none: "red" } as const;
const IMPORTANCE_TONE = { high: "red", medium: "amber", low: "slate" } as const;

export function MatchBody({ match, parsed }: { match: MatchOutput; parsed: ParsedResume | undefined }) {
  const texts = bulletTexts(parsed);
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <Chip tone={match.match_score >= 75 ? "green" : match.match_score >= 50 ? "amber" : "red"}>Match {match.match_score}/100</Chip>
        <p className="text-sm text-slate-600">{match.summary}</p>
      </div>
      <div>
        <h4 className="mb-1.5 text-sm font-semibold text-slate-800">Matched keywords ({match.matched_keywords.length})</h4>
        <div className="flex flex-wrap gap-1.5" data-testid="matched-keywords">
          {match.matched_keywords.map((k) => (
            <span key={k.keyword} title={k.evidence}>
              <Chip tone="green">{k.keyword}</Chip>
            </span>
          ))}
        </div>
      </div>
      <div>
        <h4 className="mb-1.5 text-sm font-semibold text-slate-800">Missing keywords ({match.missing_keywords.length})</h4>
        <ul className="space-y-1.5" data-testid="missing-keywords">
          {match.missing_keywords.map((k) => (
            <li key={k.keyword} className="text-sm text-slate-700">
              <Chip tone={IMPORTANCE_TONE[k.importance]}>
                {k.keyword} · {k.importance}
              </Chip>
              <span className="ml-2 text-slate-600">{k.suggestion}</span>
            </li>
          ))}
        </ul>
      </div>
      <div>
        <h4 className="mb-1.5 text-sm font-semibold text-slate-800">Requirements and your strongest bullets</h4>
        <ul className="space-y-2">
          {match.requirement_matches.map((r, i) => (
            <li key={i} className="rounded-lg border border-slate-200 p-3 text-sm" data-testid="requirement-match">
              <div className="flex flex-wrap items-center gap-2">
                <Chip tone={STRENGTH_TONE[r.strength]}>{r.strength}</Chip>
                <span className="font-medium text-slate-900">{r.requirement}</span>
              </div>
              {r.bullet_ids.map((id) => (
                <p key={id} className="mt-1.5 border-l-2 border-emerald-300 pl-2 text-slate-700">
                  {texts.get(id) ?? id}
                </p>
              ))}
              {r.note && <p className="mt-1.5 text-xs text-slate-500">{r.note}</p>}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

// --- Rewrite ---------------------------------------------------------------------------------------------------

function Words({ parts }: { parts: DiffPart[] }) {
  return (
    <>
      {parts.map((part, i) => (
        <span
          key={i}
          className={
            part.kind === "del"
              ? "rounded bg-red-100 px-0.5 text-red-800 line-through decoration-red-400"
              : part.kind === "add"
                ? "rounded bg-emerald-100 px-0.5 text-emerald-900"
                : ""
          }
        >
          {part.text}{" "}
        </span>
      ))}
    </>
  );
}

export function BulletBeforeAfter({ change }: { change: BulletChange }) {
  const diff = diffWords(change.before, change.after);
  return (
    <li className="rounded-lg border border-slate-200 p-3 text-sm" data-testid="bullet-diff" data-source={change.source_id}>
      <div className="mb-2 flex flex-wrap gap-1.5">
        {change.flags.map((f) => (
          <Chip key={f} tone="amber">
            {FLAG_LABEL[f]}
          </Chip>
        ))}
        {change.moved && <Chip tone="indigo">Moved</Chip>}
        {change.job_requirements.length > 0 && <Chip tone="green">Answers: {change.job_requirements[0]}</Chip>}
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        <div>
          <p className="mb-0.5 text-xs font-medium uppercase tracking-wide text-slate-400">Before</p>
          <p className="leading-relaxed text-slate-700" data-testid="bullet-before">
            <Words parts={diff.before} />
          </p>
        </div>
        <div>
          <p className="mb-0.5 text-xs font-medium uppercase tracking-wide text-slate-400">After</p>
          <p className="leading-relaxed text-slate-900" data-testid="bullet-after">
            <Words parts={diff.after} />
          </p>
        </div>
      </div>
    </li>
  );
}

export function RewriteBody({ result }: { result: RefinementResult }) {
  const { stats } = result;
  const summaryDiff = diffWords(result.original_summary, result.resume.summary);
  const placeholderNote = stats.placeholders > 0;
  return (
    <div className="space-y-4">
      <p className="text-sm text-slate-600">{result.notes}</p>
      <ul className="flex flex-wrap gap-2 text-xs" data-testid="rewrite-stats">
        <li>
          <Chip tone="indigo">
            {stats.bullets_rewritten} of {stats.bullets_total} bullets rewritten
          </Chip>
        </li>
        {stats.bullets_reordered > 0 && (
          <li>
            <Chip tone="slate">{stats.bullets_reordered} reordered</Chip>
          </li>
        )}
        {stats.ats_fixed_by_reformat > 0 && (
          <li>
            <Chip tone="green">{stats.ats_fixed_by_reformat} ATS formatting issues fixed in the downloads</Chip>
          </li>
        )}
        {stats.jd && (
          <li>
            <Chip tone="green">
              {stats.keywords_matched} of {stats.keywords_total} job keywords matched
            </Chip>
          </li>
        )}
      </ul>
      {placeholderNote && (
        <p className="rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-900" data-testid="placeholder-note">
          {stats.placeholders} placeholder{stats.placeholders === 1 ? "" : "s"} in [square brackets] (like [X%]) mark where a real number belongs. Fill them in
          with your own figures before you use this draft; none were invented for you.
        </p>
      )}
      {result.corrections.length > 0 && (
        <details className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600" data-testid="corrections">
          <summary className="cursor-pointer font-medium">
            {result.corrections.length} automatic correction{result.corrections.length === 1 ? "" : "s"} (guardrails on the rewrite)
          </summary>
          <ul className="mt-2 list-disc pl-4">
            {result.corrections.map((c, i) => (
              <li key={i}>
                {c.source_id}: {c.reason}
              </li>
            ))}
          </ul>
        </details>
      )}
      {result.resume.summary && (
        <div className="rounded-lg border border-slate-200 p-3 text-sm">
          <p className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-400">Summary</p>
          <div className="grid gap-2 sm:grid-cols-2">
            <p className="text-slate-700">{result.original_summary ? <Words parts={summaryDiff.before} /> : <span className="text-slate-400">(none)</span>}</p>
            <p className="text-slate-900">
              <Words parts={summaryDiff.after} />
            </p>
          </div>
        </div>
      )}
      {result.resume.experience.map((job, i) => {
        const changes = result.changes.filter((c) => c.job === i);
        const changed = changes.filter((c) => c.changed || c.moved);
        const same = changes.length - changed.length;
        return (
          <div key={i}>
            <h4 className="mb-1.5 text-sm font-semibold text-slate-800">
              {jobHeading(job)} {job.dates && <span className="font-normal text-slate-500">({job.dates})</span>}
            </h4>
            <ul className="space-y-2">
              {changed.map((c) => (
                <BulletBeforeAfter key={c.source_id} change={c} />
              ))}
            </ul>
            {same > 0 && (
              <p className="mt-1.5 text-xs text-slate-500">
                {same} bullet{same === 1 ? "" : "s"} kept exactly as written.
              </p>
            )}
          </div>
        );
      })}
      <p className="text-xs text-slate-500">Skills: {result.resume.skills.join(", ")}</p>
    </div>
  );
}
