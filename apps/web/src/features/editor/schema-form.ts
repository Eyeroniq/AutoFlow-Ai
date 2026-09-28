/**
 * Turns a node's JSON Schema (from GET /api/nodes, i.e. the engine's Pydantic config model)
 * into form field specs and a matching Zod schema. Pure (unit tested).
 *
 * {{...}} references are accepted wherever the backend accepts them: it skips static type
 * checks for templated values, so e.g. temperature may be "{{vars.temperature}}".
 */
import { z } from "zod";

import type { JsonSchema } from "@/lib/types";

export type FieldKind =
  | "text"
  | "textarea"
  | "number"
  | "integer"
  | "boolean"
  | "select"
  | "addresses" // str | list[str]: comma-separated email addresses
  | "string-list" // list[str]
  | "object" // dict: edited as JSON
  | "json-array" // list of objects: edited as JSON
  | "any"; // Any: text, or JSON when it parses as an object/array

export interface FieldSpec {
  name: string;
  label: string;
  kind: FieldKind;
  required: boolean;
  nullable: boolean;
  description?: string;
  default?: unknown;
  options?: string[];
  min?: number;
  max?: number;
  exclusiveMin?: number;
  exclusiveMax?: number;
  minLength?: number;
  maxLength?: number;
  maxItems?: number;
  pattern?: string;
}

/** Free-text fields that deserve a multi-line box. */
const LONG_TEXT = new Set(["system_prompt", "user_prompt", "body", "html_body", "text", "prompt"]);

const REFERENCE_ONLY = /^\s*\{\{[^{}]+\}\}\s*$/;
export const containsReference = (value: unknown) => typeof value === "string" && /\{\{[^{}]+\}\}/.test(value);
export const isReferenceOnly = (value: unknown) => typeof value === "string" && REFERENCE_ONLY.test(value);

