"use client";

import { Braces, Copy, TextQuote } from "lucide-react";
import { useState } from "react";

import { toast } from "@/components/ui/toast";
import { prettyJson } from "@/lib/format";

// The first of these that holds text is shown as a readable block.
const TEXT_KEYS = ["summary", "text", "response"] as const;
// Short facts worth a chip, in this order.
const FACTS: [key: string, label: string][] = [
  ["filename", "file"],
  ["page_count", "pages"],
  ["mean_confidence", "OCR confidence"],
  ["char_count", "chars"],
  ["input_chars", "input chars"],
  ["needs_ocr", "needs OCR"],
  ["truncated", "truncated"],
  ["language", "language"],
  ["duration_seconds", "duration (s)"],
  ["chunks", "chunks"],
  ["task", "task"],
  ["count", "results"],
  ["provider_used", "provider"],
  ["provider", "provider"],
  ["model", "model"],
  ["attempts", "attempts"],
  ["engine", "engine"],
];

type Json = Record<string, unknown>;
const isObject = (value: unknown): value is Json => typeof value === "object" && value !== null && !Array.isArray(value);

function Chip({ label, value }: { label: string; value: unknown }) {
  const shown = typeof value === "boolean" ? (value ? "yes" : "no") : typeof value === "number" && !Number.isInteger(value) ? value.toFixed(1) : String(value);
  return (
    <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-600">
      {label}: <span className="font-medium text-slate-800">{shown}</span>
    </span>
  );
}

function TextBlock({ text, label }: { text: string; label: string }) {
  return (
    <div>
      <div className="mb-1 flex items-center justify-between">
        <p className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">
          {label} <span className="font-normal normal-case">({text.length.toLocaleString()} chars)</span>
        </p>
        <button
          type="button"
          onClick={() => {
            void navigator.clipboard?.writeText(text);
            toast.info("Copied", `${label} (${text.length.toLocaleString()} characters)`);
          }}
          className="flex items-center gap-1 text-[11px] text-slate-500 hover:text-indigo-600"
        >
          <Copy className="size-3" aria-hidden /> Copy
        </button>
      </div>
      <div className="max-h-64 overflow-auto whitespace-pre-wrap rounded-md bg-white p-2.5 text-xs leading-5 text-slate-800 ring-1 ring-slate-200" data-testid="output-text">
        {text}
      </div>
    </div>
  );
}

function describeEntity(kind: string, entity: Json): { main: string; detail: string[] } {
  switch (kind) {
    case "people":
      return { main: String(entity.name ?? ""), detail: [entity.role].filter(Boolean).map(String) };
    case "organizations":
      return { main: String(entity.name ?? ""), detail: [entity.kind].filter(Boolean).map(String) };
    case "dates":
      return { main: String(entity.text ?? ""), detail: [entity.iso, entity.meaning].filter(Boolean).map(String) };
    case "amounts": {
      const value = typeof entity.value === "number" ? `${entity.currency ? `${entity.currency} ` : ""}${entity.value.toLocaleString(undefined, { maximumFractionDigits: 2 })}` : null;
      return { main: String(entity.text ?? ""), detail: [value, entity.meaning].filter(Boolean).map(String) };
    }
    default:
      return { main: String(entity.text ?? entity.name ?? ""), detail: [entity.note].filter(Boolean).map(String) };
  }
}

