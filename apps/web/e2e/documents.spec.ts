import path from "node:path";

import { expect, test } from "@playwright/test";

import type { UploadedFile, WorkflowListItem } from "../src/lib/types";

import { Api, edge, hideDevOverlay, node, openEditor, screenshotPath, signIn } from "./helpers";

const DOCUMENT_PIPELINE = "Demo: Scanned invoice to entities";
const SAMPLE = path.resolve(__dirname, "../../../samples/scanned-invoice.pdf");

// Real everything: the file goes through POST /api/files, OCR runs on worker-ocr (Tesseract),
// and Summarize + Entity Extraction run on worker-llm (Gemini, with the seeded fallback).
test("uploads a scanned PDF and runs the document pipeline across the ocr and llm workers", async ({ page, request }) => {
  test.setTimeout(300_000);
  const api = await Api.login(request);
  const pipeline = (await api.call<WorkflowListItem[]>("GET", "/api/workflows")).body.find((w) => w.name === DOCUMENT_PIPELINE);
  expect(pipeline, `"${DOCUMENT_PIPELINE}" is seeded`).toBeTruthy();
  const before = new Set((await api.call<UploadedFile[]>("GET", "/api/files")).body.map((f) => f.id));
  let uploaded: string | undefined;
  try {
    await signIn(page, api);
    await openEditor(page, pipeline!.id);
    await hideDevOverlay(page);

    // The library shows the Documents group; cards show which queue runs them.
    for (const type of ["pdf_extract", "ocr", "summarize", "extract_entities"]) {
      await expect(page.getByTestId(`library-${type}`)).toBeVisible();
    }
    await expect(node(page, "ocr")).toContainText("ocr");
    await expect(node(page, "summarize")).toContainText("llm");

    // Run: the file input offers your uploads (the seeded sample is the default); upload a new copy.
    await page.getByTestId("run-button").click();
    const chooser = page.getByTestId("run-input-document");
    await expect(chooser).toBeVisible();
    await page.getByTestId("run-input-document-input").setInputFiles(SAMPLE);
    await expect(page.getByTestId("run-input-document-selected")).toContainText("scanned-invoice.pdf · application/pdf");
    const now = (await api.call<UploadedFile[]>("GET", "/api/files")).body;
    uploaded = now.find((f) => !before.has(f.id))?.id;
    expect(uploaded, "the upload created a new file").toBeTruthy();
    await expect(chooser.locator("select")).toHaveValue(uploaded!);
    await page.getByTestId("confirm-run").click();

    // OCR runs first (blue), on its own queue, then the run moves to the llm workers.
    await expect(node(page, "ocr")).toHaveAttribute("data-status", /running|success/, { timeout: 60_000 });
    await expect(node(page, "ocr")).toHaveAttribute("data-status", "success", { timeout: 120_000 });
    await expect(edge(page, "input->ocr")).toHaveClass(/edge-success/);
    await expect(page.getByTestId("run-status")).toHaveText(/success/i, { timeout: 240_000 });
    for (const id of ["input", "ocr", "summarize", "entities", "output"]) {
      await expect(node(page, id)).toHaveAttribute("data-status", "success");
    }

    // Where each node ran.
    await expect(page.getByTestId("ran-on-ocr")).toContainText(/^ocr · worker-ocr@/);
    await expect(page.getByTestId("ran-on-summarize")).toContainText(/^llm · worker-llm@/);
    await expect(page.getByTestId("ran-on-entities")).toContainText(/^llm · worker-llm@/);

    // Readable output: OCR text as text, entities grouped.
    const ocrRow = page.getByTestId("timeline-ocr");
    await ocrRow.getByRole("button").first().click();
    await expect(ocrRow.getByTestId("output-text")).toContainText("HARBOURLINE FREIGHT");
    await expect(ocrRow).toContainText("OCR confidence");
    const entityRow = page.getByTestId("timeline-entities");
    await entityRow.getByRole("button").first().click();
    const entities = entityRow.getByTestId("output-entities");
    await expect(entities).toContainText("Maria Schneider");
    await expect(entities).toContainText("HF-2026-0417");
    await expect(entities).toContainText("4,389.2");
    await entityRow.scrollIntoViewIfNeeded();
    await page.screenshot({ path: screenshotPath("document-run") });

    // The execution detail page shows the same, from the API.
    await page.getByRole("link", { name: "Details" }).click();
    await expect(page.getByTestId("execution-status")).toHaveText(/success/i);
    await expect(page.getByTestId("ran-on-ocr")).toContainText("worker-ocr@");
  } finally {
    if (uploaded) await api.call("DELETE", `/api/files/${uploaded}`);
  }
});
