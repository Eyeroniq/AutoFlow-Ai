import { expect, test } from "@playwright/test";

import type { WorkflowGraph } from "../src/lib/types";

import { Api, clickEdge, connect, edge, expectSaved, node, openEditor, screenshotPath, signIn } from "./helpers";

// A small graph that runs without any provider: Input -> Text -> Output.
const scratchGraph = (): WorkflowGraph => ({
  nodes: [
    { id: "input", type: "input", label: "Topic", position: { x: 0, y: 80 }, config: { name: "topic", input_type: "text", default: "E2E" } },
    { id: "text", type: "text", label: "Greeting", position: { x: 320, y: 80 }, config: { text: "Hello {{input.topic}}" } },
    { id: "output", type: "output", label: "Result", position: { x: 640, y: 80 }, config: { name: "result", value: "{{text.text}}" } },
  ],
  edges: [
    { source: "input", target: "text" },
    { source: "text", target: "output" },
  ],
  variables: [],
});

let api: Api;
let workflowId: string;

test.beforeEach(async ({ page, request }, info) => {
  api = await Api.login(request);
  workflowId = (await api.createWorkflow(`E2E ${info.title}`.slice(0, 200), scratchGraph())).id;
  await signIn(page, api);
});

test.afterEach(async () => {
  await api.remove(workflowId);
});

test("adds a node by drag and drop, connects it, and autosaves", async ({ page }) => {
  await openEditor(page, workflowId);
  await expect(page.locator(".react-flow__node")).toHaveCount(3);
  await expect(edge(page, "input->text")).toBeAttached();
  await expectSaved(page);

  // Drag "Delay" from the library onto empty canvas below the chain.
  const canvas = page.getByTestId("canvas");
  const box = (await canvas.boundingBox())!;
  await page.getByTestId("library-delay").dragTo(canvas, { targetPosition: { x: box.width / 2, y: box.height - 200 } });
  await expect(node(page, "delay")).toBeVisible();

  // The config panel opens with the schema form; "seconds" is required.
  const panel = page.getByTestId("config-panel");
  await expect(panel).toBeVisible();
  await expect(panel.getByTestId("field-error-seconds")).toContainText("Required");
  await expect(panel.getByTestId("field-error-seconds")).toContainText("missing required config field 'seconds'");
  await panel.getByLabel("Seconds").fill("1");
  await expect(panel.getByTestId("field-error-seconds")).toHaveCount(0);

  await connect(page, "text", "delay");
  await expect(edge(page, "text->delay")).toBeAttached();

  // Debounced autosave: "Unsaved changes" -> "Saving…" -> "Saved".
  await expectSaved(page);
  const saved = await api.workflow(workflowId);
  expect(saved.graph.nodes.find((n) => n.id === "delay")?.config).toEqual({ seconds: 1 });
  expect(saved.graph.edges).toContainEqual(expect.objectContaining({ source: "text", target: "delay" }));

  // Load: the saved graph comes back after a reload.
  await page.reload();
  await expect(node(page, "delay")).toBeVisible();
  await expect(edge(page, "text->delay")).toBeAttached();
});

