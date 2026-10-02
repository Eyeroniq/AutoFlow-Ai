"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarClock, CircleCheck, CircleDashed, LayoutTemplate, Mail, Mic } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { categoryStyle, NodeIcon } from "@/components/node-icon";
import { Button } from "@/components/ui/button";
import { toast } from "@/components/ui/toast";
import { api } from "@/lib/api";
import type { NodeType, Template } from "@/lib/types";

import { browserTimeZone } from "../editor/triggers";

function TemplateCard({ template, catalog }: { template: Template; catalog: Map<string, NodeType> }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const use = useMutation({
    meta: { silent: true },
    mutationFn: () => api.templates.use(template.slug, browserTimeZone()),
    onSuccess: (workflow) => {
      void queryClient.invalidateQueries({ queryKey: ["workflows"] });
      toast.success(`“${workflow.name}” created`, template.triggers.length ? "Its trigger is off: turn it on in the Triggers panel." : undefined);
      router.push(`/pipelines/${workflow.id}`);
    },
    onError: (error) => toast.error("Couldn't create the pipeline", error.message),
  });
  const types = [...new Set(template.node_types.filter((t) => t !== "input" && t !== "output"))];
  return (
    <article className="flex flex-col gap-3 rounded-xl border border-slate-200 bg-white p-4" data-testid={`template-${template.slug}`} data-ready={template.ready}>
      <div>
        <p className="text-[11px] font-medium uppercase tracking-wide text-slate-400">{template.category}</p>
        <h3 className="font-semibold text-slate-900">{template.name}</h3>
        <p className="mt-1 text-xs leading-5 text-slate-600">{template.description}</p>
      </div>
      <div className="flex flex-wrap items-center gap-1" aria-label="Steps">
        {template.triggers.map((trigger) => (
          <span key={trigger.type} className="flex items-center gap-1 rounded-md bg-indigo-50 px-1.5 py-1 text-[10px] font-medium text-indigo-700">
            {trigger.type === "email" ? <Mail className="size-3" aria-hidden /> : <CalendarClock className="size-3" aria-hidden />}
            {trigger.type === "schedule" ? String(trigger.config.cron) : "new email"}
          </span>
        ))}
        {types.map((type) => {
          const entry = catalog.get(type);
          return (
            <span key={type} title={entry?.label ?? type} className={`grid size-6 place-items-center rounded-md ${categoryStyle(entry?.category).tile}`}>
              <NodeIcon name={entry?.icon} className="size-3.5" />
            </span>
          );
        })}
      </div>
      <p className="text-[11px] leading-4 text-slate-500" aria-label="Nodes" data-testid={`template-nodes-${template.slug}`}>
        {types.map((type) => catalog.get(type)?.label ?? type).join(" → ")}
      </p>
      <ul className="space-y-1 text-xs" aria-label="Needs">
        {template.requirements.map((requirement) => (
          <li key={requirement.label} className="flex items-start gap-1.5" title={requirement.why ?? undefined}>
            {requirement.satisfied ? (
              <CircleCheck className="mt-px size-3.5 shrink-0 text-emerald-600" aria-label="connected" />
            ) : (
              <CircleDashed className="mt-px size-3.5 shrink-0 text-amber-600" aria-label="missing" />
            )}
            <span className={requirement.satisfied ? "text-slate-700" : "text-amber-800"}>
              {requirement.label}
              {requirement.satisfied && requirement.using && <span className="text-slate-400"> · {requirement.using}</span>}
            </span>
          </li>
        ))}
      </ul>
      <div className="mt-auto flex items-center gap-2 border-t border-slate-100 pt-3">
        <Button size="sm" onClick={() => use.mutate()} loading={use.isPending} data-testid={`use-template-${template.slug}`}>
          Use template
        </Button>
        {template.slug === "resume-refiner" && (
          <Link href="/resume" className="text-xs font-medium text-indigo-600 hover:text-indigo-500" data-testid="open-resume-refiner">
            Open the full app
          </Link>
        )}
        {!template.ready && (
          <Link href="/integrations" className="text-xs font-medium text-indigo-600 hover:text-indigo-500">
            Connect what&apos;s missing
          </Link>
        )}
      </div>
    </article>
  );
}

function DiscordMeetingCard() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const use = useMutation({
    meta: { silent: true },
    mutationFn: () => api.templates.use("discord-meeting-summary", browserTimeZone()),
    onSuccess: (workflow) => {
      void queryClient.invalidateQueries({ queryKey: ["workflows"] });
      toast.success(`“${workflow.name}” created`, "The Discord bot runs this pipeline for each approved recording.");
      router.push(`/pipelines/${workflow.id}`);
    },
    onError: (error) => toast.error("Couldn't create the pipeline", error.message),
  });
  return (
    <article className="flex flex-col gap-3 rounded-xl border border-slate-200 bg-white p-4" data-testid="template-discord-meeting">
      <div>
        <p className="text-[11px] font-medium uppercase tracking-wide text-slate-400">Meetings</p>
        <h3 className="font-semibold text-slate-900">Discord voice meeting to Telegram</h3>
        <p className="mt-1 text-xs leading-5 text-slate-600">
          The pipeline starts with a <strong>Discord Voice Meeting</strong> block: set your server and channel ids on it and the always-on bot watches that channel. After you tap Yes in Telegram it records, runs the pipeline, and sends you the summary and/or transcript. Needs DISCORD_BOT_TOKEN in .env.
        </p>
      </div>
      <div className="flex items-center gap-1 text-slate-500">
        <Mic className="size-4" aria-hidden />
        <span className="text-xs">Runs from the bot, not on a schedule</span>
      </div>
      <div className="mt-auto border-t border-slate-100 pt-3">
        <Button size="sm" onClick={() => use.mutate()} loading={use.isPending} data-testid="use-template-discord-meeting">
          Create the Discord pipeline
        </Button>
      </div>
    </article>
  );
}

export function TemplatesSection() {
  const templates = useQuery({ queryKey: ["templates"], queryFn: api.templates.list, staleTime: 60_000, meta: { silent: true } });
  const nodes = useQuery({ queryKey: ["nodes"], queryFn: api.nodes.list, staleTime: Infinity, meta: { silent: true } });
  const catalog = new Map((nodes.data ?? []).map((n) => [n.type, n]));
  if (!templates.data?.length) return null;
  return (
    <section className="space-y-4" aria-label="Templates">
      <div className="flex items-end gap-2">
        <LayoutTemplate className="mb-0.5 size-4 text-slate-400" aria-hidden />
        <div>
          <h2 className="text-base font-semibold text-slate-900">Templates</h2>
          <p className="text-sm text-slate-500">Ready-made pipelines on free services. Using one makes an editable copy; its triggers start switched off.</p>
        </div>
      </div>
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        {templates.data.map((template) => (
          <TemplateCard key={template.slug} template={template} catalog={catalog} />
        ))}
        <DiscordMeetingCard />
      </div>
    </section>
  );
}
