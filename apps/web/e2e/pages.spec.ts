import { expect, test } from "@playwright/test";

import type { Integration, WorkflowGraph } from "../src/lib/types";

import { API_URL, Api, DEMO_PIPELINE, screenshotPath, signIn } from "./helpers";

const quickGraph = (): WorkflowGraph => ({
  nodes: [
    { id: "wait", type: "delay", label: "Wait", position: { x: 0, y: 0 }, config: { seconds: 1 } },
    { id: "output", type: "output", label: "Result", position: { x: 320, y: 0 }, config: { name: "result", value: "done" } },
  ],
  edges: [{ source: "wait", target: "output" }],
  variables: [],
});

test("dashboard: create, open, run, duplicate, and delete pipelines", async ({ page, request }) => {
  const api = await Api.login(request);
  const name = `E2E dashboard ${Date.now()}`;
  const workflow = await api.createWorkflow(name, quickGraph());
  const cleanup = [workflow.id];
  try {
    await signIn(page, api);
    await page.goto("/dashboard");
    const row = page.locator(`[data-testid="pipeline-row"][data-name="${name}"]`);
    await expect(row).toContainText("Never run");
    await expect(row).toContainText("2 nodes");
    await expect(page.locator(`[data-testid="pipeline-row"][data-name="${DEMO_PIPELINE}"]`)).toBeVisible();

    // Duplicate, then delete the copy.
    await row.getByRole("button", { name: `More actions for ${name}` }).click();
    await page.getByRole("menuitem", { name: "Duplicate" }).click();
    const copy = page.locator(`[data-testid="pipeline-row"][data-name="${name} (copy)"]`);
    await expect(copy).toBeVisible();
    await copy.getByRole("button", { name: `More actions for ${name} (copy)` }).click();
    await page.getByRole("menuitem", { name: "Delete" }).click();
    await page.getByRole("dialog").getByRole("button", { name: "Delete" }).click();
    await expect(copy).toHaveCount(0);

    // Run: lands on the live execution page, which finishes on its own.
    await row.getByRole("button", { name: `Run ${name}` }).click();
    await expect(page).toHaveURL(/\/executions\/[0-9a-f-]{36}$/);
    await expect(page.getByTestId("execution-status")).toHaveText(/success/i, { timeout: 60_000 });
    await expect(page.getByTestId("timeline-wait")).toHaveAttribute("data-status", "success");
    await expect(page.getByTestId("execution-output")).toContainText('"result": "done"');

    // Back on the dashboard the row shows its last run, and it's in the recent list.
    await page.getByRole("link", { name: "Dashboard", exact: true }).click();
    await expect(row).toContainText("success");
    await expect(page.getByTestId("execution-row").filter({ hasText: name }).first()).toBeVisible();

    // Create: opens the editor on the new, empty pipeline.
    await page.getByTestId("new-pipeline").click();
    await page.getByLabel("Name").fill(`${name} new`);
    await page.getByTestId("create-pipeline").click();
    await expect(page).toHaveURL(/\/pipelines\/[0-9a-f-]{36}$/);
    cleanup.push(page.url().split("/").pop()!);
    await expect(page.getByTestId("workflow-name")).toHaveText(`${name} new`);
    await expect(page.locator(".react-flow__node")).toHaveCount(0);
  } finally {
    for (const id of cleanup) await api.remove(id);
  }
});