test("selects, duplicates, renames, collapses, and deletes with undo and redo", async ({ page }) => {
  await openEditor(page, workflowId);
  const title = (id: string) => node(page, id).locator("p.font-semibold");

  // Duplicate with the keyboard, then undo and redo it.
  await title("text").click();
  await page.keyboard.press("Control+d");
  await expect(node(page, "text_2")).toBeVisible();
  await page.keyboard.press("Control+z");
  await expect(node(page, "text_2")).toHaveCount(0);
  await page.keyboard.press("Control+Shift+z");
  await expect(node(page, "text_2")).toBeVisible();

  // Rename inline by double-clicking the title.
  await title("text_2").dblclick();
  const rename = node(page, "text_2").getByLabel("Node name");
  await rename.fill("Second greeting");
  await rename.press("Enter");
  await expect(title("text_2")).toHaveText("Second greeting");

  // Collapse and expand from the node's menu.
  await node(page, "text_2").getByRole("button", { name: "Actions for Second greeting" }).click();
  await page.getByRole("menuitem", { name: "Collapse" }).click();
  await expect(node(page, "text_2")).not.toContainText("Hello {{input.topic}}");
  await node(page, "text_2").getByRole("button", { name: "Actions for Second greeting" }).click();
  await page.getByRole("menuitem", { name: "Expand" }).click();
  await expect(node(page, "text_2")).toContainText("Hello {{input.topic}}");

  // Multi-select with Ctrl+click and delete both; their edges go with them.
  await title("text").click();
  await title("text_2").click({ modifiers: ["Control"] });
  await expect(page.getByText("2 nodes selected")).toBeVisible();
  await page.keyboard.press("Delete");
  await expect(node(page, "text")).toHaveCount(0);
  await expect(node(page, "text_2")).toHaveCount(0);
  await expect(edge(page, "input->text")).toHaveCount(0);

  // One undo brings back both nodes and the edges.
  await page.getByRole("button", { name: "Undo", exact: true }).click();
  await expect(node(page, "text")).toBeVisible();
  await expect(node(page, "text_2")).toBeVisible();
  await expect(edge(page, "input->text")).toBeAttached();

  // Select an edge and delete it with the keyboard.
  await clickEdge(page, "text", "output");
  await expect(edge(page, "text->output")).toHaveClass(/selected/);
  await page.keyboard.press("Delete");
  await expect(edge(page, "text->output")).toHaveCount(0);
  await page.keyboard.press("Control+z");
  await expect(edge(page, "text->output")).toBeAttached();

  await expectSaved(page);
  const saved = await api.workflow(workflowId);
  expect(saved.graph.nodes.map((n) => n.id).sort()).toEqual(["input", "output", "text", "text_2"]);
  expect(saved.graph.nodes.find((n) => n.id === "text_2")?.label).toBe("Second greeting");
});

test("config form with {{ autocomplete, inline reference errors, and variables", async ({ page }) => {
  await openEditor(page, workflowId);
  await node(page, "text").locator("p.font-semibold").click();
  const panel = page.getByTestId("config-panel");
  const field = panel.getByLabel("Text");

  // Typing "{{" opens the reference list; Enter inserts the highlighted reference.
  await field.fill("");
  await field.pressSequentially("Topic: {{");
  const list = page.getByRole("listbox", { name: "References" });
  await expect(list).toBeVisible();
  await expect(list.getByRole("option", { name: /\{\{input\.topic\}\}/ })).toBeVisible();
  await expect(list.getByRole("option", { name: /\{\{system\.execution_id\}\}/ })).toBeVisible();
  await field.pressSequentially("input.to");
  await page.keyboard.press("Enter");
  await expect(field).toHaveValue("Topic: {{input.topic}}");
  await expect(list).toHaveCount(0);

  // A reference the backend can't resolve shows its message inline and badges the node.
  await field.pressSequentially(" {{ghost.value}}");
  await page.keyboard.press("Escape");
  await expect(panel.getByTestId("field-error-text")).toContainText("ghost");
  await expect(page.getByTestId("node-issues-text")).toHaveText("1");
  await field.fill("Topic: {{input.topic}} for {{vars.audience}}");
  await expect(panel.getByTestId("field-error-text")).toContainText("audience");

  // Define the variable in the Variables panel: the error clears.
  await page.getByRole("button", { name: "Variables", exact: true }).click();
  const variables = page.getByTestId("variables-panel");
  await variables.getByRole("button", { name: "Add variable" }).click();
  await variables.getByLabel("Variable key").fill("audience");
  await variables.getByLabel("Value of audience").fill("engineers");
  await expect(page.getByTestId("node-issues-text")).toHaveCount(0);
  await expectSaved(page);

  const saved = await api.workflow(workflowId);
  expect(saved.graph.variables).toEqual([{ key: "audience", value: "engineers", type: "workflow" }]);
  expect(saved.graph.nodes.find((n) => n.id === "text")?.config.text).toBe("Topic: {{input.topic}} for {{vars.audience}}");

  // Variables persist and show up in autocomplete after a reload.
  await page.reload();
  await page.getByRole("button", { name: "Variables", exact: true }).click();
  await expect(page.getByTestId("variable-row")).toHaveCount(1);
  await node(page, "output").locator("p.font-semibold").click();
  const value = page.getByTestId("config-panel").getByRole("textbox", { name: "Value required" });
  await value.fill("");
  await value.pressSequentially("{{vars.");
  await expect(page.getByRole("option", { name: /\{\{vars\.audience\}\}/ })).toBeVisible();
});

