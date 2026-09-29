"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { Plus, X } from "lucide-react";
import { type ReactNode, useEffect, useId, useMemo, useState } from "react";
import { Controller, type ControllerRenderProps, type FieldValues, useForm, type UseFormReturn } from "react-hook-form";

import type { JsonSchema, ValidationIssue } from "@/lib/types";

import { FileChooser } from "../files/file-chooser";
import { ReferenceField } from "./reference-field";
import {
  cleanConfig,
  containsReference,
  type FieldSpec,
  fieldsFromSchema,
  isReferenceOnly,
  parseNumberInput,
  zodForFields,
} from "./schema-form";
import { getEditorStore } from "./store";

export interface FieldRendererProps {
  spec: FieldSpec;
  field: ControllerRenderProps<FieldValues, string>;
  nodeId: string;
  inputId: string;
  invalid: boolean;
  describedBy?: string;
  form: UseFormReturn<FieldValues>;
}

export type FieldRenderer = (props: FieldRendererProps) => ReactNode;

interface SchemaFormProps {
  nodeId: string;
  schema: JsonSchema;
  config: Record<string, unknown>;
  /** Backend validation issues for this node (shown inline on their field). */
  issues: ValidationIssue[];
  /** Custom controls for specific fields (e.g. the LLM provider picker). */
  renderers?: Record<string, FieldRenderer>;
}

const text = (value: unknown) => (value === undefined || value === null ? "" : typeof value === "string" ? value : JSON.stringify(value));

export function SchemaForm({ nodeId, schema, config, issues, renderers = {} }: SchemaFormProps) {
  const fields = useMemo(() => fieldsFromSchema(schema), [schema]);
  const resolver = useMemo(() => zodResolver(zodForFields(fields)), [fields]);
  const form = useForm<FieldValues>({ defaultValues: config, resolver, mode: "onChange" });

  // Show problems with the stored config straight away, not only after the first edit.
  useEffect(() => {
    void form.trigger();
  }, [form]);

  // Every change goes to the store (and so into history and autosave), even when invalid:
  // saving work in progress is fine, and validation reports what still needs fixing.
  useEffect(
    () =>
      form.subscribe({
        formState: { values: true },
        callback: ({ values, name }) => {
          if (!name) return; // validation-only updates carry no field name
          getEditorStore().getState().updateConfig(nodeId, cleanConfig(values as Record<string, unknown>, fields), name.split(".")[0]);
        },
      }),
    [form, fields, nodeId],
  );

  const fieldIssues = (name: string) =>
    issues.filter((issue) => issue.field && (issue.field === name || issue.field.startsWith(`${name}.`)));

  return (
    <div className="space-y-4">
      {fields.map((spec) => (
        <SchemaField
          key={spec.name}
          spec={spec}
          form={form}
          nodeId={nodeId}
          serverIssues={fieldIssues(spec.name)}
          renderer={renderers[spec.name]}
        />
      ))}
    </div>
  );
}

function SchemaField({
  spec,
  form,
  nodeId,
  serverIssues,
  renderer,
}: {
  spec: FieldSpec;
  form: UseFormReturn<FieldValues>;
  nodeId: string;
  serverIssues: ValidationIssue[];
  renderer?: FieldRenderer;
}) {
  const inputId = useId();
  const errorId = `${inputId}-error`;
  const helpId = `${inputId}-help`;
  const clientError = form.formState.errors[spec.name]?.message as string | undefined;
  // Missing-field issues duplicate the client-side "Required"; show the server's wording once.
  const messages = [...new Set([clientError, ...serverIssues.map((i) => i.message)].filter(Boolean) as string[])];
  const invalid = messages.length > 0;
  const describedBy = [spec.description ? helpId : null, invalid ? errorId : null].filter(Boolean).join(" ") || undefined;

  return (
    <Controller
      control={form.control}
      name={spec.name}
      render={({ field }) => {
        const props: FieldRendererProps = { spec, field, nodeId, inputId, invalid, describedBy, form };
        const control = renderer ? renderer(props) : <DefaultControl {...props} />;
        return (
          <div data-field={spec.name}>
            {spec.kind === "boolean" && !renderer ? (
              control
            ) : (
              <>
                <label htmlFor={inputId} className="mb-1 flex items-baseline gap-1 text-xs font-medium text-slate-700">
                  {spec.label}
                  {spec.required && (
                    <span className="text-red-500" aria-label="required">
                      *
                    </span>
                  )}
                  {!spec.required && spec.default !== undefined && spec.default !== null && spec.default !== "" && (
                    <span className="font-normal text-slate-400">default: {text(spec.default)}</span>
                  )}
                </label>
                {control}
              </>
            )}
            {spec.description && (
              <p id={helpId} className="mt-1 text-[11px] leading-4 text-slate-500">
                {spec.description}
              </p>
            )}
            {invalid && (
              <div id={errorId} className="mt-1 space-y-0.5" data-testid={`field-error-${spec.name}`}>
                {messages.map((message) => (
                  <p key={message} className="text-[11px] leading-4 text-red-600">
                    {message}
                  </p>
                ))}
              </div>
            )}
          </div>
        );
      }}
    />
  );
}

