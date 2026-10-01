import { expect, test } from "@playwright/test";

import type { ExecutionDetail, WorkflowGraph } from "../src/lib/types";

import { Api, openEditor, screenshotPath, signIn } from "./helpers";

// A test card number and a made-up Aadhaar number that passes the Verhoeff check.
const CARD = "4111 1111 1111 1111";
const AADHAAR = "2345 6789 0124";

const graph = (guard: string): WorkflowGraph => ({
  nodes: [
    { id: "details", type: "input", label: "Details", position: { x: 0, y: 0 }, config: { name: "details" } },
    {
      id: "mail", type: "gmail", label: "Email the team", position: { x: 320, y: 0 },
      // The mock sender: nothing leaves the machine.
      config: { auth: "mock", to: "team@example.com", subject: "Customer", body: "{{details.value}}", privacy_guard: guard },
    },
  ],
  edges: [{ source: "details", target: "mail" }],
  variables: [],
});

test("privacy: the guard redacts, the run page shows a report without values, and settings save", async ({ page, request }) => {
  const api = await Api.login(request);
  const workflow = await api.createWorkflow(`E2E privacy ${Date.now()}`, graph("redact"));
  try {
    const run = await api.call<ExecutionDetail>("POST", `/api/workflows/${workflow.id}/run?sync=true`, {
      inputs: { details: `Card ${CARD}, Aadhaar ${AADHAAR}` },
    });
    expect(run.body.status).toBe("success");

    await signIn(page, api);
    await page.goto(`/executions/${run.body.id}`);
    const report = page.getByTestId("privacy-report");
    await expect(report).toBeVisible();
    await expect(report.getByTestId("privacy-summary")).toContainText("financial");
    await expect(report.getByTestId("privacy-summary")).toContainText("government IDs");
    await expect(report.locator('[data-testid="privacy-node"][data-node="mail"]')).toContainText("guard redacted");
    await expect(report).toContainText("stored data masked");
    // Nowhere on the page: not in the report, the steps, or the inputs.
    await expect(page.locator("body")).not.toContainText(CARD);
    await expect(page.locator("body")).not.toContainText(AADHAAR);
    await page.screenshot({ path: screenshotPath("privacy-report"), fullPage: true });

    await openEditor(page, workflow.id);
    await page.getByTestId("privacy-button").click();
    const panel = page.getByTestId("privacy-panel");
    await expect(panel.getByTestId("privacy-mask")).toBeChecked();
    await panel.getByTestId("privacy-personal").check();
    await panel.getByTestId("privacy-allowlist").fill("team@example.com");
    await panel.getByTestId("privacy-save").click();
    await expect(panel.getByTestId("privacy-save")).toBeDisabled();
    const saved = await api.call("GET", `/api/workflows/${workflow.id}/privacy`);
    expect(saved.body).toEqual({ mask_stored_io: true, detect_personal_data: true, allowlist: ["team@example.com"] });
  } finally {
    await api.call("DELETE", `/api/workflows/${workflow.id}`);
  }
});
