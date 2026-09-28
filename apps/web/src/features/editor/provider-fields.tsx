"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, CircleCheck, CircleX, Plug, Plus, X } from "lucide-react";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import type { Integration } from "@/lib/types";

import type { FieldRenderer } from "./config-form";

export function useIntegrations() {
  return useQuery({ queryKey: ["integrations"], queryFn: api.integrations.list, staleTime: 30_000, meta: { silent: true } });
}

const sourceNote = (integration?: Integration) =>
  !integration ? "" : integration.source === "none" ? " — no key" : integration.source === "user" ? " — your key" : " — server key";

const selectClass =
  "nodrag block w-full rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-900 shadow-sm focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-100";

/** Provider select showing which providers have credentials. */
export const ProviderSelect: FieldRenderer = ({ spec, field, inputId, invalid, describedBy }) => <ProviderSelectInner spec={spec} field={field} inputId={inputId} invalid={invalid} describedBy={describedBy} />;

function ProviderSelectInner({ spec, field, inputId, invalid, describedBy }: Pick<Parameters<FieldRenderer>[0], "spec" | "field" | "inputId" | "invalid" | "describedBy">) {
  const { data: integrations } = useIntegrations();
  const byProvider = new Map((integrations ?? []).map((i) => [i.provider, i]));
  const value = (field.value as string | undefined) ?? String(spec.default ?? "");
  const current = byProvider.get(value);
  return (
    <div className="space-y-1">
      <select
        id={inputId}
        value={value}
        onChange={(e) => field.onChange(e.target.value)}
        aria-invalid={invalid || undefined}
        aria-describedby={describedBy}
        className={selectClass}
      >
        {spec.options?.map((option) => (
          <option key={option} value={option}>
            {option}
            {option === "mock" ? " — canned reply, no API call" : sourceNote(byProvider.get(option))}
          </option>
        ))}
      </select>
      {current?.source === "none" && (
        <p className="text-[11px] text-amber-700">
          No {current.label} key is configured.{" "}
          <Link href="/integrations" className="font-medium underline">
            Connect one
          </Link>{" "}
          or runs will fail validation.
        </p>
      )}
    </div>
  );
}

/** Model input with the provider's default as the placeholder. */
export const ModelInput: FieldRenderer = ({ field, inputId, invalid, describedBy, form }) => <ModelInputInner field={field} inputId={inputId} invalid={invalid} describedBy={describedBy} provider={String(form.watch("provider") ?? "")} />;

function ModelInputInner({ field, inputId, invalid, describedBy, provider }: Pick<Parameters<FieldRenderer>[0], "field" | "inputId" | "invalid" | "describedBy"> & { provider: string }) {
  const { data: integrations } = useIntegrations();
  const fallbackDefault = provider === "mock" ? "mock" : undefined;
  const defaultModel = integrations?.find((i) => i.provider === provider)?.default_model ?? fallbackDefault;
  const listId = `${inputId}-models`;
  return (
    <>
      <input
        id={inputId}
        list={listId}
        value={(field.value as string | null | undefined) ?? ""}
        onChange={(e) => field.onChange(e.target.value)}
        onBlur={field.onBlur}
        placeholder={defaultModel ? `Default: ${defaultModel}` : "Provider default"}
        aria-invalid={invalid || undefined}
        aria-describedby={describedBy}
        className="nodrag block w-full rounded-md border border-slate-300 px-2.5 py-1.5 text-sm shadow-sm placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-100"
      />
      <datalist id={listId}>{defaultModel && <option value={defaultModel} />}</datalist>
    </>
  );
}

/** Renderers for LLM node fields; `providers` is the provider field's enum from the schema. */
export function llmRenderers(providers: string[]): Record<string, FieldRenderer> {
  return {
    provider: ProviderSelect,
    model: ModelInput,
    fallback: ({ field }) => (
      <FallbackEditor
        chain={Array.isArray(field.value) ? (field.value as string[]) : []}
        onChange={field.onChange}
        providers={providers}
      />
    ),
  };
}