function titleCase(name: string): string {
  const words = name.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function resolve(schema: JsonSchema, root: JsonSchema): JsonSchema {
  if (schema.$ref?.startsWith("#/$defs/")) {
    const target = root.$defs?.[schema.$ref.slice("#/$defs/".length)];
    return target ? resolve(target, root) : schema;
  }
  return schema;
}

function specFor(name: string, raw: JsonSchema, root: JsonSchema, required: boolean): FieldSpec {
  let schema = resolve(raw, root);
  let nullable = false;
  const variants = schema.anyOf?.map((s) => resolve(s, root));
  const base: FieldSpec = {
    name,
    label: raw.title && raw.title !== titleCase(name) ? raw.title : titleCase(name),
    kind: "text",
    required,
    nullable: false,
    description: raw.description ?? schema.description,
    default: raw.default,
  };

  if (variants) {
    const nonNull = variants.filter((v) => v.type !== "null");
    nullable = nonNull.length < variants.length;
    const isString = (v: JsonSchema) => v.type === "string" && !v.enum;
    const isStringArray = (v: JsonSchema) => v.type === "array" && v.items?.type === "string";
    if (nonNull.length === 2 && nonNull.some(isString) && nonNull.some(isStringArray)) {
      return { ...base, kind: "addresses", nullable };
    }
    if (nonNull.length === 1) schema = nonNull[0];
    else return { ...base, kind: "any", nullable };
  }

  const bounds = {
    min: schema.minimum,
    max: schema.maximum,
    exclusiveMin: schema.exclusiveMinimum,
    exclusiveMax: schema.exclusiveMaximum,
  };
  const withNull = { ...base, nullable };

  if (schema.enum) return { ...withNull, kind: "select", options: schema.enum.map(String) };
  switch (schema.type) {
    case "string":
      return {
        ...withNull,
        kind: LONG_TEXT.has(name) ? "textarea" : "text",
        minLength: schema.minLength,
        maxLength: schema.maxLength,
        pattern: schema.pattern,
      };
    case "number":
      return { ...withNull, kind: "number", ...bounds };
    case "integer":
      return { ...withNull, kind: "integer", ...bounds };
    case "boolean":
      return { ...withNull, kind: "boolean" };
    case "array":
      if (resolve(schema.items ?? {}, root).type === "string") {
        return { ...withNull, kind: "string-list", maxItems: schema.maxItems };
      }
      return { ...withNull, kind: "json-array", maxItems: schema.maxItems };
    case "object":
      return { ...withNull, kind: "object" };
    default:
      return { ...withNull, kind: "any" };
  }
}

/** Field specs in schema property order. */
export function fieldsFromSchema(schema: JsonSchema): FieldSpec[] {
  const required = new Set(schema.required ?? []);
  return Object.entries(schema.properties ?? {}).map(([name, prop]) => specFor(name, prop, schema, required.has(name)));
}

const isEmpty = (value: unknown) => value === undefined || value === null || value === "";

function numberRule(field: FieldSpec) {
  let rule = field.kind === "integer" ? z.number().int("Must be a whole number") : z.number();
  if (field.min !== undefined) rule = rule.min(field.min, `Must be at least ${field.min}`);
  if (field.max !== undefined) rule = rule.max(field.max, `Must be at most ${field.max}`);
  if (field.exclusiveMin !== undefined) rule = rule.gt(field.exclusiveMin, `Must be greater than ${field.exclusiveMin}`);
  if (field.exclusiveMax !== undefined) rule = rule.lt(field.exclusiveMax, `Must be less than ${field.exclusiveMax}`);
  return rule;
}

function fieldRule(field: FieldSpec): z.ZodType {
  const reference = z.string().regex(REFERENCE_ONLY, "Enter a value or a {{reference}}");
  let rule: z.ZodType;
  switch (field.kind) {
    case "text":
    case "textarea": {
      let text = z.string();
      if (field.minLength) text = text.min(field.minLength, field.minLength === 1 ? "Required" : `At least ${field.minLength} characters`);
      if (field.maxLength) text = text.max(field.maxLength, `At most ${field.maxLength} characters`);
      rule = text;
      if (field.pattern) {
        const pattern = new RegExp(field.pattern);
        rule = text.refine((v) => containsReference(v) || pattern.test(v), "Only letters, digits, _ and - are allowed");
      }
      break;
    }
    case "number":
    case "integer":
      rule = z.union([numberRule(field), reference], { error: "Enter a number or a {{reference}}" });
      break;
    case "boolean":
      rule = z.union([z.boolean(), reference]);
      break;
    case "select": {
      const options = field.options ?? [];
      rule = z.string().refine((v) => options.includes(v) || isReferenceOnly(v), `Choose one of: ${options.join(", ")}`);
      break;
    }
    case "addresses":
      rule = z.union([z.string(), z.array(z.string())]);
      break;
    case "string-list": {
      let list = z.array(z.string());
      if (field.maxItems !== undefined) list = list.max(field.maxItems, `At most ${field.maxItems} entries`);
      rule = list;
      break;
    }
    case "object":
      rule = z.union([z.record(z.string(), z.unknown()), reference], { error: "Must be a JSON object" });
      break;
    case "json-array":
      rule = z.union([z.array(z.unknown()), reference], { error: "Must be a JSON array" });
      break;
    default:
      rule = z.unknown();
  }
  if (field.required) {
    if (field.kind === "text" || field.kind === "textarea") {
      // Like the server: the key must be present; an empty string only fails a minLength.
      return z.any().refine((v) => v !== undefined && v !== null, "Required").pipe(rule);
    }
    if (field.kind === "addresses") {
      // An email with no recipient can never send.
      return rule.refine((v) => !isEmpty(v) && !(Array.isArray(v) && v.length === 0), "Required");
    }
    // An empty required field reads "Required", not the type error the rule would give.
    const present = z.any().refine((v) => !isEmpty(v), "Required");
    return field.kind === "any" ? present : present.pipe(rule);
  }
  return z.preprocess((v) => (isEmpty(v) ? undefined : v), rule.optional().nullable());
}

/** A Zod object validating a config against the fields (unknown keys pass through). */
export function zodForFields(fields: FieldSpec[]) {
  return z.looseObject(Object.fromEntries(fields.map((f) => [f.name, fieldRule(f)])));
}

/**
 * The config to store: optional fields left empty are dropped so the server's defaults
 * apply (e.g. a blank model means "the provider's default model").
 */
export function cleanConfig(values: Record<string, unknown>, fields: FieldSpec[]): Record<string, unknown> {
  const byName = new Map(fields.map((f) => [f.name, f]));
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(values)) {
    const field = byName.get(key);
    if (field && !field.required && (isEmpty(value) || (Array.isArray(value) && value.length === 0 && field.kind !== "json-array"))) {
      continue;
    }
    out[key] = value;
  }
  return out;
}

/** Parse a number field's text: numbers become numbers; references and partial input stay text. */
export function parseNumberInput(text: string, integer: boolean): number | string | undefined {
  const trimmed = text.trim();
  if (trimmed === "") return undefined;
  if (/^-?\d+(\.\d+)?$/.test(trimmed)) {
    const value = Number(trimmed);
    return integer && !Number.isInteger(value) ? trimmed : value;
  }
  return text;
}
