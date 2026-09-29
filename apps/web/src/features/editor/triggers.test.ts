import { describe, expect, it } from "vitest";

import { formatUntil } from "@/lib/format";
import type { NodeType, Trigger, TriggerSettings } from "@/lib/types";

import { configSummary, fromApiGraph } from "./graph";
import { buildReferenceSuggestions } from "./references";
import { catalog, workflow } from "./test-fixtures";
import { emailConfigPayload, formatInZone, overallTone, triggerState, webhookExample } from "./triggers";

const settings: TriggerSettings = { max_runs_per_hour: 30, max_consecutive_failures: 3 };

function trigger(overrides: Partial<Trigger> = {}): Trigger {
  return {
    type: "schedule", id: "t1", configured: true, enabled: true, config: {}, next_run_at: null, upcoming: [],
    last_fired_at: null, last_run: null, consecutive_failures: 0, auto_disabled_at: null, disabled_reason: null,
    last_error: null, last_error_at: null, warnings: [], webhook: null, mailbox: null, ...overrides,
  };
}

describe("triggerState", () => {
  it("reads on, off, not set up, failing, and switched off by failures", () => {
    expect(triggerState(trigger(), settings)).toEqual({ tone: "on", label: "On" });
    expect(triggerState(trigger({ enabled: false }), settings)).toEqual({ tone: "off", label: "Off" });
    expect(triggerState(trigger({ enabled: false, configured: false }), settings).label).toBe("Not set up");
    expect(triggerState(trigger({ consecutive_failures: 2 }), settings)).toEqual({
      tone: "warning",
      label: "On · 2 failed in a row (switches off at 3)",
    });
    expect(triggerState(trigger({ enabled: false, consecutive_failures: 3, auto_disabled_at: "2026-09-29T08:00:00Z" }), settings)).toEqual({
      tone: "disabled",
      label: "Off after 3 failed runs in a row",
    });
  });

  it("the top bar shows the most urgent state", () => {
    const off = trigger({ enabled: false });
    expect(overallTone([off], settings)).toBe("off");
    expect(overallTone([off, trigger()], settings)).toBe("on");
    expect(overallTone([trigger(), trigger({ consecutive_failures: 1 })], settings)).toBe("warning");
    expect(overallTone([trigger({ enabled: false, auto_disabled_at: "x" }), trigger()], settings)).toBe("disabled");
  });
});

describe("formatting", () => {
  it("shows an instant in the schedule's own zone", () => {
    // 02:00 UTC is 07:30 in Kolkata.
    expect(formatInZone("2026-09-29T02:00:00Z", "Asia/Kolkata", "en-GB")).toBe("Tue 29 Sept, 07:30 (Asia/Kolkata)");
    expect(formatInZone("2026-09-29T02:00:00Z", "UTC", "en-GB")).toBe("Tue 29 Sept, 02:00 (UTC)");
  });

  it("describes future times", () => {
    const now = Date.parse("2026-09-29T08:00:00Z");
    expect(formatUntil("2026-09-29T08:00:20Z", now)).toBe("now");
    expect(formatUntil("2026-09-29T08:07:00Z", now)).toBe("in 7 min");
    expect(formatUntil("2026-09-29T11:00:00Z", now)).toBe("in 3 h");
    expect(formatUntil("2026-10-01T08:00:00Z", now)).toBe("in 2 d");
  });

  it("builds a webhook example without the secret", () => {
    const example = webhookExample("http://localhost:8000", "/api/v1/deployments/d1/run", "ffk_ab12cd34");
    expect(example).toContain('curl -X POST "http://localhost:8000/api/v1/deployments/d1/run"');
    expect(example).toContain("Bearer ffk_ab12cd34…");
  });
});

describe("emailConfigPayload", () => {
  it("normalizes the form: blanks are no filter, numbers are numbers", () => {
    expect(
      emailConfigPayload({ folder: " ", from_address: "  boss@example.com ", subject: "", poll_minutes: "2", max_per_poll: "0", unread_only: false }),
    ).toEqual({
      folder: "INBOX", from_address: "boss@example.com", subject: null, unread_only: false, poll_minutes: 2,
      input_name: "email", max_per_poll: 10, mark_as_read: false, max_body_chars: 5000,
    });
  });
});

describe("per-item references", () => {
  const byType: Record<string, NodeType> = Object.fromEntries(catalog.map((c) => [c.type, c]));
  const api = workflow().graph;
  const graph = fromApiGraph(
    {
      ...api,
      nodes: [...api.nodes, { id: "each", type: "for_each", label: "Each", position: { x: 0, y: 200 }, config: { items: "{{gemini.response}}", prompt: "{{item}}" } }],
      edges: [...api.edges, { source: "gemini", target: "each" }],
    },
    byType,
  );
  const suggest = (field?: string) =>
    buildReferenceSuggestions({ nodeId: "each", nodes: graph.nodes, edges: graph.edges, variables: graph.variables, catalog: byType, field }).map((s) => s.ref);

  it("offers {{item}} and {{index}} first in per-item fields only", () => {
    expect(suggest("prompt").slice(0, 3)).toEqual(["item", "item.title", "index"]);
    expect(suggest("prompt")).toContain("gemini.response");
    expect(suggest("items")).not.toContain("item");
    expect(suggest()).not.toContain("index");
  });
});

describe("node summaries", () => {
  const entry = (type: string) => catalog.find((c) => c.type === type);
  it("summarizes the new nodes", () => {
    expect(configSummary("for_each", { items: "{{rss.items}}", provider: "groq", concurrency: 3, rate_limit_per_minute: 20 }, entry("for_each"))).toBe(
      "each of {{rss.items}} · groq · 3 at a time · 20/min",
    );
    expect(configSummary("for_each", { items: "{{x.list}}", mode: "template" })).toBe("each of {{x.list}} · template");
    expect(configSummary("filter", { field: "output.score", operator: "greater_or_equal", value: "{{vars.threshold}}" })).toBe(
      "output.score greater or equal {{vars.threshold}}",
    );
    expect(configSummary("rss", { url: "https://feeds.example.com/rss", since_last_run: true })).toBe(
      "https://feeds.example.com/rss · 10 items · new since last run",
    );
    expect(configSummary("telegram", { text: "{{digest.response}}" })).toBe("default chat · {{digest.response}}");
    expect(configSummary("discord_webhook", { content: "hi" })).toBe("connected webhook · hi");
  });
});
