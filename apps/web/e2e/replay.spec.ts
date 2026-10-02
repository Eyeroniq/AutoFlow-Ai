import { expect, test } from "@playwright/test";

import type { ExecutionDetail, ExecutionListItem } from "../src/lib/types";

import { Api, screenshotPath, signIn } from "./helpers";

test("Replay: a real past run is rebuilt with its real timing", async ({ page, request }) => {
  const api = await Api.login(request);
  // The most recent successful run with at least three steps (e.g. Meeting Notes from audio.spec).
  const { body: runs } = await api.call<ExecutionListItem[]>("GET", "/api/executions?limit=100&status=success");
  let detail: ExecutionDetail | null = null;
  for (const item of runs) {
    const { body } = await api.call<ExecutionDetail>("GET", `/api/executions/${item.id}`);
    if (body.graph && body.node_executions.filter((n) => n.started_at && n.finished_at).length >= 3) {
      detail = body;
      break;
    }
  }
  expect(detail, "a past run with three or more steps (run the other e2e tests first)").toBeTruthy();
  const run = detail!;
  const origin = Math.min(Date.parse(run.started_at!), ...run.node_executions.filter((n) => n.started_at).map((n) => Date.parse(n.started_at!)));
  const steps = run.node_executions
    .filter((n) => n.started_at && n.finished_at)
    .map((n) => ({ label: n.node_label, start: Date.parse(n.started_at!) - origin, end: Date.parse(n.finished_at!) - origin }))
    .sort((a, b) => a.start - b.start);
  const longest = [...steps].sort((a, b) => b.end - b.start - (a.end - a.start))[0];

  await signIn(page, api);
  await page.goto(`/executions/${run.id}`);
  await page.getByTestId("replay-toggle").click();
  const replay = page.getByTestId("replay");
  await expect(replay).toBeVisible();
  await expect(replay.getByTestId("replay-node")).toHaveCount(run.graph!.nodes.length);
  await expect(replay.getByTestId("replay-event")).toHaveCount(steps.length * 2);

  const scrub = replay.getByTestId("replay-scrub");
  const card = (label: string) => replay.getByTestId("replay-node").filter({ hasText: label }).first();
  // In the middle of the longest step: it's running, and what came before it is done.
  await scrub.fill(String(Math.round((longest.start + longest.end) / 2)));
  await expect(card(longest.label)).toHaveAttribute("data-status", "running");
  const before = steps.filter((s) => s.end <= longest.start)[0];
  if (before) await expect(card(before.label)).toHaveAttribute("data-status", "success");
  // Right before the first step: nothing has started.
  await scrub.fill("0");
  await expect(card(steps[0].label)).toHaveAttribute("data-status", steps[0].start > 0 ? "idle" : "running");

  // Play at 10x to the end: every step finishes green.
  await replay.getByTestId("replay-speed").selectOption("10");
  await replay.getByTestId("replay-play").click();
  await expect(replay.getByTestId("replay-play")).toContainText("Replay", { timeout: 60_000 });
  for (const step of steps) await expect(card(step.label)).toHaveAttribute("data-status", "success");
  await page.screenshot({ path: screenshotPath("run-replay"), fullPage: true });
});
