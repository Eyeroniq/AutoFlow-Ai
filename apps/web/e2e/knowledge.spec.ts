import { expect, test } from "@playwright/test";

import type { KnowledgeBase } from "../src/lib/types";

import { Api, screenshotPath, signIn } from "./helpers";

const HANDBOOK = [
  "Acme Remote Work Policy",
  "Employees may work remotely up to three days per week with their manager's agreement.",
  "The company pays a one-time home-office allowance of 500 USD, plus 40 USD per month for internet.",
  "Core hours are 11:00 to 15:00 in the employee's time zone.",
].join("\n\n");

const SECURITY = [
  "Acme Security Basics",
  "Company laptops must use the VPN on any network outside the office.",
  "Screens lock automatically after 5 minutes; report a lost laptop to IT within one hour.",
].join("\n\n");

test("knowledge base: create, upload with live status, and test a search", async ({ page, request }) => {
  const api = await Api.login(request);
  const name = `E2E handbook ${Date.now()}`;
  try {
    await signIn(page, api);
    await page.goto("/knowledge");
    await page.getByTestId("new-kb").click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("Name").fill(name);
    // Real Gemini embeddings (GEMINI_API_KEY): the same model the Retriever uses.
    await dialog.getByLabel("Embedding model").selectOption("gemini");
    await dialog.getByTestId("create-kb").click();
    await expect(page).toHaveURL(/\/knowledge\/[0-9a-f-]{36}$/);
    await expect(page.getByTestId("kb-name")).toHaveText(name);

    await page.getByTestId("kb-file-input").setInputFiles([
      { name: "remote-work.txt", mimeType: "text/plain", buffer: Buffer.from(HANDBOOK) },
      { name: "security.txt", mimeType: "text/plain", buffer: Buffer.from(SECURITY) },
    ]);
    for (const file of ["remote-work.txt", "security.txt"]) {
      const row = page.locator(`[data-testid="document-row"][data-name="${file}"]`);
      // pending -> processing -> ready, polled while the OCR worker embeds it.
      await expect(row.getByTestId("document-status")).toHaveText("ready", { timeout: 90_000 });
    }

    await page.getByTestId("kb-search-input").fill("How much does the company pay for my internet?");
    await page.getByTestId("kb-search").click();
    const first = page.getByTestId("kb-result").first();
    await expect(first).toContainText("remote-work.txt");
    await expect(first).toContainText("40 USD per month for internet");
    await expect(first).toContainText("[1]");
    await page.screenshot({ path: screenshotPath("knowledge-base"), fullPage: true });
  } finally {
    const { body } = await api.call<KnowledgeBase[]>("GET", "/api/knowledge-bases");
    for (const kb of body.filter((k) => k.name === name)) await api.call("DELETE", `/api/knowledge-bases/${kb.id}`);
  }
});
