import { expect, test } from "@playwright/test";

import type { Workflow, WorkflowGraph } from "../src/lib/types";

import { Api, openEditor, screenshotPath, signIn } from "./helpers";

const textGraph = (): WorkflowGraph => ({
  nodes: [
    { id: "note", type: "text", label: "Note", position: { x: 0, y: 0 }, config: { text: "tick" } },
    { id: "output", type: "output", label: "Result", position: { x: 320, y: 0 }, config: { name: "result", value: "{{note.text}}" } },
  ],
  edges: [{ source: "note", target: "output" }],
  variables: [],
});

test("templates: the dashboard lists them with their requirements, and Use template opens an editable copy", async ({ page, request }) => {
  const api = await Api.login(request);
  const before = new Set((await api.workflows()).map((w) => w.id));
  const created: string[] = [];
  try {
    await signIn(page, api);
    await page.goto("/dashboard");
    const card = page.getByTestId("template-job-alert-filter");
    await expect(card).toBeVisible();
    for (const slug of ["morning-digest", "invoice-extractor", "email-triage"]) await expect(page.getByTestId(`template-${slug}`)).toBeVisible();
    await expect(card).toContainText("An LLM key: Gemini, Groq, or OpenRouter");
    await expect(card).toContainText("Telegram bot (or a Discord webhook)");
    await page.getByRole("region", { name: "Templates" }).screenshot({ path: screenshotPath("templates") });

    await card.getByTestId("use-template-job-alert-filter").click();
    await expect(page).toHaveURL(/\/pipelines\/[0-9a-f-]{36}$/);
    created.push(page.url().split("/").pop()!);
    await expect(page.getByTestId("editor")).toBeVisible();
    await expect(page.locator(".react-flow__node")).toHaveCount(7);
    await expect(page.getByTestId("node-score")).toContainText("each of {{jobs.items}} · gemini · 2 at a time · 10/min");

    // Its schedule came along, switched off.
    await page.getByTestId("triggers-button").click();
    await expect(page.getByTestId("trigger-state-schedule")).toHaveText("Off");
    await expect(page.getByTestId("schedule-cron")).toHaveValue("0 */6 * * *");
  } finally {
    for (const w of await api.workflows()) if (!before.has(w.id) && w.name.startsWith("Job Alert Filter")) created.push(w.id);
    for (const id of new Set(created)) await api.remove(id);
  }
});

test("Triggers panel: a schedule is saved in a time zone, previewed, and switched off again", async ({ page, request }) => {
  const api = await Api.login(request);
  const workflow = await api.createWorkflow(`E2E triggers ${Date.now()}`, textGraph());
  try {
    await signIn(page, api);
    await openEditor(page, workflow.id);
    await page.getByTestId("triggers-button").click();
    const panel = page.getByTestId("triggers-panel");
    await expect(panel.getByTestId("trigger-state-schedule")).toHaveText("Not set up");
    await expect(panel.getByTestId("trigger-state-webhook")).toHaveText("Not set up");
    await expect(panel.getByTestId("webhook-deploy")).toBeVisible();

    await panel.getByRole("button", { name: "Every day 07:30" }).click();
    await panel.getByTestId("schedule-timezone").fill("Asia/Kolkata");
    // The server previews the next fire times in that zone.
    await expect(panel.getByTestId("schedule-preview")).toContainText("07:30 (Asia/Kolkata)");
    await panel.getByTestId("save-schedule").click();
    await expect(panel.getByTestId("trigger-state-schedule")).toHaveText("On");
    await expect(panel.getByTestId("next-run-schedule")).toContainText("07:30 (Asia/Kolkata)");
    await expect(page.getByTestId("triggers-button")).toHaveAttribute("data-tone", "on");
    for (const dismiss of await page.getByRole("button", { name: "Dismiss" }).all()) await dismiss.click();
    await expect(page.getByRole("button", { name: "Dismiss" })).toHaveCount(0);
    await panel.screenshot({ path: screenshotPath("triggers") });

    const saved = (await api.call<{ triggers: { type: string; enabled: boolean; config: Record<string, unknown>; next_run_at: string }[] }>(
      "GET",
      `/api/workflows/${workflow.id}/triggers`,
    )).body.triggers.find((t) => t.type === "schedule")!;
    expect(saved).toMatchObject({ enabled: true, config: { cron: "30 7 * * *", timezone: "Asia/Kolkata" } });
    expect(new Date(saved.next_run_at).getUTCHours() * 60 + new Date(saved.next_run_at).getUTCMinutes()).toBe(2 * 60); // 07:30 IST

    await panel.getByTestId("switch-schedule-trigger").click();
    await expect(panel.getByTestId("trigger-state-schedule")).toHaveText("Off");
    await expect(page.getByTestId("triggers-button")).toHaveAttribute("data-tone", "off");

    // The email trigger stays folded away until it's set up.
    await expect(panel.getByTestId("email-subject")).toHaveCount(0);
    await panel.getByTestId("setup-email").click();
    await expect(panel.getByTestId("email-subject")).toBeVisible();
    await expect(panel.getByTestId("save-email")).toHaveText("Save and turn it on");
  } finally {
    await api.remove(workflow.id);
  }
});

test("executions show their trigger and can be filtered by it", async ({ page, request }) => {
  const api = await Api.login(request);
  const workflow: Workflow = await api.createWorkflow(`E2E trigger filter ${Date.now()}`, textGraph());
  try {
    const { status } = await api.call("POST", `/api/workflows/${workflow.id}/run`, { inputs: {} });
    expect(status).toBe(202);
    await signIn(page, api);
    await page.goto(`/executions?workflow_id=${workflow.id}&trigger=manual`);
    const row = page.getByTestId("execution-row").first();
    await expect(row).toHaveAttribute("data-status", "success", { timeout: 60_000 });
    await expect(row.getByTestId("trigger-badge")).toHaveAttribute("data-trigger", "manual");
    await page.getByTestId("trigger-filter").selectOption("schedule");
    await expect(page.getByText("No executions match these filters.")).toBeVisible();
  } finally {
    await api.remove(workflow.id);
  }
});