test("tests a single node against sample upstream outputs", async ({ page }) => {
  await openEditor(page, workflowId);
  await node(page, "text").locator("p.font-semibold").click();
  const tester = page.getByTestId("node-tester");
  await tester.locator("summary").click();
  await tester.getByLabel("Upstream outputs (JSON)").fill(JSON.stringify({ input: { topic: "Playwright", value: "Playwright" } }));
  await tester.getByTestId("run-node-test").click();
  const result = tester.getByTestId("node-test-result");
  await expect(result).toContainText("success");
  await expect(result).toContainText('"text": "Hello Playwright"');
});

test("broken graphs show validation errors on the nodes", async ({ page }) => {
  await openEditor(page, workflowId);

  // A cycle: Result -> Greeting closes the loop Greeting -> Result.
  await connect(page, "output", "text");
  await expect(edge(page, "output->text")).toBeAttached();
  // A missing required field: a Delay without its "seconds".
  const canvas = page.getByTestId("canvas");
  const box = (await canvas.boundingBox())!;
  await page.getByTestId("library-delay").dragTo(canvas, { targetPosition: { x: box.width / 2, y: box.height - 220 } });
  await connect(page, "text", "delay");
  await expect(page.getByTestId("config-panel").getByTestId("field-error-seconds")).toContainText("Required");

  await page.getByTestId("validate-button").click();
  const panel = page.getByTestId("validation-panel");
  await expect(panel.getByTestId("validation-issue").filter({ hasText: "cycle" })).toBeVisible();
  await expect(panel.getByTestId("validation-issue").filter({ hasText: "missing required config field 'seconds'" })).toBeVisible();
  await expect(page.getByTestId("node-issues-text")).toBeVisible();
  await expect(page.getByTestId("node-issues-delay")).toBeVisible();
  await expect(page.getByTestId("issue-count")).not.toHaveText("0");
  await page.screenshot({ path: screenshotPath("validation-errors") });

  // Clicking an issue selects its node.
  await panel.getByTestId("validation-issue").filter({ hasText: "missing required config field 'seconds'" }).click();
  await expect(page.getByTestId("config-panel")).toBeVisible();
  await expect(page.getByTestId("node-name-input")).toHaveValue("Delay");

  // A run is refused with the same issues.
  await page.getByTestId("run-button").click();
  await page.getByTestId("confirm-run").click();
  await expect(page.getByTestId("validation-panel")).toBeVisible();
  await expect(page.getByText("Fix the workflow before running")).toBeVisible();
});

test("a failed save is shown with a retry, never silent", async ({ page }) => {
  await openEditor(page, workflowId);
  await expectSaved(page);

  // Make the next saves fail at the network level.
  const pattern = `**/api/workflows/${workflowId}`;
  await page.route(pattern, (route) => (route.request().method() === "PUT" ? route.fulfill({ status: 503, body: "{}" }) : route.fallback()));
  await node(page, "text").locator("p.font-semibold").dblclick();
  await node(page, "text").getByLabel("Node name").fill("Renamed while offline");
  await page.keyboard.press("Enter");
  const state = page.getByTestId("save-state");
  await expect(state).toContainText("Save failed");
  await expect(page.getByText("Couldn't save your changes")).toBeVisible();

  // Back online: Retry saves the edit.
  await page.unroute(pattern);
  await state.getByRole("button", { name: "Retry" }).click();
  await expectSaved(page);
  expect((await api.workflow(workflowId)).graph.nodes.find((n) => n.id === "text")?.label).toBe("Renamed while offline");
});