function EntityGroup({ kind, items }: { kind: string; items: unknown[] }) {
  return (
    <div>
      <p className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400">
        {kind.replace(/_/g, " ")} ({items.length})
      </p>
      {items.length === 0 ? (
        <p className="text-[11px] text-slate-400">None found</p>
      ) : (
        <ul className="flex flex-wrap gap-1">
          {items.filter(isObject).map((entity, index) => {
            const { main, detail } = describeEntity(kind, entity);
            return (
              <li key={`${main}-${index}`} className="rounded-md bg-white px-2 py-1 text-[11px] ring-1 ring-slate-200">
                <span className="font-medium text-slate-800">{main}</span>
                {detail.length > 0 && <span className="text-slate-500"> · {detail.join(" · ")}</span>}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function EntitiesView({ entities }: { entities: Json }) {
  const groups = Object.entries(entities).filter(([key, value]) => key !== "custom" && Array.isArray(value)) as [string, unknown[]][];
  const custom = isObject(entities.custom) ? (Object.entries(entities.custom).filter(([, v]) => Array.isArray(v)) as [string, unknown[]][]) : [];
  return (
    <div className="space-y-2" data-testid="output-entities">
      {[...groups, ...custom].map(([kind, items]) => (
        <EntityGroup key={kind} kind={kind} items={items} />
      ))}
    </div>
  );
}

function clock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const pad = (n: number) => String(n).padStart(2, "0");
  return s >= 3600 ? `${Math.floor(s / 3600)}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}` : `${pad(Math.floor(s / 60))}:${pad(s % 60)}`;
}

/** Speech to Text segments, with their timestamps. */
function SegmentsView({ segments }: { segments: Json[] }) {
  return (
    <div>
      <p className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400">Segments ({segments.length})</p>
      <ol className="max-h-64 space-y-0.5 overflow-auto rounded-md bg-white p-2 text-xs ring-1 ring-slate-200" data-testid="output-segments">
        {segments.map((segment, index) => (
          <li key={index} className="flex gap-2">
            <span className="shrink-0 font-mono text-[11px] text-slate-400">
              {clock(Number(segment.start))}–{clock(Number(segment.end))}
            </span>
            <span className="text-slate-800">{String(segment.text ?? "")}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}

/** Web Search results as links. */
function ResultsView({ results }: { results: Json[] }) {
  return (
    <div>
      <p className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400">Results ({results.length})</p>
      <ol className="max-h-64 space-y-1.5 overflow-auto rounded-md bg-white p-2 text-xs ring-1 ring-slate-200" data-testid="output-results">
        {results.map((result, index) => (
          <li key={index}>
            {/^https?:\/\//i.test(String(result.url)) ? (
              <a href={String(result.url)} target="_blank" rel="noreferrer noopener" className="font-medium text-indigo-700 hover:underline">
                {index + 1}. {String(result.title || result.url)}
              </a>
            ) : (
              <span className="font-medium text-slate-800">
                {index + 1}. {String(result.title || result.url)}
              </span>
            )}
            <p className="truncate text-[11px] text-emerald-700">{String(result.url)}</p>
            {typeof result.snippet === "string" && result.snippet && <p className="text-slate-600">{result.snippet}</p>}
          </li>
        ))}
      </ol>
    </div>
  );
}

const listOf = (value: unknown, key: string): Json[] | null =>
  Array.isArray(value) && value.length > 0 && value.every((item) => isObject(item) && key in item) ? (value as Json[]) : null;

/** A node's output, readable: facts as chips, long text as a text block, entities as lists.
 * Anything else (and the whole thing, on request) as JSON. */
export function OutputView({ output }: { output: Json | null | undefined }) {
  const [raw, setRaw] = useState(false);
  if (!output) return null;
  const textKey = TEXT_KEYS.find((key) => typeof output[key] === "string" && (output[key] as string).trim());
  const entities = isObject(output.entities) ? output.entities : null;
  const segments = listOf(output.segments, "start");
  const results = listOf(output.results, "url");
  const readable = Boolean(textKey || entities || segments || results);
  const facts = FACTS.filter(([key]) => output[key] !== undefined && output[key] !== null && output[key] !== "" && typeof output[key] !== "object");

  return (
    <div className="min-w-0 space-y-2">
      <div className="flex items-center justify-between">
        <p className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">Output</p>
        {readable && (
          <button type="button" onClick={() => setRaw((r) => !r)} className="flex items-center gap-1 text-[11px] text-slate-500 hover:text-indigo-600">
            {raw ? <TextQuote className="size-3" aria-hidden /> : <Braces className="size-3" aria-hidden />}
            {raw ? "Readable" : "Raw JSON"}
          </button>
        )}
      </div>
      {readable && !raw ? (
        <>
          {facts.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {facts.map(([key, label]) => (
                <Chip key={key} label={label} value={output[key]} />
              ))}
            </div>
          )}
          {textKey && <TextBlock text={output[textKey] as string} label={textKey} />}
          {segments && <SegmentsView segments={segments} />}
          {results && <ResultsView results={results} />}
          {entities && <EntitiesView entities={entities} />}
        </>
      ) : (
        <pre className="max-h-56 overflow-auto rounded-md bg-slate-900 p-2 text-[11px] leading-4 text-slate-100">{prettyJson(output)}</pre>
      )}
    </div>
  );
}
