import { describe, expect, it } from "vitest";

import type { JsonSchema } from "@/lib/types";

import { cleanConfig, fieldsFromSchema, parseNumberInput, zodForFields } from "./schema-form";

// Shapes copied from the real GET /api/nodes output.
const llm: JsonSchema = {
  type: "object",
  required: ["user_prompt"],
  properties: {
    provider: { enum: ["gemini", "groq", "mock"], type: "string", default: "gemini", title: "Provider", description: "Which LLM service answers." },
    model: { anyOf: [{ type: "string" }, { type: "null" }], default: null, title: "Model" },
    system_prompt: { type: "string", default: "", title: "System Prompt" },
    user_prompt: { type: "string", minLength: 1, title: "User Prompt" },
    temperature: { type: "number", minimum: 0, maximum: 2, default: 0.7, title: "Temperature" },
    max_tokens: { type: "integer", minimum: 1, maximum: 65536, default: 8192, title: "Max Tokens" },
    fallback: { type: "array", items: { type: "string" }, maxItems: 5, title: "Fallback" },
    stream: { type: "boolean", default: false, title: "Stream" },
  },
};

const gmail: JsonSchema = {
  type: "object",
  required: ["to", "subject", "body"],
  $defs: { AttachmentConfig: { type: "object", properties: { filename: { type: "string" } } } },
  properties: {
    to: { anyOf: [{ type: "string" }, { type: "array", items: { type: "string" } }], title: "To" },
    cc: { anyOf: [{ type: "string" }, { type: "array", items: { type: "string" } }, { type: "null" }], default: null },
    attachments: { type: "array", items: { $ref: "#/$defs/AttachmentConfig" }, maxItems: 20 },
    subject: { type: "string", minLength: 1 },
    body: { type: "string" },
  },
};

const misc: JsonSchema = {
  type: "object",
  required: ["value"],
  properties: {
    name: { type: "string", maxLength: 100, pattern: "^[A-Za-z0-9_\\-]+$", default: "result" },
    value: { title: "Value" },
    headers: { type: "object", additionalProperties: { type: "string" } },
    since_days: { anyOf: [{ type: "integer", minimum: 1, maximum: 3650 }, { type: "null" }], default: null },
  },
};

describe("fieldsFromSchema", () => {
  it("maps each JSON Schema shape to an input kind", () => {
    const fields = Object.fromEntries(fieldsFromSchema(llm).map((f) => [f.name, f]));
    expect(fields.provider).toMatchObject({ kind: "select", options: ["gemini", "groq", "mock"], default: "gemini", required: false });
    expect(fields.model).toMatchObject({ kind: "text", nullable: true });
    expect(fields.system_prompt.kind).toBe("textarea");
    expect(fields.user_prompt).toMatchObject({ kind: "textarea", required: true, minLength: 1, label: "User Prompt" });
    expect(fields.temperature).toMatchObject({ kind: "number", min: 0, max: 2 });
    expect(fields.max_tokens).toMatchObject({ kind: "integer", min: 1, max: 65536 });
    expect(fields.fallback).toMatchObject({ kind: "string-list", maxItems: 5 });
    expect(fields.stream.kind).toBe("boolean");
    expect(fields.provider.description).toBe("Which LLM service answers.");
  });

  it("handles unions, refs, objects, and untyped fields", () => {
    const g = Object.fromEntries(fieldsFromSchema(gmail).map((f) => [f.name, f]));
    expect(g.to).toMatchObject({ kind: "addresses", required: true });
    expect(g.cc).toMatchObject({ kind: "addresses", nullable: true });
    expect(g.attachments).toMatchObject({ kind: "json-array", maxItems: 20 });
    const m = Object.fromEntries(fieldsFromSchema(misc).map((f) => [f.name, f]));
    expect(m.value).toMatchObject({ kind: "any", required: true });
    expect(m.headers.kind).toBe("object");
    expect(m.since_days).toMatchObject({ kind: "integer", nullable: true, min: 1 });
    expect(m.name).toMatchObject({ kind: "text", maxLength: 100, pattern: "^[A-Za-z0-9_\\-]+$" });
  });

  it("keeps the schema's property order", () => {
    expect(fieldsFromSchema(llm).map((f) => f.name)).toEqual(Object.keys(llm.properties!));
  });
});

