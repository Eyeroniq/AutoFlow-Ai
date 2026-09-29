import { expect, type Page, test } from "@playwright/test";

import type { WorkflowGraph } from "../src/lib/types";

import { Api, edge, expectSaved, node, openEditor, screenshotPath, signIn, waitForEmail } from "./helpers";

/** Counts the live events the browser receives over the execution WebSocket. */
function watchSocket(page: Page) {
  const seen: Record<string, number> = {};
  page.on("websocket", (ws) => {
    if (!ws.url().includes("/ws/executions/")) return;
    ws.on("framereceived", ({ payload }) => {
      try {
        const type = (JSON.parse(String(payload)) as { type?: string }).type ?? "?";
        seen[type] = (seen[type] ?? 0) + 1;
      } catch {
        // not JSON
      }
    });
  });
  return seen;
}

test("runs the seeded pipeline with live status, streaming, and a real email", async ({ page, request }) => {
  const api = await Api.login(request);
  const demo = await api.demoWorkflow();
  await api.expectRunnable(demo);
  const original = demo.graph;
  const events = watchSocket(page);
  try {
    await signIn(page, api);
    await openEditor(page, demo.id);
    await expectSaved(page);
    await page.screenshot({ path: screenshotPath("editor") });

    // Edit the prompt and turn on streaming; autosave picks both up.
    await node(page, "gemini").locator("p.font-semibold").click();
    const panel = page.getByTestId("config-panel");
    const prompt = panel.getByLabel("User Prompt");
    await prompt.click();
    await prompt.press("Control+End");
    await prompt.pressSequentially(" Keep it under 80 words.");
    await panel.getByRole("checkbox", { name: "Stream" }).check();
    await expect(page.getByTestId("save-state")).not.toHaveText("Saved");
    await expectSaved(page);
    const saved = (await api.workflow(demo.id)).graph.nodes.find((n) => n.id === "gemini")!;
    expect(saved.config.user_prompt).toContain("Keep it under 80 words.");
    expect(saved.config.stream).toBe(true);

    // Deselect (closes the config panel) so the whole graph is in view while it runs.
    await page.getByTestId("canvas").click({ position: { x: 40, y: 40 } });
    await expect(page.getByTestId("config-panel")).toHaveCount(0);

    // Run with a unique topic so the email can be found afterwards.
    const marker = `e2e-${Date.now()}`;
    await page.getByTestId("run-button").click();
    await page.getByTestId("run-input-topic").fill(`why code review matters (${marker})`);
    await page.getByTestId("confirm-run").click();
    await expect(page.getByTestId("run-panel")).toBeVisible();

    // Live: the LLM node turns blue (running) with its incoming edge animated...
    await expect(node(page, "gemini")).toHaveAttribute("data-status", "running", { timeout: 30_000 });
    await expect(edge(page, "input->gemini")).toHaveClass(/edge-running/);
    await expect(page.getByTestId("connection-status")).toHaveText("Live");
    // Catch the streamed text on the node if it's still coming in (the socket count below checks it arrived).
    await page.getByTestId("node-tokens-gemini").waitFor({ timeout: 10_000 }).catch(() => undefined);
    await page.screenshot({ path: screenshotPath("run-live") });

    // ...then everything turns green.
    await expect(page.getByTestId("run-status")).toHaveText(/success/i, { timeout: 120_000 });
    for (const id of ["input", "gemini", "gmail", "output"]) {
      await expect(node(page, id)).toHaveAttribute("data-status", "success");
    }
    for (const id of ["input->gemini", "gemini->gmail", "gmail->output"]) {
      await expect(edge(page, id)).toHaveClass(/edge-success/);
    }
    await expect(page.getByTestId("final-output")).toContainText('"summary"');
    await expect(page.getByTestId("timeline-gemini")).toHaveAttribute("data-status", "success");
    await page.getByTestId("timeline-gemini").getByRole("button").click();
    await expect(page.getByTestId("timeline-gemini")).toContainText("Output");
    await page.screenshot({ path: screenshotPath("run-success") });

    expect(events.snapshot, "the socket starts with a snapshot").toBeGreaterThanOrEqual(1);
    expect(events["node.started"]).toBeGreaterThanOrEqual(4);
    expect(events["node.token"] ?? 0, "the LLM (Gemini, or its Groq fallback) streamed tokens to the browser").toBeGreaterThan(0);
    expect(events["execution.finished"]).toBe(1);

    // The real email arrived in the SMTP_USER inbox.
    const email = await waitForEmail(api, marker);
    expect(email.subject).toContain(marker);

    // The run's detail page shows the same per-node timeline.
    await page.getByRole("link", { name: "Details" }).click();
    await expect(page).toHaveURL(/\/executions\/[0-9a-f-]{36}$/);
    await expect(page.getByTestId("execution-status")).toHaveText(/success/i);
    await expect(page.getByTestId("timeline-gmail")).toHaveAttribute("data-status", "success");
    await page.getByTestId("timeline-gemini").getByRole("button").click();
    await page.screenshot({ path: screenshotPath("execution-detail") });

    // The dashboard shows the pipeline's last run.
    await page.getByRole("link", { name: "Dashboard", exact: true }).click();
    await expect(page.locator(`[data-testid="pipeline-row"][data-name="${demo.name}"]`)).toContainText("success");
    await page.screenshot({ path: screenshotPath("dashboard") });
  } finally {
    await api.putGraph(demo.id, original);
  }
});