test("executions: filter the history and open a run's detail", async ({ page, request }) => {
  const api = await Api.login(request);
  const name = `E2E history ${Date.now()}`;
  const workflow = await api.createWorkflow(name, quickGraph());
  try {
    const { body } = await api.call<{ execution_id: string }>("POST", `/api/workflows/${workflow.id}/run?sync=true`, { inputs: {} });
    const executionId = (body as unknown as { id?: string; execution_id?: string }).id ?? body.execution_id;
    await signIn(page, api);
    await page.goto("/executions");
    await expect(page.getByTestId("execution-row").first()).toBeVisible();

    await page.getByLabel("Filter by pipeline").selectOption({ label: name });
    await expect(page.getByTestId("execution-row")).toHaveCount(1);
    await page.getByLabel("Filter by status").selectOption("failed");
    await expect(page.getByText("No executions match these filters.")).toBeVisible();
    await page.getByLabel("Filter by status").selectOption("success");
    await expect(page.getByTestId("execution-row")).toHaveCount(1);

    await page.getByTestId("execution-row").first().click();
    await expect(page).toHaveURL(new RegExp(`/executions/${executionId}$`));
    await expect(page.getByRole("heading", { name })).toBeVisible();
    await expect(page.getByTestId("timeline-output")).toHaveAttribute("data-status", "success");

    // Someone else's (or a made-up) run: a clear message, not a crash.
    await page.goto("/executions/00000000-0000-4000-8000-000000000000");
    await expect(page.getByText("This execution doesn't exist, or it isn't yours.")).toBeVisible();
  } finally {
    await api.remove(workflow.id);
  }
});

test("the live socket refuses bad tokens (4401) and other people's runs (4404)", async ({ page, request }) => {
  const api = await Api.login(request);
  await page.goto("/login");
  const codes = await page.evaluate(
    async ({ wsUrl, token }) => {
      const closeCode = (id: string, auth: string) =>
        new Promise<number>((resolve) => {
          const ws = new WebSocket(`${wsUrl}/ws/executions/${id}`);
          ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token: auth }));
          ws.onclose = (event) => resolve(event.code);
        });
      const missing = "00000000-0000-4000-8000-000000000000";
      return { badToken: await closeCode(missing, "not-a-token"), notYours: await closeCode(missing, token) };
    },
    { wsUrl: API_URL.replace(/^http/, "ws"), token: api.tokens.access_token },
  );
  expect(codes).toEqual({ badToken: 4401, notYours: 4404 });
});

test("integrations: test real credentials, connect and disconnect a key", async ({ page, request }) => {
  const api = await Api.login(request);
  const before = (await api.call<Integration[]>("GET", "/api/integrations")).body;
  // Only touch a provider the demo user hasn't stored a key for, so nothing of theirs is lost.
  const spare = before.find((i) => i.kind === "llm" && i.source !== "user" && ["groq", "openrouter", "anthropic", "openai"].includes(i.provider));
  await signIn(page, api);
  await page.goto("/integrations");

  // Server-wide credentials from .env, checked with real, minimal calls.
  for (const provider of ["gemini", "gmail"]) {
    const card = page.getByTestId(`integration-${provider}`);
    await expect(card).toBeVisible();
    test.skip(before.find((i) => i.provider === provider)?.source === "none", `${provider} has no credential configured`);
    await card.getByTestId(`test-${provider}`).click();
    await expect(card.getByTestId(`integration-test-${provider}`)).toContainText("Works", { timeout: 30_000 });
  }
  await page.screenshot({ path: screenshotPath("integrations"), fullPage: true });

  if (!spare) return;
  const card = page.getByTestId(`integration-${spare.provider}`);
  const fakeKey = "e2e-not-a-real-key-0000000000000000";
  await card.getByTestId(`connect-${spare.provider}`).click();
  await card.getByLabel("API key").fill(fakeKey);
  await card.getByTestId(`save-integration-${spare.provider}`).click();
  await expect(card).toHaveAttribute("data-source", "user");
  // Write-only: the page shows a masked value, never the key itself.
  await expect(card.getByTestId(`masked-${spare.provider}-api_key`)).toBeVisible();
  expect(await page.content()).not.toContain(fakeKey);
  // Saving runs a real test: the provider rejects the fake key.
  await expect(card.getByTestId(`integration-test-${spare.provider}`)).not.toContainText("Works", { timeout: 30_000 });
  await expect(card.getByTestId(`integration-test-${spare.provider}`)).not.toBeEmpty();

  await card.getByTestId(`disconnect-${spare.provider}`).click();
  await page.getByRole("dialog").getByRole("button", { name: "Disconnect" }).click();
  await expect(card).toHaveAttribute("data-source", spare.source);
});