/** Ordered fallback chain: each entry is "provider" or "provider:model". */
function FallbackEditor({ chain, onChange, providers }: { chain: string[]; onChange: (v: string[]) => void; providers: string[] }) {
  const { data: integrations } = useIntegrations();
  const byProvider = new Map((integrations ?? []).map((i) => [i.provider, i]));
  const split = (entry: string) => {
    const at = entry.indexOf(":");
    return at === -1 ? [entry, ""] : [entry.slice(0, at), entry.slice(at + 1)];
  };
  const join = (provider: string, model: string) => (model.trim() ? `${provider}:${model.trim()}` : provider);
  const set = (index: number, entry: string) => onChange(chain.map((e, i) => (i === index ? entry : e)));
  const move = (index: number, delta: number) => {
    const next = [...chain];
    const [item] = next.splice(index, 1);
    next.splice(index + delta, 0, item);
    onChange(next);
  };
  return (
    <div className="space-y-1.5" data-testid="fallback-chain">
      {chain.length === 0 && <p className="text-[11px] text-slate-500">No fallbacks: if the provider fails, the node fails.</p>}
      {chain.map((entry, index) => {
        const [provider, model] = split(entry);
        return (
          <div key={index} className="flex items-center gap-1">
            <span className="w-4 text-right text-[11px] text-slate-400">{index + 1}.</span>
            <select value={provider} onChange={(e) => set(index, join(e.target.value, model))} className={`${selectClass} !w-28 !px-1.5 !py-1 !text-xs`} aria-label={`Fallback ${index + 1} provider`}>
              {providers.map((p) => (
                <option key={p} value={p}>
                  {p}
                  {p === "mock" ? "" : sourceNote(byProvider.get(p))}
                </option>
              ))}
            </select>
            <input
              value={model}
              onChange={(e) => set(index, join(provider, e.target.value))}
              placeholder={byProvider.get(provider)?.default_model ?? "default model"}
              aria-label={`Fallback ${index + 1} model`}
              className="nodrag min-w-0 flex-1 rounded-md border border-slate-300 px-1.5 py-1 text-xs placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-100"
            />
            <button type="button" disabled={index === 0} onClick={() => move(index, -1)} className="rounded p-0.5 text-slate-400 hover:text-slate-700 disabled:opacity-30" aria-label="Move up">
              <ArrowUp className="size-3.5" />
            </button>
            <button type="button" disabled={index === chain.length - 1} onClick={() => move(index, 1)} className="rounded p-0.5 text-slate-400 hover:text-slate-700 disabled:opacity-30" aria-label="Move down">
              <ArrowDown className="size-3.5" />
            </button>
            <button type="button" onClick={() => onChange(chain.filter((_, i) => i !== index))} className="rounded p-0.5 text-slate-400 hover:text-red-600" aria-label="Remove fallback">
              <X className="size-3.5" />
            </button>
          </div>
        );
      })}
      {chain.length < 5 && (
        <button type="button" onClick={() => onChange([...chain, providers.find((p) => p !== "mock") ?? "gemini"])} className="flex items-center gap-1 text-xs font-medium text-indigo-600 hover:text-indigo-500">
          <Plus className="size-3.5" /> Add fallback
        </button>
      )}
    </div>
  );
}

/** A real connection test for a provider (POST /api/integrations/{provider}/test). */
export function ConnectionTest({ provider, label }: { provider: string; label?: string }) {
  const test = useMutation({ mutationFn: () => api.integrations.test(provider), meta: { silent: true } });
  const result = test.data;
  return (
    <div className="rounded-lg border border-slate-200 bg-slate-50 p-2.5" data-testid={`connection-test-${provider}`}>
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs font-medium text-slate-700">{label ?? `${provider} connection`}</p>
        <Button size="sm" variant="secondary" loading={test.isPending} onClick={() => test.mutate()}>
          <Plug className="size-3.5" aria-hidden /> Test connection
        </Button>
      </div>
      {test.isError && <p className="mt-1.5 text-[11px] text-red-600">{test.error instanceof Error ? test.error.message : "Test failed"}</p>}
      {result && (
        <div className={`mt-1.5 flex items-start gap-1.5 text-[11px] ${result.success ? "text-emerald-700" : "text-red-600"}`} role="status">
          {result.success ? <CircleCheck className="mt-px size-3.5 shrink-0" /> : <CircleX className="mt-px size-3.5 shrink-0" />}
          <span>
            {result.success ? `Connected in ${result.latency_ms} ms (${result.source === "user" ? "your key" : "server key"})` : result.error}
          </span>
        </div>
      )}
    </div>
  );
}
