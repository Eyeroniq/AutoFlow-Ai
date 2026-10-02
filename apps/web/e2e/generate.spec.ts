import { expect, test } from "@playwright/test";

import type { WorkflowListItem } from "../src/lib/types";

import { Api, screenshotPath, signIn } from "./helpers";

test("Generate with AI: a prompt becomes a validated pipeline that opens in the editor", async ({ page, request }) => {
  test.setTimeout(240_000);
  const api = await Api.login(request);
  const before = new Set((await api.workflows()).map((w) => w.id));
  try {
    await signIn(page, api);
    await page.goto("/dashboard");
    await page.getByTestId("generate-pipeline").click();
    // Real Gemini: a pipeline with no side effects, so it can run right away.
    await page.getByTestId("generate-prompt").fill("Take a topic, search the web for it, and output a three-sentence summary with the source links");
    await page.getByTestId("generate-submit").click();
    await expect(page).toHaveURL(/\/pipelines\/[0-9a-f-]{36}$/, { timeout: 120_000 });
    await expect(page.getByTestId("editor")).toBeVisible();
    const cards = page.locator(".react-flow__node");
    await expect(cards.first()).toBeVisible();
    expect(await cards.count()).toBeGreaterThanOrEqual(3);
    await expect(page.locator(".react-flow__edge").first()).toBeAttached();
    await page.screenshot({ path: screenshotPath("generate-with-ai") });

    const id = page.url().split("/").pop()!;
    const { body } = await api.call<{ valid: boolean; errors: unknown[] }>("POST", `/api/workflows/${id}/validate`);
    expect(body.errors).toEqual([]);
    const saved = await api.workflow(id);
    expect(saved.graph.nodes.map((n) => n.type)).toEqual(expect.arrayContaining(["input", "web_search", "output"]));
  } finally {
    const { body } = await api.call<WorkflowListItem[]>("GET", "/api/workflows");
    for (const w of body.filter((w) => !before.has(w.id))) await api.call("DELETE", `/api/workflows/${w.id}`);
  }
});
