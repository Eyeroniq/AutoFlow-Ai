import path from "node:path";

import { expect, test } from "@playwright/test";

import type { UploadedFile, Workflow } from "../src/lib/types";

import { Api, node, openEditor, screenshotPath, signIn } from "./helpers";

const SAMPLE = path.resolve(__dirname, "../../../samples/team-meeting.mp3");

// Chromium's fake microphone (a beeping tone) and an auto-accepted permission prompt, for the
// recorder tests. (A launch option, so it applies to the whole file.) The blocked-microphone
// test serves the page with a Permissions-Policy header through page.route, and Chromium
// treats a routed page as outside localhost, so its local-network check is switched off.
test.use({
  launchOptions: {
    args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--disable-features=LocalNetworkAccessChecks"],
  },
});

async function newUploads(api: Api, before: Set<string>) {
  return (await api.call<UploadedFile[]>("GET", "/api/files")).body.filter((f) => !before.has(f.id));
}

// Real everything: the recording goes through POST /api/files, Speech to Text runs on
// worker-audio (Groq Whisper, or faster-whisper without a Groq key), the notes are written
// by a free LLM as validated JSON, and the result is posted to Telegram (or Discord/Gmail,
// whichever is connected).
test("uploads a meeting recording and runs the Meeting Notes template on the audio worker", async ({ page, request }) => {
  test.setTimeout(420_000);
  const api = await Api.login(request);
  const before = new Set((await api.call<UploadedFile[]>("GET", "/api/files")).body.map((f) => f.id));
  const created = await api.call<Workflow>("POST", "/api/templates/meeting-notes/use", {});
  expect(created.status, JSON.stringify(created.body)).toBe(201);
  const workflow = created.body;
  try {
    await api.expectRunnable(workflow);
    await signIn(page, api);
    await openEditor(page, workflow.id);
    await expect(node(page, "stt")).toContainText("audio");

    await page.getByTestId("run-button").click();
    const chooser = page.getByTestId("run-input-recording");
    await expect(chooser).toBeVisible();
    await expect(page.getByTestId("run-input-recording-record")).toBeVisible();
    await page.getByTestId("run-input-recording-input").setInputFiles(SAMPLE);
    await expect(page.getByTestId("run-input-recording-selected")).toContainText("team-meeting.mp3 · audio/mpeg");
    const [uploaded] = await newUploads(api, before);
    expect(uploaded, "the upload created a new file").toBeTruthy();
    await expect(chooser.locator("select")).toHaveValue(uploaded.id);
    await page.getByTestId("confirm-run").click();

    await expect(node(page, "stt")).toHaveAttribute("data-status", "success", { timeout: 240_000 });
    await expect(page.getByTestId("run-status")).toHaveText(/success/i, { timeout: 300_000 });
    for (const id of ["input", "stt", "notes", "decisions", "actions", "send", "out"]) {
      await expect(node(page, id)).toHaveAttribute("data-status", "success");
    }
    await expect(page.getByTestId("ran-on-stt")).toContainText(/^audio · worker-audio@/);

    // The transcript, with timestamps.
    const sttRow = page.getByTestId("timeline-stt");
    await sttRow.getByRole("button").first().click();
    await expect(sttRow.getByTestId("output-text")).toContainText(/October/i);
    await expect(sttRow.getByTestId("output-segments")).toContainText("00:00");
    await expect(sttRow).toContainText("language: en");

    // The validated notes name the owners from the recording.
    const notesRow = page.getByTestId("timeline-notes");
    await notesRow.getByRole("button").first().click();
    await expect(notesRow).toContainText(/Elena/);
    await expect(notesRow).toContainText(/Marcus/);
    await sttRow.scrollIntoViewIfNeeded();
    await page.screenshot({ path: screenshotPath("meeting-notes-run") });
  } finally {
    await api.remove(workflow.id);
    for (const file of await newUploads(api, before)) await api.call("DELETE", `/api/files/${file.id}`);
  }
});

test.describe("browser recorder", () => {
  test("records from the microphone only after the consent box is ticked, then uploads with progress", async ({ page, request }) => {
    const api = await Api.login(request);
    const before = new Set((await api.call<UploadedFile[]>("GET", "/api/files")).body.map((f) => f.id));
    const created = await api.call<Workflow>("POST", "/api/templates/meeting-notes/use", {});
    expect(created.status).toBe(201);
    try {
      await signIn(page, api);
      await openEditor(page, created.body.id);
      await page.getByTestId("run-button").click();
      await page.getByTestId("run-input-recording-record").click();
      const recorder = page.getByTestId("run-input-recording-recorder");
      await expect(recorder).toContainText("Everyone being recorded has been informed");
      const start = page.getByTestId("run-input-recording-recorder-start");
      await expect(start).toBeDisabled();
      await page.getByTestId("run-input-recording-recorder-consent").check();
      await expect(start).toBeEnabled();
      await start.click();
      await expect(recorder).toContainText(/Recording 00:0[2-9]/, { timeout: 10_000 });
      await page.getByTestId("run-input-recording-recorder-stop").click();
      await expect(page.getByTestId("run-input-recording-selected")).toContainText(/recording-.*\.webm · audio\/webm/, { timeout: 30_000 });
      const [recorded] = await newUploads(api, before);
      expect(recorded?.content_type).toBe("audio/webm");
      expect(recorded!.size_bytes).toBeGreaterThan(1000);
    } finally {
      await api.remove(created.body.id);
      for (const file of await newUploads(api, before)) await api.call("DELETE", `/api/files/${file.id}`);
    }
  });

  test("explains a blocked microphone", async ({ page, request, context }) => {
    const api = await Api.login(request);
    const created = await api.call<Workflow>("POST", "/api/templates/meeting-notes/use", {});
    try {
      // A real permission denial: this page's origin may not use the microphone.
      await context.clearPermissions();
      await page.route("**/pipelines/**", async (route) => {
        if (route.request().resourceType() !== "document") return route.continue();
        const response = await route.fetch();
        await route.fulfill({ response, headers: { ...response.headers(), "permissions-policy": "microphone=()" } });
      });
      await signIn(page, api);
      await openEditor(page, created.body.id);
      await page.getByTestId("run-button").click();
      await page.getByTestId("run-input-recording-record").click();
      await page.getByTestId("run-input-recording-recorder-consent").check();
      await page.getByTestId("run-input-recording-recorder-start").click();
      await expect(page.getByTestId("run-input-recording-recorder-error")).toContainText("Microphone access was blocked");
    } finally {
      await api.remove(created.body.id);
    }
  });
});