// A graph that takes long enough to stop or join mid-run, with no provider calls.
const slowGraph = (): WorkflowGraph => ({
  nodes: [
    { id: "wait", type: "delay", label: "Wait", position: { x: 0, y: 0 }, config: { seconds: 20 } },
    { id: "output", type: "output", label: "Result", position: { x: 320, y: 0 }, config: { name: "result", value: "done" } },
  ],
  edges: [{ source: "wait", target: "output" }],
  variables: [],
});

test("stops a running workflow from the editor", async ({ page, request }) => {
  const api = await Api.login(request);
  const workflow = await api.createWorkflow("E2E stop", slowGraph());
  try {
    await signIn(page, api);
    await openEditor(page, workflow.id);
    await page.getByTestId("run-button").click(); // no Input nodes: runs straight away
    await expect(node(page, "wait")).toHaveAttribute("data-status", "running", { timeout: 30_000 });
    await page.getByTestId("stop-button").click();
    await expect(page.getByTestId("run-status")).toHaveText(/stopped/i, { timeout: 30_000 });
    await expect(node(page, "wait")).toHaveAttribute("data-status", /stopped|cancelled|skipped|failed/);
    await expect(node(page, "output")).not.toHaveAttribute("data-status", "success");
    await expect(page.getByTestId("run-button")).toHaveText(/Run again/);
  } finally {
    await api.remove(workflow.id);
  }
});

test("joins a run that is already in progress", async ({ page, request }) => {
  const api = await Api.login(request);
  const workflow = await api.createWorkflow("E2E late join", { ...slowGraph(), nodes: slowGraph().nodes.map((n) => (n.id === "wait" ? { ...n, config: { seconds: 6 } } : n)) });
  try {
    const { status, body } = await api.call<{ execution_id: string }>("POST", `/api/workflows/${workflow.id}/run`, { inputs: {} });
    expect(status).toBe(202);
    await signIn(page, api);
    await openEditor(page, workflow.id);
    // The snapshot replays what happened before the socket opened, then events stream in.
    await expect(page.getByText("Following a run in progress")).toBeVisible();
    await expect(page.getByTestId("run-panel")).toContainText(body.execution_id.slice(0, 8));
    await expect(page.getByTestId("run-status")).toHaveText(/success/i, { timeout: 60_000 });
    await expect(node(page, "output")).toHaveAttribute("data-status", "success");
  } finally {
    await api.remove(workflow.id);
  }
});
