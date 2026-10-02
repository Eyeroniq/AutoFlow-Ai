import { expect, test } from "@playwright/test";

import type { ExecutionListItem, WorkflowGraph } from "../src/lib/types";

import { Api, screenshotPath, signIn } from "./helpers";

const graph: WorkflowGraph = {
  nodes: [
    { id: "wait", type: "delay", label: "Wait", position: { x: 0, y: 0 }, config: { seconds: 1 } },
    { id: "out", type: "output", label: "Done", position: { x: 320, y: 0 }, config: { name: "result", value: "done" } },
  ],
  edges: [{ source: "wait", target: "out" }],
  variables: [],
};

test("command palette: find a pipeline by part of its name, jump to it, and run it", async ({ page, request }) => {
  const api = await Api.login(request);
  const stamp = Date.now().toString(36);
  const workflow = await api.createWorkflow(`Palette zebra ${stamp}`, graph);
  try {
    await signIn(page, api);
    await page.goto("/dashboard");
    await expect(page.getByTestId("new-pipeline")).toBeVisible();

    // Ctrl+K opens it; a partial, out-of-order-free fragment finds the pipeline.
    await page.keyboard.press("Control+k");
    const palette = page.getByTestId("command-palette");
    await expect(palette).toBeVisible();
    await page.getByTestId("command-palette-input").fill(`zebr ${stamp.slice(0, 4)}`);
    const first = palette.getByTestId("command-palette-item").first();
    await expect(first).toHaveAttribute("data-label", workflow.name);
    await expect(first).toHaveAttribute("aria-selected", "true");
    await page.screenshot({ path: screenshotPath("command-palette") });

    // Arrow keys move the highlight (and wrap); Enter goes to the highlighted item.
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("ArrowUp");
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(new RegExp(`/pipelines/${workflow.id}$`));
    await expect(palette).toHaveCount(0);
    await expect(page.locator(".react-flow__node")).toHaveCount(2);

    // In the editor, a quick action runs the current pipeline.
    await page.keyboard.press("Control+k");
    await page.getByTestId("command-palette-input").fill("run current");
    await expect(palette.getByTestId("command-palette-item").first()).toHaveAttribute("data-label", "Run current pipeline");
    await page.keyboard.press("Enter");
    await expect.poll(async () => (await api.call<ExecutionListItem[]>("GET", `/api/workflows/${workflow.id}/executions`)).body[0]?.status,
      { timeout: 60_000 }).toBe("success");

    // Escape and an outside click both close it.
    await page.keyboard.press("Control+k");
    await expect(palette).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(palette).toHaveCount(0);
    await page.keyboard.press("Control+k");
    await page.getByTestId("command-palette-backdrop").click({ position: { x: 10, y: 10 } });
    await expect(palette).toHaveCount(0);
  } finally {
    await api.call("DELETE", `/api/workflows/${workflow.id}`);
  }
});
