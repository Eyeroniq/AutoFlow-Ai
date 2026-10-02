import { describe, expect, it } from "vitest";

import type { Integration } from "@/lib/types";

import { HOW_TO, missingConnection, providerForNode } from "./connect-model";

function integration(provider: string, patch: Partial<Integration> = {}): Integration {
  return {
    provider,
    label: provider,
    kind: "email",
    connected: false,
    source: "none",
    status: "disconnected",
    masked: null,
    connected_at: null,
    last_test: null,
    default_model: null,
    get_key_url: null,
    ...patch,
  } as Integration;
}

describe("providerForNode", () => {
  it("maps the nodes that act on a personal account", () => {
    expect(providerForNode("gmail")).toBe("gmail");
    expect(providerForNode("gmail_read")).toBe("gmail");
    expect(providerForNode("telegram")).toBe("telegram");
    expect(providerForNode("discord_webhook")).toBe("discord");
    expect(providerForNode("notion_create_page")).toBe("notion");
    expect(providerForNode("notion_query_database")).toBe("notion");
    expect(providerForNode("airtable_create_record")).toBe("airtable");
    expect(providerForNode("airtable_list_records")).toBe("airtable");
  });

  it("leaves everything else alone (the demo provides the AI)", () => {
    for (const type of ["gemini", "groq", "http_request", "summarize", "input", "pdf_extract"]) {
      expect(providerForNode(type)).toBeNull();
    }
  });
});

describe("missingConnection", () => {
  const all = [integration("gmail"), integration("telegram"), integration("discord"), integration("notion"), integration("airtable")];

  it("asks a visitor with nothing connected to connect their own", () => {
    expect(missingConnection("gmail", {}, all)?.provider).toBe("gmail");
    expect(missingConnection("notion_create_page", { database_id: "x" }, all)?.provider).toBe("notion");
  });

  it("is satisfied by the user's own credential", () => {
    const mine = all.map((i) => (i.provider === "gmail" ? integration("gmail", { connected: true, source: "user" }) : i));
    expect(missingConnection("gmail", {}, mine)).toBeNull();
    expect(missingConnection("telegram", {}, mine)?.provider).toBe("telegram");
  });

  it("is satisfied by the server's account outside the public demo (and never inside it, where the API says 'none')", () => {
    const owner = all.map((i) => integration(i.provider, { source: "server" }));
    expect(missingConnection("gmail", {}, owner)).toBeNull();
    expect(missingConnection("gmail", {}, all)).not.toBeNull();
  });

  it("does not nag a node set to the mock sender, or while integrations are still loading", () => {
    expect(missingConnection("gmail", { auth: "mock" }, all)).toBeNull();
    expect(missingConnection("gmail", {}, undefined)).toBeNull();
    expect(missingConnection("gmail", undefined, [])).toBeNull();
  });

  it("needs nothing for a node that needs no account", () => {
    expect(missingConnection("http_request", {}, all)).toBeNull();
  });
});

describe("HOW_TO", () => {
  it("has instructions for every provider a node can ask for", () => {
    for (const provider of ["gmail", "telegram", "discord", "notion", "airtable"]) {
      expect(HOW_TO[provider].steps.length).toBeGreaterThanOrEqual(3);
      expect(HOW_TO[provider].title).toBeTruthy();
    }
  });
});
