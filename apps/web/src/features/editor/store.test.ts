import { beforeEach, describe, expect, it, vi } from "vitest";

import type { WorkflowUpdate } from "@/lib/types";

import { COALESCE_MS, createEditorStore, HISTORY_LIMIT } from "./store";
import { catalog, workflow } from "./test-fixtures";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function setup() {
  let clock = 1_000_000;
  const saves: { id: string; body: WorkflowUpdate; done: ReturnType<typeof deferred<{ version: number }>> }[] = [];
  const store = createEditorStore({
    now: () => clock,
    saveWorkflow: (id, body) => {
      const done = deferred<{ version: number }>();
      saves.push({ id, body, done });
      return done.promise;
    },
  });
  store.getState().load(workflow(), catalog);
  const tick = (ms: number) => {
    clock += ms;
  };
  return { store, saves, tick, s: () => store.getState() };
}

describe("loading", () => {
  it("converts the API graph into React Flow nodes and edges", () => {
    const { s } = setup();
    expect(s().nodes.map((n) => n.id)).toEqual(["input", "gemini", "gmail"]);
    expect(s().nodes[1]).toMatchObject({ type: "flow", position: { x: 300, y: 0 }, data: { nodeType: "gemini", label: "Summarize" } });
    expect(s().edges.map((e) => e.id)).toEqual(["input->gemini", "gemini->gmail"]);
    expect(s().variables).toEqual([{ key: "recipient", value: "me@example.com", type: "workflow" }]);
    expect(s().save).toMatchObject({ status: "saved", revision: 0, savedRevision: 0 });
    expect(s().past).toEqual([]);
  });
});

describe("graph edits", () => {
  let ctx: ReturnType<typeof setup>;
  beforeEach(() => {
    ctx = setup();
  });

  it("adds a node with a readable unique id and selects it", () => {
    const id1 = ctx.s().addNode("gemini", { x: 10, y: 20 });
    const id2 = ctx.s().addNode("gemini", { x: 10, y: 20 });
    expect([id1, id2]).toEqual(["gemini_2", "gemini_3"]);
    const added = ctx.s().nodes.find((n) => n.id === "gemini_3")!;
    expect(added).toMatchObject({ selected: true, data: { label: "Gemini", config: {} } });
    expect(ctx.s().nodes.filter((n) => n.selected).map((n) => n.id)).toEqual(["gemini_3"]);
    expect(ctx.s().save.status).toBe("dirty");
    expect(ctx.s().addNode("nope", { x: 0, y: 0 })).toBeNull();
  });

  it("gives new Input nodes their id as name", () => {
    const id = ctx.s().addNode("input", { x: 0, y: 0 })!;
    expect(id).toBe("input_2");
    expect(ctx.s().nodes.find((n) => n.id === id)!.data.config).toEqual({ name: "input_2", input_type: "text" });
  });

  it("deletes nodes together with their edges, as one undo step", () => {
    ctx.s().deleteElements(["gemini"], []);
    expect(ctx.s().nodes.map((n) => n.id)).toEqual(["input", "gmail"]);
    expect(ctx.s().edges).toEqual([]);
    expect(ctx.s().past).toHaveLength(1);
    ctx.s().undo();
    expect(ctx.s().nodes.map((n) => n.id)).toEqual(["input", "gemini", "gmail"]);
    expect(ctx.s().edges).toHaveLength(2);
  });

  it("deletes the selection (nodes and selected edges)", () => {
    ctx.s().onEdgesChange([{ type: "select", id: "input->gemini", selected: true }]);
    ctx.s().onNodesChange([{ type: "select", id: "gmail", selected: true }]);
    ctx.s().deleteSelection();
    expect(ctx.s().nodes.map((n) => n.id)).toEqual(["input", "gemini"]);
    expect(ctx.s().edges).toEqual([]);
    expect(ctx.s().past).toHaveLength(1);
  });

  it("routes React Flow remove changes through history", () => {
    ctx.s().onNodesChange([{ type: "remove", id: "gmail" }]);
    expect(ctx.s().nodes).toHaveLength(2);
    ctx.s().undo();
    expect(ctx.s().nodes).toHaveLength(3);
  });

  it("duplicates nodes below the originals, copying the edges between them", () => {
    const ids = ctx.s().duplicateNodes(["input", "gemini"]);
    expect(ids).toEqual(["input_2", "gemini_2"]);
    const copy = ctx.s().nodes.find((n) => n.id === "gemini_2")!;
    // One card height (96) plus a 32px gap below, keeping the pair's layout.
    expect(copy.position).toEqual({ x: 300, y: 128 });
    expect(ctx.s().nodes.find((n) => n.id === "input_2")!.position).toEqual({ x: 0, y: 128 });
    expect(copy.data.config).toEqual({ user_prompt: "About {{input.topic}}" });
    expect(ctx.s().nodes.find((n) => n.id === "input_2")!.data.config.name).toBe("input_2");
    expect(ctx.s().edges.map((e) => e.id)).toContain("input_2->gemini_2");
    expect(ctx.s().edges.filter((e) => e.source === "gemini_2")).toEqual([]); // gmail wasn't copied
    expect(ctx.s().nodes.filter((n) => n.selected).map((n) => n.id)).toEqual(["input_2", "gemini_2"]);
    // A second copy steps past the first instead of landing on it.
    ctx.s().duplicateNodes(["gemini"]);
    expect(ctx.s().nodes.find((n) => n.id === "gemini_3")!.position.y).toBeGreaterThanOrEqual(128 + 96 + 32);
  });

  it("connects nodes once, never to themselves", () => {
    ctx.s().onConnect({ source: "input", target: "gmail", sourceHandle: null, targetHandle: null });
    ctx.s().onConnect({ source: "input", target: "gmail", sourceHandle: null, targetHandle: null });
    ctx.s().onConnect({ source: "gmail", target: "gmail", sourceHandle: null, targetHandle: null });
    expect(ctx.s().edges.map((e) => e.id)).toEqual(["input->gemini", "gemini->gmail", "input->gmail"]);
    ctx.s().onConnect({ source: "gemini", target: "gmail", sourceHandle: "true", targetHandle: null });
    expect(ctx.s().edges.at(-1)).toMatchObject({ id: "gemini:true->gmail", sourceHandle: "true" });
  });

  it("renames, describes, and collapses nodes", () => {
    ctx.s().renameNode("gemini", "Summarizer");
    ctx.s().setDescription("gemini", "Writes the summary");
    ctx.s().toggleCollapsed("gemini");
    expect(ctx.s().nodes[1].data).toMatchObject({ label: "Summarizer", description: "Writes the summary", collapsed: true });
    ctx.s().undo();
    expect(ctx.s().nodes[1].data.collapsed).toBe(false);
  });

  it("records a drag as one undo step", () => {
    ctx.s().beginDrag();
    ctx.s().onNodesChange([{ type: "position", id: "gemini", position: { x: 310, y: 5 }, dragging: true }]);
    ctx.s().onNodesChange([{ type: "position", id: "gemini", position: { x: 400, y: 40 }, dragging: true }]);
    ctx.s().onNodesChange([{ type: "position", id: "gemini", position: { x: 400, y: 40 }, dragging: false }]);
    expect(ctx.s().nodes[1].position).toEqual({ x: 400, y: 40 });
    expect(ctx.s().past).toHaveLength(1);
    expect(ctx.s().save.status).toBe("dirty");
    ctx.s().undo();
    expect(ctx.s().nodes[1].position).toEqual({ x: 300, y: 0 });
  });

  it("selection changes are not undoable edits", () => {
    ctx.s().onNodesChange([{ type: "select", id: "gemini", selected: true }]);
    expect(ctx.s().past).toEqual([]);
    expect(ctx.s().save.status).toBe("saved");
  });
});