describe("zodForFields", () => {
  const llmRules = zodForFields(fieldsFromSchema(llm));
  const miscRules = zodForFields(fieldsFromSchema(misc));
  const messages = (result: ReturnType<typeof llmRules.safeParse>) =>
    result.success ? {} : Object.fromEntries(result.error.issues.map((i) => [String(i.path[0]), i.message]));

  it("accepts a valid config and {{references}} in typed fields", () => {
    expect(llmRules.safeParse({ user_prompt: "Hi", temperature: 0.5, max_tokens: 10, provider: "groq" }).success).toBe(true);
    expect(llmRules.safeParse({ user_prompt: "Hi", temperature: "{{vars.temp}}" }).success).toBe(true);
    expect(llmRules.safeParse({ user_prompt: "Hi", model: null, model2: "extra keys pass" }).success).toBe(true);
  });

  it("reports required, range, integer, and enum errors inline", () => {
    expect(messages(llmRules.safeParse({ user_prompt: "" }))).toEqual({ user_prompt: "Required" });
    expect(messages(llmRules.safeParse({ user_prompt: "x", temperature: 3 })).temperature).toBe("Must be at most 2");
    expect(llmRules.safeParse({ user_prompt: "x", max_tokens: 1.5 }).success).toBe(false);
    expect(messages(llmRules.safeParse({ user_prompt: "x", provider: "mistral" })).provider).toBe("Choose one of: gemini, groq, mock");
    expect(llmRules.safeParse({ user_prompt: "x", fallback: ["a", "b", "c", "d", "e", "f"] }).success).toBe(false);
    expect(llmRules.safeParse({ user_prompt: "x", temperature: "hot" }).success).toBe(false);
  });

  it("applies patterns unless the value is templated, and requires Any fields", () => {
    expect(miscRules.safeParse({ value: 1, name: "bad name" }).success).toBe(false);
    expect(miscRules.safeParse({ value: 1, name: "{{vars.n}}" }).success).toBe(true);
    expect(miscRules.safeParse({ value: "" }).success).toBe(false);
    expect(miscRules.safeParse({ value: { a: 1 }, headers: { k: "v" } }).success).toBe(true);
    expect(miscRules.safeParse({ value: 1, headers: [1] }).success).toBe(false);
  });

  it("says Required for an empty required number, not a type error", () => {
    const delay = zodForFields(fieldsFromSchema({ type: "object", required: ["seconds"], properties: { seconds: { type: "number", minimum: 0, maximum: 60 } } }));
    expect(messages(delay.safeParse({}))).toEqual({ seconds: "Required" });
    expect(messages(delay.safeParse({ seconds: "" }))).toEqual({ seconds: "Required" });
    expect(messages(delay.safeParse({ seconds: 90 }))).toEqual({ seconds: "Must be at most 60" });
    expect(delay.safeParse({ seconds: "{{vars.wait}}" }).success).toBe(true);
  });

  it("matches the server on required text: present is enough unless minLength is set", () => {
    const rules = zodForFields(fieldsFromSchema(gmail));
    // body is required but has no minLength: the server accepts "" too.
    expect(messages(rules.safeParse({ to: "a@b.c", subject: "Hi", body: "" }))).toEqual({});
    expect(messages(rules.safeParse({ to: "a@b.c", subject: "Hi" }))).toEqual({ body: "Required" });
    // subject has minLength 1; an empty recipient list is refused outright.
    expect(messages(rules.safeParse({ to: "", subject: "", body: "x" }))).toEqual({ to: "Required", subject: "Required" });
  });

  it("treats empty optional values as unset", () => {
    expect(llmRules.safeParse({ user_prompt: "x", temperature: "", model: "" }).success).toBe(true);
  });
});

describe("cleanConfig", () => {
  it("drops empty optional values so server defaults apply, keeps required ones", () => {
    const fields = fieldsFromSchema(llm);
    expect(cleanConfig({ provider: "gemini", model: "", user_prompt: "", fallback: [], stream: false, temperature: undefined }, fields))
      .toEqual({ provider: "gemini", user_prompt: "", stream: false });
  });
});

describe("parseNumberInput", () => {
  it("parses numbers and keeps references or partial input as text", () => {
    expect(parseNumberInput("0.4", false)).toBe(0.4);
    expect(parseNumberInput("12", true)).toBe(12);
    expect(parseNumberInput("1.5", true)).toBe("1.5");
    expect(parseNumberInput("", false)).toBeUndefined();
    expect(parseNumberInput("{{vars.t}}", false)).toBe("{{vars.t}}");
  });
});