export function DefaultControl({ spec, field, nodeId, inputId, invalid, describedBy }: FieldRendererProps) {
  switch (spec.kind) {
    case "textarea":
    case "text":
    case "addresses":
      return (
        <ReferenceField
          id={inputId}
          nodeId={nodeId}
          field={spec.name}
          value={Array.isArray(field.value) ? (field.value as string[]).join(", ") : text(field.value)}
          onChange={field.onChange}
          onBlur={field.onBlur}
          multiline={spec.kind === "textarea"}
          invalid={invalid}
          describedBy={describedBy}
          placeholder={spec.kind === "addresses" ? "name@example.com, {{vars.recipient}}" : undefined}
        />
      );
    case "number":
    case "integer":
      return (
        <ReferenceField
          id={inputId}
          nodeId={nodeId}
          value={text(field.value)}
          onChange={(v) => field.onChange(parseNumberInput(v, spec.kind === "integer"))}
          onBlur={field.onBlur}
          inputMode="decimal"
          invalid={invalid}
          describedBy={describedBy}
          placeholder={spec.default !== undefined && spec.default !== null ? String(spec.default) : undefined}
        />
      );
    case "boolean":
      return (
        <label className="flex items-center gap-2 text-xs font-medium text-slate-700">
          <input
            id={inputId}
            type="checkbox"
            checked={field.value === undefined ? Boolean(spec.default) : Boolean(field.value)}
            onChange={(e) => field.onChange(e.target.checked)}
            className="size-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-200"
            aria-describedby={describedBy}
          />
          {spec.label}
        </label>
      );
    case "select":
      return (
        <select
          id={inputId}
          value={text(field.value)}
          onChange={(e) => field.onChange(e.target.value || undefined)}
          onBlur={field.onBlur}
          aria-invalid={invalid || undefined}
          aria-describedby={describedBy}
          className="nodrag block w-full rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-900 shadow-sm focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-100"
        >
          {!spec.required && <option value="">{spec.default !== undefined && spec.default !== null ? `Default (${text(spec.default)})` : "—"}</option>}
          {spec.options?.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      );
    case "string-list":
      return <StringList value={Array.isArray(field.value) ? (field.value as string[]) : []} onChange={field.onChange} max={spec.maxItems} />;
    case "multiselect": {
      const chosen = Array.isArray(field.value) ? (field.value as string[]) : Array.isArray(spec.default) ? (spec.default as string[]) : [];
      return (
        <div className="flex flex-wrap gap-x-4 gap-y-1.5" role="group" aria-describedby={describedBy}>
          {spec.options?.map((option) => (
            <label key={option} className="flex items-center gap-1.5 text-xs text-slate-700">
              <input
                type="checkbox"
                checked={chosen.includes(option)}
                onChange={(e) =>
                  field.onChange(e.target.checked ? [...chosen, option] : chosen.filter((v) => v !== option))
                }
                className="size-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-200"
              />
              {option}
            </label>
          ))}
        </div>
      );
    }
    case "file":
      return <FileRefField id={inputId} nodeId={nodeId} value={field.value} onChange={field.onChange} invalid={invalid} describedBy={describedBy} />;
    case "object":
    case "json-array":
      return <JsonField id={inputId} nodeId={nodeId} value={field.value} onChange={field.onChange} kind={spec.kind} invalid={invalid} describedBy={describedBy} />;
    default:
      return <AnyField id={inputId} nodeId={nodeId} value={field.value} onChange={field.onChange} invalid={invalid} describedBy={describedBy} />;
  }
}

/** A document node's `file`: a {{reference}} (usually an Input node of type file) or one of your uploads. */
function FileRefField({
  id,
  nodeId,
  value,
  onChange,
  invalid,
  describedBy,
}: {
  id: string;
  nodeId: string;
  value: unknown;
  onChange: (v: unknown) => void;
  invalid: boolean;
  describedBy?: string;
}) {
  const isUpload = typeof value === "string" && value.trim() !== "" && !containsReference(value);
  const [mode, setMode] = useState<"reference" | "upload">(isUpload ? "upload" : "reference");
  const tab = (key: "reference" | "upload", label: string) => (
    <button
      type="button"
      role="radio"
      aria-checked={mode === key}
      onClick={() => setMode(key)}
      className={`rounded px-2 py-0.5 text-[11px] font-medium ${mode === key ? "bg-indigo-50 text-indigo-700" : "text-slate-500 hover:bg-slate-100"}`}
    >
      {label}
    </button>
  );
  return (
    <div>
      <div role="radiogroup" aria-label="File source" className="mb-1 flex gap-1">
        {tab("reference", "Reference")}
        {tab("upload", "Uploaded file")}
      </div>
      {mode === "reference" ? (
        <ReferenceField
          id={id}
          nodeId={nodeId}
          value={typeof value === "string" ? value : value ? JSON.stringify(value) : ""}
          onChange={onChange}
          invalid={invalid}
          describedBy={describedBy}
          placeholder="{{input.document}}"
        />
      ) : (
        <FileChooser id={id} value={isUpload ? (value as string) : undefined} onChange={onChange} invalid={invalid} describedBy={describedBy} />
      )}
    </div>
  );
}

function StringList({ value, onChange, max }: { value: string[]; onChange: (v: string[]) => void; max?: number }) {
  return (
    <div className="space-y-1.5">
      {value.map((item, index) => (
        <div key={index} className="flex gap-1.5">
          <input
            value={item}
            onChange={(e) => onChange(value.map((v, i) => (i === index ? e.target.value : v)))}
            className="nodrag block w-full rounded-md border border-slate-300 px-2 py-1 text-sm focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-100"
          />
          <button type="button" onClick={() => onChange(value.filter((_, i) => i !== index))} className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700" aria-label="Remove">
            <X className="size-4" />
          </button>
        </div>
      ))}
      {(max === undefined || value.length < max) && (
        <button type="button" onClick={() => onChange([...value, ""])} className="flex items-center gap-1 text-xs font-medium text-indigo-600 hover:text-indigo-500">
          <Plus className="size-3.5" /> Add
        </button>
      )}
    </div>
  );
}

/** JSON editor for dict/list fields: keeps the text while it doesn't parse, emits values that do. */
function JsonField({
  id,
  nodeId,
  value,
  onChange,
  kind,
  invalid,
  describedBy,
}: {
  id: string;
  nodeId: string;
  value: unknown;
  onChange: (v: unknown) => void;
  kind: "object" | "json-array";
  invalid: boolean;
  describedBy?: string;
}) {
  const initial = value === undefined || value === null ? "" : typeof value === "string" ? value : JSON.stringify(value, null, 2);
  const [draft, setDraft] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  const update = (next: string) => {
    setDraft(next);
    if (!next.trim()) {
      setError(null);
      onChange(undefined);
      return;
    }
    if (isReferenceOnly(next)) {
      setError(null);
      onChange(next.trim());
      return;
    }
    try {
      const parsed = JSON.parse(next);
      const ok = kind === "object" ? parsed && typeof parsed === "object" && !Array.isArray(parsed) : Array.isArray(parsed);
      setError(ok ? null : kind === "object" ? "Must be a JSON object, e.g. {\"key\": \"value\"}" : "Must be a JSON array");
      if (ok) onChange(parsed);
    } catch {
      setError("Not valid JSON yet");
    }
  };
  return (
    <div>
      <ReferenceField id={id} nodeId={nodeId} value={draft} onChange={update} multiline rows={3} invalid={invalid || Boolean(error)} describedBy={describedBy} placeholder={kind === "object" ? '{"key": "value"}' : "[]"} />
      {error && <p className="mt-1 text-[11px] text-amber-700">{error}</p>}
    </div>
  );
}

/** Any-typed fields (Output value, HTTP body, ...): plain text, or JSON when you choose it. */
function AnyField({
  id,
  nodeId,
  value,
  onChange,
  invalid,
  describedBy,
}: {
  id: string;
  nodeId: string;
  value: unknown;
  onChange: (v: unknown) => void;
  invalid: boolean;
  describedBy?: string;
}) {
  const [mode, setMode] = useState<"text" | "json">(typeof value === "string" || value === undefined || value === null ? "text" : "json");
  const [draft, setDraft] = useState(mode === "json" ? JSON.stringify(value, null, 2) : text(value));
  const [error, setError] = useState<string | null>(null);

  const update = (next: string, nextMode = mode) => {
    setDraft(next);
    if (nextMode === "text") {
      setError(null);
      onChange(next);
      return;
    }
    try {
      onChange(JSON.parse(next));
      setError(null);
    } catch {
      setError("Not valid JSON yet");
    }
  };

  return (
    <div>
      <div className="mb-1 flex gap-1" role="radiogroup" aria-label="Value type">
        {(["text", "json"] as const).map((m) => (
          <button
            key={m}
            type="button"
            role="radio"
            aria-checked={mode === m}
            onClick={() => {
              setMode(m);
              update(draft, m);
            }}
            className={`rounded px-2 py-0.5 text-[11px] font-medium ${mode === m ? "bg-indigo-100 text-indigo-700" : "text-slate-500 hover:bg-slate-100"}`}
          >
            {m === "text" ? "Text" : "JSON"}
          </button>
        ))}
      </div>
      <ReferenceField id={id} nodeId={nodeId} value={draft} onChange={(v) => update(v)} multiline rows={mode === "json" ? 5 : 3} invalid={invalid || Boolean(error)} describedBy={describedBy} />
      {error && <p className="mt-1 text-[11px] text-amber-700">{error}</p>}
    </div>
  );
}
