import { expect, test } from "@playwright/test";

import type { ExecutionListItem } from "../src/lib/types";

import { API_URL, Api, DEMO_PIPELINE, expectSaved, openEditor, screenshotPath, signIn } from "./helpers";

interface DeploymentRun {
  execution_id: string;
  status: string;
  final_output: { result?: { summary?: string; email?: string } } | null;
  error: string | null;
  links: { status: string };
}

// Real everything: the dialog deploys the seeded pipeline, and the endpoint is then called
// the way an outside client would, with the key the dialog showed. The run goes through the
// Celery workers, calls Gemini (Groq, the seeded fallback, if Gemini is overloaded), and sends
// one real email (to SMTP_USER), like run.spec.ts.
test("deploys the demo pipeline and runs it through its endpoint with the generated key", async ({ page, request, context }) => {
  test.setTimeout(240_000);
  const api = await Api.login(request);
  const demo = await api.demoWorkflow();
  await api.expectRunnable(demo);
  await signIn(page, api);
  await openEditor(page, demo.id);
  await expectSaved(page);

  await page.getByTestId("deploy-button").click();
  const dialog = page.getByTestId("deploy-dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByTestId("deploy-pipeline-name")).toHaveText(DEMO_PIPELINE);
  await expect(dialog.getByTestId("deploy-inputs")).toContainText("topic");
  await expect(dialog.getByTestId("deploy-inputs")).toContainText("text");
  await expect(dialog.getByTestId("deploy-outputs")).toContainText("final_output.result");

  // A description is required (the Telegram Command Center matches messages against it).
  await dialog.getByTestId("deploy-description").fill("Summarizes a topic with Gemini and emails the summary.");
  // Deploy. The first deploy shows a key; a redeploy (an earlier run of this test) keeps the
  // existing one, which can't be shown again, so issue a new one to call the endpoint with.
  const deployed = page.waitForResponse((r) => r.url().endsWith("/api/deployments") && r.request().method() === "POST");
  await page.getByTestId("deploy-confirm").click();
  const response = await deployed;
  expect([200, 201]).toContain(response.status());
  if (response.status() === 200) {
    await dialog.getByTestId("deploy-rotate").click();
    await dialog.getByTestId("deploy-rotate-confirm").click();
  }
  const keyBox = dialog.getByTestId("deploy-api-key");
  await expect(keyBox).toBeVisible();
  const key = (await keyBox.textContent())!.trim();
  await expect(dialog.getByTestId("deploy-status")).toContainText("Deployed v");

  const endpoint = (await dialog.getByTestId("deploy-endpoint").textContent())!.trim();
  const deploymentId = endpoint.match(new RegExp(`^${API_URL}/api/v1/deployments/([0-9a-f-]{36})/run$`))?.[1];
  expect(deploymentId, endpoint).toBeTruthy();

  try {
    expect(/^ffk_[A-Za-z0-9_-]{40,}$/.test(key), "the dialog shows an ffk_ API key").toBe(true);

    // The curl example is complete (endpoint, key, the input) and the Copy button copies it.
    const curl = (await dialog.getByTestId("deploy-curl").textContent())!;
    expect(curl).toContain(`curl -X POST '${endpoint}?wait=true'`);
    expect(curl.includes(`-H 'Authorization: Bearer ${key}'`), "curl sends the key").toBe(true);
    expect(curl).toContain('"topic"');
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await dialog.getByTestId("deploy-copy-curl").click();
    // (The Windows clipboard hands text back with CRLF line endings.)
    const copied = (await page.evaluate(() => navigator.clipboard.readText())).replace(/\r\n/g, "\n");
    expect(copied === curl, "Copy puts the curl command on the clipboard").toBe(true);

    // Outside the browser now, as a client would: no key, a wrong key, then the real one.
    const body = (topic: string) => ({ data: { inputs: { topic } } });
    expect((await request.post(endpoint, body("x"))).status()).toBe(401);
    expect((await request.post(endpoint, { ...body("x"), headers: { Authorization: "Bearer ffk_not-the-key" } })).status()).toBe(401);

    const marker = `e2e-deploy-${Date.now()}`;
    const run = await request.post(`${endpoint}?wait=true&timeout=120`, {
      ...body(`why APIs need rate limits (${marker})`),
      headers: { Authorization: `Bearer ${key}` },
      timeout: 150_000,
    });
    const result = (await run.json()) as DeploymentRun;
    expect(run.status(), JSON.stringify(result)).toBe(200);
    expect(result.status, result.error ?? "").toBe("success");
    expect(result.final_output?.result?.summary?.trim().length).toBeGreaterThan(20);
    expect(result.final_output?.result?.email, "the Gmail node's Message-ID").toBeTruthy();

    // The status link answers with the same key, and the run shows up in the owner's history.
    const status = await request.get(`${API_URL}${result.links.status}`, { headers: { "X-API-Key": key } });
    expect(status.status()).toBe(200);
    expect(((await status.json()) as DeploymentRun).status).toBe("success");
    const executions = (await api.call<ExecutionListItem[]>("GET", `/api/executions?workflow_id=${demo.id}&limit=5`)).body;
    expect(executions.find((e) => e.id === result.execution_id)).toMatchObject({ trigger: "webhook", status: "success" });

    // Reopened, the dialog no longer shows the key: only its prefix, and curl reads it from the environment.
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await page.getByTestId("deploy-button").click();
    await expect(dialog.getByTestId("deploy-key-prefix")).toHaveText(`${key.slice(0, 12)}••••••••`);
    await expect(dialog.getByTestId("deploy-api-key")).toHaveCount(0);
    await expect(dialog.getByTestId("deploy-curl")).toContainText("$FLOWFORGE_API_KEY");
    await page.getByRole("dialog").screenshot({ path: screenshotPath("deploy") });
  } finally {
    // The pipeline stays deployed, but the key this test used (and may have printed) is revoked.
    const { status } = await api.call("POST", `/api/deployments/${deploymentId}/rotate-key`);
    expect(status).toBe(200);
  }
});

// Undeploy on a scratch pipeline (Input -> Text -> Output: no providers, nothing sent).
test("Undeploy takes the endpoint down after a confirmation, and the old key gets 404", async ({ page, request }) => {
  const api = await Api.login(request);
  const workflow = await api.createWorkflow("E2E undeploy", {
    nodes: [
      { id: "input", type: "input", label: "Topic", position: { x: 0, y: 0 }, config: { name: "topic", input_type: "text" } },
      { id: "text", type: "text", label: "Text", position: { x: 300, y: 0 }, config: { text: "Hello {{input.topic}}" } },
      { id: "out", type: "output", label: "Result", position: { x: 600, y: 0 }, config: { name: "result", value: "{{text.text}}" } },
    ],
    edges: [
      { id: "e1", source: "input", target: "text" },
      { id: "e2", source: "text", target: "out" },
    ],
    variables: [],
  });
  try {
    const deployed = await api.call<{ id: string; endpoint: string; api_key: string }>("POST", "/api/deployments", { workflow_id: workflow.id, description: "E2E undeploy check" });
    expect(deployed.status).toBe(201);
    const { endpoint, api_key: key } = deployed.body;
    const runIt = () => request.post(`${API_URL}${endpoint}`, { data: { inputs: { topic: "x" } }, headers: { Authorization: `Bearer ${key}` } });
    expect((await runIt()).status()).toBe(202);

    await signIn(page, api);
    await openEditor(page, workflow.id);
    const undeploy = page.getByTestId("undeploy-button");
    await expect(undeploy).toBeVisible();

    // Cancel changes nothing.
    await undeploy.click();
    const confirm = page.getByRole("dialog", { name: "Undeploy this pipeline?" });
    await expect(confirm).toContainText(endpoint);
    await confirm.getByRole("button", { name: "Cancel" }).click();
    await expect(confirm).toHaveCount(0);
    expect((await runIt()).status()).toBe(202);

    // Confirm: the button goes away and the same key now gets 404.
    await undeploy.click();
    await confirm.getByRole("button", { name: "Undeploy" }).click();
    await expect(page.getByTestId("undeploy-button")).toHaveCount(0);
    const gone = await runIt();
    expect(gone.status()).toBe(404);
    expect(await gone.json()).toEqual({ detail: "This deployment was undeployed" });

    // Kept for history.
    const history = await api.call<{ id: string; revoked_at: string | null }[]>(
      "GET", `/api/deployments?workflow_id=${workflow.id}&include_revoked=true`);
    expect(history.body.map((d) => [d.id, Boolean(d.revoked_at)])).toEqual([[deployed.body.id, true]]);
  } finally {
    await api.remove(workflow.id);
  }
});