describe("undo / redo", () => {
  it("undoes and redoes config changes", () => {
    const { s, tick } = setup();
    s().updateConfig("gemini", { user_prompt: "v1" }, "user_prompt");
    tick(COALESCE_MS + 1);
    s().updateConfig("gemini", { user_prompt: "v2" }, "user_prompt");
    s().undo();
    expect(s().nodes[1].data.config).toEqual({ user_prompt: "v1" });
    s().undo();
    expect(s().nodes[1].data.config).toEqual({ user_prompt: "About {{input.topic}}" });
    s().redo();
    s().redo();
    expect(s().nodes[1].data.config).toEqual({ user_prompt: "v2" });
    expect(s().future).toEqual([]);
  });

  it("coalesces rapid edits of the same field into one step", () => {
    const { s, tick } = setup();
    for (const text of ["A", "Ab", "Abc"]) {
      s().updateConfig("gemini", { user_prompt: text }, "user_prompt");
      tick(200);
    }
    expect(s().past).toHaveLength(1);
    s().undo();
    expect(s().nodes[1].data.config).toEqual({ user_prompt: "About {{input.topic}}" });
  });

  it("does not coalesce edits of different fields", () => {
    const { s } = setup();
    s().updateConfig("gemini", { user_prompt: "x" }, "user_prompt");
    s().updateConfig("gemini", { user_prompt: "x", provider: "groq" }, "provider");
    expect(s().past).toHaveLength(2);
  });

  it("a new edit clears the redo stack", () => {
    const { s } = setup();
    s().renameNode("gemini", "A");
    s().undo();
    expect(s().future).toHaveLength(1);
    s().renameNode("gmail", "B");
    expect(s().future).toEqual([]);
  });

  it("keeps a bounded history", () => {
    const { s, tick } = setup();
    for (let i = 0; i < HISTORY_LIMIT + 25; i += 1) {
      s().renameNode("gemini", `name ${i}`);
      tick(COALESCE_MS + 1);
    }
    expect(s().past).toHaveLength(HISTORY_LIMIT);
    for (let i = 0; i < HISTORY_LIMIT + 5; i += 1) s().undo();
    expect(s().nodes[1].data.label).toBe("name 24"); // the oldest 25 steps were dropped
  });

  it("undo/redo bump the form epoch so config forms show the restored values", () => {
    const { s } = setup();
    const epoch = s().formEpoch;
    s().renameNode("gemini", "A");
    s().undo();
    expect(s().formEpoch).toBe(epoch + 1);
    s().redo();
    expect(s().formEpoch).toBe(epoch + 2);
  });

  it("variables are part of history", () => {
    const { s } = setup();
    s().setVariables([...s().variables, { key: "tone", value: "friendly", type: "workflow" }]);
    s().undo();
    expect(s().variables.map((v) => v.key)).toEqual(["recipient"]);
  });
});

describe("save state machine", () => {
  it("saves the name and graph and goes dirty -> saving -> saved", async () => {
    const { s, saves } = setup();
    s().renameNode("gemini", "Summarizer");
    expect(s().save.status).toBe("dirty");
    const done = s().saveNow();
    expect(s().save).toMatchObject({ status: "saving", inFlight: true });
    expect(saves).toHaveLength(1);
    expect(saves[0].id).toBe("wf-1");
    expect(saves[0].body.name).toBe("Demo");
    expect(saves[0].body.graph!.nodes[1]).toMatchObject({ id: "gemini", label: "Summarizer" });
    saves[0].done.resolve({ version: 4 });
    await expect(done).resolves.toBe(true);
    expect(s().save).toMatchObject({ status: "saved", inFlight: false, savedRevision: s().save.revision, error: null });
    expect(s().version).toBe(4);
  });

  it("does nothing when there's nothing to save", async () => {
    const { s, saves } = setup();
    await expect(s().saveNow()).resolves.toBe(true);
    expect(saves).toHaveLength(0);
  });

  it("never overlaps saves; edits made during a save are saved right after", async () => {
    const { s, saves } = setup();
    s().setName("First");
    const first = s().saveNow();
    s().setName("Second"); // edit while the request is in flight
    const again = s().saveNow(); // shares the running save
    expect(again).toBe(first);
    expect(saves).toHaveLength(1);
    expect(s().save.status).toBe("saving");

    saves[0].done.resolve({ version: 4 });
    await vi.waitFor(() => expect(saves).toHaveLength(2));
    expect(saves[1].body.name).toBe("Second");
    saves[1].done.resolve({ version: 5 });
    await expect(first).resolves.toBe(true);
    expect(s().save.status).toBe("saved");
    expect(s().version).toBe(5);
  });

  it("surfaces a failed save and succeeds on retry", async () => {
    const { s, saves } = setup();
    s().setName("New name");
    const attempt = s().saveNow();
    saves[0].done.reject(new Error("Can't reach the API"));
    await expect(attempt).resolves.toBe(false);
    expect(s().save).toMatchObject({ status: "error", error: "Can't reach the API", inFlight: false });
    expect(s().save.savedRevision).toBeLessThan(s().save.revision);

    const retry = s().saveNow();
    expect(saves).toHaveLength(2);
    saves[1].done.resolve({ version: 4 });
    await expect(retry).resolves.toBe(true);
    expect(s().save).toMatchObject({ status: "saved", error: null });
  });

  it("ignores a response that arrives after another workflow was loaded", async () => {
    const { s, saves } = setup();
    s().setName("Old workflow edit");
    const pending = s().saveNow();
    s().load(workflow({ id: "wf-2", name: "Other", version: 9 }), catalog);
    saves[0].done.resolve({ version: 4 });
    await expect(pending).resolves.toBe(false);
    expect(s()).toMatchObject({ workflowId: "wf-2", name: "Other", version: 9 });
    expect(s().save.status).toBe("saved");
  });

  it("round-trips editor-only fields (description, collapsed) in the payload", async () => {
    const { s, saves } = setup();
    s().setDescription("gmail", "Sends it");
    s().toggleCollapsed("gmail");
    void s().saveNow();
    expect(saves[0].body.graph!.nodes[2]).toMatchObject({ description: "Sends it", collapsed: true });
    expect(saves[0].body.graph!.edges[0]).toEqual({
      id: "input->gemini", source: "input", target: "gemini", source_handle: null, target_handle: null,
    });
  });
});

describe("run state", () => {
  it("applies live events and ignores duplicates", () => {
    const { s } = setup();
    s().applyRunMessage({ type: "node.started", seq: 2, node_key: "gemini", started_at: "2026-09-28T00:00:01Z" });
    s().applyRunMessage({ type: "node.started", seq: 2, node_key: "gemini", started_at: "2026-09-28T00:00:09Z" });
    expect(s().run.nodes.gemini).toMatchObject({ status: "running", startedAt: "2026-09-28T00:00:01Z" });
    expect(s().run.status).toBe("running");
  });
});
